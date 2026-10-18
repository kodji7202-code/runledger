"""Tests for the RunLedger team server, the push client and the team CLI.

Each test runs a real server on a free port in a background thread, with a
temporary SQLite database.
"""
import copy
import http.client
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from runledger import client
from runledger.cli import main
from runledger.server.app import MAX_BODY_BYTES, make_server
from runledger.server.dashboard import DASHBOARD_HTML
from runledger.server.db import Database

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
NATIVE = Path(__file__).parent / "fixtures" / "native_session.json"
SESSION_ID = "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42"
XSS = "<script>alert(1)</script>"


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


def _stored(payload, run_id, *, steps=4, ended="2026-10-15T10:05:00Z", cost=0.1, risk=20, **extra):
    """A receipt for `run_id` with a chosen step count, end time, cost and risk."""
    body = copy.deepcopy(payload)
    body["session_id"] = run_id
    body["started"] = extra.pop("started", "2026-10-15T10:00:00Z")
    body["ended"] = ended
    body["totals"] = dict(body["totals"], steps=steps, cost_usd=cost)
    body["models"] = {"claude-sonnet-4-5-20250929": {"tokens": 1000, "cost_usd": cost}}
    level = "high" if risk >= 60 else "medium" if risk >= 25 else "low"
    body["risk"] = {"score": risk, "level": level, "reasons": []}
    body["html"] = f"<html><body>{run_id} version {steps}</body></html>"
    body.update(extra)
    return body


def _push(base, key, payload):
    status, _, raw = _http(base, "POST", "/api/runs", body=payload, headers=_auth(key))
    return status, _json(raw)


