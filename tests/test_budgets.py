"""Tests for team budgets: the API and its roles, the month's spend and its UTC boundaries,
threshold alerts (with a local stub receiver for the Slack and generic webhooks).

The second half covers the server changes that shipped with budgets: X-Forwarded-For from
trusted proxies only, --trust-proxy scoped to trusted peers, and dashboard sessions kept in
the database so they survive a restart and end when their key is revoked or rotated.

Each test runs a real server on a free port with a temporary database. The clock is fixed
by patching runledger.server.budgets.now, so the month is known.
"""
import copy
import http.client
import json
import queue
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from runledger import client
from runledger.cli import main
from runledger.server import budgets
from runledger.server.app import make_server
from runledger.server.auth import MAX_FAILURES, hash_key

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
CSRF = {"X-Requested-With": "runledger"}
FIXED_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
STARTED = "2026-10-05T10:00:00Z"


# Helpers

@contextmanager
def running(db_path, **options):
    srv = make_server(str(db_path), host="127.0.0.1", port=0, **options)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    scheme = "https" if srv.tls_context is not None else "http"
    try:
        yield srv, f"{scheme}://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        srv.db.close()
        thread.join(5)


@pytest.fixture
def clock(monkeypatch):
    """Pin the server's clock to FIXED_NOW. Tests move it with clock.set(...)."""
    state = {"now": FIXED_NOW}
    monkeypatch.setattr(budgets, "now", lambda: state["now"])

    class Clock:
        def set(self, when):
            state["now"] = when

    return Clock()


@pytest.fixture
def env(tmp_path, clock):
    with running(tmp_path / "budgets.db") as pair:
        yield pair


def _http(base, method, path, body=None, headers=None):
    """Plain HTTP call that never follows redirects. Returns (status, headers, body)."""
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _json(raw):
    return json.loads(raw.decode("utf-8"))


def _cookie_for(base, key, headers=None):
    status, resp_headers, _ = _http(base, "GET", f"/?key={key}", headers=headers)
    assert status == 302, status
    return resp_headers["set-cookie"].split(";")[0]


def _db_rows(db_path, sql, args=()):
    with closing(sqlite3.connect(str(db_path))) as conn:
        return conn.execute(sql, args).fetchall()


_BASE_PAYLOAD = None


def _base_payload():
    global _BASE_PAYLOAD
    if _BASE_PAYLOAD is None:
        _BASE_PAYLOAD = client.build_payload(FIX, user="base@example.com", project="payments-service")
    return _BASE_PAYLOAD


def make_payload(run_id, user="dev@example.com", cost=0.1, started=STARTED, risk="low", html=None):
    """A receipt for the sample session with a chosen id, developer, cost, start time and risk."""
    payload = copy.deepcopy(_base_payload())
    payload["session_id"] = run_id
    payload["user"] = user
    payload["started"] = started
    payload["totals"]["cost_usd"] = cost
    payload["models"] = {"claude-sonnet-4-5-20250929": {"tokens": 1000, "cost_usd": cost}}
    score = {"low": 10, "medium": 40, "high": 80}[risk]
    payload["risk"] = {"score": score, "level": risk, "reasons": []}
    payload["html"] = html
    return payload


def push(base, key, run_id, **kwargs):
    status, _, raw = _http(base, "POST", "/api/runs", body=make_payload(run_id, **kwargs), headers=_auth(key))
    assert status == 201, raw
    return _json(raw)


def set_budget(base, key, **body):
    status, _, raw = _http(base, "PUT", "/api/budgets", body=body, headers={**_auth(key), **CSRF})
    return status, _json(raw)


def team_with_keys(srv, base):
    """A team with an admin key and a member key (both returned as plain keys)."""
    team_id, admin = srv.db.create_team("alpha")
    member = srv.db.create_key(team_id, "member-key", "member", "test")["key"]
    viewer = srv.db.create_key(team_id, "viewer-key", "viewer", "test")["key"]
    return team_id, admin, member, viewer


