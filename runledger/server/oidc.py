"""OpenID Connect sign-in for the team dashboard, using only the standard library.

The browser starts an Authorization Code flow with PKCE S256, state and nonce. Pending
flows live in memory for ten minutes and are bound to a separate SameSite=Lax cookie;
consuming state removes it before any provider request. The final dashboard session
uses the existing Strict cookie, not this pre-authentication cookie.

Discovery pins the configured issuer. Token exchange uses client_secret_post over
urllib, and ID tokens require RS256 with a full PKCS#1 v1.5 encoded-message comparison.
Discovery and JWKS are cached; an unknown key id triggers one JWKS refresh. Token
errors are deliberately generic so credentials never reach pages or request logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class OIDCError(ValueError):
    """A configuration or sign-in failure safe to show without provider credentials."""


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise OIDCError("OIDC URLs must be strings.")
    parsed = urlsplit(value)
    if (
        (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
            )
        )
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise OIDCError("OIDC URLs require HTTPS (HTTP is allowed only on loopback).")
    return value


def validate_public_url(value: str) -> None:
    """Reject an unusable dashboard base URL before accepting OIDC requests."""
    try:
        _url(value)
    except (ValueError, TypeError):
        raise OIDCError(
            "OIDC requires a valid public URL; set public_url or RUNLEDGER_PUBLIC_URL."
        ) from None


class Client:
    """Configured providers, cached metadata and single-use pre-authentication state."""

    def __init__(self, path: Union[str, Path]) -> None:
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
            if (
                not isinstance(config, dict)
                or not isinstance(config.get("providers"), list)
                or not config["providers"]
            ):
                raise OIDCError("OIDC config requires a non-empty providers array.")
            self.public_url = (
                _url(config["public_url"]).rstrip("/") if config.get("public_url") else None
            )
            self.disable_key_sign_in = config.get("disable_key_sign_in", False)
            if not isinstance(self.disable_key_sign_in, bool):
                raise OIDCError("disable_key_sign_in must be boolean.")
            self.providers: Dict[str, Dict[str, Any]] = {}
            for raw in config["providers"]:
                p = dict(raw)
                for field in ("id", "name", "issuer", "client_id"):
                    if not isinstance(p.get(field), str) or not p[field]:
                        raise OIDCError(
                            "Each OIDC provider requires id, name, issuer and client_id strings."
                        )
                if not re.fullmatch(r"[A-Za-z0-9_-]+", p["id"]) or p["id"] in self.providers:
                    raise OIDCError("OIDC provider ids must be unique URL-safe identifiers.")
                _url(p["issuer"])
                if "client_secret_env" in p:
                    if not isinstance(p["client_secret_env"], str) or not p["client_secret_env"]:
                        raise OIDCError("client_secret_env must name an environment variable.")
                    p["client_secret"] = os.environ.get(p["client_secret_env"])
                if not isinstance(p.get("client_secret"), str) or not p["client_secret"]:
                    raise OIDCError(
                        "OIDC provider requires a client_secret or populated client_secret_env."
                    )
                p.setdefault("email_claim", "email")
                p.setdefault("require_email_verified", True)
                if (
                    not isinstance(p["require_email_verified"], bool)
                    or not isinstance(p["email_claim"], str)
                    or not p["email_claim"]
                ):
                    raise OIDCError("Invalid email_claim or require_email_verified.")
                domains = p.get("allowed_email_domains", [])
                if not isinstance(domains, list) or any(
                    not isinstance(d, str) or not d or "@" in d for d in domains
                ):
                    raise OIDCError("allowed_email_domains must be an array of domain strings.")
                if "hosted_domain" in p and (
                    not isinstance(p["hosted_domain"], str) or not p["hosted_domain"]
                ):
                    raise OIDCError("hosted_domain must be a non-empty string.")
                self.providers[p["id"]] = p
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise OIDCError("Invalid OIDC configuration: " + str(exc)) from None
        self._discovery: Dict[str, Dict[str, Any]] = {}
        self._jwks: Dict[str, Dict[str, Any]] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _json(self, url: str, data: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        try:
            request = Request(
                url,
                data=urlencode(data).encode() if data else None,
                headers={"Accept": "application/json"},
            )
            with urlopen(request, timeout=10) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError()
            return json.loads(body)
        except Exception:
            raise OIDCError("Identity provider request failed.") from None

    def discovery(self, provider: str) -> Dict[str, Any]:
        """Return cached discovery metadata after checking the configured issuer."""
        p = self.providers[provider]
        if provider not in self._discovery:
            doc = self._json(p["issuer"].rstrip("/") + "/.well-known/openid-configuration")
            if doc.get("issuer") != p["issuer"]:
                raise OIDCError("Identity provider issuer mismatch.")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                _url(doc.get(key))
            self._discovery[provider] = doc
        return self._discovery[provider]

    def stash(self, value: Mapping[str, Any]) -> str:
        """Store a cookie-bound flow or team choice for up to ten minutes."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if v["expires"] > now}
            if len(self._pending) >= 10000:
                raise OIDCError("Too many pending sign-ins; try again later.")
            self._pending[token] = dict(value, expires=now + 600)
        return token

    def consume(self, state: str, cookie: Optional[str]) -> Dict[str, Any]:
        """Remove and return a live pending flow only for its original browser."""
        with self._lock:
            value = self._pending.pop(state, None)
        if (
            not value
            or value["expires"] < time.time()
            or not cookie
            or not hmac.compare_digest(value["cookie"], cookie)
        ):
            raise OIDCError("Sign-in expired or invalid state. Please try again.")
        return value

    def start(self, provider: str, redirect_uri: str) -> Tuple[str, str]:
        """Return the authorization URL and the browser's pre-authentication cookie."""
        p = self.providers[provider]
        doc = self.discovery(provider)
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(32)
        cookie = secrets.token_urlsafe(32)
        state = self.stash(
            dict(
                provider=provider,
                verifier=verifier,
                nonce=nonce,
                cookie=cookie,
                redirect_uri=redirect_uri,
            )
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        fields = dict(
            response_type="code",
            client_id=p["client_id"],
            redirect_uri=redirect_uri,
            scope="openid email profile",
            state=state,
            nonce=nonce,
            code_challenge=challenge,
            code_challenge_method="S256",
        )
        if p.get("hosted_domain"):
            fields["hd"] = p["hosted_domain"]
        return doc["authorization_endpoint"] + "?" + urlencode(fields), cookie

    def exchange(self, pending: Mapping[str, Any], code: str) -> Dict[str, str]:
        """Exchange a code and return verified identity claims, never raw tokens."""
        p = self.providers[pending["provider"]]
        doc = self.discovery(pending["provider"])
        result = self._json(
            doc["token_endpoint"],
            dict(
                grant_type="authorization_code",
                code=code,
                redirect_uri=pending["redirect_uri"],
                code_verifier=pending["verifier"],
                client_id=p["client_id"],
                client_secret=p["client_secret"],
            ),
        )
        return self.verify(pending["provider"], result.get("id_token", ""), pending["nonce"])

    def verify(self, provider: str, token: str, nonce: str) -> Dict[str, str]:
        """Verify the RSA signature, registered claims and provider email policy."""
        try:
            p = self.providers[provider]
            header64, payload64, signature64 = token.split(".")
            header = json.loads(_decode(header64))
            claims = json.loads(_decode(payload64))
            if (
                header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or header.get("crit")
            ):
                raise ValueError()
            uri = self.discovery(provider)["jwks_uri"]
            key = None
            for refresh in (False, True):
                if refresh or provider not in self._jwks:
                    self._jwks[provider] = self._json(uri)
                key = next(
                    (k for k in self._jwks[provider]["keys"] if k.get("kid") == header["kid"]), None
                )
                if key:
                    break
            if (
                not key
                or key.get("kty") != "RSA"
                or key.get("use", "sig") != "sig"
                or key.get("alg", "RS256") != "RS256"
            ):
                raise ValueError()
            n = int.from_bytes(_decode(key["n"]), "big")
            e = int.from_bytes(_decode(key["e"]), "big")
            size = (n.bit_length() + 7) // 8
            sig = _decode(signature64)
            if size < 256 or len(sig) != size or int.from_bytes(sig, "big") >= n or e < 3:
                raise ValueError()
            digest = (
                bytes.fromhex("3031300d060960864801650304020105000420")
                + hashlib.sha256((header64 + "." + payload64).encode("ascii")).digest()
            )
            expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
            actual = pow(int.from_bytes(sig, "big"), e, n).to_bytes(size, "big")
            if not hmac.compare_digest(expected, actual):
                raise ValueError()
            aud = claims.get("aud")
            aud = [aud] if isinstance(aud, str) else aud
            if (
                not isinstance(aud, list)
                or p["client_id"] not in aud
                or (len(aud) > 1 and claims.get("azp") != p["client_id"])
            ):
                raise ValueError()
            now = time.time()
            for field in ("exp", "iat", "nbf"):
                if field == "nbf" and field not in claims:
                    continue
                value = claims[field]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not math.isfinite(value)
                ):
                    raise ValueError()
                if (field == "exp" and value < now - 60) or (field != "exp" and value > now + 60):
                    raise ValueError()
            if (
                claims.get("iss") != p["issuer"]
                or claims.get("nonce") != nonce
                or not isinstance(claims.get("sub"), str)
                or not claims["sub"]
            ):
                raise ValueError()
            email = claims.get(p["email_claim"])
            if (
                not isinstance(email, str)
                or email.count("@") != 1
                or not all(email.split("@"))
                or any(c.isspace() for c in email)
            ):
                raise ValueError()
            if p["require_email_verified"] and claims.get("email_verified") is not True:
                raise OIDCError("The identity provider did not verify your email address.")
            if p.get("allowed_email_domains") and email.rsplit("@", 1)[1].lower() not in [
                d.lower() for d in p["allowed_email_domains"]
            ]:
                raise OIDCError("Your email domain is not allowed.")
            if p.get("hosted_domain") and claims.get("hd") != p["hosted_domain"]:
                raise OIDCError("Your Google Workspace domain is not allowed.")
            return dict(email=email, issuer=p["issuer"], subject=claims["sub"])
        except OIDCError:
            raise
        except Exception:
            raise OIDCError("Invalid identity token.") from None