def _run_row(db_path, run_id):
    """The stored row of a run, for the fields the API does not expose."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None


def _revisions(db_path, run_id):
    with closing(sqlite3.connect(str(db_path))) as conn:
        return [row[0] for row in conn.execute(
            "SELECT revision FROM run_revisions WHERE run_id = ? ORDER BY revision", (run_id,)
        )]


def test_push_then_list_get_and_stats(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")

    result = client.push(base, key, FIX, user="dev@example.com")
    assert result["id"] == SESSION_ID
    assert result["url"] == f"/runs/{SESSION_ID}"
    assert result["risk_level"] == "high"

    status, _, raw = _http(base, "GET", "/api/runs", headers=_auth(key))
    runs = _json(raw)["runs"]
    assert status == 200
    assert [r["id"] for r in runs] == [SESSION_ID]
    assert runs[0]["user"] == "dev@example.com"
    assert runs[0]["project"] == "payments-service"
    assert runs[0]["risk_level"] == "high"
    assert runs[0]["has_html"] is True

    status, _, raw = _http(base, "GET", f"/api/runs/{SESSION_ID}", headers=_auth(key))
    run = _json(raw)["run"]
    assert status == 200
    assert run["receipt"]["session_id"] == SESSION_ID
    assert {"secret_file", "test_deleted"} <= {r["code"] for r in run["risks"]}

    status, _, raw = _http(base, "GET", "/api/stats?days=30", headers=_auth(key))
    stats = _json(raw)
    assert status == 200
    assert stats["team"] == "alpha"
    assert stats["totals"]["runs"] == 1
    assert stats["totals"]["high_risk_runs"] == 1
    assert stats["totals"]["cost_usd"] == pytest.approx(run["receipt"]["totals"]["cost_usd"], rel=1e-4)
    assert [u["user"] for u in stats["by_user"]] == ["dev@example.com"]
    assert {m["model"] for m in stats["by_model"]} == {"Sonnet 4.5", "Haiku 4.5"}
    assert sum(m["cost_usd"] for m in stats["by_model"]) == pytest.approx(stats["totals"]["cost_usd"], rel=1e-4)
    assert stats["top_risk_codes"]

    status, headers, html = _http(base, "GET", f"/runs/{SESSION_ID}", headers=_auth(key))
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert b"<html" in html
    assert "default-src 'none'" in headers["content-security-policy"]


def test_api_requires_a_valid_key_and_stores_nothing_without_one(env):
    srv, base = env
    team_id, _ = srv.db.create_team("alpha")
    body = client.build_payload(FIX, user="dev@example.com")

    status, _, raw = _http(base, "POST", "/api/runs", body=body)
    assert status == 401
    assert _json(raw)["error"]["code"] == "unauthorized"
    assert _http(base, "POST", "/api/runs", body=body, headers=_auth("rl_wrong"))[0] == 401
    assert _http(base, "GET", "/api/runs", headers=_auth("rl_wrong"))[0] == 401
    assert _http(base, "GET", "/api/stats")[0] == 401
    assert srv.db.list_runs(team_id) == []


def test_unknown_runs_routes_and_params_return_json_errors(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")

    status, headers, raw = _http(base, "GET", "/api/runs/does-not-exist", headers=_auth(key))
    assert status == 404
    assert _json(raw)["error"]["code"] == "not_found"
    assert headers["content-type"].startswith("application/json")
    assert _http(base, "GET", "/runs/does-not-exist", headers=_auth(key))[0] == 404
    assert _http(base, "GET", "/no/such/thing", headers=_auth(key))[0] == 404
    assert _http(base, "GET", "/api/stats?days=0", headers=_auth(key))[0] == 400
    assert _http(base, "GET", "/api/runs?min_risk=101", headers=_auth(key))[0] == 400
    assert _http(base, "GET", "/api/runs?limit=abc", headers=_auth(key))[0] == 400
    status, headers, _ = _http(base, "DELETE", "/api/runs", headers=_auth(key))
    assert status == 405 and "GET" in headers["allow"]


def test_teams_cannot_see_each_others_runs(env):
    srv, base = env
    _, key_a = srv.db.create_team("alpha")
    _, key_b = srv.db.create_team("beta")
    client.push(base, key_a, FIX, user="alice@example.com")

    status, _, raw = _http(base, "GET", "/api/runs", headers=_auth(key_b))
    assert status == 200 and _json(raw)["runs"] == []
    assert _http(base, "GET", f"/api/runs/{SESSION_ID}", headers=_auth(key_b))[0] == 404
    assert _http(base, "GET", f"/runs/{SESSION_ID}", headers=_auth(key_b))[0] == 404
    stats = _json(_http(base, "GET", "/api/stats", headers=_auth(key_b))[2])
    assert stats["totals"]["runs"] == 0


def test_same_session_id_in_two_teams_does_not_overwrite(env):
    srv, base = env
    _, key_a = srv.db.create_team("alpha")
    _, key_b = srv.db.create_team("beta")
    client.push(base, key_a, FIX, user="alice@example.com")
    client.push(base, key_b, FIX, user="bob@example.com")

    runs_a = _json(_http(base, "GET", "/api/runs", headers=_auth(key_a))[2])["runs"]
    runs_b = _json(_http(base, "GET", "/api/runs", headers=_auth(key_b))[2])["runs"]
    assert [r["user"] for r in runs_a] == ["alice@example.com"]
    assert [r["user"] for r in runs_b] == ["bob@example.com"]


def test_repushing_is_idempotent_and_another_key_cannot_replace_the_receipt(env):
    srv, base = env
    team_id, key = srv.db.create_team("alpha")
    other = srv.db.create_key(team_id, "other-member", "member", "test")["key"]
    payload = client.build_payload(FIX, user="first@example.com")

    assert _http(base, "POST", "/api/runs", body=payload, headers=_auth(key))[0] == 201
    assert _http(base, "POST", "/api/runs", body=payload, headers=_auth(key))[0] == 200
    # An identical retry is accepted from any key of the team: nothing changes.
    assert _http(base, "POST", "/api/runs", body=payload, headers=_auth(other))[0] == 200

    changed = dict(payload)
    changed["user"] = "second@example.com"
    status, _, raw = _http(base, "POST", "/api/runs", body=changed, headers=_auth(other))
    assert status == 409 and _json(raw)["error"]["code"] == "run_conflict"
    assert "immutable" not in _json(raw)["error"]["message"]

    runs = _json(_http(base, "GET", "/api/runs", headers=_auth(key))[2])["runs"]
    assert len(runs) == 1 and runs[0]["user"] == "first@example.com" and runs[0]["revision"] == 1
    with closing(sqlite3.connect(str(srv.db.path))) as conn:
        pushes = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE team_id = ? AND action = 'run.push' AND target = ?",
            (team_id, SESSION_ID),
        ).fetchone()[0]
        history = conn.execute("SELECT COUNT(*) FROM run_revisions").fetchone()[0]
    assert pushes == 1 and history == 0


def test_receipt_equality_is_type_sensitive_and_a_duplicate_reply_uses_the_stored_risk(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    payload = client.build_payload(FIX, user="first@example.com")
    assert _http(base, "POST", "/api/runs", body=payload, headers=_auth(key))[0] == 201

    # An exact retry reports persisted fields, which matters across parser-version
    # changes where flattened metadata can differ from today's derivation.
    with closing(sqlite3.connect(str(srv.db.path))) as conn:
        conn.execute(
            "UPDATE runs SET risk_score = 77, risk_level = 'high' WHERE team_id = 1 AND id = ?",
            (SESSION_ID,),
        )
        conn.commit()
    status, _, raw = _http(base, "POST", "/api/runs", body=payload, headers=_auth(key))
    assert status == 200
    reply = _json(raw)
    assert reply["risk_score"] == 77 and reply["risk_level"] == "high" and reply["revision"] == 1
    assert _revisions(srv.db.path, SESSION_ID) == []

    # Receipt equality is type-sensitive: True is not 1, so this is a new version, not a retry.
    typed_differently = json.loads(json.dumps(payload))
    typed_differently["risk"]["score"] = True
    status, _, raw = _http(base, "POST", "/api/runs", body=typed_differently, headers=_auth(key))
    assert status == 200
    assert _json(raw) == {"status": "updated", "id": SESSION_ID, "revision": 2}


def test_repush_grown_session_supersedes(env):
    srv, base = env
    team_id, key = srv.db.create_team("alpha")
    key_id = srv.db.key_for_token(key)["id"]
    payload = client.build_payload(FIX, user="dev@example.com")

    grown_first = _stored(payload, "sess-grow", steps=4, ended="2026-10-15T10:05:00Z", cost=0.1, risk=20)
    assert _push(base, key, grown_first)[0] == 201
    created_at = _run_row(srv.db.path, "sess-grow")["created_at"]

    grown = _stored(payload, "sess-grow", steps=9, ended="2026-10-15T10:20:00Z", cost=0.3, risk=60)
    status, body = _push(base, key, grown)
    assert status == 200
    assert body == {"status": "updated", "id": "sess-grow", "revision": 2}

    status, _, raw = _http(base, "GET", "/api/runs/sess-grow", headers=_auth(key))
    run = _json(raw)["run"]
    assert status == 200
    assert run["steps"] == 9 and run["cost_usd"] == 0.3 and run["risk_score"] == 60
    assert run["revision"] == 2 and run["receipt"]["totals"]["steps"] == 9
    row = _run_row(srv.db.path, "sess-grow")
    assert row["created_at"] == created_at and row["updated_at"] >= created_at
    assert _http(base, "GET", "/runs/sess-grow", headers=_auth(key))[2].count(b"version 9") == 1
    assert [r["id"] for r in _json(_http(base, "GET", "/api/runs", headers=_auth(key))[2])["runs"]] == ["sess-grow"]

    with closing(sqlite3.connect(str(srv.db.path))) as conn:
        history = conn.execute(
            "SELECT revision, receipt_json, risk_score, cost, pushed_by_key_id FROM run_revisions "
            "WHERE team_id = ? AND run_id = ?", (team_id, "sess-grow"),
        ).fetchall()
    assert len(history) == 1
    revision, receipt_json, risk_score, cost, pushed_by = history[0]
    assert (revision, risk_score, cost, pushed_by) == (1, 20, 0.1, key_id)
    assert json.loads(receipt_json)["totals"]["steps"] == 4

    events = _json(_http(base, "GET", "/api/audit?action=run.supersede", headers=_auth(key))[2])["events"]
    assert [(e["action"], e["target"]) for e in events] == [("run.supersede", "sess-grow")]


def test_repush_older_ended_at_or_fewer_steps_conflicts(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    payload = client.build_payload(FIX, user="dev@example.com")
    assert _push(base, key, _stored(payload, "sess-r", steps=9, ended="2026-10-15T10:20:00Z"))[0] == 201

    status, body = _push(base, key, _stored(payload, "sess-r", steps=9, ended="2026-10-15T10:05:00Z"))
    assert status == 409 and body["error"]["code"] == "run_conflict"
    status, body = _push(base, key, _stored(payload, "sess-r", steps=5, ended="2026-10-15T10:30:00Z"))
    assert status == 409 and body["error"]["code"] == "run_conflict"

    row = _run_row(srv.db.path, "sess-r")
    assert (row["steps"], row["ended_at"], row["revision"]) == (9, "2026-10-15T10:20:00Z", 1)
    assert _revisions(srv.db.path, "sess-r") == []

    # The same ended time with more steps, and a receipt without an ended time, both supersede.
    same_end = _push(base, key, _stored(payload, "sess-r", steps=11, ended="2026-10-15T10:20:00Z"))
    assert same_end == (200, {"status": "updated", "id": "sess-r", "revision": 2})
    no_end = _push(base, key, _stored(payload, "sess-r", steps=12, ended=None))
    assert no_end == (200, {"status": "updated", "id": "sess-r", "revision": 3})


def test_an_admin_key_may_supersede_a_members_run(env):
    srv, base = env
    team_id, admin = srv.db.create_team("alpha")
    member = srv.db.create_key(team_id, "alice-laptop", "member", "test")["key"]
    admin_label = srv.db.key_for_token(admin)["label"]
    payload = client.build_payload(FIX, user="dev@example.com")
    assert _push(base, member, _stored(payload, "sess-x", steps=4, ended="2026-10-15T10:05:00Z"))[0] == 201

    status, body = _push(base, admin, _stored(payload, "sess-x", steps=6, ended="2026-10-15T10:30:00Z"))
    assert (status, body["status"], body["revision"]) == (200, "updated", 2)
    assert _run_row(srv.db.path, "sess-x")["steps"] == 6

    events = _json(_http(base, "GET", "/api/audit?action=run.supersede", headers=_auth(admin))[2])["events"]
    assert len(events) == 1 and events[0]["actor"].startswith(admin_label)
    assert admin not in json.dumps(events)  # the key itself is never written to the audit log


def test_revision_history_is_capped_at_twenty(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    payload = client.build_payload(FIX, user="dev@example.com")
    for version in range(1, 26):
        status, body = _push(base, key, _stored(
            payload, "sess-many", steps=version * 2, ended=f"2026-10-15T10:{version:02d}:00Z",
        ))
        assert status in (200, 201), body
        assert body["revision"] == version

    assert _run_row(srv.db.path, "sess-many")["steps"] == 50
    assert _revisions(srv.db.path, "sess-many") == list(range(5, 25))


def test_superseding_refreshes_the_derived_fields_and_keeps_approvals(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    payload = client.build_payload(FIX, user="dev@example.com")
    status, _, raw = _http(base, "POST", "/api/approvals",
                           body={"session_id": "sess-d", "tool": "Bash", "summary": "Deploy to production",
                                 "risks": [{"severity": "high", "code": "deploy", "reason": "Prod"}]},
                           headers=_auth(key))
    assert status == 201
    approval_id = _json(raw)["id"]

    first = _stored(payload, "sess-d", steps=4, ended="2026-10-15T10:05:00Z", cost=0.1, risk=20,
                    quality={"score": 80, "grade": "B", "signals": []},
                    ai_review={"verdict": "looks_safe"})
    assert _push(base, key, first)[0] == 201

    def derived():
        row = _json(_http(base, "GET", "/api/runs", headers=_auth(key))[2])["runs"][0]
        return (row["risk_score"], row["risk_level"], row["cost_usd"], row["quality_score"],
                row["quality_grade"], row["ai_verdict"])

    assert derived() == (20, "low", 0.1, 80, "B", "looks_safe")

    second = _stored(payload, "sess-d", steps=9, ended="2026-10-15T10:30:00Z", cost=0.5, risk=70,
                     quality={"score": 40, "grade": "D", "signals": []},
                     ai_review={"verdict": "dangerous"})
    assert _push(base, key, second)[1]["revision"] == 2
    assert derived() == (70, "high", 0.5, 40, "D", "dangerous")

    dangerous = _json(_http(base, "GET", "/api/runs?ai_verdict=dangerous", headers=_auth(key))[2])["runs"]
    safe = _json(_http(base, "GET", "/api/runs?ai_verdict=looks_safe", headers=_auth(key))[2])["runs"]
    assert [r["id"] for r in dangerous] == ["sess-d"] and safe == []

    approvals = _json(_http(base, "GET", "/api/approvals", headers=_auth(key))[2])["approvals"]
    assert [a["id"] for a in approvals] == [approval_id] and approvals[0]["status"] == "pending"


def test_concurrent_pushes_of_one_session_keep_the_history_consistent(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    payload = client.build_payload(FIX, user="dev@example.com")
    results = []
    lock = threading.Lock()

    def push_version(version):
        answer = _push(base, key, _stored(
            payload, "sess-c", steps=version * 3, ended=f"2026-10-15T10:{version:02d}:00Z",
        ))
        with lock:
            results.append(answer)

    threads = [threading.Thread(target=push_version, args=(version,)) for version in range(1, 13)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert len(results) == 12
    assert all(status in (200, 201, 409) for status, _ in results)
    accepted = [body for status, body in results if status in (200, 201)]
    row = _run_row(srv.db.path, "sess-c")
    assert row["ended_at"] == "2026-10-15T10:12:00Z"
    assert row["revision"] == len(accepted)
    assert _revisions(srv.db.path, "sess-c") == list(range(1, len(accepted)))
    assert _http(base, "GET", "/health")[0] == 200


def test_agent_id_round_trips_and_the_agent_filter_keeps_working(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    payload = client.build_payload(FIX, user="dev@example.com")
    droid = _stored(payload, "run-droid", agent="Factory Droid", agent_id="droid")
    claude = _stored(payload, "run-claude", agent="Claude Code", agent_id="claude-code")
    legacy = _stored(payload, "run-legacy")
    legacy.pop("agent_id")
    for body in (droid, claude, legacy):
        assert _push(base, key, body)[0] == 201

    rows = {r["id"]: r for r in _json(_http(base, "GET", "/api/runs", headers=_auth(key))[2])["runs"]}
    assert rows["run-droid"]["agent_id"] == "droid" and rows["run-droid"]["agent"] == "Factory Droid"
    assert rows["run-claude"]["agent_id"] == "claude-code"
    assert rows["run-legacy"]["agent_id"] is None
    run = _json(_http(base, "GET", "/api/runs/run-droid", headers=_auth(key))[2])["run"]
    assert run["agent_id"] == "droid"
    assert [r["id"] for r in _json(_http(base, "GET", "/api/runs?agent=droid", headers=_auth(key))[2])["runs"]] == ["run-droid"]


def test_sort_at_drives_the_list_order_and_the_window(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    payload = client.build_payload(FIX, user="dev@example.com")
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _push(base, key, _stored(payload, "R-a", started=recent, ended=recent))[0] == 201
    assert _push(base, key, _stored(payload, "R-b", started=None, ended=None))[0] == 201
    assert _push(base, key, _stored(payload, "R-c", started=old, ended=old))[0] == 201

    listed = [r["id"] for r in _json(_http(base, "GET", "/api/runs", headers=_auth(key))[2])["runs"]]
    assert listed == ["R-b", "R-a", "R-c"]
    assert _json(_http(base, "GET", "/api/stats?days=30", headers=_auth(key))[2])["totals"]["runs"] == 2
    assert _json(_http(base, "GET", "/api/stats?days=60", headers=_auth(key))[2])["totals"]["runs"] == 3


def _session_copy(tmp_path, session_id):
    """The fixture with a different session id, so it counts as a second run."""
    lines = []
    for line in FIX.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        event["sessionId"] = session_id
        lines.append(json.dumps(event))
    out = tmp_path / f"{session_id}.jsonl"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def test_list_filters_match_user_project_and_min_risk(env, tmp_path):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    client.push(base, key, FIX, user="bob@example.com", project="payments")
    other = _session_copy(tmp_path, "a1b2c3d4-0000-4000-8000-00000000beef")
    client.push(base, key, other, user="ann@example.com", project="web_app")

    def users(query):
        status, _, raw = _http(base, "GET", "/api/runs" + query, headers=_auth(key))
        assert status == 200
        return sorted(r["user"] for r in _json(raw)["runs"])

    assert users("") == ["ann@example.com", "bob@example.com"]
    assert users("?user=BOB") == ["bob@example.com"]          # case-insensitive substring
    assert users("?user=example") == ["ann@example.com", "bob@example.com"]
    assert users("?project=web_app") == ["ann@example.com"]   # "_" is literal, not a wildcard
    assert users("?project=web%25") == []                      # "%" is literal too
    assert users("?min_risk=80") == ["ann@example.com", "bob@example.com"]
    assert users("?min_risk=81") == []


def test_user_names_are_escaped_in_served_data(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    client.push(base, key, FIX, user=XSS, project="<img src=x onerror=alert(1)>")

    status, headers, raw = _http(base, "GET", "/api/runs", headers=_auth(key))
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert b"<script" not in raw and b"<img" not in raw
    assert b"\\u003cscript\\u003e" in raw
    assert _json(raw)["runs"][0]["user"] == XSS  # the value itself round-trips intact

    stats_raw = _http(base, "GET", "/api/stats", headers=_auth(key))[2]
    assert b"<script" not in stats_raw
    assert _json(stats_raw)["by_user"][0]["user"] == XSS

    # The dashboard builds the page with DOM text APIs only, never HTML strings.
    assert "innerHTML" not in DASHBOARD_HTML
    assert "insertAdjacentHTML" not in DASHBOARD_HTML


def test_dashboard_sign_in_sets_http_only_cookie(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    assert _http(base, "GET", "/")[0] == 401

    status, headers, _ = _http(base, "GET", f"/?key={key}")
    assert status == 302 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    token_pair = cookie.split(";")[0]
    assert key not in cookie  # the API key itself is not stored in the cookie

    status, headers, page = _http(base, "GET", "/", headers={"Cookie": token_pair})
    assert status == 200 and b"RunLedger" in page
    csp = headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert nonce.encode() in page  # the one inline script is allowed by nonce only
    assert _http(base, "GET", "/api/stats", headers={"Cookie": token_pair})[0] == 200
    assert _http(base, "GET", "/api/runs", headers={"Cookie": token_pair})[0] == 200

    # A session takes the role of its key (admin here). A write through the cookie needs
    # the CSRF header; without it the request is refused.
    body = client.build_payload(FIX, user="dev@example.com")
    assert _http(base, "POST", "/api/runs", body=body, headers={"Cookie": token_pair})[0] == 403
    assert _http(base, "POST", "/api/runs", body=body,
                 headers={"Cookie": token_pair, "X-Requested-With": "runledger"})[0] == 201
    assert _http(base, "GET", "/?key=rl_wrong")[0] == 401


def test_bad_bodies_are_rejected_with_json_errors(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")

    status, _, raw = _http(base, "POST", "/api/runs", body=b"not json", headers=_auth(key))
    assert status == 400 and _json(raw)["error"]["code"] == "invalid_json"
    assert _http(base, "POST", "/api/runs", body=[1, 2], headers=_auth(key))[0] == 400
    assert _http(base, "POST", "/api/runs", body=b'{"NaN": 1, "x": NaN}', headers=_auth(key))[0] == 400
    status, _, raw = _http(base, "POST", "/api/runs", body={"totals": {}}, headers=_auth(key))
    assert status == 400 and "session_id" in _json(raw)["error"]["message"]

    status, headers, raw = _http(
        base, "POST", "/api/runs", headers={**_auth(key), "Content-Length": str(MAX_BODY_BYTES + 1)}
    )
    assert status == 413
    assert _json(raw)["error"]["code"] == "payload_too_large"
    assert b"Traceback" not in raw


def test_health_needs_no_key(env):
    _, base = env
    status, _, raw = _http(base, "GET", "/health")
    assert status == 200 and _json(raw)["ok"] is True


def test_api_keys_are_stored_only_as_hashes(tmp_path):
    db_path = tmp_path / "keys.db"
    with closing(Database(str(db_path))) as db:
        team_id, key = db.create_team("gamma")
        assert key.startswith("rl_")
        assert db.team_for_key(key) == {"id": team_id, "name": "gamma"}
        assert db.team_for_key(key + "x") is None
        with pytest.raises(ValueError):
            db.create_team("gamma")
    with closing(sqlite3.connect(str(db_path))) as conn:
        stored = repr(conn.execute("SELECT name, api_key_hash FROM teams").fetchall())
    assert key not in stored


def test_cli_team_create_then_push(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "cli.db"
    monkeypatch.delenv("RUNLEDGER_API_KEY", raising=False)
    monkeypatch.delenv("RUNLEDGER_SERVER", raising=False)
    assert main(["team", "create", "demo", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    key = next(line.strip() for line in out.splitlines() if line.strip().startswith("rl_"))

    with running_server(db_path) as (srv, base):
        assert main(["push", str(FIX), "--server", base, "--key", key, "--user", "dev@example.com"]) == 0
        printed = capsys.readouterr().out
        assert SESSION_ID in printed and "high" in printed

        monkeypatch.setenv("RUNLEDGER_SERVER", base)
        monkeypatch.setenv("RUNLEDGER_API_KEY", key)
        assert main(["push", str(FIX), "--user", "dev@example.com"]) == 0  # server and key come from the environment
        capsys.readouterr()

        assert main(["push", str(FIX), "--server", base, "--key", "rl_wrong"]) == 1
        assert "HTTP 401" in capsys.readouterr().err
        assert main(["push", str(FIX), "--server", "http://127.0.0.1:9", "--key", key]) == 1
        assert "Could not reach" in capsys.readouterr().err

        # The environment push is an idempotent retry of the same receipt.
        runs = srv.db.list_runs(1)
        assert len(runs) == 1 and runs[0]["user"] == "dev@example.com"


def test_cli_push_reports_the_revision_it_replaced(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "cli-supersede.db"
    monkeypatch.delenv("RUNLEDGER_API_KEY", raising=False)
    monkeypatch.delenv("RUNLEDGER_SERVER", raising=False)
    assert main(["team", "create", "demo", "--db", str(db_path)]) == 0
    key = next(line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("rl_"))

    native = json.loads(NATIVE.read_text(encoding="utf-8"))
    session = tmp_path / "session.runledger.json"
    session.write_text(json.dumps(native), encoding="utf-8")

    with running_server(db_path) as (srv, base):
        assert main(["push", str(session), "--server", base, "--key", key, "--user", "dev@example.com"]) == 0
        printed = capsys.readouterr().out
        assert "Pushed demo-0001-native" in printed and "Updated" not in printed

        native["steps"].append({
            "tool": "Read",
            "input": {"file_path": "/home/dev/demo-app/src/prices.py"},
            "model": "claude-sonnet-5-5",
            "result_text": "def lookup(sku):",
            "is_error": False,
        })
        session.write_text(json.dumps(native), encoding="utf-8")
        assert main(["push", str(session), "--server", base, "--key", key, "--user", "dev@example.com"]) == 0
        printed = capsys.readouterr().out
        assert "Updated demo-0001-native" in printed and "revision 2" in printed
        run = _json(_http(base, "GET", "/api/runs/demo-0001-native", headers=_auth(key))[2])["run"]
        assert run["revision"] == 2 and run["steps"] == 5
        assert _revisions(db_path, "demo-0001-native") == [1]


def test_cli_reports_bad_database_path_without_traceback(tmp_path, capsys):
    bad = tmp_path / "no" / "such" / "dir" / "rl.db"
    assert main(["team", "create", "demo", "--db", str(bad)]) == 1
    assert "cannot open database" in capsys.readouterr().err
    assert main(["serve", "--db", str(bad), "--port", "0"]) == 1


def test_push_needs_server_and_key(monkeypatch, tmp_path):
    monkeypatch.delenv("RUNLEDGER_API_KEY", raising=False)
    monkeypatch.delenv("RUNLEDGER_SERVER", raising=False)
    with pytest.raises(client.PushError):
        client.push("", "rl_x", FIX)
    with pytest.raises(client.PushError):
        client.push("http://127.0.0.1:9", "", FIX)
    with pytest.raises(client.PushError):
        client.push("http://127.0.0.1:9", "rl_x", tmp_path / "missing.jsonl")
    assert client.default_user()
