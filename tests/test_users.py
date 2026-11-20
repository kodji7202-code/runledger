"""People, membership-bound credentials, offboarding and person audit attribution."""
import csv
import io
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from runledger.cli import main
from runledger.server.auth import InvalidKey, hash_key, identity_from_row
from runledger.server.db import Database, SCHEMA, TIME_FORMAT
from test_auth import OLD_KEY, OLD_SCHEMA, _auth, _cookie_for, _http, _json, running


@pytest.fixture
def db(tmp_path):
    store = Database(str(tmp_path / "users.db"))
    try:
        yield store
    finally:
        store.close()


def personal(db, team, role="admin", email="ana@example.com"):
    user = db.add_member(team, email, role, "Ana")
    key = db.create_key(team, "laptop-ana", role, "cli", user=user["id"])
    return user, key


def test_old_database_migrates_and_reopens_without_changing_credentials(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO teams (name, api_key_hash, created_at) VALUES (?, ?, ?)",
                     ("old", hash_key(OLD_KEY), "2026-01-01T00:00:00Z"))
    store = Database(path)
    old = store.key_for_token(OLD_KEY)
    token = store.issue_session(old["team_id"], old["id"], "admin")
    store.close()
    store = Database(path)
    try:
        assert store.key_for_token(OLD_KEY)["user_id"] is None
        assert store.session_key(token)["role"] == "admin"
        assert store.list_members(old["team_id"]) == []
        assert store.list_keys(old["team_id"])[0]["user"] is None
        assert store._conn.execute("SELECT actor_user_id FROM audit_log").fetchall() == []
    finally:
        store.close()


