"""SAML 2.0 service provider for dashboard sign-in."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
_DS = "http://www.w3.org/2000/09/xmldsig#"
_REFERENCE_TRANSFORMS = {
    _DS + "enveloped-signature",
    "http://www.w3.org/2001/10/xml-exc-c14n#",
    "http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
}


class SAMLError(ValueError):
    """A configuration or sign-in failure safe to show in the dashboard."""


def _url(value: str, *, allow_query: bool = False) -> str:
    parsed = urlsplit(value)
    loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if (
        (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        or not parsed.hostname or parsed.username is not None or parsed.password is not None
        or (not allow_query and "?" in value) or "#" in value or any(c.isspace() for c in value)
    ):
        raise ValueError("SAML URLs require HTTPS (HTTP is allowed only on loopback).")
    parsed.port  # Validate the port before the server starts.
    return value


class Client:
    def __init__(self, path: str) -> None:
        try:
            from lxml import etree
            from onelogin.saml2.authn_request import OneLogin_Saml2_Authn_Request
            from onelogin.saml2.errors import OneLogin_Saml2_Error, OneLogin_Saml2_ValidationError
            from onelogin.saml2.response import OneLogin_Saml2_Response
            from onelogin.saml2.settings import OneLogin_Saml2_Settings
            from onelogin.saml2.utils import OneLogin_Saml2_Utils
            import xmlsec
        except ImportError:
            raise SAMLError("SAML requires the optional dependency: pip install 'runledger-ai[saml]'.") from None

        self._authn_request = OneLogin_Saml2_Authn_Request
        self._response = OneLogin_Saml2_Response
        self._settings_class = OneLogin_Saml2_Settings
        self._utils = OneLogin_Saml2_Utils
        self._response_errors = (
            ValueError, TypeError, etree.Error, xmlsec.Error, OneLogin_Saml2_Error,
            OneLogin_Saml2_ValidationError,
        )
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._settings: Dict[str, Any] = {}
        self.providers: Dict[str, Dict[str, str]] = {}
        try:
            source = Path(path)
            config = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or not isinstance(config.get("providers"), list) or not config["providers"]:
                raise ValueError("Expected a non-empty providers array.")
            self.public_url = _url(config["public_url"]).rstrip("/") if "public_url" in config else None
            for raw in config["providers"]:
                if not isinstance(raw, dict):
                    raise ValueError("Each provider must be an object.")
                p = dict(raw)
                p.setdefault("email_attribute", "email")
                for field in ("id", "name", "idp_entity_id", "idp_sso_url", "idp_cert", "email_attribute",
                              "name_attribute", "subject_attribute", "sp_entity_id"):
                    if field in ("name_attribute", "subject_attribute", "sp_entity_id") and field not in p:
                        continue
                    if not isinstance(p.get(field), str) or not p[field].strip():
                        raise ValueError(f"Each configured {field} must be a non-empty string.")
                if not re.fullmatch(r"[A-Za-z0-9_-]+", p["id"]) or p["id"] in self.providers:
                    raise ValueError("Provider ids must be unique URL-safe identifiers.")
                _url(p["idp_sso_url"], allow_query=True)
                cert = (source.parent / p["idp_cert"]).read_text(encoding="ascii")
                xmlsec.Key.from_memory(cert, xmlsec.KeyFormat.CERT_PEM, None)
                p["cert"] = cert
                self.providers[p["id"]] = p
        except (OSError, TypeError, ValueError, xmlsec.Error) as exc:
            raise SAMLError("Invalid SAML configuration: " + str(exc)) from None

    def bind(self, public_url: str) -> None:
        """Fix the SP endpoints from server configuration, never request headers."""
        self.public_url = _url(self.public_url or public_url)
        for provider, p in self.providers.items():
            prefix = f"{self.public_url}/auth/saml/{provider}"
            try:
                self._settings[provider] = self._settings_class({
                    "strict": True,
                    "sp": {
                        "entityId": p.get("sp_entity_id", prefix + "/metadata"),
                        "assertionConsumerService": {"url": prefix + "/acs"},
                    },
                    "idp": {
                        "entityId": p["idp_entity_id"],
                        "singleSignOnService": {"url": p["idp_sso_url"]},
                        "x509cert": p["cert"],
                    },
                    "security": {"rejectDeprecatedAlgorithm": True, "requestedAuthnContext": False},
                })
            except self._response_errors:
                raise SAMLError("Invalid SAML provider settings.") from None

    def metadata(self, provider: str) -> bytes:
        metadata = self._settings[provider].get_sp_metadata()
        return metadata.encode("utf-8") if isinstance(metadata, str) else metadata

    def _request_data(self, provider: str) -> Dict[str, str]:
        url = urlsplit(self._settings[provider].get_sp_data()["assertionConsumerService"]["url"])
        return {"https": "on" if url.scheme == "https" else "off",
                "http_host": url.netloc, "script_name": url.path}

    def _stash(self, state: str, **fields: Any) -> None:
        with self._lock:
            now = time.monotonic()
            self._pending = {key: value for key, value in self._pending.items() if value["expires"] > now}
            if len(self._pending) >= 10_000:
                raise SAMLError("Too many pending sign-ins; try again later.")
            self._pending[state] = dict(fields, expires=now + 600)

    def stash(self, value: Mapping[str, Any]) -> str:
        state = secrets.token_urlsafe(32)
        self._stash(state, **value)
        return state

    def consume(self, state: str, cookie: Optional[str]) -> Dict[str, Any]:
        with self._lock:
            pending = self._pending.get(state)
            if (not pending or pending["expires"] <= time.monotonic() or not cookie
                    or not cookie.isascii() or not hmac.compare_digest(pending["cookie"], cookie)):
                raise SAMLError("Sign-in expired or invalid state. Please try again.")
            return self._pending.pop(state)

    def start(self, provider: str) -> Tuple[str, str]:
        state, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        settings = deepcopy(self._settings[provider])
        destination = settings.get_idp_sso_url()
        settings.get_idp_data()["singleSignOnService"]["url"] = escape(destination, {'"': "&quot;"})
        request = self._authn_request(settings, set_nameid_policy=False)
        location = self._utils.redirect(destination, {"SAMLRequest": request.get_request(), "RelayState": state})
        self._stash(state, provider=provider, request_id=request.get_id(), cookie=cookie)
        return location, cookie

    def verify(self, provider: str, encoded: str, request_id: str) -> Dict[str, Any]:
        settings = self._settings[provider]
        p = self.providers[provider]
        sp = settings.get_sp_data()
        acs = sp["assertionConsumerService"]["url"]
        ns = {"saml": _SAML, "ds": _DS}
        error = "Invalid SAML response. Please try again."
        try:
            response = self._response(settings, encoded)
            document = response.document
            for signature in document.findall(".//ds:Signature", ns):
                references = signature.findall("ds:SignedInfo/ds:Reference", ns)
                signed_id = signature.getparent().get("ID")
                if (len(references) != 1 or not signed_id or references[0].get("URI") != "#" + signed_id
                        or any(
                            t.get("Algorithm") not in _REFERENCE_TRANSFORMS
                            for t in references[0].findall("ds:Transforms/ds:Transform", ns)
                        )):
                    raise SAMLError(error)
            if not response.is_valid(self._request_data(provider), request_id=request_id):
                raise SAMLError(error)
            if document.get("InResponseTo") != request_id or document.get("Destination") != acs:
                raise SAMLError(error)
            assertion = document.find("saml:Assertion", ns)
            if assertion.findtext("saml:Issuer", namespaces=ns) != p["idp_entity_id"]:
                raise SAMLError(error)
            conditions = assertion.find("saml:Conditions", ns)
            restrictions = conditions.findall("saml:AudienceRestriction", ns)
            if not restrictions or any(
                sp["entityId"] not in [a.text for a in restriction.findall("saml:Audience", ns)]
                for restriction in restrictions
            ):
                raise SAMLError(error)
            now = self._utils.now()
            expires = conditions.get("NotOnOrAfter")
            if expires and self._utils.parse_SAML_to_time(expires) <= now:
                raise SAMLError(error)
            confirmations = assertion.findall("saml:Subject/saml:SubjectConfirmation", ns)
            if not any(
                confirmation.get("Method") == "urn:oasis:names:tc:SAML:2.0:cm:bearer"
                and data.get("InResponseTo") == request_id
                and data.get("Recipient") == acs
                and data.get("NotBefore") is None
                and data.get("NotOnOrAfter")
                and self._utils.parse_SAML_to_time(data.get("NotOnOrAfter")) > now
                for confirmation in confirmations
                for data in confirmation.findall("saml:SubjectConfirmationData", ns)
            ):
                raise SAMLError(error)

            attributes = response.get_attributes()
            email = _attribute(attributes, p["email_attribute"])
            name = _attribute(attributes, p.get("name_attribute"))
            if "subject_attribute" in p:
                subject = _attribute(attributes, p["subject_attribute"])
            else:
                if response.get_nameid_format() == "urn:oasis:names:tc:SAML:2.0:nameid-format:transient":
                    raise SAMLError(error)
                subject = response.get_nameid()
            if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+", email) or not subject:
                raise SAMLError(error)
            return dict(email=email.lower(), name=name, issuer=p["idp_entity_id"], subject=subject)
        except self._response_errors:
            raise SAMLError(error) from None


def _attribute(attributes: Dict[str, Any], key: Optional[str]) -> Optional[str]:
    values = attributes.get(key, ())
    return values[0].strip() if len(values) == 1 else None
