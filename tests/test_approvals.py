"""Tests for human approvals: the API, the dashboard pages, team settings and webhooks.

Each test runs a real server on a free port in a background thread, with a
temporary SQLite database. Webhooks go to local stub servers defined below.
"""
import http.client
import json
import queue
import re
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from runledger.server.approvals import CREATE_BODY_LIMIT
from runledger.server.app import make_server
from runledger.server.dashboard import APPROVAL_HTML, DASHBOARD_HTML
from runledger.server.db import Database

SESSION = "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42"
CSRF = {"X-Requested-With": "runledger"}


@contextmanager
def running_server(db_path):
    srv = make_server(str(db_path), host="127.0.0.1", port=0)
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
def env(tmp_path):
    with running_server(tmp_path / "rl.db") as pair:
        yield pair


class _Stub:
    """A local HTTP receiver for webhook tests. Each POST is queued for the test to read.
    status sets the reply code; delay makes the reply slow."""

    def __init__(self, status=200, delay=0.0):
        self.received = queue.Queue()
        code, pause, sink = status, delay, self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                sink.put({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })
                if pause:
                    time.sleep(pause)
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
        return item["path"], item["headers"], json.loads(item["body"].decode("utf-8"))


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


def _valid(**changes):
    body = {
        "session_id": SESSION,
        "tool": "Bash",
        "summary": "rm -rf build/",
        "risks": [{"severity": "high", "code": "destructive_delete", "reason": "Deletes a directory tree"}],
        "cwd": "D:\\runledger",
    }
    body.update(changes)
    return body


def _create(base, key, body=None):
    return _http(base, "POST", "/api/approvals", body=_valid() if body is None else body, headers=_auth(key))


def _create_ok(base, key, body=None):
    status, _, raw = _create(base, key, body)
    assert status == 201, raw
    return _json(raw)["id"]


def _get_view(base, key, approval_id):
    status, _, raw = _http(base, "GET", f"/api/approvals/{approval_id}", headers=_auth(key))
    assert status == 200, raw
    return _json(raw)


def _list_ids(base, key, status=None):
    path = "/api/approvals" + (f"?status={status}" if status else "")
    status_code, _, raw = _http(base, "GET", path, headers=_auth(key))
    assert status_code == 200, raw
    return [a["id"] for a in _json(raw)["approvals"]]


def _cookie_for(base, key):
    status, headers, _ = _http(base, "GET", f"/?key={key}")
    assert status == 302
    return headers["set-cookie"].split(";")[0]


def _decide(base, approval_id, body, headers):
    return _http(base, "POST", f"/api/approvals/{approval_id}/decision", body=body, headers=headers)


def _put(base, key, body):
    return _http(base, "PUT", "/api/team/settings", body=body, headers=_auth(key))


def _age(srv, approval_id, seconds):
    """Pretend an approval was created `seconds` ago, so TTL tests need no sleeping."""
    with closing(sqlite3.connect(srv.db.path)) as conn:
        conn.execute("UPDATE approvals SET created_ts = created_ts - ? WHERE id = ?", (seconds, approval_id))
        conn.commit()


def _stderr_until(capsys, needle, timeout=5.0):
    text = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text += capsys.readouterr().err
        if needle in text:
            return text
        time.sleep(0.05)
    raise AssertionError(f"never saw {needle!r} on stderr; got {text!r}")


