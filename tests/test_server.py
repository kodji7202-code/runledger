"""Tests for the RunLedger team server, the push client and the team CLI.

Each test runs a real server on a free port in a background thread, with a
temporary SQLite database.
"""
import http.client
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from runledger import client
from runledger.cli import main
from runledger.server.app import MAX_BODY_BYTES, make_server
from runledger.server.dashboard import DASHBOARD_HTML
from runledger.server.db import Database

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
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


def test_repushing_a_session_updates_it_instead_of_duplicating(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    client.push(base, key, FIX, user="first@example.com")
    client.push(base, key, FIX, user="second@example.com")
    runs = _json(_http(base, "GET", "/api/runs", headers=_auth(key))[2])["runs"]
    assert len(runs) == 1 and runs[0]["user"] == "second@example.com"


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
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    token_pair = cookie.split(";")[0]
    assert key not in cookie  # the API key itself is not stored in the cookie

    status, headers, page = _http(base, "GET", "/", headers={"Cookie": token_pair})
    assert status == 200 and b"RunLedger" in page
    csp = headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert nonce.encode() in page  # the one inline script is allowed by nonce only
    assert _http(base, "GET", "/api/stats", headers={"Cookie": token_pair})[0] == 200
    assert _http(base, "GET", "/api/runs", headers={"Cookie": token_pair})[0] == 200

    # Cookies are for reading only; pushing needs the API key.
    body = client.build_payload(FIX, user="dev@example.com")
    assert _http(base, "POST", "/api/runs", body=body, headers={"Cookie": token_pair})[0] == 401
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
        assert main(["push", str(FIX)]) == 0  # server and key come from the environment
        capsys.readouterr()

        assert main(["push", str(FIX), "--server", base, "--key", "rl_wrong"]) == 1
        assert "HTTP 401" in capsys.readouterr().err
        assert main(["push", str(FIX), "--server", "http://127.0.0.1:9", "--key", key]) == 1
        assert "Could not reach" in capsys.readouterr().err

        # The environment push had no --user, so the developer name fell back to
        # git user.email (or the OS user) and re-pushed the same session.
        runs = srv.db.list_runs(1)
        assert len(runs) == 1 and runs[0]["user"] == client.default_user()


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
