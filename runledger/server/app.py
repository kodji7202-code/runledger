"""HTTP server for the RunLedger team dashboard and the push API.

  POST /api/runs                              push a receipt -> 201 (admin, member)
  GET  /api/runs[?user&project&agent&min_risk&min_quality&max_quality&ai_verdict&limit&after]  list runs (any role)
  GET  /api/runs/<id>                         one run with its receipt JSON and risk reasons
  GET  /api/stats[?days=30]                   team totals; cost by developer, model, project, agent
  GET  /api/insights[?days=30]                quality, cost by model and agent, recommendations, AI review (any role)
  GET  /runs/<id>                             the stored receipt HTML
  POST /api/approvals                         request a human approval -> 201 {id, status} (admin, member)
  GET  /api/approvals[?status=pending]        list approvals (any role)
  GET  /api/approvals/<id>                    one approval: status, decided_by, decided_at, reason
  POST /api/approvals/<id>/decision           approve or deny (admin)
  GET  /approvals/<id>                        approval page for a person (any role)
  GET  /api/team/settings                     webhook URLs and approval_ttl_s (any role; URLs masked unless admin)
  PUT  /api/team/settings                     change them (admin)
  GET  /api/me                                the caller's team, role and key (any role)
  GET  /api/keys                              the team's keys, never their secrets (admin)
  POST /api/keys                              {label, role, kind?, expires_in_days?, user?} -> 201 (admin)
  GET  /api/members                           team members (admin)
  POST /api/members                           {email, name?, role} -> 201 (admin)
  PATCH /api/members/<user_id>                 {role} (admin)
  DELETE /api/members/<user_id>                remove member and revoke their team keys (admin)
  POST /api/keys/<id>/revoke                  revoke a key (admin)
  POST /api/keys/<id>/rotate                  new secret for a key, same id, label and role (admin)
  GET  /api/audit[?limit&before&action]       audit log, newest first; action= keeps a prefix, e.g. key. (admin)
  GET  /api/budgets                           team budget (any role)
  PUT  /api/budgets                           replace it (admin); alerts at 50/80/100% unless set otherwise
  GET  /api/budgets/status                    this month's spend and percent of budget (any role)
  GET  /api/export/runs.csv[?days=30]         runs as CSV (admin); HEAD returns headers only
  GET  /api/export/audit.csv[?days=30]        audit log as CSV (admin)
  GET  /api/export/report.html[?days=30]      printable compliance report (admin)
  GET  /                                      dashboard; sign in with the form, which posts the key
  POST /session                               sign in: form field "key" -> session cookie (302);
                                              from another origin (Origin/Sec-Fetch-Site) -> 403
  GET  /auth/oidc/<provider>/start             start Authorization Code + PKCE sign-in
  GET  /auth/oidc/<provider>/callback          validate identity, choose team or issue cookie
  POST /auth/oidc/team                        single-use, cookie-bound team choice
  GET/POST/DELETE /api/scim/token              administer the team's dedicated SCIM token
  /scim/v2/{Users,Groups}                     SCIM provisioning (dedicated bearer token only)
  /scim/v2/{ServiceProviderConfig,ResourceTypes,Schemas}  SCIM discovery
  GET  /auth/saml/<provider>/metadata         SP metadata
  GET  /auth/saml/<provider>/start            start SP-initiated sign-in
  POST /auth/saml/<provider>/acs              validate SAML response and sign in
  POST /auth/saml/team                        single-use, cookie-bound team choice
  POST /session/logout                        sign out of the current dashboard session (204)
  GET  /sessions                              list active sessions (own key, or whole team for admin)
  DELETE /sessions/<id>                       revoke one session in the same scope (204)
  GET  /?key=API_KEY                          legacy sign-in: redirects to strip the key; never
                                              replaces a session of another team without confirmation
  GET  /health                                liveness, no auth

Credentials are "Authorization: Bearer KEY", or the dashboard session cookie. A cookie
session has the role of the key it was signed in with, and survives a restart. A write
that uses a cookie must also send the header "X-Requested-With: runledger".

A request that needs a role is authenticated before its body is read, so an oversized
unauthenticated body gets 401, not 413, and the connection is closed without reading it.

Behind a reverse proxy, pass --trusted-proxy CIDR (or RUNLEDGER_TRUSTED_PROXIES). A peer in
a trusted range may report the client in X-Forwarded-For: the server then uses the right-most
address that is not itself trusted, for the failure limit and the audit log. X-Forwarded-For
from any other peer is ignored. With --trust-proxy, X-Forwarded-Proto is honoured only from
trusted peers, or from anyone when no trusted proxy is configured (the earlier behaviour).

Errors are JSON ({"error": {"code", "message"}}) and never include tracebacks;
tracebacks go to the server's stderr.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import secrets
import ssl
import sys
import threading
import traceback
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from html import escape as _escape
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import __version__
from ..parser import _ts as _parse_ts
from . import approvals, budgets, exports, insights, maintenance, oidc, saml
from .auth import (
    SESSION_TTL_SECONDS,
    FailureLimiter,
    Identity,
    InvalidKey,
    allows,
    identity_from_row,
    validate_role,
)
from .dashboard import APPROVAL_HTML, DASHBOARD_HTML
from .db import TIME_FORMAT, Database, normalize_missing_costs
from .insights import MAX_COST_USD

MAX_BODY_BYTES = 10 * 1024 * 1024
SIGN_IN_BODY_LIMIT = 4096
SAML_BODY_LIMIT = 256 * 1024
SAML_FLOW_COOKIE = "__Host-rl_saml"
COOKIE_NAME = "rl_session"
LEVELS = ("low", "medium", "high")
SEVERITIES = ("low", "medium", "high")
# How the agent reported token usage (see the Droid adapter); anything else reads as unknown.
USAGE_SOURCES = ("messages", "settings", "missing")
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "runledger"
BACKGROUND_HEADER = "X-RunLedger-Background"
# Sec-Fetch-Site values that mean the request comes from another origin. "same-site" is another
# origin by definition (another port or scheme, or a subdomain), so a sign-in POST is refused too.
CROSS_ORIGIN_FETCH_SITES = ("cross-site", "same-site")
PUBLIC_URL_ENV = "RUNLEDGER_PUBLIC_URL"
SECURE_COOKIES_ENV = "RUNLEDGER_SECURE_COOKIES"
TRUSTED_PROXIES_ENV = "RUNLEDGER_TRUSTED_PROXIES"
SESSION_IDLE_TIMEOUT_ENV = "RUNLEDGER_SESSION_IDLE_TIMEOUT"
HSTS_VALUE = "max-age=31536000"
MAINTENANCE_INTERVAL_S = 3600
KEY_EXPIRES_HEADER = "X-RunLedger-Key-Expires"
EXPORT_ROUTES = {
    "/api/export/runs.csv": "runs",
    "/api/export/audit.csv": "audit",
    "/api/export/report.html": "report",
}
_IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
_APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

RECEIPT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"
)
SIGN_IN_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
SIGN_IN_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RunLedger sign in</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:48px 16px;background:#0a0b0d;color:#eef0f2}
@media (prefers-color-scheme: light){body{background:#f7f8f9;color:#121417}}
main{max-width:560px;margin:0 auto}code{font-family:ui-monospace,monospace;background:rgba(127,127,127,.18);padding:2px 6px;border-radius:6px}
form{display:flex;gap:8px;margin:18px 0}input{flex:1;padding:9px 11px;border:1px solid rgba(127,127,127,.4);border-radius:8px;background:transparent;color:inherit;font:inherit}
button,.sso{display:inline-block;padding:9px 16px;border:0;border-radius:8px;background:#3b82f6;color:#fff;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
.error{color:#e5484d;font-weight:600}</style>
</head><body><main>
"""
SIGN_IN_HTML = SIGN_IN_HEAD + """
<h1 style="font-size:20px">RunLedger team dashboard</h1>
__ERROR__
__SSO_BUTTONS__
__KEY_FORM__
</main></body></html>
"""
SIGN_IN_KEY_FORM = """
<p>Sign in with a team API key. The server then sets a session cookie, and the key is never put in the address bar.</p>
<form method="post" action="/session">
<label for="key" style="position:absolute;left:-9999px">API key</label>
<input id="key" name="key" type="password" autocomplete="off" required>
<button type="submit">Sign in</button>
</form>
<p>Create a team and its key with <code>runledger team create NAME</code>. Make more keys with <code>runledger key create</code>.</p>
"""
SSO_TEAM_HTML = SIGN_IN_HEAD + """
<h1 style="font-size:20px">Choose a team</h1>
<form method="post" action="/auth/__METHOD__/team">
<input type="hidden" name="state" value="__STATE__">__TEAMS__
</form>
</main></body></html>
"""
SWITCH_TEAM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Switch team · RunLedger</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:48px 16px;background:#0a0b0d;color:#eef0f2}
@media (prefers-color-scheme: light){body{background:#f7f8f9;color:#121417}}
main{max-width:560px;margin:0 auto}a{color:#3b82f6}
button{padding:9px 16px;border:0;border-radius:8px;background:#3b82f6;color:#fff;font:inherit;font-weight:600;cursor:pointer;margin:18px 0}</style>
</head><body><main>
<h1 style="font-size:20px">Switch to team __TO__?</h1>
<p>This browser is already signed in to team <b>__FROM__</b>. The link you opened carries a key for team <b>__TO__</b>.</p>
<form method="post" action="/session">
<input type="hidden" name="key" value="__KEY__">
<button type="submit">Switch to __TO__</button>
</form>
<p><a href="/">Stay signed in to __FROM__</a></p>
</main></body></html>
"""


class TLSConfigError(ValueError):
    """The TLS certificate or key cannot be used. The message is safe to print."""


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def _unauthorized() -> _HttpError:
    return _HttpError(
        401, "unauthorized",
        "Missing or invalid API key. Send 'Authorization: Bearer <key>', or sign in to the dashboard.",
        {"WWW-Authenticate": 'Bearer realm="RunLedger"'},
    )


def _not_found(message: str = "Run not found.") -> _HttpError:
    return _HttpError(404, "not_found", message)


def make_tls_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    """A server-side TLS context: TLS 1.2 or newer, with the given PEM certificate and key."""
    for path in (cert_file, key_file):
        if not os.path.isfile(path):
            raise TLSConfigError(f"file not found: {path}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    except (OSError, ssl.SSLError) as exc:
        raise TLSConfigError(f"cannot load the certificate and key: {exc}") from None
    return context


class RunLedgerServer:
    def __init__(
        self,
        address: Tuple[str, int],
        db: Database,
        log_requests: bool = False,
        public_url: Optional[str] = None,
        tls_context: Optional[ssl.SSLContext] = None,
        secure_cookies: bool = False,
        trust_proxy: bool = False,
        trusted_proxies: Sequence[_IPNetwork] = (),
        retention_days: Optional[int] = None,
        audit_retention_days: Optional[int] = None,
        oidc_client: Optional[oidc.Client] = None,
        saml_client: Optional[saml.Client] = None,
        session_idle_timeout: int = 0,
    ) -> None:
        self.db = db
        self.oidc = oidc_client
        self.saml = saml_client
        self.log_requests = log_requests
        self.tls_context = tls_context
        self.secure_cookies = secure_cookies
        self.trust_proxy = trust_proxy
        self.trusted_proxies: Tuple[_IPNetwork, ...] = tuple(trusted_proxies)
        self.limiter = FailureLimiter()
        self.retention_days = retention_days
        self.audit_retention_days = audit_retention_days
        self.session_idle_timeout = session_idle_timeout
        self._query_key_warned = False
        self._query_key_lock = threading.Lock()
        self._maintenance_stop = threading.Event()
        from .asgi import UvicornServer, create_app

        self._uvicorn = UvicornServer(address, create_app(self), tls_context=tls_context)
        self.server_address = self._uvicorn.server_address
        # The base URL people see in approval links. The default is the bound address,
        # so set RUNLEDGER_PUBLIC_URL when the server sits behind a reverse proxy.
        host, port = self.server_address[:2]
        shown = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in shown:
            shown = f"[{shown}]"
        scheme = "https" if tls_context is not None else "http"
        chosen = public_url or os.environ.get(PUBLIC_URL_ENV) or f"{scheme}://{shown}:{port}"
        self.public_url = chosen.strip().rstrip("/")
        try:
            if self.oidc:
                oidc.validate_public_url(self.public_url)
            if self.saml:
                self.saml.bind(self.public_url)
        except ValueError:
            self.server_close()
            raise

    @property
    def dashboard_key_sign_in_disabled(self) -> bool:
        return any(client.disable_key_sign_in for client in (self.oidc, self.saml) if client is not None)

    def serve_forever(self) -> None:
        self._uvicorn.serve_forever()

    def shutdown(self) -> None:
        self._uvicorn.shutdown()

    def start_maintenance(self, interval_s: float = MAINTENANCE_INTERVAL_S) -> bool:
        """Apply retention now and then every interval_s, on a daemon thread, until
        server_close(). Does nothing (returns False) when neither retention period is set."""
        if not self.retention_days and not self.audit_retention_days:
            return False

        def loop() -> None:
            while True:
                try:
                    removed = {
                        k: v for k, v in maintenance.run_maintenance(
                            self.db, self.retention_days, audit_days=self.audit_retention_days,
                        ).items() if v
                    }
                    if removed:
                        sys.stderr.write(f"RunLedger: maintenance removed {removed}\n")
                except Exception:
                    traceback.print_exc()
                if self._maintenance_stop.wait(interval_s):
                    return

        threading.Thread(target=loop, name="runledger-maintenance", daemon=True).start()
        return True

    def start_key_expiry_warnings(self, interval_s: float = MAINTENANCE_INTERVAL_S) -> None:
        """Warn about keys entering their team's warning window, now and then every interval_s,
        on a daemon thread, until server_close(). Each key is warned about once per lifetime."""

        def loop() -> None:
            while True:
                try:
                    count = maintenance.warn_expiring_keys(self.db)
                    if count:
                        sys.stderr.write(f"RunLedger: {count} API key(s) expire soon; see the audit log\n")
                except Exception:
                    traceback.print_exc()
                if self._maintenance_stop.wait(interval_s):
                    return

        threading.Thread(target=loop, name="runledger-key-expiry", daemon=True).start()

    def server_close(self) -> None:
        self._maintenance_stop.set()
        self._uvicorn.server_close()

    def secure_for(self, forwarded_proto: Optional[str], peer: str) -> bool:
        """Whether this exchange is secure: TLS on this server, --secure-cookies, or a reverse
        proxy that reports https. It sets the cookie's Secure flag and HSTS. With trusted proxies
        configured, the report is honoured only from one of them; with none configured, from anyone."""
        if self.tls_context is not None or self.secure_cookies:
            return True
        if not self.trust_proxy or _first_value(forwarded_proto) != "https":
            return False
        return not self.trusted_proxies or self.is_trusted(peer)

    def is_trusted(self, address: str) -> bool:
        ip = _ip_or_none(address)
        return ip is not None and _in_any(ip, self.trusted_proxies)

    def client_ip(self, peer: str, forwarded_for: Optional[str]) -> str:
        """The address for the failure limit and the audit log.

        Only a peer inside a trusted range may say who the client is. The client is then the
        right-most X-Forwarded-For address that is not trusted: a proxy appends the address it
        saw, so entries to the left of it were written by the client and cannot be trusted. A
        malformed chain, an untrusted peer, or no header all give the peer address itself."""
        if not self.trusted_proxies or not self.is_trusted(peer):
            return peer
        hops = [hop.strip() for hop in (forwarded_for or "").split(",") if hop.strip()]
        if not hops:
            return peer
        addresses: List[Any] = []
        for hop in hops:
            ip = _ip_or_none(hop)
            if ip is None:
                return peer
            addresses.append(ip)
        for ip in reversed(addresses):
            if not _in_any(ip, self.trusted_proxies):
                return str(ip)
        return str(addresses[0])  # every hop is a trusted proxy: the left-most is the client

    def identify_key(self, token: str) -> Optional[Identity]:
        row = self.db.key_for_token(token)
        return identity_from_row(row, "key") if row else None

    def identify_session(self, token: Optional[str], touch: bool = True) -> Optional[Identity]:
        """The identity behind a dashboard cookie. The session is stored in the database, so it
        survives a restart, and it ends when its key is revoked or rotated (see db.session_key)."""
        row = self.db.session_key(token, idle_timeout=self.session_idle_timeout, touch=touch)
        return identity_from_row(row, "session") if row else None

    def warn_query_key_once(self) -> None:
        """Write the ?key= deprecation warning once per server process. Never the key."""
        with self._query_key_lock:
            if self._query_key_warned:
                return
            self._query_key_warned = True
        sys.stderr.write(
            "warning: signing in with '?key=' in the URL is deprecated; "
            "use the sign-in form (POST /session) instead.\n"
        )


def parse_trusted_proxies(values: Iterable[str]) -> Tuple[_IPNetwork, ...]:
    """Turn "10.0.0.0/8", "203.0.113.5" or "2001:db8::/32" into networks. Raises ValueError."""
    networks: List[_IPNetwork] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            raise ValueError(
                f"not an IP address or CIDR range: {text!r} (for example 10.0.0.0/8 or 203.0.113.5)"
            ) from None
    return tuple(networks)


def make_server(
    db_path: str = "runledger.db",
    host: str = "127.0.0.1",
    port: int = 8787,
    log_requests: bool = False,
    public_url: Optional[str] = None,
    tls_cert: Optional[str] = None,
    tls_key: Optional[str] = None,
    secure_cookies: bool = False,
    trust_proxy: bool = False,
    trusted_proxies: Optional[Sequence[str]] = None,
    retention_days: Optional[int] = None,
    audit_retention_days: Optional[int] = None,
    oidc_config: Optional[str] = None,
    saml_config: Optional[str] = None,
    session_idle_timeout: Optional[int] = None,
) -> RunLedgerServer:
    """Open the database and bind the server. Port 0 picks a free port (see server_address).
    The caller owns the server: call shutdown(), server_close() and db.close() when done.

    public_url defaults to $RUNLEDGER_PUBLIC_URL, then to the bound address. tls_cert and
    tls_key (PEM files) turn on HTTPS. secure_cookies, or $RUNLEDGER_SECURE_COOKIES=1, sets
    the Secure flag on the session cookie. trust_proxy honours X-Forwarded-Proto from a
    reverse proxy the same way. trusted_proxies (and $RUNLEDGER_TRUSTED_PROXIES, comma
    separated) are the proxy addresses or CIDR ranges whose X-Forwarded-For is believed.
    retention_days defaults to RUNLEDGER_RETENTION_DAYS (unset keeps all history, and a value
    below the 35-day floor is raised to it with a warning). audit_retention_days defaults to
    RUNLEDGER_AUDIT_RETENTION_DAYS: unset keeps audit events for 365 days while runs are being
    pruned, and 0 keeps them forever.
    session_idle_timeout defaults to RUNLEDGER_SESSION_IDLE_TIMEOUT, in seconds (0 disables it).
    Raises ValueError for an invalid address, range or retention setting before the
    database is opened."""
    if bool(tls_cert) != bool(tls_key):
        raise TLSConfigError("--tls-cert and --tls-key must be given together.")
    networks = parse_trusted_proxies(list(trusted_proxies or []) + _env_list(TRUSTED_PROXIES_ENV))
    if retention_days is None:
        retention_days = maintenance.retention_from_env()
    if audit_retention_days is None:
        audit_retention_days = maintenance.audit_retention_from_env()
    idle_error = f"{SESSION_IDLE_TIMEOUT_ENV} / --session-idle-timeout must be a non-negative integer in seconds."
    if session_idle_timeout is None:
        try:
            session_idle_timeout = int(os.environ.get(SESSION_IDLE_TIMEOUT_ENV, "").strip() or "0")
        except ValueError:
            raise ValueError(idle_error) from None
    if session_idle_timeout < 0:
        raise ValueError(idle_error)
    tls_context = make_tls_context(tls_cert, tls_key) if tls_cert else None
    secure = bool(secure_cookies) or _env_flag(SECURE_COOKIES_ENV)
    config_path = oidc_config or os.environ.get("RUNLEDGER_OIDC_CONFIG")
    oidc_client = oidc.Client(config_path) if config_path else None
    if oidc_client and oidc_client.public_url:
        public_url = oidc_client.public_url
    saml_path = saml_config or os.environ.get("RUNLEDGER_SAML_CONFIG")
    saml_client = saml.Client(saml_path) if saml_path else None
    db = Database(db_path)
    try:
        return RunLedgerServer(
            (host, port), db, log_requests=log_requests, public_url=public_url,
            tls_context=tls_context, secure_cookies=secure, trust_proxy=bool(trust_proxy),
            trusted_proxies=networks, retention_days=retention_days,
            audit_retention_days=audit_retention_days,
            oidc_client=oidc_client,
            saml_client=saml_client,
            session_idle_timeout=session_idle_timeout,
        )
    except Exception:
        db.close()
        raise


class _Handler(BaseHTTPRequestHandler):
    server_version = "RunLedger"
    sys_version = ""
    timeout = 30  # a stalled client cannot hold a thread forever

    def setup(self) -> None:
        super().setup()
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()  # runs under the timeout above

    def do_GET(self) -> None:
        self._route("GET")

    def do_HEAD(self) -> None:
        self._route("HEAD")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_PATCH(self) -> None:
        self._route("PATCH")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    # Logging: never write query strings, because ?key= carries a secret.

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def log_error(self, format: str, *args: Any) -> None:
        pass

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        if self.server.log_requests:
            command = getattr(self, "command", "-") or "-"
            path = urlsplit(getattr(self, "path", "")).path or "-"
            sys.stderr.write(f"{command} {path} {code}\n")

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        # The stdlib calls this for malformed requests; keep the reply JSON.
        kind = "bad_request" if code < 500 else "server_error"
        self._send_json(code, {"error": {"code": kind, "message": message or "Request failed."}})

    # Routing

    def _route(self, method: str) -> None:
        self._extra_headers: Dict[str, str] = {}
        try:
            url = urlsplit(self.path)
            path = url.path
            query = parse_qs(url.query, keep_blank_values=True)
            if method == "HEAD" and path not in EXPORT_ROUTES:
                method = "GET"  # HEAD answers like GET without the body; exports handle HEAD themselves
            if path == "/health":
                self._only(method, ("GET",))
                self._send_json(200, {"ok": True, "version": __version__})
                return
            self._check_rate_limit()
            if path == "/scim/v2" or path.startswith("/scim/v2/") or path == "/api/scim/token":
                from .scim import dispatch
                dispatch(self, method, path, query)
                return
            self._dispatch(method, path, query)
        except _HttpError as exc:
            if urlsplit(self.path).path.startswith("/scim/v2"):
                from .scim import Error, error
                error(self, Error(exc.status, exc.message), exc.headers)
            else:
                self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, exc.headers)
        except Exception:  # never leak internals to the client
            traceback.print_exc()
            if urlsplit(self.path).path.startswith("/scim/v2"):
                from .scim import Error, error
                error(self, Error(500, "Internal server error."))
            else:
                self._send_json(500, {"error": {"code": "internal_error", "message": "Internal server error."}})

    def _dispatch(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        if path.startswith("/auth/oidc/"):
            self._oidc_route(method, path, query)
        elif path.startswith("/auth/saml/"):
            self._saml_route(method, path)
        elif path == "/":
            self._only(method, ("GET",))
            self._dashboard(query)
        elif path == "/session":
            self._only(method, ("POST",))
            self._session_post()
        elif path == "/session/logout":
            self._only(method, ("POST",))
            self._session_logout()
        elif path == "/sessions":
            self._only(method, ("GET",))
            self._list_sessions()
        elif path.startswith("/sessions/"):
            self._only(method, ("DELETE",))
            self._revoke_session(path[len("/sessions/"):])
        elif path == "/api/me":
            self._only(method, ("GET",))
            self._me()
        elif path == "/api/runs":
            self._only(method, ("GET", "POST"))
            if method == "POST":
                self._create_run()
            else:
                self._list_runs(query)
        elif path == "/api/stats":
            self._only(method, ("GET",))
            self._stats(query)
        elif path == "/api/insights":
            self._only(method, ("GET",))
            self._insights(query)
        elif path.startswith("/api/runs/"):
            self._only(method, ("GET",))
            self._get_run(path[len("/api/runs/"):])
        elif path.startswith("/runs/"):
            self._only(method, ("GET",))
            self._receipt(path[len("/runs/"):])
        elif path == "/api/approvals":
            self._only(method, ("GET", "POST"))
            if method == "POST":
                self._create_approval()
            else:
                self._list_approvals(query)
        elif path.startswith("/api/approvals/"):
            rest = path[len("/api/approvals/"):]
            if rest.endswith("/decision"):
                self._only(method, ("POST",))
                self._decide_approval(rest[:-len("/decision")])
            else:
                self._only(method, ("GET",))
                self._get_approval(rest)
        elif path == "/api/team/settings":
            self._only(method, ("GET", "PUT"))
            self._team_settings(method)
        elif path.startswith("/approvals/"):
            self._only(method, ("GET",))
            self._approval_page(path[len("/approvals/"):])
        elif path == "/api/keys":
            self._only(method, ("GET", "POST"))
            if method == "POST":
                self._create_key()
            else:
                self._list_keys()
        elif path == "/api/members":
            self._only(method, ("GET", "POST"))
            self._members(method)
        elif path.startswith("/api/members/"):
            self._only(method, ("PATCH", "DELETE"))
            self._members(method, _key_id(path[len("/api/members/"):]))
        elif path.startswith("/api/keys/"):
            rest = path[len("/api/keys/"):]
            if rest.endswith("/revoke"):
                self._only(method, ("POST",))
                self._revoke_key(rest[:-len("/revoke")])
            elif rest.endswith("/rotate"):
                self._only(method, ("POST",))
                self._rotate_key(rest[:-len("/rotate")])
            else:
                raise _HttpError(404, "not_found", "No such endpoint.")
        elif path == "/api/audit":
            self._only(method, ("GET",))
            self._audit_log(query)
        elif path == "/api/budgets":
            self._only(method, ("GET", "PUT"))
            self._budgets(method)
        elif path == "/api/budgets/status":
            self._only(method, ("GET",))
            self._budget_status()
        elif path in EXPORT_ROUTES:
            self._only(method, ("GET", "HEAD"))
            self._export(EXPORT_ROUTES[path], query, head=method == "HEAD")
        else:
            raise _HttpError(404, "not_found", "No such endpoint.")

    def _only(self, method: str, allowed: Tuple[str, ...]) -> None:
        if method not in allowed:
            raise _HttpError(
                405, "method_not_allowed", f"Use {', '.join(allowed)} for this endpoint.",
                {"Allow": ", ".join(allowed)},
            )

    # Responses

    def _secure(self) -> bool:
        headers = getattr(self, "headers", None)
        forwarded = headers.get("X-Forwarded-Proto") if headers is not None else None
        return self.server.secure_for(forwarded, self.client_address[0])

    def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
        self._start(status, content_type, headers, length=len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _start(self, status: int, content_type: str, headers: Optional[Dict[str, str]], length: Optional[int]) -> None:
        """Status line and headers. length=None sends no Content-Length (a HEAD that builds nothing)."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if self._secure():
            self.send_header("Strict-Transport-Security", HSTS_VALUE)
        for name, value in {**getattr(self, "_extra_headers", {}), **(headers or {})}.items():
            self.send_header(name, value)
        self.end_headers()

    def _send_json(self, status: int, obj: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8", headers)

    def _send_html(self, status: int, html: str, headers: Optional[Dict[str, str]] = None) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _sign_in_page(self, error: Optional[str] = None) -> None:
        message = f'<p class="error" role="alert">{_escape(error)}</p>' if error else ""
        buttons = []
        key_form = "" if self.server.dashboard_key_sign_in_disabled else SIGN_IN_KEY_FORM
        if self.server.oidc:
            for provider in self.server.oidc.providers.values():
                buttons.append(
                    f'<p><a class="sso" href="/auth/oidc/{provider["id"]}/start">'
                    f'Sign in with {_escape(provider["name"])}</a></p>'
                )
        if self.server.saml:
            for provider in self.server.saml.providers.values():
                buttons.append(
                    f'<p><a class="sso" href="/auth/saml/{provider["id"]}/start">'
                    f'Sign in with {_escape(provider["name"])}</a></p>'
                )
        page = (
            SIGN_IN_HTML.replace("__ERROR__", message)
            .replace("__SSO_BUTTONS__", "".join(buttons))
            .replace("__KEY_FORM__", key_form)
        )
        self._send_html(401, page, {"Content-Security-Policy": SIGN_IN_CSP})

    def _oidc_route(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        """Handle provider callbacks and cookie-bound, single-use team choices."""
        client = self.server.oidc
        if client is None:
            raise _not_found()
        self._check_rate_limit()
        provider = None
        user_id = None
        try:
            if path == "/auth/oidc/team":
                self._only(method, ("POST",))
                self._refuse_cross_origin_sign_in()
                length = self._content_length(SIGN_IN_BODY_LIMIT, "Team choice")
                fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                pending = client.consume((fields.get("state") or [""])[0], self._cookie("rl_oidc"))
                if "user_id" not in pending:
                    raise oidc.OIDCError("Invalid team choice.")
                user_id = pending["user_id"]
                provider = pending["provider"]
                team_id = int((fields.get("team") or [""])[0])
                if team_id not in [t["id"] for t in self.server.db.user_teams(pending["user_id"])]:
                    raise oidc.OIDCError("Team membership is no longer available.")
                self._sso_sign_in("oidc", user_id, team_id, provider)
                return
            parts = path.split("/")
            if (
                len(parts) != 5
                or parts[3] not in client.providers
                or parts[4] not in ("start", "callback")
            ):
                raise _not_found()
            self._only(method, ("GET",))
            provider, action = parts[3:]
            if action == "start":
                redirect_uri = self.server.public_url + "/auth/oidc/" + provider + "/callback"
                location, cookie = client.start(provider, redirect_uri)
                value = f"rl_oidc={cookie}; Path=/auth/oidc/; HttpOnly; SameSite=Lax; Max-Age=600"
                if self._secure():
                    value += "; Secure"
                self._send(302, b"", "text/plain", {"Location": location, "Set-Cookie": value})
                return
            pending = client.consume((query.get("state") or [""])[0], self._cookie("rl_oidc"))
            if pending["provider"] != provider or "user_id" in pending:
                raise oidc.OIDCError("Invalid sign-in state.")
            code = (query.get("code") or [""])[0]
            if not code or query.get("error"):
                raise oidc.OIDCError("Identity provider denied sign-in.")
            claims = client.exchange(pending, code)
            resolved = self.server.db.get_user(claims["email"])
            user_id = resolved["id"] if resolved else None
            person = self.server.db.oidc_user(**claims)
            self._sso_continue("oidc", client, person["id"], provider, pending["cookie"])
        except (oidc.OIDCError, ValueError) as exc:
            self.server.limiter.record_failure(self._client_ip())
            self.server.db.audit_sso_rejection("oidc", user_id, provider, self._client_ip())
            self._sign_in_page(
                str(exc)
                if isinstance(exc, oidc.OIDCError)
                or str(exc).startswith(("No RunLedger account", "This email", "This identity"))
                else "Invalid sign-in response."
            )

    def _saml_route(self, method: str, path: str) -> None:
        client = self.server.saml
        if client is None:
            raise _not_found()
        provider = None
        user_id = None
        try:
            if path == "/auth/saml/team":
                self._only(method, ("POST",))
                self._refuse_cross_origin_sign_in()
                length = self._content_length(SIGN_IN_BODY_LIMIT, "Team choice")
                fields = parse_qs(self.rfile.read(length).decode("utf-8"))
                pending = client.consume((fields.get("state") or [""])[0], self._cookie(SAML_FLOW_COOKIE))
                if "user_id" not in pending:
                    raise saml.SAMLError("Invalid team choice.")
                user_id = pending["user_id"]
                provider = pending["provider"]
                team_id = int((fields.get("team") or [""])[0])
                self._sso_sign_in("saml", user_id, team_id, provider)
                return
            parts = path.split("/")
            if len(parts) != 5 or parts[3] not in client.providers:
                raise _not_found()
            provider, action = parts[3:]
            if action == "metadata":
                self._only(method, ("GET",))
                self._send(200, client.metadata(provider), "application/samlmetadata+xml")
                return
            if action == "start":
                self._only(method, ("GET",))
                location, cookie = client.start(provider)
                value = f"{SAML_FLOW_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=None; Secure; Max-Age=600"
                self._send(302, b"", "text/plain", {"Location": location, "Set-Cookie": value})
                return
            if action != "acs":
                raise _not_found()
            self._only(method, ("POST",))
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise _HttpError(415, "unsupported_media_type", "SAML responses must be form encoded.")
            length = self._content_length(SAML_BODY_LIMIT, "SAML responses")
            fields = parse_qs(self.rfile.read(length).decode("ascii"), max_num_fields=8)
            state, response = fields.get("RelayState", []), fields.get("SAMLResponse", [])
            if len(state) != 1 or len(response) != 1:
                raise saml.SAMLError("Invalid SAML response. Please try again.")
            pending = client.consume(state[0], self._cookie(SAML_FLOW_COOKIE))
            if pending["provider"] != provider or "request_id" not in pending:
                raise saml.SAMLError("Invalid sign-in state.")
            claims = client.verify(provider, response[0], pending["request_id"])
            resolved = self.server.db.get_user(claims["email"])
            user_id = resolved["id"] if resolved else None
            person = self.server.db.saml_user(**claims)
            self._sso_continue("saml", client, person["id"], provider, pending["cookie"])
        except ValueError as exc:
            self.server.limiter.record_failure(self._client_ip())
            self.server.db.audit_sso_rejection("saml", user_id, provider, self._client_ip())
            self._sign_in_page(
                str(exc) if isinstance(exc, saml.SAMLError)
                or str(exc).startswith(("No RunLedger account", "This email", "This identity"))
                else "Invalid sign-in response."
            )

    def _sso_continue(
        self, method: str, client: Union[oidc.Client, saml.Client], user_id: str, provider: str, cookie: str
    ) -> None:
        teams = self.server.db.user_teams(user_id)
        if len(teams) == 1:
            self._sso_sign_in(method, user_id, teams[0]["id"], provider)
            return
        state = client.stash(dict(user_id=user_id, provider=provider, cookie=cookie))
        buttons = "".join(
            f'<button name="team" value="{t["id"]}">{_escape(t["name"])}</button>' for t in teams
        )
        page = SSO_TEAM_HTML.replace("__METHOD__", method).replace("__STATE__", state).replace("__TEAMS__", buttons)
        self._send_html(200, page, {"Content-Security-Policy": SIGN_IN_CSP})

    def _sso_sign_in(self, method: str, user_id: str, team_id: int, provider: str) -> None:
        """Issue the Strict session cookie on a same-site continuation document."""
        token = self.server.db.issue_sso_session(method, team_id, user_id, provider,
                                                 idle_timeout=self.server.session_idle_timeout)
        identity = self.server.identify_session(token)
        if identity is None:
            error = saml.SAMLError if method == "saml" else oidc.OIDCError
            raise error("Team membership is no longer available.")
        self.server.db.record_audit(
            team_id,
            identity,
            "auth.sign_in",
            user_id,
            {"method": method, "provider": provider, "ip": self._client_ip()},
        )
        cookie = (
            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}"
        )
        if self._secure() or (method == "saml" and self.server.saml.public_url.startswith("https://")):
            cookie += "; Secure"
        self._send_html(
            200,
            '<!doctype html><meta http-equiv="refresh" content="0;url=/">'
            '<title>Signed in</title><p>Signed in. <a href="/">Continue to dashboard</a></p>',
            {"Set-Cookie": cookie, "Content-Security-Policy": SIGN_IN_CSP},
        )

    # Auth

    def _client_ip(self) -> str:
        """The address the failure limit and the audit log use. See RunLedgerServer.client_ip.
        Every X-Forwarded-For line is read, since a proxy may append its own line."""
        forwarded = None
        if getattr(self, "headers", None) is not None:
            forwarded = ", ".join(self.headers.get_all("X-Forwarded-For") or [])
        return self.server.client_ip(self.client_address[0], forwarded)

    def _check_rate_limit(self) -> None:
        wait = self.server.limiter.blocked_for(self._client_ip())
        if wait:
            raise _HttpError(
                429, "rate_limited",
                "Too many failed sign-in attempts from this address. Wait a few minutes and try again.",
                {"Retry-After": str(wait)},
            )

    def _bearer_token(self) -> Optional[str]:
        scheme, _, token = (self.headers.get("Authorization") or "").strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return token.strip()

    def _cookie(self, name: str) -> Optional[str]:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name and value:
                return value
        return None

    def _session_identity(self) -> Optional[Identity]:
        return self.server.identify_session(
            self._cookie(COOKIE_NAME), touch=(self.headers.get(BACKGROUND_HEADER) or "").strip() != "1",
        )

    def _identify(self) -> Identity:
        """Who is calling: an API key first, then the dashboard session.

        A rejected API key counts toward the address's failure limit, because it is a
        guess. A session cookie does not: it is random and cannot be guessed, and an
        expired or revoked one is ordinary (a restart or an old tab), so counting it
        would lock out a dashboard that is simply left open. Requests with no
        credentials do not count either."""
        token = self._bearer_token()
        if token is not None:
            ident = self.server.identify_key(token)
            if ident is not None:
                if ident.expires_soon:
                    self._extra_headers[KEY_EXPIRES_HEADER] = ident.expires_at or ""
                return ident
        expired = self.server.db.expired_key_for_token(token) if token is not None else None
        ident = self._session_identity()
        if ident is not None:
            return ident
        if expired is not None:
            # The right secret, past its end: not a guess, so not counted as a failure.
            raise _HttpError(
                401, "key_expired",
                f"This API key expired at {expired['expires_at']}. Ask a team admin to rotate it "
                "(runledger key rotate ID, or Rotate on the dashboard's API keys page).",
            )
        if token is not None:
            self.server.limiter.record_failure(self._client_ip())
        raise _unauthorized()

    def _try_identify(self) -> Optional[Identity]:
        try:
            return self._identify()
        except _HttpError as exc:
            if exc.status == 401:
                return None
            raise

    def _require(self, ident: Identity, minimum: str, what: str, write: bool = False) -> None:
        """Check the role, and for a cookie write, the CSRF header. A cross-site form cannot set it."""
        if not allows(ident.role, minimum):
            raise _HttpError(
                403, "forbidden",
                f"The {ident.role} role cannot {what}. This needs the {minimum} role or higher.",
            )
        if write and ident.via == "session" and (self.headers.get(CSRF_HEADER) or "").strip() != CSRF_VALUE:
            raise _HttpError(
                403, "csrf_required",
                f"Dashboard changes must send the header '{CSRF_HEADER}: {CSRF_VALUE}'.",
            )

    def _discard_small_body_before_close(self, limit: int = 64 * 1024) -> None:
        """Drain a small body from an authenticated caller before closing the connection.

        Closing a TCP connection with unread input can reset it on some platforms and discard the
        error response before the client reads it. Unauthenticated and oversized bodies still are
        not read; this bounded drain only makes authorization errors to authenticated callers
        reliable.
        """
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return
        if 0 < length <= limit:
            try:
                read_with_timeout = getattr(self.rfile, "read_with_timeout", None)
                if read_with_timeout is not None:
                    read_with_timeout(length, 2.0)
                else:
                    self.rfile.read(length)
            except Exception:
                pass

    def _authed(self, minimum: str, what: str, write: bool = False) -> Identity:
        """Identify and authorize before the request body is read, so a huge unauthenticated
        body is answered 401 at once, and the connection closes without draining it."""
        try:
            ident = self._identify()
        except _HttpError as exc:
            exc.headers.setdefault("Connection", "close")
            raise
        try:
            self._require(ident, minimum, what, write=write)
        except _HttpError as exc:
            self._discard_small_body_before_close()
            exc.headers.setdefault("Connection", "close")
            raise
        return ident

    # Endpoints

    def _content_length(self, limit: int = MAX_BODY_BYTES, what: str = "Receipts") -> int:
        header = self.headers.get("Content-Length")
        if header is None:
            raise _HttpError(411, "length_required", "Send a Content-Length header.")
        try:
            length = int(header)
        except ValueError:
            raise _HttpError(400, "bad_request", "Content-Length must be a number.") from None
        if length < 0:
            raise _HttpError(400, "bad_request", "Content-Length must not be negative.")
        if length > limit:
            raise _HttpError(413, "payload_too_large", f"{what} are limited to {_size_text(limit)}.")
        return length

    def _me(self) -> None:
        ident = self._identify()
        me: Dict[str, Any] = {
            "team": {"id": ident.team_id, "name": ident.team_name},
            "role": ident.role,
            "key": {
                "id": ident.key_id,
                "label": ident.label,
                "prefix": ident.prefix,
                "kind": ident.kind,
                "expires_at": ident.expires_at,
                "expires_soon": ident.expires_soon,
            } if ident.key_id else None,
            "user": {"id": ident.user_id, "email": ident.email, "name": ident.name} if ident.user_id else None,
        }
        if self.server.oidc or self.server.saml or ident.sign_in_method != "key":
            me["sign_in_method"] = ident.sign_in_method
        self._send_json(200, me)

    def _create_run(self) -> None:
        ident = self._authed("member", "push runs", write=True)
        if ident.key_id is None:
            raise _HttpError(403, "api_key_required", "Use an API key to push runs.")
        length = self._content_length()
        raw = self.rfile.read(length) if length else b""
        if not raw:
            raise _HttpError(400, "empty_body", "Send the receipt as a JSON object in the request body.")
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _HttpError(400, "invalid_json", "The body must be UTF-8 JSON.") from None
        if not isinstance(payload, dict):
            raise _HttpError(400, "invalid_json", "The body must be a JSON object.")
        _validate_json_values(payload)
        run = receipt_to_run(
            payload,
            attribution=self.server.db.team_attribution(ident.team_id),
            pushed_by=ident.label,
            role=ident.role,
        )
        result = self.server.db.upsert_run(
            ident.team_id,
            run,
            actor=ident,
            key_id=ident.key_id,
            role=ident.role,
        )
        outcome = result["outcome"]
        if outcome == "conflict":
            raise _HttpError(
                409,
                "run_conflict",
                "A different key already pushed this session, or the new receipt is older. "
                "Push the newest version of a session with the key that pushed it first.",
            )
        self._check_budget_alerts(ident)
        if outcome == "updated":
            self._send_json(200, {
                "status": "updated",
                "id": run["id"],
                "revision": result["revision"],
            })
            return
        body: Dict[str, Any] = {
            "id": run["id"],
            "url": "/runs/" + quote(run["id"], safe=""),
            "risk_score": result["risk_score"],
            "risk_level": result["risk_level"],
            "revision": result["revision"],
        }
        if outcome == "duplicate":
            body["status"] = "duplicate"
        self._send_json(201 if outcome == "inserted" else 200, body)

    def _list_runs(self, query: Dict[str, List[str]]) -> None:
        ident = self._identify()
        ai_verdict = _text_param(query, "ai_verdict", 20)
        if ai_verdict is not None and ai_verdict not in insights.VERDICTS:
            raise _HttpError(400, "bad_request", "'ai_verdict' must be one of looks_safe, needs_review, dangerous.")
        filters = {
            "user": _text_param(query, "user"),
            "project": _text_param(query, "project"),
            "min_risk": _int_param(query, "min_risk", None, 0, 100),
            "agent": _text_param(query, "agent"),
            "min_quality": _int_param(query, "min_quality", None, 0, 100),
            "max_quality": _int_param(query, "max_quality", None, 0, 100),
            "ai_verdict": ai_verdict,
        }
        after = (query.get("after") or [""])[0] or None  # a run id, kept verbatim: ids may hold spaces
        db = self.server.db
        try:
            if after is not None and not _valid_id(after):
                raise KeyError(after)
            runs = db.list_runs(ident.team_id, limit=_int_param(query, "limit", 100, 1, 500), after=after, **filters)
        except KeyError:
            raise _HttpError(400, "bad_request", "'after' must be the id of one of the team's runs.") from None
        self._send_json(200, {"runs": runs, "total": db.count_runs(ident.team_id, **filters)})

    def _stats(self, query: Dict[str, List[str]]) -> None:
        ident = self._identify()
        stats = self.server.db.stats(ident.team_id, _int_param(query, "days", 30, 1, 3650))
        stats["team"] = ident.team_name
        self._send_json(200, stats)

    def _insights(self, query: Dict[str, List[str]]) -> None:
        ident = self._identify()
        days = _int_param(query, "days", 30, 1, 3650)
        moment = budgets.now()
        since = exports.since_for(moment, days)
        until = moment.astimezone(timezone.utc).strftime(TIME_FORMAT)
        self._send_json(200, insights.compute(self.server.db, ident.team_id, since, days, until))

    def _get_run(self, raw_id: str) -> None:
        ident = self._identify()
        run = self.server.db.get_run(ident.team_id, _run_id(raw_id))
        if run is None:
            raise _not_found()
        self._send_json(200, {"run": run})

    def _receipt(self, raw_id: str) -> None:
        ident = self._identify()
        html = self.server.db.get_receipt_html(ident.team_id, _run_id(raw_id))
        if html is None:
            raise _not_found("Run not found, or it has no stored HTML receipt.")
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": RECEIPT_CSP})

    def _dashboard(self, query: Dict[str, List[str]]) -> None:
        key = (query.get("key") or [""])[0].strip()
        if key and self.server.dashboard_key_sign_in_disabled:
            self._sign_in_page("Key sign-in is disabled; use your identity provider.")
            return
        if key:
            # Legacy sign-in: trade the key for an opaque cookie and redirect so the key leaves
            # the URL. A session of another team is never replaced without a confirming click,
            # and a browser navigation that comes from another origin is refused outright: the
            # Set-Cookie that would follow replaces the visitor's session for another team.
            self._refuse_cross_origin_sign_in()
            self.server.warn_query_key_once()
            row = self.server.db.key_for_token(key)
            if row is None:
                self._refuse_sign_in_key(key)
                return
            current = self._try_identify()
            if current is not None and current.team_id != row["team_id"]:
                self._switch_team_page(row, current, key)
                return
            self._sign_in(row)
            return
        if self._try_identify() is None:
            self._sign_in_page()
            return
        nonce = secrets.token_urlsafe(16)
        self._send_html(200, DASHBOARD_HTML.replace("__CSP_NONCE__", nonce), {"Content-Security-Policy": _page_csp(nonce)})

    def _refuse_cross_origin_sign_in(self) -> None:
        """Refuse a sign-in request that a browser marks as coming from another origin.

        This covers the sign-in form (POST /session) and the legacy key-in-URL link
        (GET /?key=). The session cookie is SameSite=Strict, so a cross-site request arrives
        without the cookie the visitor already holds: the server cannot show the confirmation
        page (see _switch_team_page) and the Set-Cookie that would follow replaces the visitor's
        session for another team. A browser describes such a request with Origin and
        Sec-Fetch-Site, so a mismatch is refused here, before the body is read.

        Sec-Fetch-Site is the browser's own statement and page script cannot set it, so
        "same-origin" is accepted without the Origin header: every response carries
        `Referrer-Policy: no-referrer`, which makes Chromium serialize the Origin of a
        same-origin form POST as "null" (measured with Chrome 155). That opaque "null" is the
        only Origin `same-origin` excuses; a concrete Origin is still compared, so a request
        that contradicts its own fetch metadata is refused. Without the fetch metadata, the
        Origin is compared with the Host header at the scheme the server believes the client
        used, so a request behind a reverse proxy still matches when the server already trusts
        X-Forwarded-Proto (see RunLedgerServer.secure_for). A request with neither header is not
        a browser (curl, a pasted link, an agent) and keeps working; a browser navigation from
        another origin sends Sec-Fetch-Site: same-site or cross-site, never "none"."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in CROSS_ORIGIN_FETCH_SITES:
            self._refuse_sign_in(f"Sec-Fetch-Site: {site}")
        origin = self.headers.get("Origin")
        if origin is None:
            return
        if site == "same-origin" and origin.strip().lower() == "null":
            return
        host = (self.headers.get("Host") or "").strip()
        scheme = "https" if self._secure() else "http"
        if not host or not _same_origin(origin, scheme, host):
            self._refuse_sign_in(f"Origin: {_log_text(origin)}")

    def _refuse_sign_in(self, reason: str) -> None:
        """403 without a cookie and without a session row. The line names the header that
        refused the request, never the key."""
        sys.stderr.write(f"warning: refused a cross-site sign-in request ({reason})\n")
        raise _HttpError(
            403, "cross_site",
            "Sign-in must come from this site. Open the dashboard in a new tab and use the form there.",
            {"Connection": "close"},
        )

    def _session_post(self) -> None:
        """The sign-in form: the key travels in the body, never in the URL."""
        if self.server.dashboard_key_sign_in_disabled:
            self._sign_in_page("Key sign-in is disabled; use your identity provider.")
            self.close_connection = True
            return
        self._refuse_cross_origin_sign_in()
        length = self._content_length(SIGN_IN_BODY_LIMIT, "Sign-in forms")
        raw = self.rfile.read(length) if length else b""
        fields = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        key = (fields.get("key") or [""])[0].strip()
        row = self.server.db.key_for_token(key) if key else None
        if row is None:
            self._refuse_sign_in_key(key)
            return
        self._sign_in(row)

    def _refuse_sign_in_key(self, key: str) -> None:
        """Sign-in page for a key that did not sign in. An expired key is named as expired and,
        not being a guess, does not count toward the failure limit."""
        expired = self.server.db.expired_key_for_token(key) if key else None
        if expired is not None:
            self._sign_in_page(f"That key expired at {expired['expires_at']}. Ask a team admin to rotate it.")
            return
        if key:
            self.server.limiter.record_failure(self._client_ip())
        self._sign_in_page("That key was not recognized. Check the key and try again.")

    def _sign_in(self, row: Mapping[str, Any]) -> None:
        if row.get("kind") == "service":
            self._sign_in_page("Service keys work on the API only and cannot sign in to the dashboard. "
                               "Sign in with a personal key.")
            return
        token = self.server.db.issue_session(
            row["team_id"], row["id"], row["role"], idle_timeout=self.server.session_idle_timeout,
        )
        session = identity_from_row(row, "session")
        self.server.db.record_audit(row["team_id"], session, "auth.sign_in", row["id"],
                                    {"role": row["role"], "ip": self._client_ip()})
        cookie = self._session_cookie(token, SESSION_TTL_SECONDS)
        self._send(302, b"", "text/plain; charset=utf-8", {"Location": "/", "Set-Cookie": cookie})

    def _session_cookie(self, token: str, max_age: int) -> str:
        cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        if self._secure():
            cookie += "; Secure"
        return cookie

    def _session_logout(self) -> None:
        ident = self._session_identity()
        if ident is None or ident.session_id is None:
            raise _HttpError(401, "unauthorized", "Sign in to the dashboard to sign out.")
        self._require(ident, "viewer", "sign out", write=True)
        self.server.db.revoke_session(
            ident.team_id, ident.key_id, ident.session_id, ident,
            "auth.sign_out", self.server.session_idle_timeout,
        )
        self._session_ended(clear_cookie=True)

    def _list_sessions(self) -> None:
        ident = self._identify()
        key_id = None if ident.role == "admin" else ident.key_id
        user_id = ident.user_id if ident.role != "admin" and ident.key_id is None else None
        sessions = self.server.db.list_sessions(ident.team_id, key_id, self.server.session_idle_timeout, user_id=user_id)
        for session in sessions:
            session["current"] = session["id"] == ident.session_id
        self._send_json(200, {"sessions": sessions})

    def _revoke_session(self, raw_id: str) -> None:
        ident = self._identify()
        self._require(ident, "viewer", "revoke sessions", write=True)
        session_id = unquote(raw_id)
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise _not_found("Session not found.")
        key_id = None if ident.role == "admin" else ident.key_id
        user_id = ident.user_id if ident.role != "admin" and ident.key_id is None else None
        if not self.server.db.revoke_session(
            ident.team_id, key_id, session_id, ident, "auth.session_revoked", self.server.session_idle_timeout,
            user_id=user_id,
        ):
            raise _not_found("Session not found.")
        self._session_ended(clear_cookie=(session_id == ident.session_id))

    def _session_ended(self, clear_cookie: bool) -> None:
        headers = {"Set-Cookie": self._session_cookie("", 0)} if clear_cookie else None
        self._start(204, "text/plain; charset=utf-8", headers, length=None)

    def _switch_team_page(self, row: Mapping[str, Any], current: Identity, key: str) -> None:
        """A ?key= link for another team is a switch, not a sign-in: ask for one click first."""
        page = (
            SWITCH_TEAM_HTML
            .replace("__FROM__", _escape(current.team_name))
            .replace("__TO__", _escape(row["team_name"]))
            .replace("__KEY__", _escape(key))
        )
        self._send_html(200, page, {"Content-Security-Policy": SIGN_IN_CSP})

    # Approvals

    def _create_approval(self) -> None:
        ident = self._authed("member", "request approvals", write=True)
        if ident.key_id is None:
            raise _HttpError(403, "api_key_required", "Use an API key to request approvals.")
        length = self._content_length(approvals.CREATE_BODY_LIMIT, "Approval requests")
        raw = self.rfile.read(length) if length else b""
        try:
            fields = approvals.validate_new_approval(_json_object(raw))
        except approvals.InvalidInput as exc:
            raise _HttpError(400, "invalid_approval", str(exc)) from None
        db = self.server.db
        settings = db.approval_settings(ident.team_id)
        approval_id = approvals.new_approval_id()
        view = db.create_approval(
            ident.team_id,
            approval_id,
            fields,
            settings["approval_ttl_s"],
            requester_key_id=ident.key_id,
            requested_by=_attested_identity(ident),
        )
        approvals.notify_new_approval(settings, view, self.server.public_url)
        self._send_json(201, {"id": approval_id, "status": "pending"})

    def _list_approvals(self, query: Dict[str, List[str]]) -> None:
        ident = self._identify()
        status = _text_param(query, "status", 20)
        if status is not None and status not in approvals.APPROVAL_STATUSES:
            raise _HttpError(400, "bad_request", "'status' must be one of pending, approved, denied, expired.")
        db = self.server.db
        items = db.list_approvals(
            ident.team_id, status, db.approval_ttl_s(ident.team_id), limit=_int_param(query, "limit", 100, 1, 200),
        )
        self._send_json(200, {"approvals": items})

    def _get_approval(self, raw_id: str) -> None:
        ident = self._identify()
        approval_id = _approval_id(raw_id)
        db = self.server.db
        view = db.get_approval(ident.team_id, approval_id, db.approval_ttl_s(ident.team_id))
        if view is None:
            raise _not_found("Approval not found.")
        self._send_json(200, view)

    def _decide_approval(self, raw_id: str) -> None:
        approval_id = _approval_id(raw_id)
        ident = self._authed("admin", "decide approvals", write=True)
        length = self._content_length(approvals.SMALL_BODY_LIMIT, "Decisions")
        raw = self.rfile.read(length) if length else b""
        try:
            decision, reason, _name = approvals.validate_decision(_json_object(raw))
        except approvals.InvalidInput as exc:
            raise _HttpError(400, "invalid_decision", str(exc)) from None
        status = "approved" if decision == "approve" else "denied"
        decided_by = _attested_identity(ident)
        db = self.server.db
        outcome, view = db.decide_approval(
            ident.team_id, approval_id, status, decided_by, reason, db.approval_ttl_s(ident.team_id),
            approver_key_id=ident.key_id,
            actor=ident,
        )
        if outcome == "missing":
            raise _not_found("Approval not found.")
        if outcome == "self":
            raise _HttpError(
                403,
                "self_approval",
                "The key that requested this approval cannot decide it. Use a different admin key.",
            )
        if outcome == "legacy_unverifiable":
            raise _HttpError(
                409,
                "legacy_unverifiable",
                "This approval predates requester identity tracking and cannot be decided safely. Submit a new approval request.",
            )
        if outcome == "expired":
            raise _HttpError(409, "expired", "This approval expired before it was decided.")
        if outcome == "conflict":
            raise _HttpError(409, "already_decided", f"This approval was already {view['status']}.")
        self._send_json(200, view)

    def _team_settings(self, method: str) -> None:
        db = self.server.db
        if method == "PUT":
            ident = self._authed("admin", "change team settings", write=True)
            length = self._content_length(approvals.SMALL_BODY_LIMIT, "Settings")
            raw = self.rfile.read(length) if length else b""
            try:
                changes = approvals.validate_settings(_json_object(raw))
            except approvals.InvalidInput as exc:
                raise _HttpError(400, "invalid_settings", str(exc)) from None
            db.update_approval_settings(ident.team_id, changes, actor=ident)
        else:
            ident = self._identify()
        settings = db.approval_settings(ident.team_id)
        if ident.role != "admin":
            # Webhook URLs are secrets. Other roles see that a URL is set, and where it points.
            settings = {
                **settings,
                "slack_webhook_url": _masked_url(settings["slack_webhook_url"]),
                "webhook_url": _masked_url(settings["webhook_url"]),
            }
        self._send_json(200, {"team": ident.team_name, **settings})

    def _approval_page(self, raw_id: str) -> None:
        approval_id = _approval_id(raw_id)
        ident = self._try_identify()
        if ident is None:
            self._sign_in_page()
            return
        db = self.server.db
        if db.get_approval(ident.team_id, approval_id, db.approval_ttl_s(ident.team_id)) is None:
            raise _not_found("Approval not found.")
        nonce = secrets.token_urlsafe(16)
        self._send_html(200, APPROVAL_HTML.replace("__CSP_NONCE__", nonce), {"Content-Security-Policy": _page_csp(nonce)})

    # Keys and audit (admin only)

    def _members(self, method: str, user_id: Optional[str] = None) -> None:
        ident = self._authed("admin", "manage members", write=method != "GET")
        db = self.server.db
        if method == "GET":
            self._send_json(200, {"members": db.list_members(ident.team_id)})
            return
        body = {}
        if method != "DELETE":
            length = self._content_length(approvals.SMALL_BODY_LIMIT, "Member requests")
            body = _json_object(self.rfile.read(length) if length else b"")
        try:
            if method == "POST":
                member = db.add_member(ident.team_id, body.get("email"), body.get("role"),
                                       body.get("name"), actor=ident)
                self._send_json(201, member)
                return
            if method == "PATCH":
                validate_role(body.get("role"))
            db.change_member(ident.team_id, user_id, body.get("role") if method == "PATCH" else None, actor=ident)
        except InvalidKey as exc:
            raise _HttpError(400, "invalid_member", str(exc)) from None
        self._send_json(200, {"ok": True})

    def _list_keys(self) -> None:
        ident = self._identify()
        self._require(ident, "admin", "list the team's keys")
        self._send_json(200, {"keys": self.server.db.list_keys(ident.team_id)})

    def _create_key(self) -> None:
        ident = self._authed("admin", "create keys", write=True)
        length = self._content_length(approvals.SMALL_BODY_LIMIT, "Key requests")
        raw = self.rfile.read(length) if length else b""
        body = _json_object(raw)
        try:
            view = self.server.db.create_key(ident.team_id, body.get("label"), body.get("role"), ident,
                                             kind=body.get("kind"), expires_in_days=body.get("expires_in_days"),
                                             user=body.get("user"))
        except InvalidKey as exc:
            raise _HttpError(400, "invalid_key", str(exc)) from None
        self._send_json(201, view)

    def _revoke_key(self, raw_id: str) -> None:
        key_id = _key_id(raw_id)
        ident = self._identify()
        self._require(ident, "admin", "revoke keys", write=True)
        outcome, view = self.server.db.revoke_key(ident.team_id, key_id, ident)
        if outcome == "missing":
            raise _HttpError(404, "not_found", "Key not found.")
        if outcome == "already_revoked":
            raise _HttpError(409, "already_revoked", "This key is already revoked.")
        if outcome == "last_admin":
            raise _HttpError(
                409, "last_admin",
                "This is the team's last active admin key. Create or rotate another admin key first.",
            )
        self._send_json(200, view)

    def _rotate_key(self, raw_id: str) -> None:
        key_id = _key_id(raw_id)
        ident = self._identify()
        self._require(ident, "admin", "rotate keys", write=True)
        body: Dict[str, Any] = {}
        if self.headers.get("Content-Length") not in (None, "0"):
            length = self._content_length(approvals.SMALL_BODY_LIMIT, "Key requests")
            body = _json_object(self.rfile.read(length) if length else b"")
        try:
            outcome, view = self.server.db.rotate_key(ident.team_id, key_id, ident,
                                                      expires_in_days=body.get("expires_in_days"))
        except InvalidKey as exc:
            raise _HttpError(400, "invalid_key", str(exc)) from None
        if outcome == "missing":
            raise _HttpError(404, "not_found", "Key not found.")
        if outcome == "revoked":
            raise _HttpError(409, "key_revoked", "A revoked key cannot be rotated.")
        self._send_json(200, view)

    def _audit_log(self, query: Dict[str, List[str]]) -> None:
        ident = self._identify()
        self._require(ident, "admin", "read the audit log")
        events = self.server.db.list_audit(
            ident.team_id,
            limit=_int_param(query, "limit", 100, 1, 500),
            before=_int_param(query, "before", None, 1, 10 ** 18),
            action=_text_param(query, "action", 100),
        )
        self._send_json(200, {"events": events})

    # Budgets

    def _check_budget_alerts(self, ident: Identity) -> None:
        """After a push: record and announce any budget threshold that is newly crossed.
        The push has already been stored, so a failure here is logged and never fails the push."""
        try:
            db = self.server.db
            fired = budgets.check_alerts(db, ident.team_id)
            if fired:
                approvals.notify_budget_alerts(db.approval_settings(ident.team_id), ident.team_name, fired)
        except Exception:
            traceback.print_exc()

    def _budgets(self, method: str) -> None:
        db = self.server.db
        if method == "PUT":
            ident = self._authed("admin", "change the team budget", write=True)
            length = self._content_length(approvals.SMALL_BODY_LIMIT, "Budgets")
            raw = self.rfile.read(length) if length else b""
            try:
                values = budgets.validate_budget(_json_object(raw))
            except _HttpError as exc:  # no body, or not a JSON object
                raise _HttpError(400, "invalid_budget", exc.message) from None
            except budgets.InvalidBudget as exc:
                raise _HttpError(400, "invalid_budget", str(exc)) from None
            settings = db.update_budget_settings(ident.team_id, values, ident)
        else:
            ident = self._identify()
            settings = db.budget_settings(ident.team_id)
        self._send_json(200, settings)

    def _budget_status(self) -> None:
        ident = self._identify()
        self._send_json(200, budgets.status(self.server.db, ident.team_id))

    # Exports (admin). HEAD answers with the headers only and builds nothing.

    def _export(self, kind: str, query: Dict[str, List[str]], head: bool) -> None:
        ident = self._identify()
        self._require(ident, "admin", "export the team's data")
        days = _int_param(query, "days", 30, 1, 3650)
        now = budgets.now()
        content_type, headers = exports.describe(kind, now)
        if head:
            self._start(200, content_type, headers, length=None)
            return
        body, rows = exports.build(kind, self.server.db, ident.team_id, ident.team_name, days, now)
        self.server.db.record_audit(ident.team_id, ident, f"export.{kind}", None,
                                    {"days": days, "rows": rows})
        self._send(200, body, content_type, headers)


# Helpers


def _attested_identity(ident: Identity) -> str:
    """Stable human-readable attribution derived only from authenticated key data."""
    if ident.user_id:
        return ident.actor()
    source = "dashboard" if ident.via == "session" else "api"
    key_ref = ident.prefix or ident.key_id
    return f"{source}: {ident.label} ({key_ref})"


def _same_origin(origin: str, scheme: str, host: str) -> bool:
    """Whether an Origin header names exactly the scheme and host of this request. "null", an
    opaque origin, a value without a scheme and a value the parser rejects (such as a malformed
    bracketed IPv6 authority) are never this request's origin."""
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    if parts.scheme.lower() != scheme or not parts.netloc:
        return False
    return _authority(parts.netloc, scheme) == _authority(host, scheme)


def _authority(value: str, scheme: str) -> Tuple[str, str]:
    """A Host header or an origin's authority as (host, port), lower-cased, with the scheme's
    default port filled in, so `example.com` and `example.com:443` compare equal."""
    text = value.strip().lower()
    if text.startswith("["):  # an IPv6 literal, with or without a port
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    else:
        host, port = text, ""
    return host, port or ("443" if scheme == "https" else "80")


def _log_text(value: str, limit: int = 120) -> str:
    """Header text for a log line: printable ASCII only, one line, bounded."""
    return re.sub(r"[^\x20-\x7e]", "?", value.strip())[:limit]


def _page_csp(nonce: str) -> str:
    return (
        "default-src 'none'; script-src 'nonce-" + nonce + "'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def _size_text(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit // 1024} KB"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _first_value(header: Optional[str]) -> str:
    """The first value of a possibly comma-separated proxy header, lower-cased."""
    return (header or "").split(",")[0].strip().lower()


def _env_list(name: str) -> List[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def _ip_or_none(text: str) -> Any:
    """An IP address object for `text`, or None. An IPv4-mapped IPv6 address becomes IPv4."""
    value = text.strip().strip('"')
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _in_any(ip: Any, networks: Iterable[_IPNetwork]) -> bool:
    return any(ip.version == net.version and ip in net for net in networks)


def _masked_url(url: Optional[str]) -> Optional[str]:
    """Scheme and host only. The path of a webhook URL is the secret part."""
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}/[hidden]"


def _validate_json_values(payload: Dict[str, Any]) -> None:
    pending: List[Any] = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise _HttpError(400, "invalid_json", "JSON strings must not contain lone Unicode surrogates.") from None
        elif isinstance(value, (int, float)):
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise _HttpError(400, "invalid_json", "JSON numbers must be finite and representable as floats.")


def _json_object(raw: bytes) -> Dict[str, Any]:
    """Parse a request body that must be one JSON object."""
    if not raw:
        raise _HttpError(400, "empty_body", "Send a JSON object in the request body.")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _HttpError(400, "invalid_json", "The body must be UTF-8 JSON.") from None
    if not isinstance(payload, dict):
        raise _HttpError(400, "invalid_json", "The body must be a JSON object.")
    return payload


def _approval_id(raw: str) -> str:
    value = unquote(raw)
    if not _APPROVAL_ID.fullmatch(value):
        raise _not_found("Approval not found.")
    return value


def _key_id(raw: str) -> str:
    value = unquote(raw)
    if not _KEY_ID.fullmatch(value):
        raise _HttpError(404, "not_found", "Key not found.")
    return value


def _json_bytes(obj: Any) -> bytes:
    text = json.dumps(obj, ensure_ascii=False)
    # Inside JSON strings, escape HTML-significant characters, so the body can
    # never close a <script> tag wherever it ends up.
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return text.encode("utf-8")


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON constant {name}")


def _valid_id(value: str) -> bool:
    return 0 < len(value) <= 200 and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _run_id(raw: str) -> str:
    run_id = unquote(raw)
    if not _valid_id(run_id):
        raise _not_found()
    return run_id


def _text_param(query: Dict[str, List[str]], name: str, limit: int = 200) -> Optional[str]:
    values = query.get(name)
    if not values:
        return None
    text = values[0].strip()[:limit]
    return text or None


def _int_param(query: Dict[str, List[str]], name: str, default: Optional[int], lo: int, hi: int) -> Optional[int]:
    raw = _text_param(query, name, 20)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _HttpError(400, "bad_request", f"'{name}' must be a whole number.") from None
    if not lo <= value <= hi:
        raise _HttpError(400, "bad_request", f"'{name}' must be between {lo} and {hi}.")
    return value


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()[:limit]
    return text or None


def _int(value: Any, default: Optional[int], lo: int = 0, hi: int = 10 ** 12) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, number))


def _money(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value > MAX_COST_USD or not math.isfinite(value):
        return None
    return round(max(0.0, float(value)), 6)


def _enum(value: Any, allowed: Tuple[str, ...]) -> Optional[str]:
    """A value from `allowed` (matched without case or padding), or None."""
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in allowed else None


def _iso(value: Any) -> Optional[str]:
    when = _parse_ts(value)
    return when.strftime(TIME_FORMAT) if when else None


def _level_for(score: int) -> str:
    return "low" if score < 25 else "medium" if score < 60 else "high"


def _title(payload: Dict[str, Any]) -> str:
    for item in _list(payload.get("request")):
        if isinstance(item, str) and item.strip():
            return " ".join(item.split())[:200]
    return _text(payload.get("overview"), 200) or ""


def receipt_to_run(
    payload: Dict[str, Any],
    *,
    attribution: str = "client",
    pushed_by: Optional[str] = None,
    role: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a pushed receipt and flatten it into the columns the store keeps.

    Accepts the output of `runledger receipt --format json`, plus the fields the
    client adds: user, project, html, and models with cost_usd. Missing or odd
    values fall back to safe defaults. Only a missing session_id is rejected.

    With `attribution` "key" a run pushed by a member is displayed under the label of the
    key that pushed it (`pushed_by`), leaving the client's own user value in the receipt;
    admin keys always keep the client user. A receipt whose usage is missing keeps an
    unknown cost even when it carries a cost of 0.0."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not _valid_id(session_id):
        raise _HttpError(400, "invalid_receipt", "The receipt needs a session_id of 1-200 characters.")

    totals = _dict(payload.get("totals"))
    risk = _dict(payload.get("risk"))
    steps = [s for s in _list(payload.get("steps")) if isinstance(s, dict)]
    cwd = _text(payload.get("cwd"), 500)
    client_user = _text(payload.get("user"), 200) or "unknown"
    user = client_user
    if attribution == "key" and pushed_by and role != "admin":
        user = pushed_by
    project = _text(payload.get("project"), 200) or _folder_name(cwd) or "unknown"
    agent = _text(payload.get("agent"), 100)  # the agent's label, e.g. "Claude Code"

    score = _int(risk.get("score"), 0, 0, 100) or 0
    level = str(risk.get("level") or "").lower()
    if level not in LEVELS:
        level = _level_for(score)

    usage_source = _enum(totals.get("usage_source"), USAGE_SOURCES)
    # An agent that recorded no usage has an unknown cost: whatever the client put in cost_usd
    # (usually 0.0) would read as a free run. The same goes for each model's bucket, so a model
    # group made of such runs stays unpriced instead of a priced zero.
    missing_usage = usage_source == "missing"

    models: Dict[str, Dict[str, Any]] = {}
    for name, info in list(_dict(payload.get("models")).items())[:50]:
        label = str(name)[:200]
        info = _dict(info)
        cost = None if missing_usage else _money(info.get("cost_usd"))
        if cost is None and not missing_usage:  # older receipts: fall back to the per-step costs
            step_costs = [
                c for c in (_money(s.get("cost_usd")) for s in steps if s.get("model") == name)
                if c is not None
            ]
            cost = _money(sum(step_costs)) if step_costs else None
        models[label] = {"tokens": _int(info.get("tokens"), 0) or 0, "cost_usd": cost}

    risks: List[Dict[str, Any]] = []
    for item in _list(risk.get("reasons"))[:500]:
        if not isinstance(item, dict):
            continue
        severity = (_text(item.get("severity"), 16) or "low").lower()
        risks.append({
            "severity": severity if severity in SEVERITIES else "low",
            "code": _text(item.get("code"), 64) or "unknown",
            "reason": _text(item.get("reason"), 500) or "",
            "step": _int(item.get("step"), None),
        })

    html = payload.get("html")
    if html is not None and not isinstance(html, str):
        raise _HttpError(400, "invalid_receipt", "'html' must be a string.")

    # The quality, recommendations and AI review keys are checked leniently: a bad value is
    # stored as null (or dropped item by item) and never rejects the push.
    review = insights.review_fields(payload)
    receipt = {k: v for k, v in payload.items() if k != "html"}
    receipt["user"] = client_user
    receipt["project"] = project
    receipt.update({k: review[k] for k in ("quality", "recommendations", "ai_review")})
    normalize_missing_costs(receipt, usage_source)

    cost = None if missing_usage else _money(totals.get("cost_usd"))
    return {
        "id": session_id,
        "user": user,
        "project": project,
        "agent": agent,
        "agent_id": _text(payload.get("agent_id"), 100),
        "pushed_by": pushed_by,
        "title": _title(payload),
        "started_at": _iso(payload.get("started")),
        "ended_at": _iso(payload.get("ended")),
        "models": models,
        "steps": _int(totals.get("steps"), len(steps)) or 0,
        "tokens": _int(totals.get("tokens"), 0) or 0,
        "files_changed": _int(totals.get("files_changed"), 0) or 0,
        "cost": cost,
        "usage_source": usage_source,
        "cost_partial": totals.get("cost_partial") is True and cost is not None,
        "risk_score": score,
        "risk_level": level,
        "risks": risks,
        "quality_score": review["quality_score"],
        "quality_grade": review["quality_grade"],
        "ai_verdict": review["ai_verdict"],
        "est_savings_usd": review["est_savings_usd"],
        "recommendations": json.dumps(review["recommendation_counts"], ensure_ascii=False),
        "ai_assessment": json.dumps(review["assessment_counts"]),
        "receipt": receipt,
        "receipt_html": html,
    }


def _folder_name(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]
    return parts[-1] if parts else None
