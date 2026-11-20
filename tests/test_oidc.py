"""OIDC integration with a local, signed-token identity provider; no external services."""

import base64
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from runledger.server.oidc import Client, OIDCError
from test_auth import APPROVAL, CSRF, _http, _json, running, _auth

N = int(
    "bcff197de087597304150526d7f593b85d21710b6720c13f59f0d6c535bf1ca2470cf1452152b062d0df37c1f7994337c8cd2426ffac628a8166a939ec14df5f66b4226c35b05cd4855fb464046ffe89ba9c2e6857942c8945c194ae8742484b88cacfee880dad665a318bb36abfd51ae20f0054340759f2e0ea64d75ddc35490a9b10c63fb187a92925ce1108d6bb33129022577387c3d69c3ae234024e0ab8e82b35ec576abc7bb5dd246416d218b6a59302e666b72e70cb0ef166b2e63a229f3628271ce41b3fe946ee2ea95188c3ea95a824db0910008676cfba8592a124e81cf87a3a645a1f456a8038d6115b7dd34702c6e7efa3ab40bc50e7628feedb",
    16,
)
D = int(
    "080eb01d87b6fd7f43cb499ef0520dec2c3c42bf83b407c915a288e57b771c5766c40c7d3000807896030e337cc4d1f2cce2c209cf989fd2dbd56e9d838d1840cef61b0995f860dcc69adfebebac82a3888b24075ac47c6995c9bcf6fbd93d8fcad32aae36aaae9bd078904623e4edc6e9a04e1948e68f55257598baf dcb679214d8753c812326b47ae3cfef5e232f665a51779d1c2fee35cf7aa85dbc7edc42823dd3d31e8a5998320c7b7a816a530eb87b4b7260799c2b5477fa0e4a404e605cf889c6ecc56fbe13dc2b1c554551a8fcc6f71561524c786b885137f6534550f339ea10b12b80e6efdac44ee371e0c809292e7dfffa8498612eefad92326831".replace(
        " ", ""
    ),
    16,
)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed(claims, alg="RS256", kid="one", bad_signature=False):
    data = b64(json.dumps(dict(alg=alg, kid=kid)).encode()) + "." + b64(json.dumps(claims).encode())
    digest = (
        bytes.fromhex("3031300d060960864801650304020105000420")
        + hashlib.sha256(data.encode()).digest()
    )
    em = b"\x00\x01" + b"\xff" * (256 - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(em, "big"), D, N).to_bytes(256, "big")
    if bad_signature:
        signature = bytes([signature[0] ^ 1]) + signature[1:]
    return data + "." + b64(signature)


