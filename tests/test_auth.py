"""Tests for team API keys and roles, dashboard sessions, the audit log, agents in runs,
HTTPS, the Secure cookie flag, security headers and the failed-sign-in limit.

Each test runs a real server on a free port in a background thread, with a
temporary SQLite database. The TLS tests make a self-signed certificate with the
openssl command and are skipped when it is not installed.
"""
import http.client
import json
import re
import shutil
import sqlite3
import ssl
import subprocess
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from runledger import client
from runledger.cli import main
from runledger.server.app import TLSConfigError, make_server
from runledger.server.auth import MAX_FAILURES, FailureLimiter, hash_key
from runledger.server.db import TIME_FORMAT, Database

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
SESSION_ID = "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42"
CSRF = {"X-Requested-With": "runledger"}
KEY_RE = re.compile(r"rl_[A-Za-z0-9_-]{40}")
APPROVAL = {
    "session_id": SESSION_ID,
    "tool": "Bash",
    "summary": "rm -rf build/",
    "risks": [{"severity": "high", "code": "destructive_delete", "reason": "Deletes a directory tree"}],
    "cwd": "D:\\runledger",
}

# The schema as it was before keys had roles: one key per team, in teams.api_key_hash.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    api_key_hash    TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    slack_webhook_url TEXT,
    webhook_url     TEXT,
    approval_ttl_s  INTEGER NOT NULL DEFAULT 600
);
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    team_id      INTEGER NOT NULL REFERENCES teams (id),
    session_id   TEXT NOT NULL,
    tool         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    risks        TEXT NOT NULL DEFAULT '[]',
    cwd          TEXT,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied')),
    decided_by   TEXT,
    decided_at   TEXT,
    reason       TEXT,
    created_at   TEXT NOT NULL,
    created_ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_by_team ON approvals (team_id, status, created_ts);
