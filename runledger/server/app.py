"""HTTP server for the RunLedger team dashboard and the push API.

  POST /api/runs                              push a receipt -> 201 (admin, member)
  GET  /api/runs[?user&project&agent&min_risk&min_quality&max_quality&ai_verdict&limit]  list runs (any role)
  GET  /api/runs/<id>                         one run with its receipt JSON and risk reasons
  GET  /api/stats[?days=30]                   team totals; cost by developer, model, project, agent
  GET  /api/insights[?days=30]                quality, cost by model and agent, recommendations, AI review (any role)
  GET  /runs/<id>                             the stored receipt HTML
  POST /api/approvals                         request a human approval -> 201 {id, status} (admin, member)
  GET  /api/approvals[?status=pending]        list approvals (any role)
  GET  /api/approvals/<id>                    one approval: status, decided_by, decided_at, reason
  POST /api/approvals/<id>/decision           approve or deny (admin, member)
  GET  /approvals/<id>                        approval page for a person (any role)
  GET  /api/team/settings                     webhook URLs and approval_ttl_s (any role; URLs masked unless admin)
  PUT  /api/team/settings                     change them (admin)
  GET  /api/me                                the caller's team, role and key (any role)
  GET  /api/keys                              the team's keys, never their secrets (admin)
  POST /api/keys                              {label, role} -> 201; the key is shown once (admin)
  POST /api/keys/<id>/revoke                  revoke a key (admin)
  POST /api/keys/<id>/rotate                  new secret for a key, same id, label and role (admin)
  GET  /api/audit[?limit&before&action]       audit log, newest first; action= keeps a prefix, e.g. key. (admin)
  GET  /api/budgets                           team budget (any role)
  PUT  /api/budgets                           replace it (admin); alerts at 50/80/100% unless set otherwise
  GET  /api/budgets/status                    this month's spend and percent of budget (any role)
  GET  /api/export/runs.csv[?days=30]         runs as CSV (admin); HEAD returns headers only
  GET  /api/export/audit.csv[?days=30]        audit log as CSV (admin)
  GET  /api/export/report.html[?days=30]      printable compliance report (admin)
  GET  /                                      dashboard; sign in once with /?key=API_KEY
  GET  /health                                liveness, no auth

With billing on (RUNLEDGER_POLAR_WEBHOOK_SECRET, see billing.py):

  POST /billing/polar/webhook                 Polar subscription events (Standard Webhooks signature)
  POST /billing/recover                       {email} -> 202; emails a new admin key to a subscriber
  GET  /recover                               page for the above

A billed team that is past its payment grace period, or whose subscription ended, gets 402 on
POST /api/runs and POST /api/approvals; so does a push from a new developer when every seat is taken.

Credentials are "Authorization: Bearer KEY", or the dashboard session cookie. A cookie
session has the role of the key it was signed in with, and survives a restart. A POST or
PUT that uses a cookie must also send the header "X-Requested-With: runledger".

Behind a reverse proxy, pass --trusted-proxy CIDR (or RUNLEDGER_TRUSTED_PROXIES). A peer in
a trusted range may report the client in X-Forwarded-For: the server then uses the right-most
address that is not itself trusted, for the failure limit and the audit log. X-Forwarded-For
from any other peer is ignored. With --trust-proxy, X-Forwarded-Proto is honoured only from
trusted peers, or from anyone when no trusted proxy is configured (the earlier behaviour).

Errors are JSON ({"error": {"code", "message"}}) and never include tracebacks;
tracebacks go to the server's stderr. Standard library only.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import __version__
from . import approvals, billing, budgets, exports, insights
from .auth import (
    SESSION_TTL_SECONDS,
    FailureLimiter,
    Identity,
    InvalidKey,
    allows,
    identity_from_row,
    key_prefix,
)
from .dashboard import APPROVAL_HTML, DASHBOARD_HTML
from .db import TIME_FORMAT, Database

MAX_BODY_BYTES = 10 * 1024 * 1024
COOKIE_NAME = "rl_session"
LEVELS = ("low", "medium", "high")
SEVERITIES = ("low", "medium", "high")
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "runledger"
PUBLIC_URL_ENV = "RUNLEDGER_PUBLIC_URL"
SECURE_COOKIES_ENV = "RUNLEDGER_SECURE_COOKIES"
TRUSTED_PROXIES_ENV = "RUNLEDGER_TRUSTED_PROXIES"
HSTS_VALUE = "max-age=31536000"
MAINTENANCE_INTERVAL_S = 3600
RECOVERY_MAX_REQUESTS = 5
RECOVERY_WINDOW_S = 3600
RECOVERY_REPLY = ("If that email has a RunLedger Team subscription, a new admin key is on its way. "
                  "Check your inbox in a few minutes.")
WEBHOOK_HEADERS = ("webhook-id", "webhook-timestamp", "webhook-signature")
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
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SIGN_IN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RunLedger sign in</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:48px 16px;background:#0a0b0d;color:#eef0f2}
@media (prefers-color-scheme: light){body{background:#f7f8f9;color:#121417}}
main{max-width:560px;margin:0 auto}code{font-family:ui-monospace,monospace;background:rgba(127,127,127,.18);padding:2px 6px;border-radius:6px}</style>
</head><body><main>
<h1 style="font-size:20px">RunLedger team dashboard</h1>
<p>Open the dashboard once with your team API key. The server then sets a session cookie and the key leaves the address bar.</p>
<p><code>/?key=YOUR_API_KEY</code></p>
<p>Create a team and its key with <code>runledger team create NAME</code>. Make more keys with <code>runledger key create</code>.</p>
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


class RunLedgerServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR lets a second process take over a bound port.
    allow_reuse_address = os.name != "nt"

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
        billing_config: Optional[billing.BillingConfig] = None,
        mailer: Any = None,
        retention_days: Optional[int] = None,
    ) -> None:
        self.db = db
        self.log_requests = log_requests
        self.tls_context = tls_context
        self.secure_cookies = secure_cookies
        self.trust_proxy = trust_proxy
        self.trusted_proxies: Tuple[_IPNetwork, ...] = tuple(trusted_proxies)
        self.limiter = FailureLimiter()
        self.recovery_limiter = FailureLimiter(max_failures=RECOVERY_MAX_REQUESTS, window_s=RECOVERY_WINDOW_S)
        self.billing = billing_config
        if mailer is None and billing_config is not None:
            mailer = billing.mailer_for(billing_config)
        self.mailer = mailer
        self.retention_days = retention_days
        self._maintenance_stop = threading.Event()
        super().__init__(address, _Handler)
        # The base URL people see in approval links. The default is the bound address,
        # so set RUNLEDGER_PUBLIC_URL when the server sits behind a reverse proxy.
        host, port = self.server_address[:2]
        shown = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in shown:
            shown = f"[{shown}]"
        scheme = "https" if tls_context is not None else "http"
        chosen = public_url or os.environ.get(PUBLIC_URL_ENV) or f"{scheme}://{shown}:{port}"
        self.public_url = chosen.strip().rstrip("/")

    def get_request(self) -> Tuple[socket.socket, Any]:
        # The handshake runs in the request's own thread (see _Handler.setup), so a slow
        # client cannot stall the accept loop.
        sock, addr = super().get_request()
        if self.tls_context is not None:
            sock = self.tls_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr

    def start_maintenance(self, interval_s: float = MAINTENANCE_INTERVAL_S) -> bool:
        """Run billing.run_maintenance now and then every interval_s, on a daemon thread, until
        server_close(). Does nothing (returns False) when neither billing nor retention is on."""
        if self.billing is None and not self.retention_days:
            return False

        def loop() -> None:
            while True:
                try:
                    removed = {k: v for k, v in billing.run_maintenance(self.db, self.retention_days).items() if v}
                    if removed:
                        sys.stderr.write(f"RunLedger: maintenance removed {removed}\n")
                except Exception:
                    traceback.print_exc()
                if self._maintenance_stop.wait(interval_s):
                    return

        threading.Thread(target=loop, name="runledger-maintenance", daemon=True).start()
        return True

    def server_close(self) -> None:
        self._maintenance_stop.set()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):  # includes ssl.SSLError: a client that drops or speaks the wrong protocol
            sys.stderr.write(f"RunLedger: connection from {client_address[0]} ended ({type(exc).__name__})\n")
            return
        super().handle_error(request, client_address)

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

    def identify_session(self, token: Optional[str]) -> Optional[Identity]:
        """The identity behind a dashboard cookie. The session is stored in the database, so it
        survives a restart, and it ends when its key is revoked or rotated (see db.session_key)."""
        row = self.db.session_key(token)
        return identity_from_row(row, "session") if row else None


class _IPv6Server(RunLedgerServer):
    address_family = socket.AF_INET6


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
    billing_config: Optional[billing.BillingConfig] = None,
    mailer: Any = None,
    retention_days: Optional[int] = None,
) -> RunLedgerServer:
    """Open the database and bind the server. Port 0 picks a free port (see server_address).
    The caller owns the server: call shutdown(), server_close() and db.close() when done.

    public_url defaults to $RUNLEDGER_PUBLIC_URL, then to the bound address. tls_cert and
    tls_key (PEM files) turn on HTTPS. secure_cookies, or $RUNLEDGER_SECURE_COOKIES=1, sets
    the Secure flag on the session cookie. trust_proxy honours X-Forwarded-Proto from a
    reverse proxy the same way. trusted_proxies (and $RUNLEDGER_TRUSTED_PROXIES, comma
    separated) are the proxy addresses or CIDR ranges whose X-Forwarded-For is believed.
    billing_config and retention_days default to the environment (see billing.py); mailer
    replaces the Resend sender (tests). Raises ValueError for a bad address or range, and
    billing.BillingConfigError for half-configured billing, before the database is opened."""
    if bool(tls_cert) != bool(tls_key):
        raise TLSConfigError("--tls-cert and --tls-key must be given together.")
    networks = parse_trusted_proxies(list(trusted_proxies or []) + _env_list(TRUSTED_PROXIES_ENV))
    if billing_config is None:
        billing_config = billing.BillingConfig.from_env()
    if retention_days is None:
        retention_days = billing.retention_from_env()
    tls_context = make_tls_context(tls_cert, tls_key) if tls_cert else None
    secure = bool(secure_cookies) or _env_flag(SECURE_COOKIES_ENV)
    db = Database(db_path)
    cls = _IPv6Server if ":" in host else RunLedgerServer
    try:
        return cls(
            (host, port), db, log_requests=log_requests, public_url=public_url,
            tls_context=tls_context, secure_cookies=secure, trust_proxy=bool(trust_proxy),
            trusted_proxies=networks, billing_config=billing_config, mailer=mailer,
            retention_days=retention_days,
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
            sys.stderr.write(f"{self.command} {urlsplit(self.path).path} {code}\n")

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        # The stdlib calls this for malformed requests; keep the reply JSON.
        kind = "bad_request" if code < 500 else "server_error"
        self._send_json(code, {"error": {"code": kind, "message": message or "Request failed."}})

    # Routing

    def _route(self, method: str) -> None:
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
            self._dispatch(method, path, query)
        except _HttpError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, exc.headers)
        except Exception:  # never leak internals to the client
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "internal_error", "message": "Internal server error."}})

    def _dispatch(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        if path == "/":
            self._only(method, ("GET",))
            self._dashboard(query)
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
        elif path == "/billing/polar/webhook" and self.server.billing is not None:
            self._only(method, ("POST",))
            self._polar_webhook()
        elif path == "/billing/recover" and self.server.billing is not None:
            self._only(method, ("POST",))
            self._recover()
        elif path == "/recover" and self.server.billing is not None:
            self._only(method, ("GET",))
            nonce = secrets.token_urlsafe(16)
            self._send_html(200, billing.RECOVER_HTML.replace("__CSP_NONCE__", nonce),
                            {"Content-Security-Policy": _page_csp(nonce)})
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
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send_json(self, status: int, obj: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8", headers)

    def _send_html(self, status: int, html: str, headers: Optional[Dict[str, str]] = None) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _sign_in_page(self) -> None:
        self._send_html(401, SIGN_IN_HTML, {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

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
                return ident
        cookie = self._cookie(COOKIE_NAME)
        if cookie:
            ident = self.server.identify_session(cookie)
            if ident is not None:
                return ident
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
            "key": {"id": ident.key_id, "label": ident.label, "prefix": ident.prefix},
        }
        view = self._billing_view(ident.team_id)
        if view is not None:  # only billed (hosted) teams have this key
            me["billing"] = view
        self._send_json(200, me)

    # Billing (see billing.py)

    def _billing_view(self, team_id: int) -> Optional[Dict[str, Any]]:
        """The subscription as the dashboard shows it, or None for a team that is not billed."""
        row = self.server.db.billing_for_team(team_id)
        if row is None:
            return None
        info = billing.access(row)
        info["seats_used"] = len(self.server.db.seat_users(team_id, _seat_window_start()))
        info["portal_url"] = self.server.billing.portal_url if self.server.billing else None
        return info

    def _check_billing(self, ident: Identity, user: Optional[str] = None) -> None:
        """Refuse a write (402) from a billed team that is read only, or, for a push by `user`,
        from a developer who would need a seat when every seat is taken."""
        db = self.server.db
        row = db.billing_for_team(ident.team_id)
        if row is None:
            return
        info = billing.access(row)
        refusal = billing.write_refusal(info)
        if refusal:
            raise _HttpError(402, "payment_required", refusal)
        if user is not None:
            users = db.seat_users(ident.team_id, _seat_window_start())
            if user not in users and len(users) >= info["seats"]:
                raise _HttpError(402, "seat_limit", billing.seat_refusal(info["seats"]))

    def _polar_webhook(self) -> None:
        config = self.server.billing
        length = self._content_length(billing.WEBHOOK_BODY_LIMIT, "Webhooks")
        raw = self.rfile.read(length) if length else b""
        headers = {name: self.headers.get(name) for name in WEBHOOK_HEADERS}
        try:
            webhook_id = billing.verify_webhook(config.webhook_secret, headers, raw, time.time())
        except billing.InvalidSignature as exc:
            self.server.limiter.record_failure(self._client_ip())
            sys.stderr.write(f"RunLedger: Polar webhook rejected ({exc})\n")
            raise _HttpError(401, "invalid_signature", "The webhook signature is not valid.") from None
        db = self.server.db
        if db.billing_event_seen(webhook_id):
            self._send_json(200, {"ok": True, "result": "duplicate"})
            return
        try:
            event = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _HttpError(400, "invalid_json", "The body must be UTF-8 JSON.") from None
        event_type = str(event.get("type") or "")[:100] if isinstance(event, dict) else ""
        outcome = "ignored"
        if event_type.startswith("subscription."):
            outcome = self._apply_subscription_event(webhook_id, event.get("data"))
        # Recorded last: an event that failed part way (503 below) is redelivered and applied again.
        db.record_billing_event(webhook_id, event_type or "unknown")
        self._send_json(200, {"ok": True, "result": outcome})

    def _apply_subscription_event(self, webhook_id: str, data: Any) -> str:
        config = self.server.billing
        db = self.server.db
        try:
            sub = billing.parse_subscription(data)
        except billing.InvalidEvent as exc:
            sys.stderr.write(f"RunLedger: Polar event {webhook_id} ignored ({exc})\n")
            return "ignored"
        if config.product_ids and sub["product_id"] not in config.product_ids:
            return "other_product"
        result = db.apply_subscription(sub)
        if result["stale"]:
            return "stale"
        team_id = result["team_id"]
        if team_id is None:
            return "pending"
        public_url = self.server.public_url
        status = result["status"]
        if status in billing.ACTIVE and not result["welcome_sent_at"]:
            key = result["created_key"] or db.welcome_key(team_id)
            subject, text = billing.welcome_email(config, public_url, result["team_name"], result["seats"], key)
            try:
                self.server.mailer.send(result["email"], subject, text,
                                        idempotency_key=f"welcome-{sub['id']}-{key_prefix(key)}")
            except Exception as exc:
                sys.stderr.write(f"RunLedger: welcome email for {sub['id']} not sent ({exc})\n")
                raise _HttpError(503, "mail_failed",
                                 "The welcome email could not be sent. Deliver this event again later.") from None
            db.mark_welcome_sent(sub["id"])
            return "provisioned" if result["created_key"] else "welcomed"
        previous = result["previous_status"]
        info = billing.access(result)
        if status in billing.PAST_DUE and previous not in billing.PAST_DUE:
            billing.send_quietly(self.server.mailer, result["email"],
                                 billing.past_due_email(config, result["team_name"], info["grace_until"]),
                                 idempotency_key=f"past-due-{sub['id']}-{result['past_due_since']}")
        elif status in billing.ENDED and previous is not None and previous not in billing.ENDED:
            billing.send_quietly(self.server.mailer, result["email"],
                                 billing.ended_email(config, public_url, result["team_name"], info["deletes_at"]),
                                 idempotency_key=f"ended-{sub['id']}-{result['ended_at']}")
        return "updated"

    def _recover(self) -> None:
        length = self._content_length(approvals.SMALL_BODY_LIMIT, "Requests")
        raw = self.rfile.read(length) if length else b""
        # A cross-site form cannot set this header, and a cross-site fetch with it needs CORS, which
        # this server never grants.
        if (self.headers.get(CSRF_HEADER) or "").strip() != CSRF_VALUE:
            raise _HttpError(403, "csrf_required", f"Send the header '{CSRF_HEADER}: {CSRF_VALUE}'.")
        address = self._client_ip()
        wait = self.server.recovery_limiter.blocked_for(address)
        if wait:
            raise _HttpError(429, "rate_limited", "Too many key requests from this address. Try again later.",
                             {"Retry-After": str(wait)})
        self.server.recovery_limiter.record_failure(address)
        email = billing.valid_email(_json_object(raw).get("email"))
        if email is None:
            raise _HttpError(400, "invalid_email", "Send {\"email\": \"you@example.com\"}.")
        db = self.server.db
        config = self.server.billing
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=billing.RECOVERY_INTERVAL_S)).strftime(TIME_FORMAT)
        for sub in db.subscriptions_for_email(email):
            if not db.claim_recovery(sub["polar_id"], cutoff):
                continue
            label = "recovered " + datetime.now(timezone.utc).strftime("%Y-%m-%d")
            view = db.create_key(sub["team_id"], label, "admin", actor="billing:recover")
            db.record_audit(sub["team_id"], "billing:recover", "billing.recover", view["id"], {"ip": address})
            billing.send_quietly(self.server.mailer, email,
                                 billing.recovery_email(config, self.server.public_url, sub["team_name"], view["key"]))
        # The same answer whether or not the email is known, so it cannot be used to find customers.
        self._send_json(202, {"ok": True, "message": RECOVERY_REPLY})

    def _create_run(self) -> None:
        length = self._content_length()
        raw = self.rfile.read(length) if length else b""
        ident = self._identify()
        self._require(ident, "member", "push runs", write=True)
        if not raw:
            raise _HttpError(400, "empty_body", "Send the receipt as a JSON object in the request body.")
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _HttpError(400, "invalid_json", "The body must be UTF-8 JSON.") from None
        if not isinstance(payload, dict):
            raise _HttpError(400, "invalid_json", "The body must be a JSON object.")
        run = receipt_to_run(payload)
        self._check_billing(ident, user=run["user"])
        self.server.db.upsert_run(ident.team_id, run, actor=ident.actor())
        self._check_budget_alerts(ident)
        self._send_json(201, {
            "id": run["id"],
            "url": "/runs/" + quote(run["id"], safe=""),
            "risk_score": run["risk_score"],
            "risk_level": run["risk_level"],
        })

    def _list_runs(self, query: Dict[str, List[str]]) -> None:
        ident = self._identify()
        ai_verdict = _text_param(query, "ai_verdict", 20)
        if ai_verdict is not None and ai_verdict not in insights.VERDICTS:
            raise _HttpError(400, "bad_request", "'ai_verdict' must be one of looks_safe, needs_review, dangerous.")
        runs = self.server.db.list_runs(
            ident.team_id,
            user=_text_param(query, "user"),
            project=_text_param(query, "project"),
            min_risk=_int_param(query, "min_risk", None, 0, 100),
            limit=_int_param(query, "limit", 100, 1, 500),
            agent=_text_param(query, "agent"),
            min_quality=_int_param(query, "min_quality", None, 0, 100),
            max_quality=_int_param(query, "max_quality", None, 0, 100),
            ai_verdict=ai_verdict,
        )
        self._send_json(200, {"runs": runs})

    def _stats(self, query: Dict[str, List[str]]) -> None:
        ident = self._identify()
        stats = self.server.db.stats(ident.team_id, _int_param(query, "days", 30, 1, 3650))
        stats["team"] = ident.team_name
        self._send_json(200, stats)

    def _insights(self, query: Dict[str, List[str]]) -> None:
        ident = self._identify()
        days = _int_param(query, "days", 30, 1, 3650)
        since = exports.since_for(budgets.now(), days)
        self._send_json(200, insights.compute(self.server.db, ident.team_id, since, days))

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
        if "key" in query:
            # Sign-in: trade the key for an opaque cookie and redirect so the key leaves the URL.
            row = self.server.db.key_for_token((query["key"][0] or "").strip())
            if row is None:
                self.server.limiter.record_failure(self._client_ip())
                self._sign_in_page()
                return
            token = self.server.db.issue_session(row["team_id"], row["id"], row["role"])
            session = identity_from_row(row, "session")
            self.server.db.record_audit(row["team_id"], session.actor(), "auth.sign_in", row["id"],
                                        {"role": row["role"], "ip": self._client_ip()})
            cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}"
            if self._secure():
                cookie += "; Secure"
            self._send(302, b"", "text/plain; charset=utf-8", {"Location": "/", "Set-Cookie": cookie})
            return
        if self._try_identify() is None:
            self._sign_in_page()
            return
        nonce = secrets.token_urlsafe(16)
        self._send_html(200, DASHBOARD_HTML.replace("__CSP_NONCE__", nonce), {"Content-Security-Policy": _page_csp(nonce)})

    # Approvals

    def _create_approval(self) -> None:
        length = self._content_length(approvals.CREATE_BODY_LIMIT, "Approval requests")
        raw = self.rfile.read(length) if length else b""
        ident = self._identify()
        self._require(ident, "member", "request approvals", write=True)
        self._check_billing(ident)
        try:
            fields = approvals.validate_new_approval(_json_object(raw))
        except approvals.InvalidInput as exc:
            raise _HttpError(400, "invalid_approval", str(exc)) from None
        db = self.server.db
        settings = db.approval_settings(ident.team_id)
        approval_id = approvals.new_approval_id()
        view = db.create_approval(ident.team_id, approval_id, fields, settings["approval_ttl_s"])
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
        length = self._content_length(approvals.SMALL_BODY_LIMIT, "Decisions")
        raw = self.rfile.read(length) if length else b""
        ident = self._identify()
        self._require(ident, "member", "decide approvals", write=True)
        try:
            decision, reason, name = approvals.validate_decision(_json_object(raw))
        except approvals.InvalidInput as exc:
            raise _HttpError(400, "invalid_decision", str(exc)) from None
        status = "approved" if decision == "approve" else "denied"
        source = "dashboard" if ident.via == "session" else "api"
        decided_by = f"{source}: {name}" if name else source
        db = self.server.db
        outcome, view = db.decide_approval(
            ident.team_id, approval_id, status, decided_by, reason, db.approval_ttl_s(ident.team_id),
            actor=ident.actor(),
        )
        if outcome == "missing":
            raise _not_found("Approval not found.")
        if outcome == "expired":
            raise _HttpError(409, "expired", "This approval expired before it was decided.")
        if outcome == "conflict":
            raise _HttpError(409, "already_decided", f"This approval was already {view['status']}.")
        self._send_json(200, view)

    def _team_settings(self, method: str) -> None:
        db = self.server.db
        if method == "PUT":
            length = self._content_length(approvals.SMALL_BODY_LIMIT, "Settings")
            raw = self.rfile.read(length) if length else b""
            ident = self._identify()
            self._require(ident, "admin", "change team settings", write=True)
            try:
                changes = approvals.validate_settings(_json_object(raw))
            except approvals.InvalidInput as exc:
                raise _HttpError(400, "invalid_settings", str(exc)) from None
            db.update_approval_settings(ident.team_id, changes, actor=ident.actor())
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

    def _list_keys(self) -> None:
        ident = self._identify()
        self._require(ident, "admin", "list the team's keys")
        self._send_json(200, {"keys": self.server.db.list_keys(ident.team_id)})

    def _create_key(self) -> None:
        length = self._content_length(approvals.SMALL_BODY_LIMIT, "Key requests")
        raw = self.rfile.read(length) if length else b""
        ident = self._identify()
        self._require(ident, "admin", "create keys", write=True)
        body = _json_object(raw)
        try:
            view = self.server.db.create_key(ident.team_id, body.get("label"), body.get("role"), ident.actor())
        except InvalidKey as exc:
            raise _HttpError(400, "invalid_key", str(exc)) from None
        self._send_json(201, view)

    def _revoke_key(self, raw_id: str) -> None:
        key_id = _key_id(raw_id)
        ident = self._identify()
        self._require(ident, "admin", "revoke keys", write=True)
        outcome, view = self.server.db.revoke_key(ident.team_id, key_id, ident.actor())
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
        outcome, view = self.server.db.rotate_key(ident.team_id, key_id, ident.actor())
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
            length = self._content_length(approvals.SMALL_BODY_LIMIT, "Budgets")
            raw = self.rfile.read(length) if length else b""
            ident = self._identify()
            self._require(ident, "admin", "change the team budget", write=True)
            try:
                values = budgets.validate_budget(_json_object(raw))
            except _HttpError as exc:  # no body, or not a JSON object
                raise _HttpError(400, "invalid_budget", exc.message) from None
            except budgets.InvalidBudget as exc:
                raise _HttpError(400, "invalid_budget", str(exc)) from None
            settings = db.update_budget_settings(ident.team_id, values, ident.actor())
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
        self.server.db.record_audit(ident.team_id, ident.actor(), f"export.{kind}", None,
                                    {"days": days, "rows": rows})
        self._send(200, body, content_type, headers)


# Helpers


def _seat_window_start() -> str:
    return billing.stamp(billing.now() - timedelta(days=billing.SEAT_WINDOW_DAYS))


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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(max(0.0, float(value)), 6)


def _iso(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime(TIME_FORMAT)


def _level_for(score: int) -> str:
    return "low" if score < 25 else "medium" if score < 60 else "high"


def _title(payload: Dict[str, Any]) -> str:
    for item in _list(payload.get("request")):
        if isinstance(item, str) and item.strip():
            return " ".join(item.split())[:200]
    return _text(payload.get("overview"), 200) or ""


def receipt_to_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a pushed receipt and flatten it into the columns the store keeps.

    Accepts the output of `runledger receipt --format json`, plus the fields the
    client adds: user, project, html, and models with cost_usd. Missing or odd
    values fall back to safe defaults. Only a missing session_id is rejected."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not _valid_id(session_id):
        raise _HttpError(400, "invalid_receipt", "The receipt needs a session_id of 1-200 characters.")

    totals = _dict(payload.get("totals"))
    risk = _dict(payload.get("risk"))
    steps = [s for s in _list(payload.get("steps")) if isinstance(s, dict)]
    cwd = _text(payload.get("cwd"), 500)
    user = _text(payload.get("user"), 200) or "unknown"
    project = _text(payload.get("project"), 200) or _folder_name(cwd) or "unknown"
    agent = _text(payload.get("agent"), 100)  # the agent's label, e.g. "Claude Code"

    score = _int(risk.get("score"), 0, 0, 100) or 0
    level = str(risk.get("level") or "").lower()
    if level not in LEVELS:
        level = _level_for(score)

    models: Dict[str, Dict[str, Any]] = {}
    for name, info in list(_dict(payload.get("models")).items())[:50]:
        label = str(name)[:200]
        info = _dict(info)
        cost = _money(info.get("cost_usd"))
        if cost is None:  # older receipts: fall back to the per-step costs
            step_costs = [
                c for c in (_money(s.get("cost_usd")) for s in steps if s.get("model") == name)
                if c is not None
            ]
            cost = round(sum(step_costs), 6) if step_costs else None
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
    receipt["user"] = user
    receipt["project"] = project
    receipt.update({k: review[k] for k in ("quality", "recommendations", "ai_review")})

    return {
        "id": session_id,
        "user": user,
        "project": project,
        "agent": agent,
        "title": _title(payload),
        "started_at": _iso(payload.get("started")),
        "ended_at": _iso(payload.get("ended")),
        "models": models,
        "steps": _int(totals.get("steps"), len(steps)) or 0,
        "tokens": _int(totals.get("tokens"), 0) or 0,
        "files_changed": _int(totals.get("files_changed"), 0) or 0,
        "cost": _money(totals.get("cost_usd")),
        "risk_score": score,
        "risk_level": level,
        "risks": risks,
        "quality_score": review["quality_score"],
        "quality_grade": review["quality_grade"],
        "ai_verdict": review["ai_verdict"],
        "est_savings_usd": review["est_savings_usd"],
        "receipt": receipt,
        "receipt_html": html,
    }


def _folder_name(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]
    return parts[-1] if parts else None
