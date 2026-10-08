"""Tests for the admin exports (runs and audit CSV, the printable report), HEAD on the export
routes, the audit log's action filter, and the export audit entries.

Each test runs a real server on a free port with a temporary database. The clock is fixed
by patching runledger.server.budgets.now to 2026-10-15 12:00 UTC.
"""
import copy
import csv
import http.client
import io
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from runledger import client
from runledger.server import budgets, exports
from runledger.server.app import RECEIPT_CSP, make_server, receipt_to_run

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
CSRF = {"X-Requested-With": "runledger"}
FIXED_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
RUN_HEADER = [
    "id", "started_at", "user", "project", "agent", "models", "steps", "tokens",
    "files_changed", "cost_usd", "risk_score", "risk_level", "title",
]
AUDIT_HEADER = ["id", "at", "actor", "action", "target", "details_json"]
KINDS = {"runs": "/api/export/runs.csv", "audit": "/api/export/audit.csv", "report": "/api/export/report.html"}
APPROVAL = {
    "session_id": "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42",
    "tool": "Bash",
    "summary": "rm -rf build/",
    "risks": [{"severity": "high", "code": "destructive_delete", "reason": "Deletes a directory tree"}],
    "cwd": "D:\\runledger",
}


# Helpers

@contextmanager
def running(db_path, **options):
    srv = make_server(str(db_path), host="127.0.0.1", port=0, **options)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        srv.db.close()
        thread.join(5)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(budgets, "now", lambda: FIXED_NOW)


@pytest.fixture
def env(tmp_path, clock):
    with running(tmp_path / "exports.db") as pair:
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


def _cookie_for(base, key):
    status, headers, _ = _http(base, "GET", f"/?key={key}")
    assert status == 302, status
    return headers["set-cookie"].split(";")[0]


def _rows(raw):
    """Parse an export: a UTF-8 BOM, then CSV with CRLF line ends. Returns the rows as lists."""
    assert raw.startswith(b"\xef\xbb\xbf"), "the CSV must start with a byte-order mark"
    return list(csv.reader(io.StringIO(raw[3:].decode("utf-8"), newline="")))


_BASE_PAYLOAD = None


def _base_payload():
    global _BASE_PAYLOAD
    if _BASE_PAYLOAD is None:
        _BASE_PAYLOAD = client.build_payload(FIX, user="base@example.com", project="payments-service")
    return _BASE_PAYLOAD


def make_payload(run_id, user="dev@example.com", cost=0.6, started="2026-10-05T10:00:00Z",
                 risk="low", html=None, title=None, project=None, reasons=()):
    """A receipt for the sample session with a chosen id, developer, cost, start, risk and title."""
    payload = copy.deepcopy(_base_payload())
    payload["session_id"] = run_id
    payload["user"] = user
    payload["started"] = started
    payload["totals"]["cost_usd"] = cost
    payload["models"] = {"claude-sonnet-4-5-20250929": {"tokens": 1000, "cost_usd": cost}}
    payload["risk"] = {
        "score": {"low": 10, "medium": 40, "high": 80}[risk], "level": risk,
        "reasons": [dict(r) for r in reasons],
    }
    payload["html"] = html
    if title is not None:
        payload["request"] = [title]
    if project is not None:
        payload["project"] = project
    return payload


def push(base, key, run_id, **kwargs):
    status, _, raw = _http(base, "POST", "/api/runs", body=make_payload(run_id, **kwargs), headers=_auth(key))
    assert status == 201, raw
    return _json(raw)


def team_with_keys(srv):
    team_id, admin = srv.db.create_team("alpha")
    member = srv.db.create_key(team_id, "member-key", "member", "test")["key"]
    viewer = srv.db.create_key(team_id, "viewer-key", "viewer", "test")["key"]
    return team_id, admin, member, viewer


def export(base, key, kind, query="", head=False):
    method = "HEAD" if head else "GET"
    headers = _auth(key) if key else {}
    return _http(base, method, KINDS[kind] + query, headers=headers)


# Shape of the CSV files