def test_create_poll_and_approve_from_the_dashboard(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    approval_id = _create_ok(base, key)

    view = _get_view(base, key, approval_id)
    assert view["status"] == "pending" and view["decided_by"] is None and view["decided_at"] is None
    assert view["tool"] == "Bash" and view["summary"] == "rm -rf build/"
    assert view["risks"] == [{"severity": "high", "code": "destructive_delete", "reason": "Deletes a directory tree"}]

    cookie = _cookie_for(base, key)
    status, _, raw = _decide(
        base, approval_id, {"decision": "approve", "name": "Ana", "reason": "build dir only"},
        {"Cookie": cookie, **CSRF},
    )
    assert status == 200
    decided = _json(raw)
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "dashboard: Ana"
    assert decided["decided_at"] and decided["reason"] == "build dir only"

    polled = _get_view(base, key, approval_id)
    assert polled["status"] == "approved" and polled["decided_by"] == "dashboard: Ana"
    assert _list_ids(base, key, "pending") == []


def test_bearer_decision_is_recorded_as_api_and_can_deny(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    approval_id = _create_ok(base, key)
    status, _, raw = _decide(base, approval_id, {"decision": "deny", "reason": "not on the release branch"}, _auth(key))
    assert status == 200
    view = _json(raw)
    assert view["status"] == "denied" and view["decided_by"] == "api" and view["reason"] == "not on the release branch"


def test_second_decision_is_a_conflict_and_changes_nothing(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    approval_id = _create_ok(base, key)
    cookie = _cookie_for(base, key)
    assert _decide(base, approval_id, {"decision": "approve"}, {"Cookie": cookie, **CSRF})[0] == 200

    status, _, raw = _decide(base, approval_id, {"decision": "deny", "reason": "changed my mind"}, _auth(key))
    assert status == 409 and _json(raw)["error"]["code"] == "already_decided"
    status, _, raw = _decide(base, approval_id, {"decision": "approve"}, {"Cookie": cookie, **CSRF})
    assert status == 409 and _json(raw)["error"]["code"] == "already_decided"

    view = _get_view(base, key, approval_id)
    assert view["status"] == "approved" and view["decided_by"] == "dashboard" and view["reason"] is None


def test_pending_approval_expires_after_the_team_ttl(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    status, _, raw = _put(base, key, {"approval_ttl_s": 1})
    assert status == 200 and _json(raw)["approval_ttl_s"] == 1

    approval_id = _create_ok(base, key)
    assert _get_view(base, key, approval_id)["status"] == "pending"
    time.sleep(1.2)

    assert _get_view(base, key, approval_id)["status"] == "expired"
    assert _list_ids(base, key, "pending") == []
    assert approval_id in _list_ids(base, key, "expired")
    status, _, raw = _decide(base, approval_id, {"decision": "approve"}, _auth(key))
    assert status == 409 and _json(raw)["error"]["code"] == "expired"
    assert _get_view(base, key, approval_id)["decided_by"] is None


def test_default_ttl_is_600_seconds(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    status, _, raw = _http(base, "GET", "/api/team/settings", headers=_auth(key))
    assert status == 200 and _json(raw)["approval_ttl_s"] == 600

    approval_id = _create_ok(base, key)
    _age(srv, approval_id, 599)
    assert _get_view(base, key, approval_id)["status"] == "pending"
    _age(srv, approval_id, 2)  # now 601 seconds old
    assert _get_view(base, key, approval_id)["status"] == "expired"


def test_teams_cannot_see_or_decide_each_others_approvals(env):
    srv, base = env
    _, key_a = srv.db.create_team("alpha")
    _, key_b = srv.db.create_team("beta")
    approval_id = _create_ok(base, key_a)
    cookie_b = _cookie_for(base, key_b)

    assert _http(base, "GET", f"/api/approvals/{approval_id}", headers=_auth(key_b))[0] == 404
    assert _http(base, "GET", f"/api/approvals/{approval_id}", headers={"Cookie": cookie_b})[0] == 404
    assert _decide(base, approval_id, {"decision": "approve"}, _auth(key_b))[0] == 404
    assert _decide(base, approval_id, {"decision": "approve"}, {"Cookie": cookie_b, **CSRF})[0] == 404
    assert _http(base, "GET", f"/approvals/{approval_id}", headers={"Cookie": cookie_b})[0] == 404
    assert _list_ids(base, key_b) == []

    assert _get_view(base, key_a, approval_id)["status"] == "pending"


def test_missing_or_wrong_credentials_are_401_and_store_nothing(env):
    srv, base = env
    team_id, key = srv.db.create_team("alpha")
    approval_id = _create_ok(base, key)
    bad = _auth("rl_wrong")

    assert _http(base, "POST", "/api/approvals", body=_valid())[0] == 401
    assert _http(base, "POST", "/api/approvals", body=_valid(), headers=bad)[0] == 401
    assert _http(base, "GET", f"/api/approvals/{approval_id}")[0] == 401
    assert _http(base, "GET", f"/api/approvals/{approval_id}", headers=bad)[0] == 401
    assert _http(base, "GET", "/api/approvals")[0] == 401
    assert _http(base, "GET", "/api/approvals", headers=bad)[0] == 401
    assert _decide(base, approval_id, {"decision": "approve"}, {})[0] == 401
    assert _decide(base, approval_id, {"decision": "approve"}, bad)[0] == 401
    assert _http(base, "GET", "/api/team/settings")[0] == 401
    assert _put(base, "rl_wrong", {"approval_ttl_s": 30})[0] == 401
    assert _http(base, "GET", f"/approvals/{approval_id}")[0] == 401

    assert [a["status"] for a in srv.db.list_approvals(team_id, None, 600)] == ["pending"]
    assert _get_view(base, key, approval_id)["decided_by"] is None


def test_dashboard_cookie_follows_the_role_of_its_key(env):
    srv, base = env
    team_id, key = srv.db.create_team("alpha")
    viewer = srv.db.create_key(team_id, "reader", "viewer", actor="test")["key"]

    # A viewer session can read the settings but cannot create approvals or change them.
    viewer_cookie = _cookie_for(base, viewer)
    assert _http(base, "POST", "/api/approvals", body=_valid(), headers={"Cookie": viewer_cookie, **CSRF})[0] == 403
    assert _http(base, "GET", "/api/team/settings", headers={"Cookie": viewer_cookie})[0] == 200
    status, _, raw = _http(base, "PUT", "/api/team/settings", body={"approval_ttl_s": 30},
                           headers={"Cookie": viewer_cookie, **CSRF})
    assert status == 403 and _json(raw)["error"]["code"] == "forbidden"
    assert _list_ids(base, key) == []

    # An admin session can write, but only with the CSRF header.
    admin_cookie = _cookie_for(base, key)
    assert _http(base, "POST", "/api/approvals", body=_valid(), headers={"Cookie": admin_cookie})[0] == 403
    assert _http(base, "POST", "/api/approvals", body=_valid(), headers={"Cookie": admin_cookie, **CSRF})[0] == 201
    assert len(_list_ids(base, key)) == 1


def test_cookie_decisions_need_the_csrf_header(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    approval_id = _create_ok(base, key)
    cookie = _cookie_for(base, key)

    status, _, raw = _decide(base, approval_id, {"decision": "approve"}, {"Cookie": cookie})
    assert status == 403 and _json(raw)["error"]["code"] == "csrf_required"
    status, _, _ = _decide(base, approval_id, {"decision": "approve"},
                           {"Cookie": cookie, "X-Requested-With": "something-else"})
    assert status == 403
    assert _get_view(base, key, approval_id)["status"] == "pending"

    status, _, _ = _decide(base, approval_id, {"decision": "approve"}, {"Cookie": cookie, **CSRF})
    assert status == 200


@pytest.mark.parametrize("body", [
    {"session_id": ""},
    {"session_id": "a\nb"},
    {"session_id": 12},
    {"session_id": "x" * 201},
    {"tool": "   "},
    {"tool": 5},
    {"tool": "t" * 101},
    {"summary": None},
    {"summary": "  "},
    {"risks": "high"},
    {"risks": ["high"]},
    {"risks": [{"severity": "critical", "code": "x", "reason": "y"}]},
    {"risks": [{"severity": "low", "reason": "no code"}]},
    {"risks": [{"severity": "low", "code": "bad\tcode", "reason": "y"}]},
    {"risks": [{"severity": "low", "code": "x", "reason": 3}]},
    {"cwd": 3},
])
def test_create_rejects_bad_fields_with_a_400(env, body):
    srv, base = env
    team_id, key = srv.db.create_team("alpha")
    status, _, raw = _create(base, key, _valid(**body))
    assert status == 400, body
    assert _json(raw)["error"]["code"] == "invalid_approval"
    assert srv.db.list_approvals(team_id, None, 600) == []


def test_create_rejects_non_object_and_broken_json(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    assert _http(base, "POST", "/api/approvals", body=b"{nope", headers=_auth(key))[0] == 400
    assert _http(base, "POST", "/api/approvals", body=[_valid()], headers=_auth(key))[0] == 400
    assert _http(base, "POST", "/api/approvals", headers=_auth(key))[0] == 400
    status, _, raw = _http(base, "POST", "/api/approvals", body=b"{}", headers={"Authorization": "Bearer " + "x"})
    assert status == 401


def test_size_limits_for_summary_risks_reason_and_body(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    assert _create(base, key, _valid(summary="s" * 2000))[0] == 201
    assert _create(base, key, _valid(summary="s" * 2001))[0] == 400

    fifty = [{"severity": "low", "code": "c", "reason": "r" * 500} for _ in range(50)]
    assert _create(base, key, _valid(risks=fifty))[0] == 201
    assert _create(base, key, _valid(risks=fifty + [{"severity": "low", "code": "c", "reason": ""}]))[0] == 400
    assert _create(base, key, _valid(risks=[{"severity": "low", "code": "c", "reason": "r" * 501}]))[0] == 400

    status, _, raw = _http(base, "POST", "/api/approvals",
                           headers={**_auth(key), "Content-Length": str(CREATE_BODY_LIMIT + 1)})
    assert status == 413 and _json(raw)["error"]["code"] == "payload_too_large"


def test_decision_validation_and_unknown_ids(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    approval_id = _create_ok(base, key)
    assert _decide(base, approval_id, {"decision": "maybe"}, _auth(key))[0] == 400
    assert _decide(base, approval_id, {"reason": "no decision field"}, _auth(key))[0] == 400
    assert _decide(base, approval_id, {"decision": "approve", "reason": 5}, _auth(key))[0] == 400
    assert _decide(base, approval_id, {"decision": "approve", "name": "n" * 101}, _auth(key))[0] == 400
    assert _decide(base, approval_id, {"decision": "approve", "reason": "r" * 501}, _auth(key))[0] == 400

    assert _decide(base, "does-not-exist-123", {"decision": "approve"}, _auth(key))[0] == 404
    assert _http(base, "GET", "/api/approvals/bad.id", headers=_auth(key))[0] == 404
    assert _decide(base, "bad.id", {"decision": "approve"}, _auth(key))[0] == 404
    status, headers, _ = _http(base, "GET", f"/api/approvals/{approval_id}/decision", headers=_auth(key))
    assert status == 405 and "POST" in headers["allow"]
    status, headers, _ = _http(base, "DELETE", "/api/approvals", headers=_auth(key))
    assert status == 405 and "GET" in headers["allow"] and "POST" in headers["allow"]
    assert _get_view(base, key, approval_id)["status"] == "pending"


def test_list_filters_by_status(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    pending = _create_ok(base, key)
    approved = _create_ok(base, key)
    denied = _create_ok(base, key)
    assert _decide(base, approved, {"decision": "approve"}, _auth(key))[0] == 200
    assert _decide(base, denied, {"decision": "deny"}, _auth(key))[0] == 200

    assert sorted(_list_ids(base, key)) == sorted([pending, approved, denied])
    assert _list_ids(base, key, "pending") == [pending]
    assert _list_ids(base, key, "approved") == [approved]
    assert _list_ids(base, key, "denied") == [denied]
    assert _list_ids(base, key, "expired") == []
    assert len(_json(_http(base, "GET", "/api/approvals?limit=1", headers=_auth(key))[2])["approvals"]) == 1
    assert _http(base, "GET", "/api/approvals?status=bogus", headers=_auth(key))[0] == 400
    assert _http(base, "GET", "/api/approvals?limit=0", headers=_auth(key))[0] == 400


def test_approval_ids_are_random_and_url_safe(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    ids = [_create_ok(base, key) for _ in range(3)]
    assert len(set(ids)) == 3
    for approval_id in ids:
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", approval_id), approval_id


def test_response_shapes_match_the_contract(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    status, _, raw = _create(base, key)
    assert status == 201
    created = _json(raw)
    assert set(created) == {"id", "status"} and created["status"] == "pending"

    view = _get_view(base, key, created["id"])
    assert {"id", "status", "decided_by", "decided_at", "reason"} <= set(view)
    assert view["id"] == created["id"]
    status, _, raw = _decide(base, created["id"], {"decision": "approve"}, _auth(key))
    assert status == 200
    assert {"id", "status", "decided_by", "decided_at", "reason"} <= set(_json(raw))


def test_generic_webhook_receives_the_approval_json(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    with _Stub() as hook:
        assert _put(base, key, {"webhook_url": hook.url})[0] == 200
        approval_id = _create_ok(base, key)
        path, headers, payload = hook.next_json()
    assert path == "/hook"
    assert headers["content-type"].startswith("application/json")
    assert payload["id"] == approval_id and payload["status"] == "pending"
    assert payload["tool"] == "Bash" and payload["summary"] == "rm -rf build/"
    assert payload["risks"][0]["code"] == "destructive_delete"
    assert payload["url"] == f"{base}/approvals/{approval_id}"


def test_slack_and_webhook_both_get_a_clear_message(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    with _Stub() as slack, _Stub() as hook:
        assert _put(base, key, {"slack_webhook_url": slack.url, "webhook_url": hook.url})[0] == 200
        body = _valid(
            summary="Run <b>cleanup</b> & purge",
            risks=[{"severity": "high", "code": "destructive_delete", "reason": "Writes outside <project>"}],
        )
        approval_id = _create_ok(base, key, body)

        path, _, message = slack.next_json()
        assert path == "/hook"
        text = message["text"]
        assert "RunLedger approval needed: Bash" in text
        assert "Run &lt;b&gt;cleanup&lt;/b&gt; &amp; purge" in text
        assert "- [high] destructive_delete: Writes outside &lt;project&gt;" in text
        assert "<b>" not in text and "<project>" not in text
        assert "Session " + SESSION in text
        assert f"Review: {base}/approvals/{approval_id}" in text

        _, _, payload = hook.next_json()
        assert payload["id"] == approval_id


def test_slow_or_failing_webhooks_never_block_or_break_the_request(env, capsys):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    with _Stub(status=500, delay=1.5) as hook:
        assert _put(base, key, {"webhook_url": hook.url, "slack_webhook_url": "http://127.0.0.1:9/hook"})[0] == 200
        started = time.monotonic()
        status, _, raw = _create(base, key)
        elapsed = time.monotonic() - started
        assert status == 201, raw
        assert elapsed < 1.0, f"create took {elapsed:.2f}s; webhook delivery must run in the background"

        hook.next_json()  # the delivery happened, in the background
        text = _stderr_until(capsys, "webhook notification failed (HTTP 500)")
    assert "Slack notification failed" in text
    assert hook.url not in text and "127.0.0.1:9" not in text  # webhook URLs are secrets; never logged


@pytest.mark.parametrize("value", [
    "https://hooks.slack.com/services/T000/B000/XXXX",
    "HTTPS://Hooks.Example.com/x",
    "http://127.0.0.1:9999/hook",
    "http://localhost:8080/hook",
])
def test_webhook_urls_accepted_when_https_or_loopback(env, value):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    status, _, raw = _put(base, key, {"webhook_url": value})
    assert status == 200 and _json(raw)["webhook_url"] == value


@pytest.mark.parametrize("value", [
    "http://example.com/hook",
    "ftp://files.example.com/x",
    "javascript:alert(1)",
    "https://",
    "https://exa mple.com/hook",
    "http://localhost.evil.test/hook",
    "https://hooks.example.com:99999/x",
    "https://" + "a" * 2000 + ".example.com/x",
    42,
])
def test_webhook_urls_rejected_unless_https_or_loopback(env, value):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    assert _put(base, key, {"webhook_url": "https://hooks.example.com/keep"})[0] == 200

    status, _, raw = _put(base, key, {"webhook_url": value})
    assert status == 400 and _json(raw)["error"]["code"] == "invalid_settings"
    status, _, raw = _http(base, "GET", "/api/team/settings", headers=_auth(key))
    assert _json(raw)["webhook_url"] == "https://hooks.example.com/keep"


def test_webhook_urls_can_be_cleared_and_settings_are_partial(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    assert _put(base, key, {"slack_webhook_url": "https://hooks.example.com/s", "webhook_url": "https://h.example.com/w"})[0] == 200
    status, _, raw = _put(base, key, {"webhook_url": None})
    assert status == 200
    settings = _json(raw)
    assert settings["webhook_url"] is None and settings["slack_webhook_url"] == "https://hooks.example.com/s"
    assert _json(_put(base, key, {"slack_webhook_url": ""})[2])["slack_webhook_url"] is None


@pytest.mark.parametrize("value", [0, -1, 604801, "600", 600.5, True, None])
def test_approval_ttl_must_be_a_whole_number_in_range(env, value):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    status, _, raw = _put(base, key, {"approval_ttl_s": value})
    assert status == 400 and _json(raw)["error"]["code"] == "invalid_settings"
    assert _json(_http(base, "GET", "/api/team/settings", headers=_auth(key))[2])["approval_ttl_s"] == 600


def test_unknown_or_empty_settings_are_rejected(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    assert _put(base, key, {"color": "red"})[0] == 400
    assert _put(base, key, {"approval_ttl_s": 30, "color": "red"})[0] == 400
    assert _put(base, key, {})[0] == 400
    assert _json(_http(base, "GET", "/api/team/settings", headers=_auth(key))[2])["approval_ttl_s"] == 600


def test_existing_database_is_upgraded_with_the_approval_settings(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(
            "CREATE TABLE teams (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
            "api_key_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);"
            "INSERT INTO teams (name, api_key_hash, created_at) VALUES ('old', 'x', '2026-01-01T00:00:00Z');"
        )
        conn.commit()

    with closing(Database(str(path))) as db:
        assert db.approval_settings(1) == {"slack_webhook_url": None, "webhook_url": None, "approval_ttl_s": 600}
        db.update_approval_settings(1, {"webhook_url": "https://example.com/hook"})
    with closing(Database(str(path))) as db:  # reopening does not fail or reset anything
        assert db.approval_settings(1)["webhook_url"] == "https://example.com/hook"


def test_public_url_comes_from_argument_then_env_then_bound_address(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNLEDGER_PUBLIC_URL", raising=False)
    srv = make_server(str(tmp_path / "a.db"), host="127.0.0.1", port=0)
    try:
        assert srv.public_url == f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.server_close()
        srv.db.close()

    monkeypatch.setenv("RUNLEDGER_PUBLIC_URL", "https://runledger.example.com/")
    srv = make_server(str(tmp_path / "b.db"), host="127.0.0.1", port=0)
    try:
        assert srv.public_url == "https://runledger.example.com"
    finally:
        srv.server_close()
        srv.db.close()

    srv = make_server(str(tmp_path / "c.db"), host="127.0.0.1", port=0, public_url="https://arg.example.com/")
    try:
        assert srv.public_url == "https://arg.example.com"
    finally:
        srv.server_close()
        srv.db.close()


def test_approval_page_needs_a_session_and_is_scoped_to_the_team(env):
    srv, base = env
    _, key_a = srv.db.create_team("alpha")
    _, key_b = srv.db.create_team("beta")
    approval_id = _create_ok(base, key_a)
    path = f"/approvals/{approval_id}"

    assert _http(base, "GET", path)[0] == 401
    status, headers, page = _http(base, "GET", path, headers={"Cookie": _cookie_for(base, key_a)})
    assert status == 200 and headers["content-type"].startswith("text/html")
    nonce = headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert nonce.encode() in page and b"__CSP_NONCE__" not in page
    assert b"innerHTML" not in page
    assert _http(base, "GET", path, headers={"Cookie": _cookie_for(base, key_b)})[0] == 404
    assert _http(base, "GET", "/approvals/bad.id", headers={"Cookie": _cookie_for(base, key_a)})[0] == 404
    assert _http(base, "POST", path, headers={"Cookie": _cookie_for(base, key_a), **CSRF})[0] == 405


def test_dashboard_has_a_pending_approvals_section_built_with_dom_text_only():
    assert "Pending approvals" in DASHBOARD_HTML
    assert DASHBOARD_HTML.index("Pending approvals") < DASHBOARD_HTML.index('id="notice"')
    assert "APPROVAL_POLL_MS = 3000" in DASHBOARD_HTML
    for page in (DASHBOARD_HTML, APPROVAL_HTML):
        assert "innerHTML" not in page
        assert "insertAdjacentHTML" not in page
        assert "outerHTML" not in page
        assert "X-Requested-With" in page
