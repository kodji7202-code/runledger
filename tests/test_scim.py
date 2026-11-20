"""IdP-style provisioning against the real HTTP server."""
from urllib.parse import quote

import pytest

from runledger.cli import main
from test_auth import _auth, _cookie_for, _http, _json, running


@pytest.fixture
def scim(tmp_path):
    with running(tmp_path / "scim.db") as (srv, base):
        team, key = srv.db.create_team("a")
        status, _, raw = _http(base, "POST", "/api/scim/token", headers=_auth(key))
        assert status == 201
        token = _json(raw)["token"]

        def call(method, path="/Users", body=None, headers=None):
            status, hdrs, raw = _http(base, method, "/scim/v2" + path, body=body, headers=headers or _auth(token))
            assert hdrs["content-type"].startswith("application/scim+json")
            return status, _json(raw) if raw else None

        yield srv.db, team, key, token, base, call


def test_idp_lifecycle(scim):
    db, team, key, token, base, call = scim
    assert call("GET")[1]["Resources"] == []
    status, user = call("POST", body={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                                     "userName": "Ana@example.com", "name": {"formatted": "Ana"},
                                     "active": True, "externalId": "okta-123"})
    assert status == 201
    uid = user["id"]
    path = "/Users/" + uid
    personal = db.create_key(team, "personal", "member", "cli", user=uid)
    cookie = _cookie_for(base, personal["key"])
    session_count = db._conn.execute("SELECT count(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0]
    assert session_count == 1
    status, found = call("GET", '/Users?filter=' + quote('userName eq "ANA@example.com"'))
    assert status == 200 and found["Resources"][0]["id"] == uid
    assert call("GET", '/Users?filter=' + quote('externalId eq "okta-123"'))[1]["totalResults"] == 1
    assert call("PATCH", path, {"Operations": [{"op": "replace", "path": "name.formatted", "value": "Anna"},
                                               {"op": "add", "path": "roles", "value": [{"value": "viewer"}]}]})[1]["displayName"] == "Anna"
    status, replaced = call("PUT", path, {"userName": "anna@example.com", "displayName": "Ann", "active": True,
                                         "roles": [{"value": "member"}], "externalId": "entra-42"})
    assert status == 200 and replaced["userName"] == "anna@example.com"
    assert call("PATCH", path, {"Operations": [{"op": "Replace", "value": {"active": "False"}}]})[1]["active"] is False
    assert _http(base, "GET", "/api/me", headers=_auth(personal["key"]))[0] == 401
    assert _http(base, "GET", "/api/me", headers={"Cookie": cookie})[0] == 401
    assert db._conn.execute("SELECT count(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0] == 0
    assert db.get_user(uid)["disabled_at"] is None
    assert call("PATCH", path, {"Operations": [{"op": "replace", "path": "active", "value": True}]})[1]["active"]
    assert _http(base, "GET", "/api/me", headers=_auth(personal["key"]))[0] == 200
    for group, op, expected in (("admin", "add", "admin"), ("admin", "remove", "member"), ("member", "remove", "viewer")):
        assert call("PATCH", "/Groups/" + group, {"Operations": [{"op": op, "path": "members", "value": [{"value": uid}]}]})[0] == 200
        assert call("GET", path)[1]["roles"] == [{"value": expected}]
    assert call("DELETE", path)[0] == 204
    assert call("GET", path)[0] == 404
    assert _http(base, "GET", "/api/me", headers=_auth(personal["key"]))[0] == 401
    actions = {e["action"] for e in db.list_audit(team) if e["actor"] == "scim"}
    assert {"scim.user.create", "scim.user.update", "scim.user.deactivate", "scim.user.reactivate", "scim.user.delete", "scim.group.update"} <= actions


def test_credentials_rotation_revocation_and_csrf(scim):
    db, team, key, token, base, call = scim
    assert call("GET", headers=_auth(key))[0] == 401
    cookie = {"Cookie": _cookie_for(base, key)}
    assert call("GET", headers=cookie)[0] == 401
    assert _http(base, "POST", "/api/scim/token", headers=cookie)[0] == 403
    metadata = _json(_http(base, "GET", "/api/scim/token", headers=_auth(key))[2])
    assert "token" not in metadata and "token_hash" not in metadata
    newer = _json(_http(base, "POST", "/api/scim/token", headers=_auth(key))[2])["token"]
    assert call("GET")[0] == 401
    assert call("GET", headers=_auth(newer))[0] == 200
    assert _http(base, "DELETE", "/api/scim/token", headers=_auth(key))[0] == 200
    assert call("GET", headers=_auth(newer))[0] == 401


def test_errors_and_team_isolation(scim):
    db, team, key, token, base, call = scim
    uid = call("POST", body={"userName": "a@example.com"})[1]["id"]
    assert call("POST", body={"userName": "A@example.com"})[0] == 409
    assert call("GET", "/Users?filter=" + quote('displayName co "a"')) == (400, {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"], "status": "400", "detail": "Unsupported filter.", "scimType": "invalidFilter"})
    b, _ = db.create_team("b")
    other_token = db.scim_token(b, "create")["token"]
    assert call("GET", "/Users/" + uid, headers=_auth(other_token))[0] == 404
    assert call("GET", headers=_auth(other_token))[1]["Resources"] == []
    same = call("POST", body={"userName": "a@example.com"}, headers=_auth(other_token))[1]
    assert same["id"] == uid
    for body in ({"userName": "bad"}, {"userName": "x@example.com", "active": "invalid"}, {"userName": "x@example.com", "roles": []}):
        assert call("POST", body=body)[0] == 400
    assert len(db.list_members(team)) == 1
    assert call("PATCH", "/Users/" + uid, {"Operations": []})[0] == 400
    assert call("PATCH", "/Users/" + uid, {"Operations": [{"op": "replace", "path": "password", "value": "x"}]})[0] == 400
    assert call("POST", "/Groups", {})[0] == 405
    assert call("DELETE", "/Groups/admin")[0] == 405
    for endpoint in ("ServiceProviderConfig", "ResourceTypes", "Schemas", "Groups"):
        assert call("GET", "/" + endpoint)[0] == 200
    assert call("GET", "/Users?count=0")[1]["Resources"] == []


def test_last_admin_guard(scim):
    db, team, key, token, base, call = scim
    user = db.add_member(team, "admin@example.com", "admin")
    personal = db.create_key(team, "admin", "admin", "cli", user=user["id"])
    service = db.key_for_token(key)
    db.revoke_key(team, service["id"], "cli")
    path = "/Users/" + user["id"]
    for method, body in (("PATCH", {"Operations": [{"op": "Replace", "path": "active", "value": "False"}]}),
                         ("PUT", {"userName": "admin@example.com", "roles": [{"value": "viewer"}]}), ("DELETE", None)):
        assert call(method, path, body)[0] == 400
    assert call("PATCH", "/Groups/viewer", {"Operations": [{"op": "add", "path": "members", "value": [{"value": user["id"]}]}]})[0] == 400
    assert db.key_for_token(personal["key"])["role"] == "admin"
    assert call("GET", path)[1]["active"] is True


def test_cli(scim, capsys):
    db, team, key, token, base, call = scim
    assert main(["scim-token", "create", "--team", "a", "--db", db.path]) == 0
    secret = capsys.readouterr().out.strip()
    assert call("GET", headers=_auth(secret))[0] == 200
    assert main(["scim-token", "revoke", "--team", "a", "--db", db.path]) == 0
    assert call("GET", headers=_auth(secret))[0] == 401


def test_validation_is_atomic_and_bodies_are_bounded(scim):
    db, team, key, token, base, call = scim
    uid = call("POST", body={"userName": "first@example.com"})[1]["id"]
    other = call("POST", body={"userName": "second@example.com"})[1]["id"]
    path = "/Users/" + uid
    assert call("PUT", path, {"userName": "SECOND@example.com"})[0] == 409
    assert call("GET", path)[1]["userName"] == "first@example.com"
    assert call("PATCH", path, {"Operations": [{"op": "replace", "path": "displayName", "value": "Changed"},
                                               {"op": "replace", "path": "active", "value": "wrong"}]})[0] == 400
    assert call("GET", path)[1]["displayName"] == ""
    assert call("PATCH", "/Groups/admin", {"Operations": [{"op": "add", "path": "members", "value": [{"value": uid}, {"value": "missing"}]}]})[0] == 400
    assert call("GET", path)[1]["roles"][0]["value"] == "member"
    assert call("POST", body=b"not json")[0] == 400
    assert call("POST", body={"userName": "x@example.com", "displayName": "x" * 65536})[0] == 413
    assert call("POST", body=b"x" * 65537, headers=_auth(key))[0] == 401
    assert call("PATCH", path, {"Operations": [{"op": "remove", "path": "displayName"}]})[0] == 200
    assert call("GET", "/Users?startIndex=2&count=1")[1]["itemsPerPage"] == 1
    assert call("GET", "/Users?count=invalid")[0] == 400
    assert call("GET", "/Groups/member")[1]["members"]
    assert call("PATCH", "/Groups/member", {"Operations": [{"op": "remove", "path": 'members[value eq "' + other + '"]'}]})[0] == 200
    assert call("GET", "/Users/" + other)[1]["roles"] == [{"value": "viewer"}]