@pytest.fixture
def env(tmp_path):
    class IdP(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.endswith("openid-configuration"):
                value = dict(
                    issuer=issuer,
                    authorization_endpoint=issuer + "/authorize",
                    token_endpoint=issuer + "/token",
                    jwks_uri=issuer + "/jwks",
                )
            else:
                state["jwks_calls"] += 1
                value = dict(
                    keys=[
                        dict(
                            kty="RSA",
                            kid=state["jwks_kid"],
                            n=b64(N.to_bytes(256, "big")),
                            e=b64((65537).to_bytes(3, "big")),
                        )
                    ]
                )
            self.reply(value)

        def do_POST(self):
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            assert fields["client_secret"] == ["secret"]
            assert fields["redirect_uri"][0].endswith("/auth/oidc/test/callback")
            assert (
                b64(hashlib.sha256(fields["code_verifier"][0].encode()).digest())
                == state["challenge"]
            )
            claims = dict(
                iss=issuer,
                sub="person-1",
                aud="client",
                exp=time.time() + 300,
                iat=time.time(),
                nonce=state["nonce"],
                email="ana@example.com",
                email_verified=True,
            )
            claims.update(state["claims"])
            for field in state.get("omit", []):
                claims.pop(field, None)
            self.reply(dict(id_token=signed(claims, **state["sign"])))

        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    state = dict(claims={}, sign={}, jwks_kid="one", jwks_calls=0)
    idp = ThreadingHTTPServer(("127.0.0.1", 0), IdP)
    issuer = "http://127.0.0.1:" + str(idp.server_port)
    thread = threading.Thread(target=idp.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "oidc.json"
    config.write_text(
        json.dumps(
            dict(
                providers=[
                    dict(
                        id="test",
                        name="Test SSO",
                        issuer=issuer,
                        client_id="client",
                        client_secret="secret",
                    )
                ]
            )
        )
    )
    try:
        with running(tmp_path / "server.db", oidc_config=str(config)) as (server, base):
            team, key = server.db.create_team("Team")
            person = server.db.add_member(team, "ana@example.com", "member", "Ana")
            yield server, base, state, team, person, key, config
    finally:
        idp.shutdown()
        idp.server_close()
        thread.join()


def start(env):
    _, base, state, *_ = env
    status, headers, _ = _http(base, "GET", "/auth/oidc/test/start")
    assert status == 302
    assert "SameSite=Lax" in headers["set-cookie"]
    fields = parse_qs(urlsplit(headers["location"]).query)
    assert fields["code_challenge_method"] == ["S256"]
    state["nonce"] = fields["nonce"][0]
    state["challenge"] = fields["code_challenge"][0]
    return (
        "/auth/oidc/test/callback?" + urlencode(dict(state=fields["state"][0], code="code")),
        headers["set-cookie"].split(";")[0],
    )


def finish(env):
    path, cookie = start(env)
    return _http(env[1], "GET", path, headers={"Cookie": cookie})


def test_happy_path_person_cookie_and_live_role(env):
    server, base, _, team, person, key, _ = env
    status, headers, raw = finish(env)
    assert status == 200, raw
    assert "location" not in headers
    assert b'http-equiv="refresh" content="0;url=/"' in raw
    assert "SameSite=Strict" in headers["set-cookie"]
    cookie = {"Cookie": headers["set-cookie"].split(";")[0]}
    me = _json(_http(base, "GET", "/api/me", headers=cookie)[2])
    assert me["user"]["id"] == person["id"] and me["key"] is None
    assert me["role"] == "member" and me["sign_in_method"] == "oidc"
    assert server.db.get_user(person["id"])["oidc_subject"] == "person-1"
    server.db.change_member(team, person["id"], role="viewer")
    assert _json(_http(base, "GET", "/api/me", headers=cookie)[2])["role"] == "viewer"
    events = server.db.list_audit(team)
    assert any(
        e["action"] == "auth.sign_in"
        and e["details"]["method"] == "oidc"
        and "ana@example.com" in e["actor"]
        for e in events
    )
    server.db.change_member(team, person["id"])
    assert _http(base, "GET", "/api/me", headers=cookie)[0] == 401


@pytest.mark.parametrize(
    "claims,sign",
    [
        ({"nonce": "wrong"}, {}),
        ({"aud": "other"}, {}),
        ({"iss": "https://wrong"}, {}),
        ({"exp": 1}, {}),
        ({}, {"bad_signature": True}),
        ({}, {"alg": "none"}),
        ({"email_verified": False}, {}),
        ({"email_verified": None}, {}),
        ({"iat": time.time() + 10000}, {}),
        ({"nbf": time.time() + 10000}, {}),
        ({"aud": ["client", "other"]}, {}),
        ({"email": "nobody@example.com"}, {}),
    ],
)
def test_rejected_tokens(env, claims, sign):
    env[2]["claims"] = claims
    env[2]["sign"] = sign
    status, headers, raw = finish(env)
    assert status == 401, raw
    assert "set-cookie" not in headers
    assert env[0].limiter.blocked_for("127.0.0.1") == 0
    assert not any(e["action"] == "auth.sign_in_rejected" for e in env[0].db.list_audit(env[3]))


@pytest.mark.parametrize("mode", ["missing", "bad", "no_cookie", "reused"])
def test_state(env, mode):
    path, cookie = start(env)
    if mode == "missing":
        path = "/auth/oidc/test/callback?code=code"
    elif mode == "bad":
        path = "/auth/oidc/test/callback?code=code&state=bad"
    elif mode == "reused":
        assert _http(env[1], "GET", path, headers={"Cookie": cookie})[0] == 200
    assert (
        _http(env[1], "GET", path, headers={"Cookie": cookie if mode != "no_cookie" else ""})[0]
        == 401
    )


def test_rotation(env):
    assert finish(env)[0] == 200
    calls = env[2]["jwks_calls"]
    env[2]["jwks_kid"] = "two"
    env[2]["sign"] = {"kid": "two"}
    assert finish(env)[0] == 200
    assert env[2]["jwks_calls"] == calls + 1


@pytest.mark.parametrize("mode", ["disabled", "no_membership", "subject", "domain", "hd"])
def test_account_and_domain_restrictions(env, mode):
    server, _, state, team, person, *_ = env
    if mode == "disabled":
        server.db.set_user_disabled(person["id"], True)
    elif mode == "no_membership":
        server.db.change_member(team, person["id"])
    elif mode == "subject":
        assert finish(env)[0] == 200
        state["claims"]["sub"] = "different"
    elif mode == "domain":
        server.oidc.providers["test"]["allowed_email_domains"] = ["other.com"]
    else:
        server.oidc.providers["test"]["hosted_domain"] = "example.com"
        state["claims"]["hd"] = "other.com"
    assert finish(env)[0] == 401


def test_team_chooser_csrf_and_one_use(env):
    server, base, _, _, person, *_ = env
    second, _ = server.db.create_team("Second")
    server.db.add_member(second, person["email"], "viewer")
    path, cookie = start(env)
    status, _, raw = _http(base, "GET", path, headers={"Cookie": cookie})
    assert status == 200 and b"Choose a team" in raw
    assert b'<meta name="viewport"' in raw and b"<style>body{" in raw
    assert b"<main>" in raw and b"__KEY_FORM__" not in raw
    state = re.search(rb'name="state" value="([^"]+)"', raw)[1].decode()
    body = urlencode(dict(state=state, team=second)).encode()
    assert (
        _http(
            base,
            "POST",
            "/auth/oidc/team",
            body=body,
            headers={"Cookie": cookie, "Origin": "https://evil.example"},
        )[0]
        == 403
    )
    status, headers, _ = _http(
        base, "POST", "/auth/oidc/team", body=body, headers={"Cookie": cookie}
    )
    assert status == 200
    me = _json(
        _http(base, "GET", "/api/me", headers={"Cookie": headers["set-cookie"].split(";")[0]})[2]
    )
    assert me["team"]["id"] == second
    assert _http(base, "POST", "/auth/oidc/team", body=body, headers={"Cookie": cookie})[0] == 401


def test_disabled_key_sign_in_keeps_api(env):
    server, base, _, _, _, key, _ = env
    server.oidc.disable_key_sign_in = True
    raw = _http(base, "GET", "/")[2]
    assert b"Sign in with Test SSO" in raw and b'action="/session"' not in raw
    assert b'<a class="sso" href="/auth/oidc/test/start">' in raw
    assert _http(base, "GET", "/?key=" + key)[0] == 401
    assert _http(base, "POST", "/session", body=("key=" + key).encode())[0] == 401
    assert _http(base, "GET", "/api/me", headers=_auth(key))[0] == 200


def test_config_validation(env, monkeypatch):
    config = env[-1]
    data = json.loads(config.read_text())
    data["providers"][0].pop("client_secret")
    data["providers"][0]["client_secret_env"] = "TEST_OIDC_SECRET"
    config.write_text(json.dumps(data))
    monkeypatch.delenv("TEST_OIDC_SECRET", raising=False)
    with pytest.raises(OIDCError, match="client_secret"):
        Client(config)
    monkeypatch.setenv("TEST_OIDC_SECRET", "value")
    assert Client(config).providers["test"]["client_secret"] == "value"


def test_missing_verified_claim(env):
    env[2]["omit"] = ["email_verified"]
    assert finish(env)[0] == 401
    env[0].oidc.providers["test"]["require_email_verified"] = False
    assert finish(env)[0] == 200


def test_email_case_insensitive_and_domain_hd_success(env):
    env[0].oidc.providers["test"].update(
        allowed_email_domains=["example.com"], hosted_domain="example.com"
    )
    env[2]["claims"].update(email="ANA@EXAMPLE.COM", hd="example.com")
    assert finish(env)[0] == 200


def test_unknown_kid_rejected_after_refetch(env):
    env[2]["sign"]["kid"] = "missing"
    assert finish(env)[0] == 401
    assert env[2]["jwks_calls"] == 2


def test_expired_state_and_failed_callbacks_limited(env):
    path, cookie = start(env)
    for value in env[0].oidc._pending.values():
        value["expires"] = 0
    assert _http(env[1], "GET", path, headers={"Cookie": cookie})[0] == 401
    env[0].limiter.max_failures = 2
    for _ in range(2):
        assert _http(env[1], "GET", "/auth/oidc/test/callback")[0] == 401
    assert _http(env[1], "GET", "/auth/oidc/test/callback")[0] == 429


def test_disabled_after_sign_in_ends_session(env):
    status, headers, _ = finish(env)
    assert status == 200
    env[0].db.set_user_disabled(env[4]["id"], True)
    assert (
        _http(env[1], "GET", "/api/me", headers={"Cookie": headers["set-cookie"].split(";")[0]})[0]
        == 401
    )


def test_keyless_session_survives_database_reopen(env):
    from runledger.server.db import Database

    status, headers, _ = finish(env)
    assert status == 200
    token = headers["set-cookie"].split(";")[0].split("=", 1)[1]
    db = Database(env[0].db.path)
    try:
        assert db.session_key(token)["user_id"] == env[4]["id"]
        assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_discovery_issuer_must_match(env):
    client = env[0].oidc
    client._json = lambda *args: {"issuer": "https://wrong.example"}
    assert _http(env[1], "GET", "/auth/oidc/test/start")[0] == 401


@pytest.mark.parametrize("mode", ["disabled", "subject"])
def test_rejection_audit_stays_in_resolved_users_teams(env, mode):
    server, base, state, team, person, *_ = env
    other, _ = server.db.create_team("Unrelated")
    second, _ = server.db.create_team("Another membership")
    server.db.add_member(second, person["email"], "member")
    if mode == "disabled":
        server.db.set_user_disabled(person["id"], True)
    else:
        server.db.oidc_user(person["email"], server.oidc.providers["test"]["issuer"], "different")
    assert finish(env)[0] == 401
    for member_team in (team, second):
        events = [
            e for e in server.db.list_audit(member_team) if e["action"] == "auth.sign_in_rejected"
        ]
        assert len(events) == 1 and events[0]["target"] == person["id"]
    assert not any(e["action"] == "auth.sign_in_rejected" for e in server.db.list_audit(other))


def test_invalid_state_does_not_write_team_audits(env):
    assert _http(env[1], "GET", "/auth/oidc/test/callback?state=unknown")[0] == 401
    assert not any(e["action"] == "auth.sign_in_rejected" for e in env[0].db.list_audit(env[3]))


def test_sign_in_placeholders_render_key_form_and_sso(env):
    raw = _http(env[1], "GET", "/")[2]
    assert b'action="/session"' in raw and b"Sign in with Test SSO" in raw
    assert b"__SSO_BUTTONS__" not in raw and b"__KEY_FORM__" not in raw


def test_missing_public_url_derives_bound_url(env, tmp_path, monkeypatch):
    monkeypatch.delenv("RUNLEDGER_PUBLIC_URL", raising=False)
    with running(tmp_path / "derived.db", oidc_config=str(env[-1])) as (server, base):
        status, headers, _ = _http(base, "GET", "/auth/oidc/test/start")
        assert status == 302
        fields = parse_qs(urlsplit(headers["location"]).query)
        assert fields["redirect_uri"] == [base + "/auth/oidc/test/callback"]


def test_unusable_public_url_fails_at_startup(env, tmp_path):
    with pytest.raises(OIDCError, match="valid public URL"):
        with running(tmp_path / "invalid.db", oidc_config=str(env[-1]), public_url=" "):
            pass


@pytest.mark.parametrize("role", ["member", "admin"])
def test_keyless_sessions_cannot_push_runs_or_request_approvals(env, role):
    server, base, _, team, person, *_ = env
    server.db.change_member(team, person["id"], role=role)
    status, headers, _ = finish(env)
    assert status == 200
    auth = dict(CSRF, Cookie=headers["set-cookie"].split(";")[0])
    for path, action in (("/api/runs", "push runs"), ("/api/approvals", "request approvals")):
        status, _, raw = _http(base, "POST", path, body={}, headers=auth)
        assert status == 403
        assert _json(raw)["error"]["message"] == f"Use an API key to {action}."


def test_sso_admin_can_decide_key_requested_approval(env):
    server, base, _, team, person, key, _ = env
    server.db.change_member(team, person["id"], role="admin")
    requester = server.db.create_key(team, "guard", "member", "cli")
    status, _, raw = _http(base, "POST", "/api/approvals", body=APPROVAL,
                           headers=_auth(requester["key"]))
    assert status == 201
    approval = _json(raw)["id"]
    status, headers, _ = finish(env)
    assert status == 200
    auth = dict(CSRF, Cookie=headers["set-cookie"].split(";")[0])
    status, _, raw = _http(base, "POST", f"/api/approvals/{approval}/decision",
                           body={"decision": "approve"}, headers=auth)
    assert status == 200, raw
    assert "ana@example.com" in _json(raw)["decided_by"]
