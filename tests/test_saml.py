"""Dashboard SAML integration with locally generated IdP signing keys."""

import base64
from datetime import datetime, timedelta, timezone
import json
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
import zlib

import pytest

from runledger import client
from runledger.server.saml import SAMLError
from test_auth import CSRF, FIX, _auth, _http, _json, running

etree = pytest.importorskip("lxml.etree")
xmlsec = pytest.importorskip("xmlsec")
pytest.importorskip("onelogin.saml2")

NS = {
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
}
ISSUER = "https://idp.example.test/metadata"
ROOT = "/auth/saml/work"


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to generate the SAML test IdP certificates")
    directory = tmp_path_factory.mktemp("saml-keys")
    result = []
    for name in ("idp", "other"):
        key = directory / f"{name}.key"
        cert = directory / f"{name}.pem"
        subprocess.run(
            [openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-x509", "-days", "1",
             "-subj", "/CN=" + name, "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True,
        )
        result.append((key.read_bytes(), cert))
    return result


@pytest.fixture
def saml_config(tmp_path, keys, request):
    config = tmp_path / "saml.json"
    config.write_text(json.dumps({"providers": [{
        "id": "work", "name": "Company SAML", "idp_entity_id": ISSUER,
        "idp_sso_url": getattr(request, "param", "https://idp.example.test/sso"),
        "idp_cert": str(keys[0][1]),
        "email_attribute": "workEmail", "name_attribute": "displayName",
    }]}))
    return config


@pytest.fixture
def oidc_config(tmp_path, monkeypatch):
    oidc_config = tmp_path / "oidc.json"
    oidc_config.write_text(json.dumps({"providers": [{
        "id": "oidc", "name": "Company OIDC", "issuer": "https://oidc.example.test",
        "client_id": "oidc-client", "client_secret_env": "TEST_OIDC_CLIENT_SECRET",
    }]}))
    monkeypatch.setenv("TEST_OIDC_CLIENT_SECRET", "test-only")
    return oidc_config


@pytest.fixture
def env(tmp_path, saml_config, oidc_config):
    with running(tmp_path / "db", saml_config=str(saml_config), oidc_config=str(oidc_config)) as (server, base):
        team, _ = server.db.create_team("Engineering")
        person = server.db.add_member(team, "ana@example.com", "viewer")
        yield server, base, team, person


def _start(base, expected_sso_url="https://idp.example.test/sso"):
    status, headers, _ = _http(base, "GET", ROOT + "/start")
    assert status == 302
    assert "HttpOnly; SameSite=None; Secure" in headers["set-cookie"]
    separator = "&" if "?" in expected_sso_url else "?"
    assert headers["location"].startswith(expected_sso_url + separator + "SAMLRequest=")
    url = urlsplit(headers["location"])
    assert url.netloc == "idp.example.test"
    query = parse_qs(url.query)
    request = etree.fromstring(zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15))
    assert request.get("Destination") == expected_sso_url
    assert request.get("AssertionConsumerServiceURL") == base + ROOT + "/acs"
    assert request.findtext("saml:Issuer", namespaces=NS) == base + ROOT + "/metadata"
    return query["RelayState"][0], headers["set-cookie"].split(";")[0], request.get("ID")


@pytest.mark.parametrize(
    "saml_config", ["https://idp.example.test/sso?tenant=acme&connection=Work%20SSO"], indirect=True,
)
def test_saml_start_preserves_idp_query(env):
    _, base, _, _ = env
    state, _, request_id = _start(base, "https://idp.example.test/sso?tenant=acme&connection=Work%20SSO")
    assert state and request_id