def test_runs_csv_is_utf8_with_a_bom_an_attachment_name_and_one_row_per_run(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "run-1")
    push(base, admin, "run-2", started="2026-10-06T10:00:00Z")
    status, headers, raw = export(base, admin, "runs")
    assert status == 200
    assert headers["content-type"] == "text/csv; charset=utf-8"
    assert headers["content-disposition"] == 'attachment; filename="runledger-runs-20261015.csv"'
    rows = _rows(raw)
    assert rows[0] == RUN_HEADER
    assert [r[0] for r in rows[1:]] == ["run-1", "run-2"]
    assert raw.endswith(b"\r\n")  # RFC 4180 line endings


def test_runs_csv_values_match_the_stored_run(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "run-1", user="ann@example.com", cost=0.6, risk="medium", title="Fix retry")
    row = dict(zip(RUN_HEADER, _rows(export(base, admin, "runs")[2])[1]))
    assert row["id"] == "run-1" and row["started_at"] == "2026-10-05T10:00:00Z"
    assert row["user"] == "ann@example.com" and row["project"] == "payments-service"
    assert row["agent"] == "Claude Code" and row["models"] == "claude-sonnet-4-5-20250929"
    assert row["cost_usd"] == "0.6" and row["risk_score"] == "40" and row["risk_level"] == "medium"
    assert row["title"] == "Fix retry"
    assert row["steps"].isdigit() and row["tokens"].isdigit() and row["files_changed"].isdigit()


def test_audit_csv_has_its_header_and_the_details_as_json(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    created = _json(_http(base, "POST", "/api/keys", body={"label": "ci", "role": "member"},
                          headers={**_auth(admin), **CSRF})[2])
    status, headers, raw = export(base, admin, "audit")
    assert status == 200
    assert headers["content-disposition"] == 'attachment; filename="runledger-audit-20261015.csv"'
    rows = _rows(raw)
    assert rows[0] == AUDIT_HEADER
    create = [dict(zip(AUDIT_HEADER, r)) for r in rows[1:] if r[3] == "key.create" and r[4] == created["id"]]
    assert len(create) == 1
    assert json.loads(create[0]["details_json"]) == {"label": "ci", "prefix": created["key"][:8], "role": "member"}


@pytest.mark.parametrize("title, user", [
    ('Fix "retry", then run tests', 'Ann "Ops", QA\nlead'),
])
def test_quoting_keeps_commas_quotes_and_newlines_inside_one_cell(env, title, user):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "run-1", user=user, title=title)
    push(base, admin, "run-2")
    rows = _rows(export(base, admin, "runs")[2])
    assert len(rows) == 3 and all(len(r) == len(RUN_HEADER) for r in rows)
    first = dict(zip(RUN_HEADER, rows[1]))
    assert first["user"] == user and first["title"] == title


def test_formula_like_text_is_neutralised_and_numbers_are_not(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "run-evil", user='=HYPERLINK("http://example.invalid","x")', project="@SUM(A1:A2)",
         title="+1+1 total", cost=0.6)
    row = dict(zip(RUN_HEADER, _rows(export(base, admin, "runs")[2])[1]))
    assert row["user"].startswith("'=HYPERLINK")
    assert row["project"] == "'@SUM(A1:A2)"
    assert row["title"] == "'+1+1 total"
    assert row["cost_usd"] == "0.6" and row["risk_score"] == "10"  # numbers keep their form


@pytest.mark.parametrize("value, expected", [
    ("=cmd", "'=cmd"),
    ("+1", "'+1"),
    ("-2+3", "'-2+3"),
    ("@x", "'@x"),
    ("\tcmd", "'\tcmd"),
    ("\rcmd", "'\rcmd"),
    ("   =1+1", "'   =1+1"),
    ("plain", "plain"),
    ("", ""),
    (-5, -5),
    (0.25, 0.25),
    (None, None),
])
def test_safe_cell_only_prefixes_text_that_starts_with_a_formula_character(value, expected):
    assert exports.safe_cell(value) == expected