class _Hook:
    """A local HTTP receiver for webhook tests. Every POST is queued for the test to read.
    status sets the reply code, so a failing receiver can be simulated."""

    def __init__(self, status=200):
        self.received = queue.Queue()
        sink = self.received
        code = status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                sink.put({"path": self.path, "body": body})
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/hook"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()

    def next_json(self, timeout=5.0):
        item = self.received.get(timeout=timeout)
        return json.loads(item["body"].decode("utf-8"))


# Budget API: defaults, CRUD, validation and roles

def test_a_new_team_has_no_limit_and_the_default_thresholds(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    status, _, raw = _http(base, "GET", "/api/budgets", headers=_auth(admin))
    assert status == 200
    assert _json(raw) == {"monthly_usd": None, "per_user_monthly_usd": None, "alert_thresholds": [50, 80, 100]}


def test_put_replaces_the_budget_and_is_audited(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    status, body = set_budget(base, admin, monthly_usd=12.5, per_user_monthly_usd=3, alert_thresholds=[60, 90])
    assert status == 200
    assert body == {"monthly_usd": 12.5, "per_user_monthly_usd": 3.0, "alert_thresholds": [60, 90]}
    status, _, raw = _http(base, "GET", "/api/budgets", headers=_auth(admin))
    assert _json(raw) == body
    events = _json(_http(base, "GET", "/api/audit?action=budget.", headers=_auth(admin))[2])["events"]
    assert [e["action"] for e in events] == ["budget.update"]
    assert events[0]["details"] == {"monthly_usd": 12.5, "per_user_monthly_usd": 3.0, "alert_thresholds": [60, 90]}


def test_a_put_without_amounts_clears_them_and_restores_default_thresholds(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=5, per_user_monthly_usd=1, alert_thresholds=[10])
    status, body = set_budget(base, admin)  # the body replaces the whole budget
    assert status == 200
    assert body == {"monthly_usd": None, "per_user_monthly_usd": None, "alert_thresholds": [50, 80, 100]}


def test_zero_means_no_limit_so_there_is_no_percentage_and_no_alert(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    assert set_budget(base, admin, monthly_usd=0, per_user_monthly_usd=0)[0] == 200
    push(base, admin, "run-1", cost=5.0)
    status = _json(_http(base, "GET", "/api/budgets/status", headers=_auth(admin))[2])
    assert status["monthly_usd"] == 0 and status["pct"] is None
    assert all(row["pct"] is None for row in status["per_user"])
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM budget_alerts")[0][0] == 0


@pytest.mark.parametrize("case", ["admin", "member", "viewer"])
def test_budget_role_matrix(env, case):
    srv, base = env
    _, admin, member, viewer = team_with_keys(srv, base)
    key = {"admin": admin, "member": member, "viewer": viewer}[case]
    assert _http(base, "GET", "/api/budgets", headers=_auth(key))[0] == 200
    assert _http(base, "GET", "/api/budgets/status", headers=_auth(key))[0] == 200
    status, _, raw = _http(base, "PUT", "/api/budgets", body={"monthly_usd": 7}, headers={**_auth(key), **CSRF})
    if case == "admin":
        assert status == 200 and _json(raw)["monthly_usd"] == 7
    else:
        assert status == 403 and _json(raw)["error"]["code"] == "forbidden"
        assert _json(_http(base, "GET", "/api/budgets", headers=_auth(admin))[2])["monthly_usd"] is None


def test_a_cookie_budget_change_needs_the_csrf_header(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    cookie = _cookie_for(base, admin)
    status, _, raw = _http(base, "PUT", "/api/budgets", body={"monthly_usd": 9}, headers={"Cookie": cookie})
    assert status == 403 and _json(raw)["error"]["code"] == "csrf_required"
    assert _http(base, "PUT", "/api/budgets", body={"monthly_usd": 9},
                 headers={"Cookie": cookie, **CSRF})[0] == 200
    assert _http(base, "GET", "/api/budgets/status", headers={"Cookie": cookie})[0] == 200


@pytest.mark.parametrize("body", [
    {"monthly_usd": -1},
    {"monthly_usd": "10"},
    {"monthly_usd": True},
    {"monthly_usd": 2_000_000_000},
    {"per_user_monthly_usd": -0.5},
    {"alert_thresholds": []},
    {"alert_thresholds": [50, 80, 100, 120]},
    {"alert_thresholds": [80, 50]},
    {"alert_thresholds": [50, 50]},
    {"alert_thresholds": [0]},
    {"alert_thresholds": [501]},
    {"alert_thresholds": [50.5]},
    {"alert_thresholds": [True]},
    {"alert_thresholds": None},
    {"alert_thresholds": "50"},
    {"monthy_usd": 5},
    ["monthly_usd", 5],
])
def test_invalid_budgets_are_400_and_change_nothing(env, body):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=4, alert_thresholds=[70])
    status, _, raw = _http(base, "PUT", "/api/budgets", body=body, headers={**_auth(admin), **CSRF})
    assert status == 400, body
    assert _json(raw)["error"]["code"] == "invalid_budget"
    assert _json(_http(base, "GET", "/api/budgets", headers=_auth(admin))[2]) == {
        "monthly_usd": 4.0, "per_user_monthly_usd": None, "alert_thresholds": [70],
    }


@pytest.mark.parametrize("raw_body", [b"", b"{not json", b'{"monthly_usd": NaN}'])
def test_unreadable_budget_bodies_are_400_invalid_budget(env, raw_body):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    status, _, raw = _http(base, "PUT", "/api/budgets", body=raw_body, headers={**_auth(admin), **CSRF})
    assert status == 400 and _json(raw)["error"]["code"] == "invalid_budget"


# Month status

def test_status_adds_up_the_team_and_each_developer(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=1.0, per_user_monthly_usd=0.25)
    push(base, admin, "run-ann-1", user="ann@example.com", cost=0.30)
    push(base, admin, "run-bob-1", user="bob@example.com", cost=0.20)
    status = _json(_http(base, "GET", "/api/budgets/status", headers=_auth(admin))[2])
    assert status["month"] == "2026-10"
    assert status["spend_usd"] == 0.5 and status["monthly_usd"] == 1.0 and status["pct"] == 50.0
    assert status["per_user"] == [
        {"user": "ann@example.com", "spend_usd": 0.3, "pct": 120.0},
        {"user": "bob@example.com", "spend_usd": 0.2, "pct": 80.0},
    ]


def test_status_without_a_budget_has_no_percentages(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    push(base, admin, "run-1", cost=0.4)
    status = _json(_http(base, "GET", "/api/budgets/status", headers=_auth(admin))[2])
    assert status["spend_usd"] == 0.4 and status["monthly_usd"] is None and status["pct"] is None
    assert status["per_user"] == [{"user": "dev@example.com", "spend_usd": 0.4, "pct": None}]


def test_status_is_the_same_for_every_role(env):
    srv, base = env
    _, admin, member, viewer = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=2)
    push(base, admin, "run-1", cost=0.5)
    bodies = {_http(base, "GET", "/api/budgets/status", headers=_auth(k))[2] for k in (admin, member, viewer)}
    assert len(bodies) == 1


def test_only_runs_started_in_the_utc_month_count(env, clock):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=100)
    push(base, admin, "before", started="2026-09-30T23:59:59Z", cost=1.0)
    push(base, admin, "first", started="2026-10-01T00:00:00Z", cost=2.0)
    push(base, admin, "last", started="2026-10-31T23:59:59Z", cost=4.0)
    push(base, admin, "after", started="2026-11-01T00:00:00Z", cost=8.0)
    status = _json(_http(base, "GET", "/api/budgets/status", headers=_auth(admin))[2])
    assert status["month"] == "2026-10" and status["spend_usd"] == 6.0


def test_the_month_is_taken_in_utc_not_local_time(env, clock):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    push(base, admin, "run-1", started="2026-10-01T02:00:00Z", cost=1.0)
    push(base, admin, "run-2", started="2026-09-30T20:00:00Z", cost=2.0)
    # 23:30 on 30 September at UTC-05:00 is 04:30 on 1 October UTC.
    clock.set(datetime(2026, 9, 30, 23, 30, tzinfo=timezone(timedelta(hours=-5))))
    status = _json(_http(base, "GET", "/api/budgets/status", headers=_auth(admin))[2])
    assert status["month"] == "2026-10" and status["spend_usd"] == 1.0


def test_the_month_rolls_over_with_the_clock(env, clock):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    push(base, admin, "run-oct", started="2026-10-20T10:00:00Z", cost=3.0)
    clock.set(datetime(2026, 11, 1, 0, 0, 1, tzinfo=timezone.utc))
    status = _json(_http(base, "GET", "/api/budgets/status", headers=_auth(admin))[2])
    assert status["month"] == "2026-11" and status["spend_usd"] == 0.0


def test_a_run_without_a_start_time_counts_from_its_push_time(env, clock):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    payload = make_payload("no-start", cost=2.5)
    payload["started"] = None
    assert _http(base, "POST", "/api/runs", body=payload, headers=_auth(admin))[0] == 201
    # Put the push time in September. Being pushed "now" then no longer counts for October.
    with closing(sqlite3.connect(str(srv.db.path))) as conn:
        conn.execute("UPDATE runs SET created_at = '2026-09-30T23:00:00Z' WHERE id = 'no-start'")
        conn.commit()
    assert _json(_http(base, "GET", "/api/budgets/status", headers=_auth(admin))[2])["spend_usd"] == 0.0
    clock.set(datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert _json(_http(base, "GET", "/api/budgets/status", headers=_auth(admin))[2])["spend_usd"] == 2.5


# Threshold alerts

def test_a_team_threshold_fires_once_per_month_with_a_stub_webhook(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=1.0, alert_thresholds=[50, 80])
    with _Hook() as hook:
        assert _http(base, "PUT", "/api/team/settings", body={"webhook_url": hook.url},
                     headers={**_auth(admin), **CSRF})[0] == 200
        push(base, admin, "run-a", cost=0.6)
        first = hook.next_json()
        assert first["type"] == "budget_alert" and first["scope"] == "team" and first["threshold"] == 50
        push(base, admin, "run-b", cost=0.3)  # 90%: the 80 threshold is new
        second = hook.next_json()
        assert second["threshold"] == 80 and second["spend_usd"] == 0.9
        push(base, admin, "run-c", cost=0.05)  # 95%: nothing new
        push(base, admin, "run-b", cost=0.3)   # a re-push of run-b changes nothing either
    rows = _db_rows(srv.db.path, "SELECT scope, threshold FROM budget_alerts ORDER BY threshold")
    assert rows == [("team", 50), ("team", 80)]


def test_the_webhook_payload_and_the_slack_text_describe_the_alert(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=1.0, alert_thresholds=[50])
    with _Hook() as hook, _Hook() as slack:
        _http(base, "PUT", "/api/team/settings",
              body={"webhook_url": hook.url, "slack_webhook_url": slack.url}, headers={**_auth(admin), **CSRF})
        push(base, admin, "run-a", cost=0.6)
        payload = hook.next_json()
        message = slack.next_json()
    assert payload == {
        "type": "budget_alert", "team": "alpha", "month": "2026-10", "scope": "team", "user": None,
        "threshold": 50, "spend_usd": 0.6, "limit_usd": 1.0, "pct": 60.0,
    }
    assert message == {"text": "RunLedger budget: team spend $0.60 is 60% of $1.00 for 2026-10"}


def test_a_developer_threshold_fires_per_developer(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, per_user_monthly_usd=0.5, alert_thresholds=[50, 100])
    push(base, admin, "run-ann", user="ann@example.com", cost=0.6)  # 120%: both thresholds
    push(base, admin, "run-bob", user="bob@example.com", cost=0.1)  # 20%: none
    rows = _db_rows(srv.db.path, "SELECT scope, threshold FROM budget_alerts ORDER BY scope, threshold")
    assert rows == [("user:ann@example.com", 50), ("user:ann@example.com", 100)]


def test_developer_alert_text_names_the_developer(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, per_user_monthly_usd=0.5, alert_thresholds=[50])
    with _Hook() as slack:
        _http(base, "PUT", "/api/team/settings", body={"slack_webhook_url": slack.url},
              headers={**_auth(admin), **CSRF})
        push(base, admin, "run-ann", user="ann@example.com", cost=0.6)
        message = slack.next_json()
    assert message["text"] == "RunLedger budget: developer ann@example.com spend $0.60 is 120% of $0.50 for 2026-10"


def test_a_developer_name_in_slack_text_is_escaped(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, per_user_monthly_usd=0.1, alert_thresholds=[50])
    with _Hook() as slack:
        _http(base, "PUT", "/api/team/settings", body={"slack_webhook_url": slack.url},
              headers={**_auth(admin), **CSRF})
        push(base, admin, "run-x", user="<b>bob</b> & co", cost=0.5)
        text = slack.next_json()["text"]
    assert "<b>" not in text and "&lt;b&gt;bob&lt;/b&gt; &amp; co" in text


def test_a_new_month_fires_the_same_thresholds_again(env, clock):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=1.0, alert_thresholds=[50])
    push(base, admin, "run-oct", started="2026-10-03T09:00:00Z", cost=0.6)
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc))
    push(base, admin, "run-nov", started="2026-11-02T09:00:00Z", cost=0.6)
    rows = _db_rows(srv.db.path, "SELECT month, scope, threshold FROM budget_alerts ORDER BY month")
    assert rows == [("2026-10", "team", 50), ("2026-11", "team", 50)]