def _timestamp(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _response(base, request_id, keys, *, email="Ana@example.com", subject="person-42",
              sign="assertion", invalid=None):
    acs = base + ROOT + "/acs"
    audience = base + ROOT + "/metadata"
    recipient = acs + "/wrong" if invalid == "recipient" else acs
    if invalid == "audience":
        audience += "/wrong"
    confirmation_id = "_wrong" if invalid == "confirmation_id" else request_id
    response_id = "_wrong" if invalid == "response_id" else request_id
    now = _timestamp(0)
    expiry = _timestamp(-5 if invalid == "expired" else 180)
    condition_expiry = _timestamp(-5 if invalid == "conditions_expired" else 180)
    tree = etree.fromstring(f"""
    <samlp:Response xmlns:samlp="{NS['samlp']}" xmlns:saml="{NS['saml']}"
        ID="_{uuid4().hex}" Version="2.0" IssueInstant="{now}" Destination="{acs}" InResponseTo="{response_id}">
      <saml:Issuer>{ISSUER}</saml:Issuer>
      <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
      <saml:Assertion ID="_{uuid4().hex}" Version="2.0" IssueInstant="{now}">
        <saml:Issuer>{ISSUER}</saml:Issuer>
        <saml:Subject>
          <saml:NameID>{subject}</saml:NameID>
          <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
            <saml:SubjectConfirmationData Recipient="{recipient}" NotOnOrAfter="{expiry}"
                InResponseTo="{confirmation_id}"/>
          </saml:SubjectConfirmation>
        </saml:Subject>
        <saml:Conditions NotBefore="{_timestamp(-60)}" NotOnOrAfter="{condition_expiry}">
          <saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>
        </saml:Conditions>
        <saml:AuthnStatement AuthnInstant="{now}">
          <saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password
          </saml:AuthnContextClassRef></saml:AuthnContext>
        </saml:AuthnStatement>
        <saml:AttributeStatement>
          <saml:Attribute Name="workEmail"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>
          <saml:Attribute Name="displayName"><saml:AttributeValue>Ana SAML</saml:AttributeValue></saml:Attribute>
        </saml:AttributeStatement>
      </saml:Assertion>
    </samlp:Response>""".encode())
    if sign:
        signed = tree.find("saml:Assertion", NS) if sign == "assertion" else tree
        signature = xmlsec.template.create(signed, xmlsec.Transform.EXCL_C14N, xmlsec.Transform.RSA_SHA256)
        signed.insert(1, signature)
        reference = xmlsec.template.add_reference(signature, xmlsec.Transform.SHA256, uri="#" + signed.get("ID"))
        xmlsec.template.add_transform(reference, xmlsec.Transform.ENVELOPED)
        xmlsec.template.add_transform(reference, xmlsec.Transform.EXCL_C14N)
        key, _ = keys[1 if invalid == "signature" else 0]
        context = xmlsec.SignatureContext()
        context.key = xmlsec.Key.from_memory(key, xmlsec.KeyFormat.PEM)
        xmlsec.tree.add_ids(tree, ["ID"])
        context.sign(signature)
    return base64.b64encode(etree.tostring(tree)).decode()


def _post(base, path, fields, cookie, **headers):
    return _http(base, "POST", path, urlencode(fields).encode(),
                 {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie, **headers})


def _acs(base, state, encoded, cookie):
    return _post(base, ROOT + "/acs", {"RelayState": state, "SAMLResponse": encoded}, cookie)


@pytest.mark.parametrize("sign", ["assertion", "response"])
def test_saml_sign_in(env, keys, sign):
    server, base, team, person = env
    login_page = _http(base, "GET", "/")[2]
    assert b"Sign in with Company SAML" in login_page
    assert b"Sign in with Company OIDC" in login_page
    status, headers, metadata = _http(base, "GET", ROOT + "/metadata")
    assert status == 200
    assert headers["content-type"] == "application/samlmetadata+xml"
    entity = etree.fromstring(metadata)
    assert entity.get("entityID") == base + ROOT + "/metadata"
    acs = entity.find(".//md:AssertionConsumerService", NS)
    assert acs.get("Location") == base + ROOT + "/acs"
    assert acs.get("Binding") == "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
    state, cookie, request_id = _start(base)
    encoded = _response(base, request_id, keys, sign=sign)
    assert _acs(base, state, encoded, "unrelated=browser")[0] == 401
    status, headers, _ = _acs(base, state, encoded, cookie)
    assert status == 200
    assert "HttpOnly; SameSite=Strict" in headers["set-cookie"]
    status, _, raw = _http(base, "GET", "/api/me", headers={"Cookie": headers["set-cookie"].split(";")[0]})
    assert status == 200
    assert _json(raw) == {
        "team": {"id": team, "name": "Engineering"}, "role": "viewer", "key": None,
        "user": {"id": person["id"], "email": "ana@example.com", "name": "Ana SAML"}, "sign_in_method": "saml",
    }
    assert server.db.get_user(person["id"])["saml_subject"] == "person-42"
    assert _acs(base, state, encoded, cookie)[0] == 401
    new_state, new_cookie, _ = _start(base)
    assert _acs(base, new_state, encoded, new_cookie)[0] == 401


@pytest.mark.parametrize("with_oidc", [False, True], ids=["saml-only", "saml-and-oidc"])
def test_saml_disables_dashboard_key_sign_in(tmp_path, saml_config, oidc_config, monkeypatch, keys, with_oidc):
    config = json.loads(saml_config.read_text())
    config["disable_key_sign_in"] = True
    saml_config.write_text(json.dumps(config))
    monkeypatch.delenv("RUNLEDGER_OIDC_CONFIG", raising=False)
    with running(tmp_path / "db", saml_config=str(saml_config),
                 oidc_config=str(oidc_config) if with_oidc else None) as (server, base):
        team, admin = server.db.create_team("Engineering")
        server.db.add_member(team, "ana@example.com", "admin")
        assert (server.oidc is not None) == with_oidc

        status, _, body = _http(base, "GET", "/")
        assert status == 401
        assert b"Sign in with Company SAML" in body
        assert b'action="/session"' not in body
        requests = (
            _http(base, "GET", "/?" + urlencode({"key": admin})),
            _post(base, "/session", {"key": admin}, ""),
        )
        for status, headers, body in requests:
            assert status == 401
            assert "set-cookie" not in headers
            assert b"Key sign-in is disabled" in body

        state, cookie, request_id = _start(base)
        status, headers, _ = _acs(base, state, _response(base, request_id, keys), cookie)
        assert status == 200
        status, _, body = _http(base, "GET", "/api/me", headers={"Cookie": headers["set-cookie"].split(";")[0]})
        assert status == 200
        assert _json(body)["sign_in_method"] == "saml"

        payload = client.build_payload(FIX, user="ana@example.com")
        assert _http(base, "POST", "/api/runs", body=payload, headers=_auth(admin))[0] == 201


@pytest.mark.parametrize(("override", "error"), [
    ({"providers": [], "disable_key_sign_in": True}, "non-empty providers array"),
    ({"disable_key_sign_in": "true"}, "disable_key_sign_in must be boolean"),
])
def test_saml_rejects_invalid_key_sign_in_policy(tmp_path, saml_config, override, error):
    config = json.loads(saml_config.read_text())
    config.update(override)
    saml_config.write_text(json.dumps(config))
    with pytest.raises(SAMLError, match=error):
        with running(tmp_path / "db", saml_config=str(saml_config)):
            pytest.fail("invalid SAML config must not start the server")


@pytest.mark.parametrize("role", ["member", "admin"])
def test_saml_sessions_cannot_push_runs_or_request_approvals(env, keys, role):
    server, base, team, person = env
    server.db.change_member(team, person["id"], role=role)
    state, cookie, request_id = _start(base)
    status, headers, _ = _acs(base, state, _response(base, request_id, keys), cookie)
    assert status == 200
    auth = dict(CSRF, Cookie=headers["set-cookie"].split(";")[0])
    for path in ("/api/runs", "/api/approvals"):
        status, _, body = _http(base, "POST", path, body={}, headers=auth)
        assert status == 403
        assert _json(body)["error"]["code"] == "api_key_required"


@pytest.mark.parametrize("invalid", [
    "signature", "audience", "expired", "conditions_expired", "recipient",
    "response_id", "confirmation_id", "unsigned", "malformed",
])
def test_saml_rejects_invalid_assertions(env, keys, invalid):
    server, base, _, person = env
    state, cookie, request_id = _start(base)
    encoded = "bad" if invalid == "malformed" else _response(
        base, request_id, keys, invalid=invalid, sign=None if invalid == "unsigned" else "assertion",
    )
    status, headers, body = _acs(base, state, encoded, cookie)
    assert status == 401
    assert b"Invalid SAML response." in body
    assert "set-cookie" not in headers
    assert server.db.get_user(person["id"])["saml_subject"] is None


def test_saml_requires_provisioning_and_preserves_other_sso_binding(env, keys):
    server, base, team, person = env
    state, cookie, request_id = _start(base)
    encoded = _response(base, request_id, keys, email="other@example.com")
    assert _acs(base, state, encoded, cookie)[0] == 401
    assert server.db.get_user("other@example.com") is None
    server.db.oidc_user(person["email"], "oidc-issuer", "oidc-subject")
    state, cookie, request_id = _start(base)
    assert _acs(base, state, _response(base, request_id, keys), cookie)[0] == 200
    assert server.db.get_user(person["id"])["oidc_subject"] == "oidc-subject"
    state, cookie, request_id = _start(base)
    assert _acs(base, state, _response(base, request_id, keys, subject="different"), cookie)[0] == 401
    server.db.add_member(team, "other@example.com", "viewer")
    state, cookie, request_id = _start(base)
    encoded = _response(base, request_id, keys, email="other@example.com")
    assert _acs(base, state, encoded, cookie)[0] == 401


def test_saml_team_choice_is_same_site_and_single_use(env, keys):
    server, base, _, person = env
    other, _ = server.db.create_team("Operations")
    server.db.add_member(other, person["email"], "member")
    state, cookie, request_id = _start(base)
    status, headers, body = _acs(base, state, _response(base, request_id, keys), cookie)
    assert status == 200
    assert "set-cookie" not in headers
    assert b"Engineering" in body and b"Operations" in body
    assert b'action="/auth/saml/team"' in body
    choice = {"state": re.search(rb'name="state" value="([^"]+)"', body)[1].decode(), "team": other}
    assert _post(base, "/auth/saml/team", choice, cookie, **{"Sec-Fetch-Site": "cross-site"})[0] == 403
    status, headers, _ = _post(base, "/auth/saml/team", choice, cookie)
    assert status == 200
    status, _, raw = _http(base, "GET", "/api/me", headers={"Cookie": headers["set-cookie"].split(";")[0]})
    assert status == 200
    assert _json(raw)["team"]["id"] == other
    assert _post(base, "/auth/saml/team", choice, cookie)[0] == 401