CREATE TABLE IF NOT EXISTS runs (
    id             TEXT NOT NULL,
    team_id        INTEGER NOT NULL REFERENCES teams (id),
    user           TEXT NOT NULL,
    project        TEXT NOT NULL,
    title          TEXT,
    started_at     TEXT,
    ended_at       TEXT,
    models         TEXT NOT NULL DEFAULT '{}',
    steps          INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER NOT NULL DEFAULT 0,
    files_changed  INTEGER NOT NULL DEFAULT 0,
    cost           REAL,
    risk_score     INTEGER NOT NULL DEFAULT 0,
    risk_level     TEXT NOT NULL DEFAULT 'low',
    receipt_json   TEXT NOT NULL,
    receipt_html   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (team_id, id)
);
CREATE INDEX IF NOT EXISTS runs_by_time ON runs (team_id, started_at);
CREATE TABLE IF NOT EXISTS risks (
    team_id   INTEGER NOT NULL,
    run_id    TEXT NOT NULL,
    severity  TEXT NOT NULL,
    code      TEXT NOT NULL,
    reason    TEXT NOT NULL,
    step      INTEGER,
    FOREIGN KEY (team_id, run_id) REFERENCES runs (team_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS risks_by_run ON risks (team_id, run_id);
CREATE INDEX IF NOT EXISTS risks_by_code ON risks (team_id, code);
"""
OLD_KEY = "rl_" + "A" * 43  # the old key format: "rl_" plus 43 characters
OLD_KEY_TWO = "rl_" + "B" * 43


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
def env(tmp_path):
    with running(tmp_path / "rl.db") as pair:
        yield pair


def _http(base, method, path, body=None, headers=None, context=None):
    """Plain HTTP call that never follows redirects. Returns (status, headers, body)."""
    parts = urlsplit(base)
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=10, context=context)
    else:
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


def _create_key(base, key, label, role):
    status, _, raw = _http(base, "POST", "/api/keys", body={"label": label, "role": role}, headers=_auth(key))
    assert status == 201, raw
    return _json(raw)


def _my_key_id(base, key):
    return _json(_http(base, "GET", "/api/me", headers=_auth(key))[2])["key"]["id"]


def _approval(base, key):
    status, _, raw = _http(base, "POST", "/api/approvals", body=APPROVAL, headers=_auth(key))
    assert status == 201, raw
    return _json(raw)["id"]


def _audit(base, key, query=""):
    status, _, raw = _http(base, "GET", "/api/audit" + query, headers=_auth(key))
    assert status == 200, raw
    return _json(raw)["events"]


def _db_rows(db_path, sql, args=()):
    with closing(sqlite3.connect(str(db_path))) as conn:
        return conn.execute(sql, args).fetchall()


def _set_last_used(db_path, key_id, seconds_ago):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(TIME_FORMAT)
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (stamp, key_id))
        conn.commit()
    return stamp


def _last_used(db_path, key_id):
    return _db_rows(db_path, "SELECT last_used_at FROM api_keys WHERE id = ?", (key_id,))[0][0]


# Roles across every endpoint. Columns: admin, member, viewer.

MATRIX = {
    "me": (200, 200, 200),
    "list_runs": (200, 200, 200),
    "get_run": (200, 200, 200),
    "receipt": (200, 200, 200),
    "stats": (200, 200, 200),
    "list_approvals": (200, 200, 200),
    "get_approval": (200, 200, 200),
    "approval_page": (200, 200, 200),
    "settings_get": (200, 200, 200),
    "settings_put": (200, 403, 403),
    "push_run": (201, 201, 403),
    "create_approval": (201, 201, 403),
    "decide_approval": (200, 200, 403),
    "list_keys": (200, 403, 403),
    "create_key": (201, 403, 403),
    "revoke_key": (200, 403, 403),
    "rotate_key": (200, 403, 403),
    "audit": (200, 403, 403),
}
ROLES = ("admin", "member", "viewer")


def _request_for(case, ids, payload):
    return {
        "me": ("GET", "/api/me", None),
        "list_runs": ("GET", "/api/runs", None),
        "get_run": ("GET", f"/api/runs/{SESSION_ID}", None),
        "receipt": ("GET", f"/runs/{SESSION_ID}", None),
        "stats": ("GET", "/api/stats", None),
        "list_approvals": ("GET", "/api/approvals", None),
        "get_approval": ("GET", f"/api/approvals/{ids['approval']}", None),
        "approval_page": ("GET", f"/approvals/{ids['approval']}", None),
        "settings_get": ("GET", "/api/team/settings", None),
        "settings_put": ("PUT", "/api/team/settings", {"approval_ttl_s": 900}),
        "push_run": ("POST", "/api/runs", payload),
        "create_approval": ("POST", "/api/approvals", APPROVAL),
        "decide_approval": ("POST", f"/api/approvals/{ids['approval']}/decision", {"decision": "approve"}),
        "list_keys": ("GET", "/api/keys", None),
        "create_key": ("POST", "/api/keys", {"label": "new", "role": "viewer"}),
        "revoke_key": ("POST", f"/api/keys/{ids['spare']}/revoke", None),
        "rotate_key": ("POST", f"/api/keys/{ids['spare']}/rotate", None),
        "audit": ("GET", "/api/audit", None),
    }[case]


@pytest.fixture
def world(env):
    srv, base = env
    team_id, admin = srv.db.create_team("alpha")
    keys = {
        "admin": admin,
        "member": srv.db.create_key(team_id, "member-key", "member", "test")["key"],
        "viewer": srv.db.create_key(team_id, "viewer-key", "viewer", "test")["key"],
    }
    ids = {
        "spare": srv.db.create_key(team_id, "spare", "viewer", "test")["id"],
        "approval": _approval(base, admin),
    }
    client.push(base, admin, FIX, user="dev@example.com")
    return srv, base, team_id, keys, ids


@pytest.mark.parametrize("role_index", range(3), ids=ROLES)
@pytest.mark.parametrize("case", list(MATRIX))
def test_role_matrix_for_every_endpoint(world, case, role_index):
    srv, base, team_id, keys, ids = world
    role = ROLES[role_index]
    method, path, body = _request_for(case, ids, client.build_payload(FIX, user="dev@example.com"))
    status, _, raw = _http(base, method, path, body=body, headers=_auth(keys[role]))
    assert status == MATRIX[case][role_index], (role, case, raw[:200])
    if status == 403:
        assert _json(raw)["error"]["code"] == "forbidden"


def test_forbidden_writes_change_nothing(world):
    srv, base, team_id, keys, ids = world
    before = len(srv.db.list_approvals(team_id, None, 600))
    status, headers, raw = _http(base, "POST", "/api/approvals", body=APPROVAL, headers=_auth(keys["viewer"]))
    assert status == 403 and headers["content-type"].startswith("application/json")
    assert "viewer" in _json(raw)["error"]["message"]
    assert len(srv.db.list_approvals(team_id, None, 600)) == before
    assert _http(base, "POST", f"/api/approvals/{ids['approval']}/decision",
                 body={"decision": "deny"}, headers=_auth(keys["viewer"]))[0] == 403
    status, _, raw = _http(base, "GET", f"/api/approvals/{ids['approval']}", headers=_auth(keys["viewer"]))
    assert status == 200 and _json(raw)["status"] == "pending"


def test_key_format_and_stored_prefix(env):
    srv, base = env
    team_id, key = srv.db.create_team("alpha")
    assert KEY_RE.fullmatch(key)
    rows = _db_rows(srv.db.path, "SELECT label, role, prefix, key_hash FROM api_keys WHERE team_id = ?", (team_id,))
    assert rows == [("initial", "admin", key[:8], hash_key(key))]
    assert _json(_http(base, "GET", "/api/me", headers=_auth(key))[2])["key"]["prefix"] == key[:8]


def test_me_reports_team_role_and_key_for_a_bearer_key_and_a_session(env):
    srv, base = env
    team_id, admin = srv.db.create_team("alpha")
    member = _create_key(base, admin, "laptop", "member")
    me = _json(_http(base, "GET", "/api/me", headers=_auth(member["key"]))[2])
    assert me == {
        "team": {"id": team_id, "name": "alpha"},
        "role": "member",
        "key": {"id": member["id"], "label": "laptop", "prefix": member["key"][:8]},
    }
    cookie = _cookie_for(base, member["key"])
    assert _json(_http(base, "GET", "/api/me", headers={"Cookie": cookie})[2]) == me


def test_create_key_returns_the_secret_once_and_listing_never_does(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    created = _create_key(base, admin, "laptop", "member")
    assert set(created) == {"id", "label", "role", "prefix", "created_at", "key"}
    assert created["label"] == "laptop" and created["role"] == "member"
    assert KEY_RE.fullmatch(created["key"]) and created["prefix"] == created["key"][:8]

    status, _, raw = _http(base, "GET", "/api/keys", headers=_auth(admin))
    assert status == 200 and created["key"].encode() not in raw
    keys = _json(raw)["keys"]
    assert {k["label"] for k in keys} == {"initial", "laptop"}
    assert all("key" not in k and "key_hash" not in k for k in keys)
    assert set(keys[0]) == {"id", "label", "role", "prefix", "created_at", "last_used_at", "revoked_at"}
    assert _http(base, "GET", "/api/me", headers=_auth(created["key"]))[0] == 200


@pytest.mark.parametrize("body", [
    {"label": "x", "role": "owner"},
    {"label": "", "role": "member"},
    {"label": "   ", "role": "member"},
    {"label": "bad\nlabel", "role": "member"},
    {"label": "x" * 101, "role": "member"},
    {"label": 5, "role": "member"},
    {"role": "member"},
    {"label": "x"},
])
def test_create_key_rejects_bad_labels_and_roles(env, body):
    srv, base = env
    team_id, admin = srv.db.create_team("alpha")
    status, _, raw = _http(base, "POST", "/api/keys", body=body, headers=_auth(admin))
    assert status == 400 and _json(raw)["error"]["code"] == "invalid_key"
    assert len(srv.db.list_keys(team_id)) == 1


def test_rotate_replaces_the_secret_and_the_old_key_stops_at_once(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    ci = _create_key(base, admin, "ci", "member")
    status, _, raw = _http(base, "POST", f"/api/keys/{ci['id']}/rotate", headers=_auth(admin))
    assert status == 200
    rotated = _json(raw)
    assert rotated["id"] == ci["id"] and rotated["label"] == "ci" and rotated["role"] == "member"
    assert KEY_RE.fullmatch(rotated["key"]) and rotated["key"] != ci["key"]
    assert rotated["prefix"] == rotated["key"][:8]
    assert _http(base, "GET", "/api/me", headers=_auth(ci["key"]))[0] == 401
    me = _json(_http(base, "GET", "/api/me", headers=_auth(rotated["key"]))[2])
    assert me["role"] == "member" and me["key"]["id"] == ci["id"]
    assert ci["key"] not in json.dumps(_json(_http(base, "GET", "/api/keys", headers=_auth(admin))[2]))


def test_revoked_key_is_401_and_listed_with_its_revoked_time(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    member = _create_key(base, admin, "old-laptop", "member")
    status, _, raw = _http(base, "POST", f"/api/keys/{member['id']}/revoke", headers=_auth(admin))
    assert status == 200 and _json(raw)["id"] == member["id"] and _json(raw)["revoked_at"]
    assert _http(base, "GET", "/api/runs", headers=_auth(member["key"]))[0] == 401
    assert _http(base, "POST", "/api/approvals", body=APPROVAL, headers=_auth(member["key"]))[0] == 401
    listed = {k["id"]: k for k in _json(_http(base, "GET", "/api/keys", headers=_auth(admin))[2])["keys"]}
    assert listed[member["id"]]["revoked_at"] is not None


def test_a_revoked_key_cannot_be_revoked_or_rotated_again(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    member = _create_key(base, admin, "m", "member")
    assert _http(base, "POST", f"/api/keys/{member['id']}/revoke", headers=_auth(admin))[0] == 200
    status, _, raw = _http(base, "POST", f"/api/keys/{member['id']}/revoke", headers=_auth(admin))
    assert status == 409 and _json(raw)["error"]["code"] == "already_revoked"
    status, _, raw = _http(base, "POST", f"/api/keys/{member['id']}/rotate", headers=_auth(admin))
    assert status == 409 and _json(raw)["error"]["code"] == "key_revoked"


def test_the_last_active_admin_key_cannot_be_revoked(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    status, _, raw = _http(base, "POST", f"/api/keys/{_my_key_id(base, admin)}/revoke", headers=_auth(admin))
    assert status == 409 and _json(raw)["error"]["code"] == "last_admin"
    assert _http(base, "GET", "/api/me", headers=_auth(admin))[0] == 200


def test_another_admin_key_can_be_revoked_and_then_the_last_one_is_protected(env):
    srv, base = env
    _, first = srv.db.create_team("alpha")
    second = _create_key(base, first, "second admin", "admin")
    first_id = _my_key_id(base, first)
    assert _http(base, "POST", f"/api/keys/{first_id}/revoke", headers=_auth(second["key"]))[0] == 200
    assert _http(base, "GET", "/api/me", headers=_auth(first))[0] == 401
    status, _, raw = _http(base, "POST", f"/api/keys/{second['id']}/revoke", headers=_auth(second["key"]))
    assert status == 409 and _json(raw)["error"]["code"] == "last_admin"


def test_a_member_or_viewer_key_can_be_revoked_even_when_it_is_the_only_one(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    viewer = _create_key(base, admin, "v", "viewer")
    assert _http(base, "POST", f"/api/keys/{viewer['id']}/revoke", headers=_auth(admin))[0] == 200


def test_key_management_is_scoped_to_the_callers_team(env):
    srv, base = env
    _, admin_a = srv.db.create_team("alpha")
    _, admin_b = srv.db.create_team("beta")
    member_a = _create_key(base, admin_a, "a-member", "member")
    assert _http(base, "POST", f"/api/keys/{member_a['id']}/revoke", headers=_auth(admin_b))[0] == 404
    assert _http(base, "POST", f"/api/keys/{member_a['id']}/rotate", headers=_auth(admin_b))[0] == 404
    assert _http(base, "GET", "/api/me", headers=_auth(member_a["key"]))[0] == 200
    listed = _json(_http(base, "GET", "/api/keys", headers=_auth(admin_b))[2])["keys"]
    assert [k["label"] for k in listed] == ["initial"]


def test_unknown_and_malformed_key_ids_are_404(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    assert _http(base, "POST", "/api/keys/no-such-key/revoke", headers=_auth(admin))[0] == 404
    assert _http(base, "POST", "/api/keys/bad.id/rotate", headers=_auth(admin))[0] == 404
    assert _http(base, "GET", "/api/keys/someone/revoke", headers=_auth(admin))[0] == 405


def test_last_used_is_written_at_most_once_a_minute(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    key_id = _my_key_id(base, admin)
    assert _last_used(srv.db.path, key_id) is not None
    _set_last_used(srv.db.path, key_id, 30)
    marker = _last_used(srv.db.path, key_id)
    _http(base, "GET", "/api/me", headers=_auth(admin))
    assert _last_used(srv.db.path, key_id) == marker  # less than a minute later: no write
    _set_last_used(srv.db.path, key_id, 61)
    marker = _last_used(srv.db.path, key_id)
    _http(base, "GET", "/api/me", headers=_auth(admin))
    assert _last_used(srv.db.path, key_id) != marker


def test_session_follows_the_role_of_the_key_it_was_signed_in_with(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    viewer = _create_key(base, admin, "reader", "viewer")
    cookie = _cookie_for(base, viewer["key"])
    assert _json(_http(base, "GET", "/api/me", headers={"Cookie": cookie})[2])["role"] == "viewer"
    assert _http(base, "GET", "/api/audit", headers={"Cookie": cookie})[0] == 403
    status, _, raw = _http(base, "POST", "/api/keys", body={"label": "x", "role": "viewer"},
                           headers={"Cookie": cookie, **CSRF})
    assert status == 403 and _json(raw)["error"]["code"] == "forbidden"


def test_session_ends_when_its_key_is_revoked(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    member = _create_key(base, admin, "m", "member")
    cookie = _cookie_for(base, member["key"])
    assert _http(base, "GET", "/api/runs", headers={"Cookie": cookie})[0] == 200
    assert _http(base, "POST", f"/api/keys/{member['id']}/revoke", headers=_auth(admin))[0] == 200
    assert _http(base, "GET", "/api/runs", headers={"Cookie": cookie})[0] == 401
    assert _http(base, "GET", "/", headers={"Cookie": cookie})[0] == 401


def test_session_ends_when_its_key_is_revoked_from_the_command_line(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    with running(db_path) as (srv, base):
        team_id, admin = srv.db.create_team("alpha")
        member = srv.db.create_key(team_id, "m", "member", "test")
        cookie = _cookie_for(base, member["key"])
        assert _http(base, "GET", "/api/runs", headers={"Cookie": cookie})[0] == 200
        assert main(["key", "revoke", member["id"], "--db", str(db_path)]) == 0
        assert "Revoked" in capsys.readouterr().out
        assert _http(base, "GET", "/api/runs", headers={"Cookie": cookie})[0] == 401


def test_session_ends_when_its_key_is_rotated(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    member = _create_key(base, admin, "m", "member")
    cookie = _cookie_for(base, member["key"])
    rotated = _json(_http(base, "POST", f"/api/keys/{member['id']}/rotate", headers=_auth(admin))[2])
    assert _http(base, "GET", "/api/runs", headers={"Cookie": cookie})[0] == 401
    assert _http(base, "GET", "/api/runs", headers=_auth(rotated["key"]))[0] == 200


def test_cookie_writes_need_the_csrf_header(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    cookie = _cookie_for(base, admin)
    status, _, raw = _http(base, "POST", "/api/keys", body={"label": "x", "role": "viewer"},
                           headers={"Cookie": cookie})
    assert status == 403 and _json(raw)["error"]["code"] == "csrf_required"
    assert _http(base, "POST", "/api/keys", body={"label": "x", "role": "viewer"},
                 headers={"Cookie": cookie, **CSRF})[0] == 201


def test_admin_only_endpoints_refuse_member_sessions(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    member = _create_key(base, admin, "m", "member")
    cookie = _cookie_for(base, member["key"])
    assert _http(base, "GET", "/api/keys", headers={"Cookie": cookie})[0] == 403
    assert _http(base, "GET", "/api/audit", headers={"Cookie": cookie})[0] == 403


def test_webhook_urls_are_masked_for_roles_other_than_admin(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    viewer = _create_key(base, admin, "v", "viewer")
    hook = "https://hooks.example.com/services/T000/B000/SECRETPATH"
    assert _http(base, "PUT", "/api/team/settings", body={"webhook_url": hook}, headers=_auth(admin))[0] == 200
    full = _json(_http(base, "GET", "/api/team/settings", headers=_auth(admin))[2])
    assert full["webhook_url"] == hook
    status, _, raw = _http(base, "GET", "/api/team/settings", headers=_auth(viewer["key"]))
    assert status == 200 and b"SECRETPATH" not in raw
    assert _json(raw)["webhook_url"] == "https://hooks.example.com/[hidden]"


def test_audit_records_keys_settings_pushes_decisions_and_sign_ins(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    ci = _create_key(base, admin, "ci", "member")
    assert _http(base, "POST", f"/api/keys/{ci['id']}/rotate", headers=_auth(admin))[0] == 200
    assert _http(base, "POST", f"/api/keys/{ci['id']}/revoke", headers=_auth(admin))[0] == 200
    assert _http(base, "PUT", "/api/team/settings", body={"webhook_url": "https://h.example.com/x",
                                                          "approval_ttl_s": 900},
                 headers=_auth(admin))[0] == 200
    client.push(base, admin, FIX, user="dev@example.com")
    approval_id = _approval(base, admin)
    assert _http(base, "POST", f"/api/approvals/{approval_id}/decision", body={"decision": "approve"},
                 headers=_auth(admin))[0] == 200
    _cookie_for(base, admin)

    events = _audit(base, admin)
    actions = [e["action"] for e in events]
    for action in ("key.create", "key.rotate", "key.revoke", "team.settings", "run.push",
                   "approval.decide", "auth.sign_in"):
        assert action in actions, action
    assert events == sorted(events, key=lambda e: -e["id"])
    by_action = {}
    for event in events:
        by_action.setdefault(event["action"], event)
    assert by_action["run.push"]["target"] == SESSION_ID
    assert by_action["run.push"]["details"] == {"agent": "Claude Code", "project": "payments-service"}
    assert by_action["run.push"]["actor"] == f"initial ({admin[:8]})"
    assert by_action["approval.decide"]["details"] == {"status": "approved"}
    assert by_action["approval.decide"]["target"] == approval_id
    assert by_action["team.settings"]["details"] == {"fields": ["approval_ttl_s", "webhook_url"],
                                                     "approval_ttl_s": 900}
    assert by_action["auth.sign_in"]["actor"] == "dashboard:initial"
    assert by_action["auth.sign_in"]["target"] == _my_key_id(base, admin)
    assert by_action["key.revoke"]["target"] == ci["id"]


def test_audit_never_holds_a_key_or_a_webhook_url(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    ci = _create_key(base, admin, "ci", "member")
    rotated = _json(_http(base, "POST", f"/api/keys/{ci['id']}/rotate", headers=_auth(admin))[2])
    hook = "https://hooks.example.com/services/SECRET-PATH-123"
    assert _http(base, "PUT", "/api/team/settings", body={"webhook_url": hook}, headers=_auth(admin))[0] == 200
    _cookie_for(base, admin)
    _create_key(base, admin, "second", "viewer")
    raw = _http(base, "GET", "/api/audit", headers=_auth(admin))[2].decode("utf-8")
    stored = " ".join(" ".join(map(str, row)) for row in _db_rows(
        srv.db.path, "SELECT actor, action, target, details FROM audit_log"))
    for secret in (admin, ci["key"], rotated["key"], hook, "SECRET-PATH-123"):
        assert secret not in raw
        assert secret not in stored


def test_audit_is_newest_first_and_pages_with_before(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    for index in range(4):
        _create_key(base, admin, f"k{index}", "viewer")
    everything = _audit(base, admin)
    assert len(everything) == 5
    page_one = _audit(base, admin, "?limit=2")
    assert [e["id"] for e in page_one] == [e["id"] for e in everything[:2]]
    page_two = _audit(base, admin, f"?limit=2&before={page_one[-1]['id']}")
    assert [e["id"] for e in page_two] == [e["id"] for e in everything[2:4]]
    assert _http(base, "GET", "/api/audit?limit=0", headers=_auth(admin))[0] == 400


def test_cli_key_changes_are_audited_as_cli(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    assert main(["team", "create", "demo", "--db", str(db_path)]) == 0
    admin = next(line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("rl_"))
    assert main(["key", "create", "--team-id", "1", "--label", "laptop", "--role", "member",
                 "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    new_key = next(line.strip() for line in out.splitlines() if line.strip().startswith("rl_"))
    assert KEY_RE.fullmatch(new_key)
    laptop_id = re.search(r"\(id ([A-Za-z0-9_-]+)\)", out).group(1)
    actions = _db_rows(db_path, "SELECT actor, action FROM audit_log ORDER BY id")
    assert ("cli", "key.create") in actions

    assert main(["key", "list", "--team-id", "1", "--db", str(db_path)]) == 0
    listing = capsys.readouterr().out
    assert "laptop" in listing and new_key not in listing and admin not in listing

    assert main(["key", "rotate", laptop_id, "--db", str(db_path)]) == 0
    rotated = next(line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("rl_"))
    assert rotated != new_key and KEY_RE.fullmatch(rotated)
    assert ("cli", "key.rotate") in _db_rows(db_path, "SELECT actor, action FROM audit_log ORDER BY id")

    initial_id = _db_rows(db_path, "SELECT id FROM api_keys WHERE label = 'initial'")[0][0]
    assert main(["key", "revoke", initial_id, "--db", str(db_path)]) == 1
    assert "last active admin key" in capsys.readouterr().err
    assert main(["key", "revoke", "no-such-key", "--db", str(db_path)]) == 1
    assert main(["key", "create", "--team-id", "99", "--label", "x", "--role", "viewer",
                 "--db", str(db_path)]) == 1
    with pytest.raises(SystemExit):  # argparse refuses a role outside admin, member, viewer
        main(["key", "create", "--team-id", "1", "--label", "x", "--role", "root", "--db", str(db_path)])


def test_old_database_keeps_its_key_working_and_gains_an_admin_key_row(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO teams (name, api_key_hash, created_at) VALUES ('old', ?, '2026-01-01T00:00:00Z')",
                     (hash_key(OLD_KEY),))
        conn.execute("INSERT INTO teams (name, api_key_hash, created_at) VALUES ('second', ?, '2026-02-01T00:00:00Z')",
                     (hash_key(OLD_KEY_TWO),))
        conn.execute(
            "INSERT INTO runs (id, team_id, user, project, title, started_at, ended_at, models, steps, tokens, "
            "files_changed, cost, risk_score, risk_level, receipt_json, receipt_html, created_at, updated_at) "
            "VALUES ('legacy-run', 1, 'dev@example.com', 'payments', 'old run', '2026-01-02T10:00:00Z', "
            "'2026-01-02T11:00:00Z', '{}', 3, 100, 1, 0.5, 40, 'medium', '{\"session_id\": \"legacy-run\"}', NULL, "
            "'2026-01-02T11:00:00Z', '2026-01-02T11:00:00Z')"
        )
        conn.commit()

    with running(path) as (srv, base):
        status, _, raw = _http(base, "GET", "/api/runs", headers=_auth(OLD_KEY))
        assert status == 200
        runs = _json(raw)["runs"]
        assert [r["id"] for r in runs] == ["legacy-run"] and runs[0]["agent"] is None
        me = _json(_http(base, "GET", "/api/me", headers=_auth(OLD_KEY))[2])
        assert me["role"] == "admin" and me["key"]["label"] == "initial" and me["key"]["prefix"] is None
        assert _http(base, "GET", "/api/keys", headers=_auth(OLD_KEY))[0] == 200
        assert _http(base, "GET", "/api/runs", headers=_auth(OLD_KEY_TWO))[0] == 200

    rows = _db_rows(path, "SELECT team_id, label, role, key_hash, prefix FROM api_keys ORDER BY team_id")
    assert rows == [(1, "initial", "admin", hash_key(OLD_KEY), None),
                    (2, "initial", "admin", hash_key(OLD_KEY_TWO), None)]


def test_migration_never_brings_back_a_rotated_or_revoked_key(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO teams (name, api_key_hash, created_at) VALUES ('old', ?, '2026-01-01T00:00:00Z')",
                     (hash_key(OLD_KEY),))
        conn.commit()
    with closing(Database(str(path))) as db:
        key_id = db.key_for_token(OLD_KEY)["id"]
        outcome, view = db.rotate_key(1, key_id, actor="test")
        assert outcome == "ok"
    for _ in range(2):  # reopening, again and again, must not restore the old key
        with closing(Database(str(path))) as db:
            assert db.key_for_token(OLD_KEY) is None
            assert db.key_for_token(view["key"])["label"] == "initial"
    assert _db_rows(path, "SELECT COUNT(*) FROM api_keys WHERE team_id = 1")[0][0] == 1


def test_migration_adds_the_agent_column_to_an_old_runs_table(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO teams (name, api_key_hash, created_at) VALUES ('old', ?, '2026-01-01T00:00:00Z')",
                     (hash_key(OLD_KEY),))
        conn.commit()
    with closing(Database(str(path))) as db:
        assert "agent" in {row[1] for row in _db_rows(path, "PRAGMA table_info(runs)")}
        assert db.list_runs(1) == []


def test_agent_is_stored_listed_filtered_and_summed_by_stats(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    client.push(base, admin, FIX, user="dev@example.com")
    payload = client.build_payload(FIX, user="ann@example.com")
    payload["session_id"] = "codex-0001"
    payload["agent"] = "Codex CLI"
    assert _http(base, "POST", "/api/runs", body=payload, headers=_auth(admin))[0] == 201

    runs = _json(_http(base, "GET", "/api/runs", headers=_auth(admin))[2])["runs"]
    assert {r["id"]: r["agent"] for r in runs} == {SESSION_ID: "Claude Code", "codex-0001": "Codex CLI"}
    one = _json(_http(base, "GET", f"/api/runs/{SESSION_ID}", headers=_auth(admin))[2])["run"]
    assert one["agent"] == "Claude Code"

    def ids(query):
        raw = _http(base, "GET", "/api/runs" + query, headers=_auth(admin))[2]
        return sorted(r["id"] for r in _json(raw)["runs"])

    assert ids("?agent=claude") == [SESSION_ID]          # case-insensitive substring
    assert ids("?agent=CODEX") == ["codex-0001"]
    assert ids("?agent=%25") == []                       # "%" is literal
    assert ids("?agent=nothing") == []

    stats = _json(_http(base, "GET", "/api/stats", headers=_auth(admin))[2])
    by_agent = {row["agent"]: row for row in stats["by_agent"]}
    assert by_agent["Claude Code"]["runs"] == 1 and by_agent["Codex CLI"]["runs"] == 1
    assert by_agent["Claude Code"]["cost_usd"] == pytest.approx(one["receipt"]["totals"]["cost_usd"], rel=1e-4)


def test_a_receipt_without_an_agent_is_stored_as_null_and_counted_as_unknown(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    payload = client.build_payload(FIX, user="dev@example.com")
    payload.pop("agent")
    assert _http(base, "POST", "/api/runs", body=payload, headers=_auth(admin))[0] == 201
    run = _json(_http(base, "GET", f"/api/runs/{SESSION_ID}", headers=_auth(admin))[2])["run"]
    assert run["agent"] is None
    stats = _json(_http(base, "GET", "/api/stats", headers=_auth(admin))[2])
    assert [row["agent"] for row in stats["by_agent"]] == ["unknown"]


def test_every_response_carries_the_security_headers(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    responses = [
        _http(base, "GET", "/health"),
        _http(base, "GET", "/api/runs"),
        _http(base, "GET", "/no/such/page", headers=_auth(admin)),
        _http(base, "GET", f"/?key={admin}"),
        _http(base, "GET", "/", headers=_auth(admin)),
        _http(base, "DELETE", "/api/runs", headers=_auth(admin)),
    ]
    for status, headers, _ in responses:
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-frame-options"] == "DENY"
        assert "strict-transport-security" not in headers  # plain HTTP, no proxy: not secure


def test_plain_http_sets_no_secure_flag_by_default(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    status, headers, _ = _http(base, "GET", f"/?key={admin}")
    assert status == 302 and "Secure" not in headers["set-cookie"]


def test_secure_cookies_option_sets_the_flag_and_hsts(tmp_path):
    with running(tmp_path / "s.db", secure_cookies=True) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        status, headers, _ = _http(base, "GET", f"/?key={admin}")
        assert status == 302 and "; Secure" in headers["set-cookie"]
        assert headers["strict-transport-security"].startswith("max-age=")


def test_secure_cookies_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_SECURE_COOKIES", "1")
    with running(tmp_path / "e.db") as (srv, base):
        _, admin = srv.db.create_team("alpha")
        assert "; Secure" in _http(base, "GET", f"/?key={admin}")[1]["set-cookie"]


def test_trust_proxy_honours_forwarded_https_for_the_secure_flag(tmp_path):
    with running(tmp_path / "p.db", trust_proxy=True) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        status, headers, _ = _http(base, "GET", f"/?key={admin}", headers={"X-Forwarded-Proto": "https"})
        assert "; Secure" in headers["set-cookie"]
        assert headers["strict-transport-security"].startswith("max-age=")
        _, headers, _ = _http(base, "GET", f"/?key={admin}", headers={"X-Forwarded-Proto": "http"})
        assert "Secure" not in headers["set-cookie"]
        assert "strict-transport-security" not in headers

    with running(tmp_path / "q.db") as (srv, base):  # trust_proxy off: the header is ignored
        _, admin = srv.db.create_team("alpha")
        _, headers, _ = _http(base, "GET", f"/?key={admin}", headers={"X-Forwarded-Proto": "https"})
        assert "Secure" not in headers["set-cookie"]


def test_failure_limiter_blocks_after_twenty_failures_and_recovers():
    clock = [1000.0]
    limiter = FailureLimiter(clock=lambda: clock[0])
    for _ in range(MAX_FAILURES):
        limiter.record_failure("10.0.0.1")
    assert limiter.blocked_for("10.0.0.1") == 0          # twenty failures are still allowed
    limiter.record_failure("10.0.0.1")                   # the twenty-first
    assert limiter.blocked_for("10.0.0.1") == 300
    assert limiter.blocked_for("10.0.0.2") == 0          # other addresses are not affected
    clock[0] += 299
    assert limiter.blocked_for("10.0.0.1") == 1
    clock[0] += 1
    assert limiter.blocked_for("10.0.0.1") == 0


def test_wrong_keys_get_429_for_the_address_after_twenty_failures(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    for _ in range(MAX_FAILURES + 1):
        assert _http(base, "GET", "/api/runs", headers=_auth("rl_wrong"))[0] == 401
    status, headers, raw = _http(base, "GET", "/api/runs", headers=_auth(admin))
    assert status == 429 and _json(raw)["error"]["code"] == "rate_limited"
    assert int(headers["retry-after"]) > 0
    assert _http(base, "GET", "/", headers=_auth(admin))[0] == 429
    assert _http(base, "GET", f"/?key={admin}")[0] == 429     # sign-in is blocked too
    assert _http(base, "GET", "/health")[0] == 200            # liveness is not limited


def test_requests_without_credentials_do_not_count_toward_the_limit(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    for _ in range(MAX_FAILURES + 10):
        assert _http(base, "GET", "/api/runs")[0] == 401
    assert _http(base, "GET", "/api/runs", headers=_auth(admin))[0] == 200


def test_stale_session_cookies_do_not_count_toward_the_limit(env):
    # An open dashboard after a restart or a revoke polls with a dead cookie; that must not lock it out.
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    member = _create_key(base, admin, "m", "member")
    cookie = _cookie_for(base, member["key"])
    assert _http(base, "POST", f"/api/keys/{member['id']}/revoke", headers=_auth(admin))[0] == 200
    for _ in range(MAX_FAILURES + 10):
        assert _http(base, "GET", "/api/approvals", headers={"Cookie": cookie})[0] == 401
    assert _http(base, "GET", "/api/runs", headers=_auth(admin))[0] == 200


def test_failed_sign_ins_count_toward_the_limit(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    for _ in range(MAX_FAILURES + 1):
        assert _http(base, "GET", "/?key=rl_wrong")[0] == 401
    assert _http(base, "GET", f"/?key={admin}")[0] == 429


def test_tls_context_reports_a_missing_certificate_file(tmp_path):
    from runledger.server.app import make_tls_context
    with pytest.raises(TLSConfigError, match="file not found"):
        make_tls_context(str(tmp_path / "missing.pem"), str(tmp_path / "missing.key"))


@pytest.fixture
def tls_files(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("the openssl command is not available")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True, timeout=120,
    )
    return cert, key


def test_https_serving_with_a_self_signed_certificate(tmp_path, tls_files):
    cert, key = tls_files
    with running(tmp_path / "tls.db", tls_cert=str(cert), tls_key=str(key)) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        ctx = ssl.create_default_context(cafile=str(cert))
        assert base.startswith("https://") and srv.public_url.startswith("https://")
        assert srv.tls_context.minimum_version == ssl.TLSVersion.TLSv1_2

        status, headers, _ = _http(base, "GET", "/health", context=ctx)
        assert status == 200 and headers["strict-transport-security"].startswith("max-age=")
        status, headers, _ = _http(base, "GET", f"/?key={admin}", context=ctx)
        assert status == 302 and "; Secure" in headers["set-cookie"]
        assert _http(base, "GET", "/api/me", headers=_auth(admin), context=ctx)[0] == 200

        # Plain HTTP to the TLS port gets no answer, and the server keeps serving.
        with pytest.raises((http.client.HTTPException, OSError)):
            _http(base.replace("https://", "http://"), "GET", "/health")
        assert _http(base, "GET", "/health", context=ctx)[0] == 200


def test_bad_tls_files_are_reported_without_a_traceback(tmp_path, tls_files, capsys):
    cert, key = tls_files
    bad_key = tmp_path / "bad.key"
    bad_key.write_text("this is not a key\n", encoding="utf-8")
    with pytest.raises(TLSConfigError, match="file not found"):
        make_server(str(tmp_path / "a.db"), port=0, tls_cert=str(tmp_path / "nope.pem"), tls_key=str(key))
    with pytest.raises(TLSConfigError, match="cannot load"):
        make_server(str(tmp_path / "b.db"), port=0, tls_cert=str(cert), tls_key=str(bad_key))
    with pytest.raises(TLSConfigError, match="must be given together"):
        make_server(str(tmp_path / "c.db"), port=0, tls_cert=str(cert))
    assert main(["serve", "--db", str(tmp_path / "d.db"), "--port", "0", "--tls-cert", str(cert)]) == 1
    assert "must be given together" in capsys.readouterr().err