def test_nothing_fires_without_a_budget_or_below_every_threshold(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    push(base, admin, "run-1", cost=50.0)  # no budget yet: nothing to compare with
    set_budget(base, admin, monthly_usd=1000, alert_thresholds=[50, 80, 100])
    push(base, admin, "run-2", cost=0.1)   # 5.01% of 1000
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM budget_alerts")[0][0] == 0


def test_a_changed_threshold_list_applies_to_later_pushes(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=1.0, alert_thresholds=[10])
    push(base, admin, "run-1", cost=0.2)
    assert _db_rows(srv.db.path, "SELECT threshold FROM budget_alerts") == [(10,)]
    set_budget(base, admin, monthly_usd=1.0, alert_thresholds=[90])
    push(base, admin, "run-2", cost=0.5)  # 70%: 90 is not crossed yet
    assert _db_rows(srv.db.path, "SELECT threshold FROM budget_alerts ORDER BY threshold") == [(10,)]
    push(base, admin, "run-3", cost=0.3)  # 100%
    assert _db_rows(srv.db.path, "SELECT threshold FROM budget_alerts ORDER BY threshold") == [(10,), (90,)]


def test_a_failing_webhook_never_fails_the_push(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=1.0, alert_thresholds=[50])
    with _Hook(status=500) as hook:
        _http(base, "PUT", "/api/team/settings", body={"webhook_url": hook.url}, headers={**_auth(admin), **CSRF})
        reply = push(base, admin, "run-1", cost=0.6)
        hook.next_json()  # the delivery was attempted
    assert reply["id"] == "run-1"
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM runs WHERE id = 'run-1'")[0][0] == 1
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM budget_alerts")[0][0] == 1


def test_each_new_alert_is_audited_by_the_system(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=1.0, alert_thresholds=[50, 80])
    push(base, admin, "run-1", cost=0.9)
    events = _json(_http(base, "GET", "/api/audit?action=budget.alert", headers=_auth(admin))[2])["events"]
    assert [(e["actor"], e["target"], e["details"]["threshold"]) for e in events] == [
        ("system", "team", 80), ("system", "team", 50),
    ]
    assert events[0]["details"]["pct"] == 90.0 and events[0]["details"]["limit_usd"] == 1.0


def test_a_repeated_push_does_not_fire_again(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    set_budget(base, admin, monthly_usd=1.0, alert_thresholds=[50])
    push(base, admin, "run-1", cost=0.6)
    push(base, admin, "run-1", cost=0.6)
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM budget_alerts")[0][0] == 1


# Trusted proxies: X-Forwarded-For and X-Forwarded-Proto

def _proxy_server(tmp_path, name="proxy.db", **options):
    return running(tmp_path / name, **options)


@pytest.mark.parametrize("peer, forwarded, trusted, expected", [
    ("127.0.0.1", "198.51.100.7", ["127.0.0.0/8"], "198.51.100.7"),
    ("198.51.100.9", "1.2.3.4", ["127.0.0.0/8"], "198.51.100.9"),        # untrusted peer: header ignored
    ("127.0.0.1", None, ["127.0.0.0/8"], "127.0.0.1"),                   # no header: the proxy itself
    ("127.0.0.1", "garbage, 1.2.3.4", ["127.0.0.0/8"], "127.0.0.1"),     # malformed chain: distrust it
    ("127.0.0.1", "203.0.113.9, 198.51.100.7, 10.1.2.3", ["127.0.0.0/8", "10.0.0.0/8"], "198.51.100.7"),
    ("127.0.0.1", "10.0.0.5, 10.0.0.6", ["127.0.0.0/8", "10.0.0.0/8"], "10.0.0.5"),  # all trusted: left-most
    ("::ffff:127.0.0.1", "198.51.100.7", ["127.0.0.0/8"], "198.51.100.7"),  # IPv4-mapped peer
    ("127.0.0.1", "2001:db8::7", ["127.0.0.0/8"], "2001:db8::7"),
    ("127.0.0.1", "[2001:db8::8]", ["127.0.0.0/8"], "2001:db8::8"),
])
def test_client_address_rules(tmp_path, peer, forwarded, trusted, expected):
    srv = make_server(str(tmp_path / "addr.db"), port=0, trusted_proxies=trusted)
    try:
        assert srv.client_ip(peer, forwarded) == expected
    finally:
        srv.server_close()
        srv.db.close()


def test_without_trusted_proxies_the_header_is_never_believed(tmp_path):
    srv = make_server(str(tmp_path / "none.db"), port=0)
    try:
        assert srv.client_ip("127.0.0.1", "198.51.100.7") == "127.0.0.1"
    finally:
        srv.server_close()
        srv.db.close()


def test_spoofed_forwarded_for_from_an_untrusted_peer_cannot_dodge_the_limit(tmp_path):
    with _proxy_server(tmp_path, trusted_proxies=["10.0.0.0/8"]) as (srv, base):  # 127.0.0.1 is not trusted
        _, admin, _, _ = team_with_keys(srv, base)
        for index in range(MAX_FAILURES + 1):
            spoof = {"Authorization": "Bearer rl_wrong", "X-Forwarded-For": f"198.51.100.{index + 1}"}
            assert _http(base, "GET", "/api/runs", headers=spoof)[0] == 401
        fresh = {**_auth(admin), "X-Forwarded-For": "203.0.113.200"}
        assert _http(base, "GET", "/api/runs", headers=fresh)[0] == 429


def test_forwarded_for_from_a_trusted_proxy_is_the_limit_key(tmp_path):
    with _proxy_server(tmp_path, trusted_proxies=["127.0.0.1/32"]) as (srv, base):
        _, admin, _, _ = team_with_keys(srv, base)
        for _ in range(MAX_FAILURES + 1):
            assert _http(base, "GET", "/api/runs", headers={
                "Authorization": "Bearer rl_wrong", "X-Forwarded-For": "198.51.100.1"})[0] == 401
        assert _http(base, "GET", "/api/runs", headers={**_auth(admin), "X-Forwarded-For": "198.51.100.1"})[0] == 429
        assert _http(base, "GET", "/api/runs", headers={**_auth(admin), "X-Forwarded-For": "198.51.100.2"})[0] == 200


def test_the_audit_log_records_the_proxied_client_address(tmp_path):
    with _proxy_server(tmp_path, trusted_proxies=["127.0.0.0/8", "10.0.0.0/8"]) as (srv, base):
        _, admin, _, _ = team_with_keys(srv, base)
        _http(base, "GET", f"/?key={admin}", headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.7, 10.1.2.3"})
        events = _json(_http(base, "GET", "/api/audit?action=auth.sign_in", headers=_auth(admin))[2])["events"]
        assert events[0]["details"]["ip"] == "198.51.100.7"


def test_every_forwarded_for_line_is_read_as_one_chain(tmp_path):
    with _proxy_server(tmp_path, trusted_proxies=["127.0.0.0/8"]) as (srv, base):
        _, admin, _, _ = team_with_keys(srv, base)
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        try:
            conn.putrequest("GET", f"/?key={admin}")
            conn.putheader("X-Forwarded-For", "203.0.113.99")          # the client's own line
            conn.putheader("X-Forwarded-For", "198.51.100.7, 127.0.0.1")  # the proxy's appended line
            conn.endheaders()
            assert conn.getresponse().status == 302
        finally:
            conn.close()
        events = _json(_http(base, "GET", "/api/audit?action=auth.sign_in", headers=_auth(admin))[2])["events"]
        assert events[0]["details"]["ip"] == "198.51.100.7"


def test_trust_proxy_honours_https_only_from_a_trusted_peer_when_proxies_are_set(tmp_path):
    with _proxy_server(tmp_path, trust_proxy=True, trusted_proxies=["10.0.0.0/8"]) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        _, headers, _ = _http(base, "GET", f"/?key={admin}", headers={"X-Forwarded-Proto": "https"})
        assert "Secure" not in headers["set-cookie"] and "strict-transport-security" not in headers
    with _proxy_server(tmp_path, name="proxy2.db", trust_proxy=True, trusted_proxies=["127.0.0.1/32"]) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        _, headers, _ = _http(base, "GET", f"/?key={admin}", headers={"X-Forwarded-Proto": "https"})
        assert "; Secure" in headers["set-cookie"]


def test_trust_proxy_with_no_trusted_proxies_keeps_the_old_behaviour(tmp_path):
    with _proxy_server(tmp_path, trust_proxy=True) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        _, headers, _ = _http(base, "GET", f"/?key={admin}", headers={"X-Forwarded-Proto": "https"})
        assert "; Secure" in headers["set-cookie"]


def test_a_bad_trusted_proxy_entry_is_refused_before_the_database_opens(tmp_path):
    path = tmp_path / "never.db"
    with pytest.raises(ValueError, match="not an IP address or CIDR range"):
        make_server(str(path), port=0, trusted_proxies=["10.0.0.0/99"])
    assert not path.exists()


def test_the_serve_command_reports_a_bad_trusted_proxy(tmp_path, capsys):
    code = main(["serve", "--db", str(tmp_path / "cli.db"), "--port", "0", "--trusted-proxy", "nonsense"])
    assert code == 1
    assert "not an IP address or CIDR range" in capsys.readouterr().err


def test_trusted_proxies_from_the_environment_are_added_to_the_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_TRUSTED_PROXIES", " 10.0.0.0/8 , 192.0.2.1,")
    srv = make_server(str(tmp_path / "env.db"), port=0, trusted_proxies=["127.0.0.0/8"])
    try:
        assert [str(net) for net in srv.trusted_proxies] == ["127.0.0.0/8", "10.0.0.0/8", "192.0.2.1/32"]
    finally:
        srv.server_close()
        srv.db.close()


# Dashboard sessions in the database

def test_sessions_survive_a_restart_with_the_same_database(tmp_path):
    path = tmp_path / "restart.db"
    with running(path) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        cookie = _cookie_for(base, admin)
    with running(path) as (srv, base):
        status, _, raw = _http(base, "GET", "/api/me", headers={"Cookie": cookie})
        assert status == 200 and _json(raw)["role"] == "admin"
        assert _http(base, "GET", "/", headers={"Cookie": cookie})[0] == 200


def test_the_sessions_table_holds_only_hashes(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    cookie = _cookie_for(base, admin)
    token = cookie.partition("=")[2]
    rows = _db_rows(srv.db.path, "SELECT token_hash, role FROM sessions")
    assert rows == [(hash_key(token), "admin")]
    assert token not in json.dumps(rows)


def test_a_session_ends_when_its_key_is_revoked_and_its_row_goes(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    member = _json(_http(base, "POST", "/api/keys", body={"label": "m", "role": "member"},
                         headers={**_auth(admin), **CSRF})[2])
    cookie = _cookie_for(base, member["key"])
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM sessions WHERE key_id = ?", (member["id"],))[0][0] == 1
    assert _http(base, "POST", f"/api/keys/{member['id']}/revoke", headers=_auth(admin))[0] == 200
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM sessions WHERE key_id = ?", (member["id"],))[0][0] == 0
    assert _http(base, "GET", "/api/runs", headers={"Cookie": cookie})[0] == 401


def test_rotating_a_key_deletes_its_sessions_too(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    member = _json(_http(base, "POST", "/api/keys", body={"label": "m", "role": "member"},
                         headers={**_auth(admin), **CSRF})[2])
    cookie = _cookie_for(base, member["key"])
    assert _http(base, "POST", f"/api/keys/{member['id']}/rotate", headers=_auth(admin))[0] == 200
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM sessions WHERE key_id = ?", (member["id"],))[0][0] == 0
    assert _http(base, "GET", "/api/runs", headers={"Cookie": cookie})[0] == 401


def test_an_expired_session_is_refused_and_purged(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    cookie = _cookie_for(base, admin)
    with closing(sqlite3.connect(str(srv.db.path))) as conn:
        conn.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00Z'")
        conn.commit()
    assert _http(base, "GET", "/api/me", headers={"Cookie": cookie})[0] == 401
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM sessions")[0][0] == 0


def test_signing_in_purges_other_expired_sessions(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv, base)
    old = _cookie_for(base, admin)
    with closing(sqlite3.connect(str(srv.db.path))) as conn:
        conn.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00Z'")
        conn.commit()
    _cookie_for(base, admin)  # a fresh sign-in sweeps the table
    assert _db_rows(srv.db.path, "SELECT COUNT(*) FROM sessions")[0][0] == 1
    assert _http(base, "GET", "/api/me", headers={"Cookie": old})[0] == 401


def test_a_session_keeps_its_role_after_a_restart(tmp_path):
    path = tmp_path / "role.db"
    with running(path) as (srv, base):
        team_id, admin = srv.db.create_team("alpha")
        viewer = srv.db.create_key(team_id, "reader", "viewer", "test")["key"]
        cookie = _cookie_for(base, viewer)
    with running(path) as (srv, base):
        assert _json(_http(base, "GET", "/api/me", headers={"Cookie": cookie})[2])["role"] == "viewer"
        assert _http(base, "GET", "/api/audit", headers={"Cookie": cookie})[0] == 403