def test_an_empty_team_exports_only_the_header_and_the_bom(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    rows = _rows(export(base, admin, "runs")[2])
    assert rows == [RUN_HEADER]


def test_the_audit_csv_follows_the_day_window(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    with closing(sqlite3.connect(str(srv.db.path))) as conn:
        conn.execute("INSERT INTO audit_log (team_id, at, actor, action, target, details) "
                     "VALUES (1, '2020-01-01T00:00:00Z', 'old', 'key.create', 'x', '{}')")
        conn.commit()
    recent = {r[2] for r in _rows(export(base, admin, "audit")[2])[1:]}
    assert "old" not in recent
    everything = {r[2] for r in _rows(export(base, admin, "audit", "?days=3650")[2])[1:]}
    assert "old" in everything


# Window and parameters

def test_the_window_counts_whole_days_back_from_now_by_start_time(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "today", started="2026-10-15T01:00:00Z")    # 11 hours before "now"
    push(base, admin, "old", started="2026-09-05T10:00:00Z")      # 40 days before "now"

    def ids(query):
        return [r[0] for r in _rows(export(base, admin, "runs", query)[2])[1:]]

    assert ids("") == ["today"]                       # default is 30 days
    assert ids("?days=1") == ["today"]
    assert ids("?days=30") == ["today"]
    assert sorted(ids("?days=60")) == ["old", "today"]


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("value", ["0", "3651", "-1", "abc", "1.5"])
def test_days_outside_one_to_3650_are_a_400(env, kind, value):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    status, _, raw = export(base, admin, kind, f"?days={value}")
    assert status == 400 and _json(raw)["error"]["code"] == "bad_request"


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("value", ["1", "3650"])
def test_the_edges_of_the_day_range_are_accepted(env, kind, value):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    assert export(base, admin, kind, f"?days={value}")[0] == 200


# Roles

@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("role", ["admin", "member", "viewer", "anonymous"])
def test_exports_need_the_admin_role(env, kind, role):
    srv, base = env
    _, admin, member, viewer = team_with_keys(srv)
    key = {"admin": admin, "member": member, "viewer": viewer, "anonymous": None}[role]
    status, _, raw = export(base, key, kind)
    if role == "admin":
        assert status == 200
    elif role == "anonymous":
        assert status == 401 and _json(raw)["error"]["code"] == "unauthorized"
    else:
        assert status == 403 and _json(raw)["error"]["code"] == "forbidden"


@pytest.mark.parametrize("kind", list(KINDS))
def test_admin_sessions_can_export_and_member_sessions_cannot(env, kind):
    srv, base = env
    _, admin, member, _ = team_with_keys(srv)
    admin_cookie = _cookie_for(base, admin)
    member_cookie = _cookie_for(base, member)
    assert _http(base, "GET", KINDS[kind], headers={"Cookie": admin_cookie})[0] == 200
    assert _http(base, "GET", KINDS[kind], headers={"Cookie": member_cookie})[0] == 403


# HEAD

@pytest.mark.parametrize("kind", list(KINDS))
def test_head_returns_headers_only_and_builds_nothing(env, kind, monkeypatch):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "run-1")
    calls = []

    def builder(*args, **kwargs):
        calls.append(args)
        raise AssertionError("HEAD must not build the export")

    monkeypatch.setattr(exports, "build", builder)
    monkeypatch.setattr(srv.db, "export_runs", builder)
    monkeypatch.setattr(srv.db, "audit_events", builder)
    status, headers, raw = export(base, admin, kind, head=True)
    assert status == 200 and raw == b""
    assert calls == []
    assert headers["content-type"].startswith("text/html" if kind == "report" else "text/csv")
    assert "content-disposition" in headers and "runledger-" in headers["content-disposition"]
    assert "content-length" not in headers  # the length of a body that was never built is not claimed
    assert headers["cache-control"] == "no-store"
    if kind == "report":
        assert headers["content-security-policy"] == RECEIPT_CSP


def test_head_is_refused_for_members_and_still_checks_the_days(env):
    srv, base = env
    _, admin, member, _ = team_with_keys(srv)
    assert export(base, member, "runs", head=True)[0] == 403
    assert export(base, None, "runs", head=True)[0] == 401
    assert export(base, admin, "runs", "?days=0", head=True)[0] == 400


def test_head_on_other_endpoints_answers_like_get_without_a_body(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    status, headers, raw = _http(base, "HEAD", "/api/budgets", headers=_auth(admin))
    assert status == 200 and raw == b""
    assert headers["content-type"].startswith("application/json")


def test_post_to_an_export_is_405_and_says_what_is_allowed(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    status, headers, _ = _http(base, "POST", "/api/export/runs.csv", body={}, headers=_auth(admin))
    assert status == 405 and headers["allow"] == "GET, HEAD"


# Audit entries for exports

def test_each_export_is_audited_with_its_days_and_row_count(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "run-1")
    push(base, admin, "run-2")
    runs_rows = len(_rows(export(base, admin, "runs")[2])) - 1
    report_status, _, _ = export(base, admin, "report", "?days=14")
    assert report_status == 200
    export(base, admin, "runs", head=True)  # a HEAD is not an export and leaves no entry
    events = _json(_http(base, "GET", "/api/audit?action=export.", headers=_auth(admin))[2])["events"]
    by_action = {}
    for event in events:
        by_action.setdefault(event["action"], event)
    assert by_action["export.runs"]["details"] == {"days": 30, "rows": runs_rows}
    assert by_action["export.report"]["details"] == {"days": 14, "rows": 2}
    assert by_action["export.runs"]["actor"] == f"initial ({admin[:8]})"
    assert sum(1 for e in events if e["action"] == "export.runs") == 1


# The HTML report

def test_report_is_self_contained_and_served_with_the_receipt_csp(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    status, headers, raw = export(base, admin, "report")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["content-security-policy"] == RECEIPT_CSP == exports.REPORT_CSP
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["content-disposition"] == 'inline; filename="runledger-report-20261015.html"'
    body = raw.decode("utf-8")
    assert body.startswith("<!doctype html>")
    assert "<style>" in body
    for outside in (b"<script", b"<link", b"<img", b"src=", b"http://", b"https://"):
        assert outside not in raw, outside


def test_report_states_the_period_totals_and_method_without_claiming_certification(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "run-1", cost=0.6)
    body = export(base, admin, "report")[2].decode("utf-8")
    assert "alpha" in body
    assert "2026-09-15 12:00 to 2026-10-15 12:00 UTC (last 30 days)" in body
    assert "Estimated cost at list prices" in body and "$0.6000" in body
    assert "Prepared for SOC 2 and EU AI Act record-keeping" in body
    assert "not a certification" in body
    assert "Methodology" in body and "fixed rules" in body and "list prices" in body
    lowered = body.lower()
    for claim in ("is certified", "certified by", "compliant with", "certified as"):
        assert claim not in lowered, claim


def test_report_escapes_a_malicious_developer_name_title_and_project(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "run-x", user="<script>alert(1)</script>", title="<img src=x onerror=alert(1)>",
         project="<b>payments</b>", risk="high", html="<p>receipt</p>")
    raw = export(base, admin, "report")[2]
    assert b"<script>alert(1)" not in raw and b"<img" not in raw and b"<b>payments" not in raw
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in raw
    assert b"&lt;img src=x onerror=alert(1)&gt;" in raw
    assert b"&lt;b&gt;payments&lt;/b&gt;" in raw


def test_report_links_high_risk_runs_only_where_a_receipt_is_stored(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "hi-1", risk="high", html="<p>receipt</p>")
    push(base, admin, "a b/c", risk="high", html="<p>receipt</p>")
    push(base, admin, "hi-2", risk="high")
    body = export(base, admin, "report")[2].decode("utf-8")
    assert '<a href="/runs/hi-1">Open receipt</a>' in body
    assert '<a href="/runs/a%20b%2Fc">Open receipt</a>' in body
    assert "<td>none</td>" in body  # hi-2 has no stored receipt
    assert "No high-risk runs" not in body


def test_report_counts_runs_by_risk_level(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, "lo", risk="low")
    push(base, admin, "mid", risk="medium")
    push(base, admin, "hi", risk="high")
    body = export(base, admin, "report")[2].decode("utf-8")
    assert '<td>high</td><td class="num">1</td>' in body
    assert '<td>medium</td><td class="num">1</td>' in body
    assert '<td>low</td><td class="num">1</td>' in body


def test_report_lists_approvals_decided_and_key_lifecycle_events(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    approval_id = _json(_http(base, "POST", "/api/approvals", body=APPROVAL, headers=_auth(admin))[2])["id"]
    assert _http(base, "POST", f"/api/approvals/{approval_id}/decision", body={"decision": "approve"},
                 headers=_auth(admin))[0] == 200
    created = _json(_http(base, "POST", "/api/keys", body={"label": "ci", "role": "member"},
                          headers={**_auth(admin), **CSRF})[2])
    body = export(base, admin, "report")[2].decode("utf-8")
    assert approval_id in body and "approved" in body and "api" in body
    assert "key.create" in body and created["id"] in body
    assert "No key lifecycle events" not in body


def test_report_budget_section_shows_the_month_and_the_limit(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    _http(base, "PUT", "/api/budgets", body={"monthly_usd": 1.0}, headers={**_auth(admin), **CSRF})
    push(base, admin, "run-1", cost=0.6)
    body = export(base, admin, "report")[2].decode("utf-8")
    assert "Month 2026-10: spend $0.6000, $1.0000 budget, 60.0% used." in body


def test_report_says_when_a_long_table_is_cut_short(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    for index in range(205):
        run = receipt_to_run(make_payload(f"hi-{index:03d}", risk="high"))
        srv.db.upsert_run(team_id, run)
    body = export(base, admin, "report")[2].decode("utf-8")
    assert "Showing the first 200 rows" in body
    assert len(_rows(export(base, admin, "runs")[2])) - 1 == 205  # the CSV has every row


def test_exports_hold_only_the_callers_team(env):
    srv, base = env
    _, admin_a, _, _ = team_with_keys(srv)
    team_b, admin_b = srv.db.create_team("beta")
    push(base, admin_a, "alpha-run", user="alpha-dev@example.com")
    push(base, admin_b, "beta-run", user="beta-dev@example.com")
    runs_a = export(base, admin_a, "runs")[2]
    runs_b = export(base, admin_b, "runs")[2]
    assert b"alpha-run" in runs_a and b"beta-run" not in runs_a
    assert b"beta-run" in runs_b and b"alpha-run" not in runs_b
    report_b = export(base, admin_b, "report")[2]
    assert b"alpha-run" not in report_b and b"alpha-dev" not in report_b


# The audit log's action filter

def test_action_filter_is_a_prefix_match(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    created = _json(_http(base, "POST", "/api/keys", body={"label": "ci", "role": "member"},
                          headers={**_auth(admin), **CSRF})[2])
    _http(base, "POST", f"/api/keys/{created['id']}/rotate", headers=_auth(admin))
    _http(base, "POST", f"/api/keys/{created['id']}/revoke", headers=_auth(admin))

    def actions(query):
        raw = _http(base, "GET", "/api/audit" + query, headers=_auth(admin))[2]
        return sorted(e["action"] for e in _json(raw)["events"])

    # initial key, member-key and viewer-key from team_with_keys, then ci: four creates in all
    assert actions("?action=key.") == ["key.create"] * 4 + ["key.revoke", "key.rotate"]
    assert actions("?action=key.revoke") == ["key.revoke"]
    assert actions("?action=key.create") == ["key.create"] * 4
    assert actions("?action=auth.") == []
    assert actions("?action=key") == actions("?action=key.")  # every key event starts with "key"


def test_action_filter_is_literal_so_wildcards_and_case_match_nothing(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    for query in ("?action=%25", "?action=_", "?action=KEY.", "?action=*"):
        assert _json(_http(base, "GET", "/api/audit" + query, headers=_auth(admin))[2])["events"] == [], query


def test_an_empty_action_filter_is_ignored(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    everything = _json(_http(base, "GET", "/api/audit", headers=_auth(admin))[2])["events"]
    empty = _json(_http(base, "GET", "/api/audit?action=", headers=_auth(admin))[2])["events"]
    assert empty == everything and len(everything) >= 1


def test_the_action_filter_pages_with_before(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    for index in range(4):
        _http(base, "POST", "/api/keys", body={"label": f"k{index}", "role": "viewer"},
              headers={**_auth(admin), **CSRF})
    seen = []
    before = ""
    while True:  # pages of two until the filter runs out
        page = _json(_http(base, "GET", f"/api/audit?action=key.create&limit=2{before}",
                           headers=_auth(admin))[2])["events"]
        if not page:
            break
        seen += page
        before = f"&before={page[-1]['id']}"
    ids = [e["id"] for e in seen]
    assert len(ids) == 7  # initial, member-key, viewer-key and four more creates
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == len(ids)
    assert all(e["action"] == "key.create" for e in seen)
