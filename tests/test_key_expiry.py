"""API key kinds (personal, service), expiry, the team's key lifetime caps and expiry warnings."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from runledger.cli import main
from runledger.server import approvals, maintenance
from runledger.server.db import TIME_FORMAT, Database

from test_auth import CSRF, KEY_RE, _audit, _auth, _cookie_for, _http, _json, env, running  # noqa: F401


def _stamp(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime(TIME_FORMAT)


def _set_expiry(db_path, key_id, stamp):
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("UPDATE api_keys SET expires_at = ? WHERE id = ?", (stamp, key_id))
        conn.commit()


def _new_key(base, admin, **body):
    body = {"label": "ci", "role": "member", **body}
    return _http(base, "POST", "/api/keys", body=body, headers=_auth(admin))


def _settings(base, admin, **changes):
    return _http(base, "PUT", "/api/team/settings", body=changes, headers=_auth(admin))


def _days_left(expires_at):
    end = datetime.strptime(expires_at, TIME_FORMAT).replace(tzinfo=timezone.utc)
    return (end - datetime.now(timezone.utc)).total_seconds() / 86400


def test_keys_default_to_personal_and_never_expire_without_a_policy(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    status, _, raw = _new_key(base, admin)
    view = _json(raw)
    assert status == 201 and view["kind"] == "personal" and view["expires_at"] is None
    me = _json(_http(base, "GET", "/api/me", headers=_auth(view["key"]))[2])
    assert me["key"]["kind"] == "personal" and me["key"]["expires_at"] is None


def test_a_key_can_ask_for_a_lifetime_and_a_service_kind(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    view = _json(_new_key(base, admin, kind="service", expires_in_days=30)[2])
    assert view["kind"] == "service"
    assert 29.9 < _days_left(view["expires_at"]) <= 30
    listed = {k["id"]: k for k in _json(_http(base, "GET", "/api/keys", headers=_auth(admin))[2])["keys"]}
    assert listed[view["id"]]["kind"] == "service"
    assert listed[view["id"]]["expires_at"] == view["expires_at"]
    event = _audit(base, admin, "?action=key.create")[0]
    assert event["details"]["kind"] == "service" and event["details"]["expires_at"] == view["expires_at"]


@pytest.mark.parametrize("body", [
    {"kind": "robot"}, {"kind": 1}, {"expires_in_days": 0}, {"expires_in_days": -3},
    {"expires_in_days": 3651}, {"expires_in_days": "30"}, {"expires_in_days": 1.5}, {"expires_in_days": True},
])
def test_bad_kinds_and_lifetimes_are_refused(env, body):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    status, _, raw = _new_key(base, admin, **body)
    assert status == 400 and _json(raw)["error"]["code"] == "invalid_key"
    assert len(srv.db.list_keys(1)) == 1


def test_team_caps_set_the_default_lifetime_and_refuse_longer_ones(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    status, _, raw = _settings(base, admin, key_max_ttl_days=90, service_key_max_ttl_days=365)
    assert status == 200, raw
    settings = _json(raw)
    assert settings["key_max_ttl_days"] == 90 and settings["service_key_max_ttl_days"] == 365
    assert settings["key_expiry_warn_days"] == 14

    personal = _json(_new_key(base, admin)[2])
    assert 89.9 < _days_left(personal["expires_at"]) <= 90
    service = _json(_new_key(base, admin, kind="service")[2])
    assert 364.9 < _days_left(service["expires_at"]) <= 365
    status, _, raw = _new_key(base, admin, expires_in_days=91)
    assert status == 400 and "at most 90 days" in _json(raw)["error"]["message"]
    assert _new_key(base, admin, kind="service", expires_in_days=200)[0] == 201


def test_a_new_cap_shortens_existing_keys_of_that_kind_without_expiring_them(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    service = _json(_new_key(base, admin, kind="service")[2])
    short = _json(_new_key(base, admin, expires_in_days=5)[2])
    assert _settings(base, admin, key_max_ttl_days=30)[0] == 200
    keys = {k["id"]: k for k in srv.db.list_keys(1)}
    assert keys[service["id"]]["expires_at"] is None  # the service cap is separate
    assert keys[short["id"]]["expires_at"] == short["expires_at"]  # already inside the cap
    initial = next(k for k in keys.values() if k["label"] == "initial")
    assert 29.9 < _days_left(initial["expires_at"]) <= 30
    assert _http(base, "GET", "/api/me", headers=_auth(admin))[0] == 200
    event = _audit(base, admin, "?action=team.settings")[0]
    assert event["details"]["key_max_ttl_days"] == 30 and event["details"]["keys_shortened"] == 1


@pytest.mark.parametrize("changes", [
    {"key_max_ttl_days": 0}, {"key_max_ttl_days": 3651}, {"service_key_max_ttl_days": "90"},
    {"key_expiry_warn_days": -1}, {"key_expiry_warn_days": 366}, {"key_expiry_warn_days": None},
])
def test_bad_key_policy_settings_are_refused(env, changes):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    status, _, raw = _settings(base, admin, **changes)
    assert status == 400 and _json(raw)["error"]["code"] == "invalid_settings"


def test_a_cap_can_be_removed_and_only_admins_change_the_policy(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    member = _json(_new_key(base, admin)[2])["key"]
    assert _settings(base, member, key_max_ttl_days=30)[0] == 403
    assert _settings(base, admin, key_max_ttl_days=30)[0] == 200
    assert _json(_settings(base, admin, key_max_ttl_days=None)[2])["key_max_ttl_days"] is None
    assert _json(_new_key(base, admin)[2])["expires_at"] is None


def test_an_expired_key_is_refused_with_key_expired_and_not_counted_as_a_guess(tmp_path):
    with running(tmp_path / "rl.db") as (srv, base):
        _, admin = srv.db.create_team("alpha")
        view = _json(_new_key(base, admin, expires_in_days=10)[2])
        _set_expiry(tmp_path / "rl.db", view["id"], _stamp(seconds=-1))
        for _ in range(30):  # more than the failure limit
            status, _, raw = _http(base, "GET", "/api/me", headers=_auth(view["key"]))
            assert status == 401 and _json(raw)["error"]["code"] == "key_expired"
        assert _http(base, "GET", "/api/me", headers=_auth(admin))[0] == 200
        listed = {k["id"]: k for k in srv.db.list_keys(1)}
        assert listed[view["id"]]["revoked_at"] is None
        status, _, raw = _http(base, "GET", "/api/me", headers=_auth("rl_" + "Z" * 40))
        assert _json(raw)["error"]["code"] != "key_expired"


def test_rotation_renews_an_expired_key_and_clears_its_warning(tmp_path):
    db_path = tmp_path / "rl.db"
    with running(db_path) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        view = _json(_new_key(base, admin, expires_in_days=20)[2])
        _set_expiry(db_path, view["id"], _stamp(days=1))
        assert len(srv.db.keys_expiring()) == 1
        _set_expiry(db_path, view["id"], _stamp(seconds=-1))
        status, _, raw = _http(base, "POST", f"/api/keys/{view['id']}/rotate", headers=_auth(admin))
        assert status == 200, raw
        rotated = _json(raw)
        assert 19.9 < _days_left(rotated["expires_at"]) <= 20
        assert _http(base, "GET", "/api/me", headers=_auth(rotated["key"]))[0] == 200
        assert _http(base, "GET", "/api/me", headers=_auth(view["key"]))[0] == 401
        _set_expiry(db_path, view["id"], _stamp(days=1))
        assert [k["id"] for k in srv.db.keys_expiring()] == [view["id"]]  # warned again after rotation


def test_rotation_takes_a_new_lifetime_held_to_the_cap(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    view = _json(_new_key(base, admin, expires_in_days=200)[2])
    assert _settings(base, admin, key_max_ttl_days=60)[0] == 200
    path = f"/api/keys/{view['id']}/rotate"
    status, _, raw = _http(base, "POST", path, body={"expires_in_days": 61}, headers=_auth(admin))
    assert status == 400 and _json(raw)["error"]["code"] == "invalid_key"
    assert 59.9 < _days_left(_json(_http(base, "POST", path, headers=_auth(admin))[2])["expires_at"]) <= 60
    rotated = _json(_http(base, "POST", path, body={"expires_in_days": 7}, headers=_auth(admin))[2])
    assert 6.9 < _days_left(rotated["expires_at"]) <= 7


def test_a_key_inside_the_warning_window_says_so(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    soon = _json(_new_key(base, admin, expires_in_days=10)[2])
    later = _json(_new_key(base, admin, expires_in_days=60)[2])
    status, headers, raw = _http(base, "GET", "/api/me", headers=_auth(soon["key"]))
    assert headers["x-runledger-key-expires"] == soon["expires_at"]
    assert _json(raw)["key"]["expires_soon"] is True
    status, headers, raw = _http(base, "GET", "/api/me", headers=_auth(later["key"]))
    assert "x-runledger-key-expires" not in headers and _json(raw)["key"]["expires_soon"] is False
    assert _settings(base, admin, key_expiry_warn_days=0)[0] == 200
    assert "x-runledger-key-expires" not in _http(base, "GET", "/api/me", headers=_auth(soon["key"]))[1]


def test_a_dashboard_session_never_outlives_its_key(tmp_path):
    db_path = tmp_path / "rl.db"
    with running(db_path) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        view = _json(_new_key(base, admin, expires_in_days=10)[2])
        end = _stamp(hours=1)
        _set_expiry(db_path, view["id"], end)
        cookie = _cookie_for(base, view["key"])
        with closing(sqlite3.connect(str(db_path))) as conn:
            (session_end,) = conn.execute("SELECT expires_at FROM sessions WHERE key_id = ?", (view["id"],)).fetchone()
        assert session_end == end
        _set_expiry(db_path, view["id"], _stamp(seconds=-1))
        assert _http(base, "GET", "/api/me", headers={"Cookie": cookie})[0] == 401


def test_sessions_with_an_expired_key_are_not_listed_or_revocable(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    member = _json(_new_key(base, admin)[2])
    cookie = {"Cookie": _cookie_for(base, member["key"])}
    status, _, raw = _http(base, "GET", "/sessions", headers=_auth(admin))
    assert status == 200
    session, = _json(raw)["sessions"]
    assert session["key_id"] == member["id"]
    _set_expiry(srv.db.path, member["id"], _stamp(seconds=-1))
    status, _, raw = _http(base, "GET", "/sessions", headers=_auth(admin))
    assert status == 200 and _json(raw)["sessions"] == []
    assert _http(base, "DELETE", f"/sessions/{session['id']}", headers=_auth(admin))[0] == 404
    assert _http(base, "GET", "/api/me", headers=cookie)[0] == 401


def test_service_keys_cannot_sign_in_to_the_dashboard(env):
    srv, base = env
    _, admin = srv.db.create_team("alpha")
    service = _json(_new_key(base, admin, kind="service")[2])
    status, headers, raw = _http(base, "GET", f"/?key={service['key']}")
    assert status == 401 and "set-cookie" not in headers and b"Service keys" in raw
    form = f"key={service['key']}".encode()
    status, headers, raw = _http(base, "POST", "/session", body=form, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Origin": base})
    assert status == 401 and "set-cookie" not in headers
    assert _http(base, "GET", "/api/me", headers=_auth(service["key"]))[0] == 200


def test_the_last_usable_admin_is_protected_even_when_another_admin_expired(tmp_path):
    db_path = tmp_path / "rl.db"
    with running(db_path) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        other = _json(_new_key(base, admin, role="admin", expires_in_days=5)[2])
        _set_expiry(db_path, other["id"], _stamp(seconds=-1))
        me = _json(_http(base, "GET", "/api/me", headers=_auth(admin))[2])["key"]["id"]
        status, _, raw = _http(base, "POST", f"/api/keys/{me}/revoke", headers=_auth(admin))
        assert status == 409 and _json(raw)["error"]["code"] == "last_admin"


def test_expiry_warnings_go_out_once_without_the_secret(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "rl.db"))
    try:
        team_id, _ = db.create_team("alpha")
        db.update_approval_settings(team_id, {"webhook_url": "https://hooks.example.com/x",
                                              "slack_webhook_url": "https://hooks.slack.com/services/x"})
        soon = db.create_key(team_id, "ci", "member", "test", kind="service", expires_in_days=3)
        db.create_key(team_id, "later", "member", "test", expires_in_days=100)
        sent = []
        monkeypatch.setattr(approvals, "_deliver", lambda jobs: sent.extend(jobs))
        monkeypatch.setattr(approvals.threading, "Thread",
                            lambda target, args, **kw: type("T", (), {"start": lambda self: target(*args)})())
        assert maintenance.warn_expiring_keys(db) == 1
        assert maintenance.warn_expiring_keys(db) == 0
        assert [label for label, _, _ in sent] == ["Slack", "webhook"]
        payload = sent[1][2]
        assert payload["type"] == "key_expiring" and payload["key_id"] == soon["id"]
        assert payload["kind"] == "service" and payload["expires_at"] == soon["expires_at"]
        assert soon["key"] not in json.dumps([job[2] for job in sent])
        events = db.list_audit(team_id, action="key.expiring")
        assert len(events) == 1 and events[0]["target"] == soon["id"]
    finally:
        db.close()


def test_a_database_from_before_expiry_keeps_its_keys_personal_and_unexpiring(tmp_path):
    path = tmp_path / "rl.db"
    db = Database(str(path))
    team_id, key = db.create_team("alpha")
    db.close()
    with closing(sqlite3.connect(str(path))) as conn:
        for column in ("kind", "expires_at", "ttl_days", "expiry_warned_at"):
            conn.execute(f"ALTER TABLE api_keys DROP COLUMN {column}")
        for column in ("key_max_ttl_days", "service_key_max_ttl_days", "key_expiry_warn_days"):
            conn.execute(f"ALTER TABLE teams DROP COLUMN {column}")
        conn.commit()
    db = Database(str(path))
    try:
        row = db.key_for_token(key)
        assert row is not None and row["kind"] == "personal" and row["expires_at"] is None
        assert db.approval_settings(team_id)["key_expiry_warn_days"] == 14
    finally:
        db.close()


def test_cli_creates_rotates_and_lists_keys_with_kind_and_expiry(tmp_path, capsys):
    db_path = str(tmp_path / "rl.db")
    assert main(["team", "create", "alpha", "--db", db_path]) == 0
    capsys.readouterr()
    assert main(["key", "create", "--team-id", "1", "--label", "ci", "--role", "member",
                 "--kind", "service", "--expires-in-days", "30", "--db", db_path]) == 0
    out = capsys.readouterr().out
    assert "Created service member key 'ci'" in out and "Expires: 20" in out
    key_id = out.split("(id ")[1].split(")")[0]
    assert main(["key", "rotate", key_id, "--expires-in-days", "5", "--db", db_path]) == 0
    assert "Expires: 20" in capsys.readouterr().out
    assert main(["key", "list", "--team-id", "1", "--db", db_path]) == 0
    listing = capsys.readouterr().out
    assert "service" in listing and "expires" in listing
    assert KEY_RE.search(listing) is None


def test_an_expired_key_at_sign_in_says_expired_and_is_not_counted(tmp_path):
    db_path = tmp_path / "rl.db"
    with running(db_path) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        view = _json(_new_key(base, admin, expires_in_days=10)[2])
        _set_expiry(db_path, view["id"], _stamp(seconds=-1))
        for _ in range(25):  # more than the failure limit
            status, _, raw = _http(base, "GET", f"/?key={view['key']}")
            assert status == 401 and b"expired" in raw
        assert _http(base, "GET", "/api/me", headers=_auth(admin))[0] == 200


def test_an_expired_bearer_key_does_not_hide_a_valid_session(tmp_path):
    db_path = tmp_path / "rl.db"
    with running(db_path) as (srv, base):
        _, admin = srv.db.create_team("alpha")
        view = _json(_new_key(base, admin, expires_in_days=10)[2])
        cookie = _cookie_for(base, admin)
        _set_expiry(db_path, view["id"], _stamp(seconds=-1))
        headers = {**_auth(view["key"]), "Cookie": cookie}
        assert _http(base, "GET", "/api/me", headers=headers)[0] == 200


def test_a_warning_is_not_recorded_for_a_lifetime_that_changed_after_it_was_read(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "rl.db"))
    try:
        team_id, _ = db.create_team("alpha")
        view = db.create_key(team_id, "ci", "member", "test", expires_in_days=3)
        real = db._conn.execute

        def rotate_between(sql, *args):
            result = real(sql, *args)
            if sql.startswith("SELECT k.id, k.team_id") and "expiry_warned_at IS NULL" in sql:
                real("UPDATE api_keys SET expires_at = ?, expiry_warned_at = NULL WHERE id = ?",
                     (_stamp(days=30), view["id"]))
            return result

        monkeypatch.setattr(db, "_conn", type("C", (), {
            "execute": staticmethod(rotate_between), "__enter__": lambda s: db.__dict__["_real"].__enter__(),
            "__exit__": lambda s, *a: db.__dict__["_real"].__exit__(*a)})())
        db.__dict__["_real"] = real.__self__
        assert db.keys_expiring() == []
        assert db.list_audit(team_id, action="key.expiring") == []
    finally:
        monkeypatch.undo()
        db.close()