def test_pre_accounts_keys_sessions_and_audit_migrate(tmp_path):
    path = str(tmp_path / "before-users.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("DROP TABLE sessions")
        conn.execute("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, team_id INTEGER NOT NULL, "
                     "key_id TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)")
        conn.execute("INSERT INTO teams (name, api_key_hash, created_at) VALUES ('old', ?, '2026')", (hash_key(OLD_KEY),))
        conn.execute("INSERT INTO api_keys (id, team_id, label, role, key_hash, created_at) "
                     "VALUES ('key', 1, 'old', 'admin', ?, '2026')", (hash_key(OLD_KEY),))
        conn.execute("INSERT INTO sessions VALUES (?, 1, 'key', 'admin', '2026', '2099-01-01T00:00:00Z')",
                     (hash_key("session"),))
        conn.execute("INSERT INTO audit_log (team_id, at, actor, action) VALUES (1, '2026', 'cli', 'old')")
    store = Database(path)
    try:
        assert store.session_key("session")["user_id"] is None
        assert store.key_for_token(OLD_KEY)["role"] == "admin"
        assert store.list_audit(1)[0]["actor_user_id"] is None
    finally:
        store.close()


@pytest.mark.parametrize("email", [None, "", "ana", "@example.com", "a@", "a@@example.com",
                                  "a@.com", "a@example.", "a\n@example.com", "a @example.com",
                                  "a\x7f@example.com", "a\u0085@example.com", "a\u00a0@example.com"])
def test_invalid_email_does_not_create_user_or_audit(db, email):
    team, _ = db.create_team("team")
    before = db.list_audit(team)
    with pytest.raises(InvalidKey):
        db.add_member(team, email, "member")
    assert db.list_members(team) == []
    assert db.list_audit(team) == before


def test_server_wide_users_and_key_role_validation(db):
    a, _ = db.create_team("a")
    b, _ = db.create_team("b")
    first = db.add_member(a, "ANA@EXAMPLE.COM", "member", "Ana")
    second = db.add_member(b, "ana@example.com", "viewer")
    assert first["id"] == second["id"]
    assert second["name"] == "Ana"
    assert first["email"] == "ana@example.com"
    for user, role in ((first["id"], "admin"), ("missing@example.com", "viewer"), ([], "viewer")):
        with pytest.raises(InvalidKey):
            db.create_key(a, "bad", role, "cli", user=user)
    with pytest.raises(InvalidKey):
        db.add_member(a, "Ana@example.com", "viewer")
    with pytest.raises(InvalidKey):
        db.add_member(a, "other@example.com", "viewer", "Bad\nName")
    assert len(db.list_keys(a)) == 1
    owned = db.create_key(a, "personal", "viewer", "cli", user="ANA@EXAMPLE.COM")
    assert owned["user_id"] == first["id"]
    assert db.list_keys(a)[-1]["user"]["email"] == "ana@example.com"


@pytest.mark.parametrize("action", ["remove", "disable", "lower"])
@pytest.mark.parametrize("via", ["key", "session"])
def test_live_auth_gating_for_keys_and_dashboard(tmp_path, action, via):
    with running(tmp_path / "auth.db") as (srv, base):
        team, service = srv.db.create_team("a")
        user, key = personal(srv.db, team)
        headers = _auth(key["key"]) if via == "key" else {"Cookie": _cookie_for(base, key["key"])}
        me = _json(_http(base, "GET", "/api/me", headers=headers)[2])
        assert me["user"] == {"id": user["id"], "email": user["email"], "name": "Ana"}
        if action == "remove":
            srv.db.change_member(team, user["id"])
        elif action == "disable":
            srv.db.set_user_disabled(user["id"], True)
        else:
            srv.db.change_member(team, user["id"], "viewer")
        status, _, raw = _http(base, "GET", "/api/me", headers=headers)
        if action == "lower":
            assert status == 200 and _json(raw)["role"] == "viewer"
            assert _http(base, "GET", "/api/members", headers=headers)[0] == 403
            assert _http(base, "POST", "/api/keys", body={}, headers=headers)[0] == 403
        else:
            assert status == 401
        assert _http(base, "GET", "/api/me", headers=_auth(service))[0] == 200


@pytest.mark.parametrize("method", ["key", "oidc", "saml"])
@pytest.mark.parametrize("mutation", ["disabled", "removed", "lowered", "scim-deactivated"])
def test_session_management_applies_live_membership_gates(db, method, mutation):
    team, _ = db.create_team("a")
    user, key = personal(db, team)
    token = (db.issue_session(team, key["id"], "admin") if method == "key" else
             db.issue_sso_session(method, team, user["id"], "provider"))
    session, = db.list_sessions(team, None, 0)
    assert db._conn.execute("SELECT user_id FROM sessions").fetchone()[0] == user["id"]
    assert db.session_key(token)["session_id"] == session["id"]
    with db._conn:
        if mutation == "disabled":
            db._conn.execute("UPDATE users SET disabled_at = '2026' WHERE id = ?", (user["id"],))
        elif mutation == "removed":
            db._conn.execute("DELETE FROM memberships WHERE user_id = ?", (user["id"],))
        elif mutation == "scim-deactivated":
            db._conn.execute("UPDATE memberships SET deactivated_at = '2026' WHERE user_id = ?", (user["id"],))
        else:
            db._conn.execute("UPDATE memberships SET role = 'viewer' WHERE user_id = ?", (user["id"],))
    if mutation == "lowered":
        assert db.list_sessions(team, None, 0)[0]["role"] == "viewer"
        assert db.session_key(token)["role"] == "viewer"
        assert db.revoke_session(team, None, session["id"], "test", "auth.session_revoked", 0)
    else:
        assert db.list_sessions(team, None, 0) == []
        assert not db.revoke_session(team, None, session["id"], "test", "auth.session_revoked", 0)
        assert db.session_key(token) is None


@pytest.mark.parametrize("method", ["key", "oidc", "saml"])
@pytest.mark.parametrize("logout", [True, False])
def test_person_session_controls_preserve_scope_and_audit(tmp_path, method, logout):
    with running(tmp_path / "sessions.db") as (srv, base):
        team, admin = srv.db.create_team("a")
        user, key = personal(srv.db, team, role="viewer")
        other, _ = personal(srv.db, team, role="viewer", email="other@example.com")
        foreign_token = srv.db.issue_sso_session("oidc", team, other["id"], "provider")
        foreign_id = srv.db.session_key(foreign_token)["session_id"]
        cookie = (_cookie_for(base, key["key"]) if method == "key" else
                  "rl_session=" + srv.db.issue_sso_session(method, team, user["id"], "provider"))
        headers = {"Cookie": cookie, "X-Requested-With": "runledger"}
        session, = _json(_http(base, "GET", "/sessions", headers=headers)[2])["sessions"]
        assert session["current"]
        assert _http(base, "DELETE", "/sessions/" + foreign_id, headers=headers)[0] == 404
        assert len(_json(_http(base, "GET", "/sessions", headers=_auth(admin))[2])["sessions"]) == 2
        verb, path = ("POST", "/session/logout") if logout else ("DELETE", "/sessions/" + session["id"])
        assert _http(base, verb, path, headers=headers)[0] == 204
        event = srv.db.list_audit(team)[0]
        assert event["action"] == ("auth.sign_out" if logout else "auth.session_revoked")
        assert event["actor_user_id"] == user["id"]
        assert user["email"] in event["actor"]
        assert _http(base, "GET", "/sessions", headers=headers)[0] == 401
        assert srv.db.session_key(foreign_token)


@pytest.mark.parametrize("method", ["key", "oidc", "saml"])
def test_scim_deactivation_only_deletes_sessions_in_its_team(db, method):
    a, _ = db.create_team("a")
    b, _ = db.create_team("b")
    user, ka = personal(db, a)
    db.add_member(b, user["email"], "member")
    kb = db.create_key(b, "b", "member", "cli", user=user["id"])
    def issue(team, key):
        return (db.issue_session(team, key["id"], key["role"]) if method == "key" else
                db.issue_sso_session(method, team, user["id"], "provider"))
    ta, tb = issue(a, ka), issue(b, kb)
    db.scim_update(a, user["id"], {"active": False})
    assert db.list_sessions(a, None, 0) == []
    assert db.session_key(ta) is None
    assert db.session_key(tb)["user_id"] == user["id"]
    assert len(db.list_sessions(b, None, 0)) == 1
    db.scim_update(a, user["id"], {"active": True})
    assert db.session_key(ta) is None
    assert db.key_for_token(ka["key"])


def test_disable_is_server_wide_and_enable_does_not_restore_sessions(db):
    a, _ = db.create_team("a")
    b, _ = db.create_team("b")
    user, ka = personal(db, a)
    db.add_member(b, user["email"], "member")
    kb = db.create_key(b, "b", "member", "cli", user=user["email"])
    tokens = [db.issue_session(team, key["id"], key["role"]) for team, key in ((a, ka), (b, kb))]
    assert db._conn.execute("SELECT DISTINCT user_id FROM sessions").fetchone()[0] == user["id"]
    db.set_user_disabled(user["email"], True)
    for key in (ka, kb):
        assert db.key_for_token(key["key"]) is None
    assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    db.set_user_disabled(user["email"], False)
    for key in (ka, kb):
        assert db.key_for_token(key["key"])
    assert all(db.session_key(t) is None for t in tokens)
    db.change_member(a, user["id"])
    db.add_member(a, user["email"], "admin")
    assert db.key_for_token(ka["key"]) is None
    assert db.key_for_token(kb["key"])
    for team in (a, b):
        assert {e["action"] for e in db.list_audit(team)} >= {"user.disable", "user.enable"}
        for action in ("user.disable", "user.enable"):
            event = next(e for e in db.list_audit(team) if e["action"] == action)
            assert event["actor"] == "cli" and event["actor_user_id"] is None
            assert event["target"] == user["email"]
            expected = {"user_id": user["id"], "email": user["email"]}
            if action == "user.disable":
                expected["sessions_deleted"] = 1
            assert event["details"] == expected


@pytest.mark.parametrize("mutation", ["DELETE FROM memberships WHERE user_id = ?",
                                     "UPDATE users SET disabled_at = '2026' WHERE id = ?"])
def test_membership_and_disabled_gates_do_not_depend_on_revocation_or_session_deletion(db, mutation):
    team, _ = db.create_team("a")
    user, key = personal(db, team)
    token = db.issue_session(team, key["id"], "admin")
    with db._conn:
        db._conn.execute(mutation, (user["id"],))
    assert db._conn.execute("SELECT revoked_at FROM api_keys WHERE id = ?", (key["id"],)).fetchone()[0] is None
    assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert db.key_for_token(key["key"]) is None
    assert db.active_key(key["id"]) is None
    assert db.session_key(token) is None


def test_disable_refusal_in_one_team_rolls_back_everywhere(db):
    a, _ = db.create_team("a")
    b, service = db.create_team("b")
    user, ka = personal(db, a)
    db.add_member(b, user["email"], "admin")
    kb = db.create_key(b, "b", "admin", "cli", user=user["id"])
    db.revoke_key(b, db.key_for_token(service)["id"], "cli")
    tokens = [db.issue_session(team, key["id"], "admin") for team, key in ((a, ka), (b, kb))]
    before = [db.list_audit(team) for team in (a, b)]
    with pytest.raises(InvalidKey, match="last working admin"):
        db.set_user_disabled(user["email"], True)
    assert db.get_user(user["id"])["disabled_at"] is None
    assert [db.list_audit(team) for team in (a, b)] == before
    assert all(db.session_key(token) for token in tokens)


@pytest.mark.parametrize("action", ["remove", "lower", "disable", "revoke"])
def test_last_working_admin_is_protected_and_failure_is_atomic(db, action):
    team, service = db.create_team("a")
    user, key = personal(db, team)
    assert db.revoke_key(team, db.key_for_token(service)["id"], "cli")[0] == "ok"
    token = db.issue_session(team, key["id"], "admin")
    before = db.list_audit(team)
    if action == "revoke":
        assert db.revoke_key(team, key["id"], "cli")[0] == "last_admin"
    else:
        with pytest.raises(InvalidKey, match="last working admin"):
            if action == "disable":
                db.set_user_disabled(user["id"], True)
            else:
                db.change_member(team, user["id"], "member" if action == "lower" else None)
    assert db.list_audit(team) == before
    assert db.key_for_token(key["key"])["role"] == "admin"
    assert db.session_key(token)


def test_capped_or_disabled_admin_key_cannot_satisfy_last_admin(db):
    team, service = db.create_team("a")
    user, key = personal(db, team)
    db.change_member(team, user["id"], "member")
    service_id = db.key_for_token(service)["id"]
    assert db.revoke_key(team, service_id, "cli")[0] == "last_admin"
    db.change_member(team, user["id"], "admin")
    db.set_user_disabled(user["id"], True)
    assert db.revoke_key(team, service_id, "cli")[0] == "last_admin"


def test_person_audit_attribution_and_service_actors(db):
    team, service = db.create_team("a")
    user, key = personal(db, team)
    ident = identity_from_row(db.key_for_token(key["key"]), "key")
    assert ident.actor() == f"ana@example.com via laptop-ana ({key['prefix']})"
    db.add_member(team, "bob@example.com", "member", actor=ident)
    db.change_member(team, "bob@example.com", "viewer", actor=ident)
    bob = db.get_user("bob@example.com")
    db.change_member(team, "bob@example.com", actor=ident)
    for event in db.list_audit(team)[:3]:
        assert event["actor_user_id"] == user["id"]
        assert event["actor"] == ident.actor()
        assert event["target"] == "bob@example.com"
        assert event["details"]["user_id"] == bob["id"]
        assert event["details"]["email"] == "bob@example.com"
    service_ident = identity_from_row(db.key_for_token(service), "session")
    assert service_ident.actor() == "dashboard:initial"
    db.record_audit(team, service_ident, "service")
    assert db.list_audit(team)[0]["actor_user_id"] is None


def test_member_api_crud_csrf_ownership_and_audit(tmp_path):
    with running(tmp_path / "api.db") as (srv, base):
        team, admin = srv.db.create_team("a")
        assert _json(_http(base, "GET", "/api/members", headers=_auth(admin))[2]) == {"members": []}
        status, _, raw = _http(base, "POST", "/api/members", body={"email": "ANA@example.com", "name": "Ana", "role": "admin"}, headers=_auth(admin))
        assert status == 201
        user = _json(raw)
        status, _, raw = _http(base, "POST", "/api/keys", body={"label": "ana", "role": "admin", "user": user["email"]}, headers=_auth(admin))
        assert status == 201
        key = _json(raw)
        cookie = {"Cookie": _cookie_for(base, key["key"])}
        members = _json(_http(base, "GET", "/api/members", headers=cookie)[2])["members"]
        assert len(members) == 1 and members[0]["id"] == user["id"]
        for method, path, body in (("POST", "/api/members", {"email": "bob@example.com", "role": "member"}),
                                   ("PATCH", "/api/members/" + user["id"], {"role": "member"}),
                                   ("DELETE", "/api/members/" + user["id"], None)):
            assert _http(base, method, path, body=body, headers=cookie)[0] == 403
        csrf = dict(cookie, **{"X-Requested-With": "runledger"})
        status, _, raw = _http(base, "POST", "/api/members", body={"email": "bob@example.com", "role": "member"}, headers=csrf)
        assert status == 201
        bob = _json(raw)
        for body in ({"email": "bad", "role": "member"}, {"email": bob["email"], "role": "member"}, {"email": "c@example.com", "role": "owner"}):
            assert _http(base, "POST", "/api/members", body=body, headers=csrf)[0] == 400
        assert _http(base, "PATCH", "/api/members/" + bob["id"], body={}, headers=csrf)[0] == 400
        assert _http(base, "PATCH", "/api/members/missing", body={"role": "viewer"}, headers=csrf)[0] == 400
        assert _http(base, "PATCH", "/api/members/" + bob["id"], body={"role": "viewer"}, headers=csrf)[0] == 200
        assert _http(base, "DELETE", "/api/members/" + bob["id"], headers=csrf)[0] == 200
        events = _json(_http(base, "GET", "/api/audit", headers=csrf)[2])["events"]
        person_events = [e for e in events if e["actor_user_id"] == user["id"]]
        assert {e["action"] for e in person_events} >= {"auth.sign_in", "member.add", "member.role", "member.remove"}
        status, _, raw = _http(base, "GET", "/api/export/audit.csv", headers=csrf)
        assert status == 200
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        assert any(r["actor_user_id"] == user["id"] for r in rows)


@pytest.mark.parametrize("role", ["viewer", "member"])
def test_member_api_requires_admin(tmp_path, role):
    with running(tmp_path / "roles.db") as (srv, base):
        team, _ = srv.db.create_team("a")
        user, key = personal(srv.db, team, role)
        for headers in (_auth(key["key"]), {"Cookie": _cookie_for(base, key["key"]), "X-Requested-With": "runledger"}):
            for method, path in (("GET", "/api/members"), ("POST", "/api/members"),
                                 ("PATCH", "/api/members/" + user["id"]), ("DELETE", "/api/members/" + user["id"])):
                assert _http(base, method, path, body={} if method in ("POST", "PATCH") else None, headers=headers)[0] == 403
        assert _http(base, "GET", "/api/members")[0] == 401


def test_cli_user_lifecycle_and_personal_key(tmp_path, capsys):
    path = str(tmp_path / "cli.db")
    store = Database(path)
    team, _ = store.create_team("a")
    store.close()
    common = ["--db", path]
    scoped = common + ["--team-id", str(team)]
    email = ["--email", "ana@example.com"]
    assert main(["user", "list"] + scoped) == 0
    assert main(["user", "add"] + scoped + email + ["--name", "Ana", "--role", "member"]) == 0
    assert main(["user", "list"] + scoped) == 0
    assert "ana@example.com  Ana  member  active" in capsys.readouterr().out
    assert main(["key", "create"] + scoped + ["--label", "ana", "--role", "member", "--user", "ana@example.com"]) == 0
    assert "rl_" in capsys.readouterr().out
    assert main(["user", "role"] + scoped + email + ["--role", "viewer"]) == 0
    assert main(["user", "disable"] + common + email) == 0
    assert main(["user", "enable"] + common + email) == 0
    assert main(["user", "remove"] + scoped + email) == 0
    assert main(["user", "remove"] + scoped + email) == 1
    assert main(["user", "disable"] + common + ["--email", "missing@example.com"]) == 1
    assert main(["key", "create"] + scoped + ["--label", "bad", "--role", "member", "--user", "ana@example.com"]) == 1


def test_person_cannot_own_a_service_key(db):
    team, _ = db.create_team("a")
    user = db.add_member(team, "ana@example.com", "member")
    before = db.list_audit(team)
    for owner in (user["id"], user["email"]):
        with pytest.raises(InvalidKey, match="service key cannot be owned"):
            db.create_key(team, "bad", "member", "cli", kind="service", user=owner)
    assert len(db.list_keys(team)) == 1
    assert db.list_audit(team) == before


def test_owned_keys_keep_expiry_rotation_and_person_audit(tmp_path):
    with running(tmp_path / "owned-expiry.db") as (srv, base):
        team, admin = srv.db.create_team("a")
        user = srv.db.add_member(team, "ana@example.com", "admin")
        status, _, raw = _http(base, "POST", "/api/keys", body={"label": "ana", "role": "admin",
                               "user": user["email"], "kind": "personal", "expires_in_days": 1}, headers=_auth(admin))
        assert status == 201
        owned = _json(raw)
        cookie = {"Cookie": _cookie_for(base, owned["key"])}
        me = _json(_http(base, "GET", "/api/me", headers=cookie)[2])
        assert me["user"]["id"] == user["id"]
        assert me["key"]["expires_soon"] and me["key"]["kind"] == "personal"
        status, _, _ = _http(base, "POST", "/api/keys", body={"label": "bad", "role": "member",
                             "user": user["id"], "kind": "service"}, headers=_auth(admin))
        assert status == 400
        target = srv.db.create_key(team, "target", "member", "cli")
        status, _, raw = _http(base, "POST", "/api/keys/" + target["id"] + "/rotate",
                               body={"expires_in_days": 3}, headers=_auth(owned["key"]))
        assert status == 200 and _json(raw)["expires_at"]
        event = srv.db.list_audit(team)[0]
        assert event["action"] == "key.rotate" and event["actor_user_id"] == user["id"]
        srv.db.keys_expiring()
        warning = next(e for e in srv.db.list_audit(team) if e["action"] == "key.expiring")
        assert warning["actor"] == "system" and warning["actor_user_id"] is None
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(TIME_FORMAT)
        with srv.db._lock, srv.db._conn:
            srv.db._conn.execute("UPDATE api_keys SET expires_at = ? WHERE id = ?", (past, owned["id"]))
        assert _http(base, "GET", "/api/me", headers=cookie)[0] == 401
        status, _, raw = _http(base, "GET", "/api/me", headers=_auth(owned["key"]))
        assert status == 401 and _json(raw)["error"]["code"] == "key_expired"


def test_expired_key_does_not_satisfy_membership_last_admin_safety(db):
    team, service = db.create_team("a")
    user, owned = personal(db, team)
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(TIME_FORMAT)
    with db._conn:
        db._conn.execute("UPDATE api_keys SET expires_at = ? WHERE key_hash = ?", (past, hash_key(service)))
    for operation in (lambda: db.change_member(team, user["id"]),
                      lambda: db.change_member(team, user["id"], "member"),
                      lambda: db.set_user_disabled(user["id"], True)):
        with pytest.raises(InvalidKey, match="last working admin"):
            operation()
    assert db.revoke_key(team, owned["id"], "cli")[0] == "last_admin"


def test_non_ascii_uppercase_email_lookup_matches_stored_user(db):
    team, _ = db.create_team("unicode")
    db.add_member(team, "Ärni@Example.com", "member")
    assert db.get_user("ÄRNI@example.com")["email"] == "ärni@example.com"
    view = db.create_key(team, "laptop", "member", actor="cli", user="Ärni@Example.com")
    assert view["user"]["email"] == "ärni@example.com"
