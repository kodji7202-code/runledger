"""HTTP server for the RunLedger team dashboard and the push API.

  POST /api/runs                              push a receipt (Bearer API key) -> 201
  GET  /api/runs[?user&project&min_risk&limit] list runs (API key or dashboard session)
  GET  /api/runs/<id>                         one run with its receipt JSON and risk reasons
  GET  /api/stats[?days=30]                   team totals, cost by developer/model/project, top risks
  GET  /runs/<id>                             the stored receipt HTML
  POST /api/approvals                         request a human approval (Bearer key) -> 201 {id, status}
  GET  /api/approvals[?status=pending]        list approvals (API key or dashboard session)
  GET  /api/approvals/<id>                    one approval: status, decided_by, decided_at, reason
  POST /api/approvals/<id>/decision           approve or deny: Bearer key, or dashboard session
                                              plus header X-Requested-With: runledger
  GET  /approvals/<id>                        approval page for a person (dashboard session)
  GET|PUT /api/team/settings                  webhook URLs and approval_ttl_s (Bearer key)
  GET  /                                      dashboard; sign in once with /?key=API_KEY
  GET  /health                                liveness, no auth

Standard library only. Errors are JSON ({"error": {"code", "message"}}) and never
include tracebacks; tracebacks go to the server's stderr.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import __version__
from . import approvals
from .dashboard import APPROVAL_HTML, DASHBOARD_HTML
from .db import TIME_FORMAT, Database

MAX_BODY_BYTES = 10 * 1024 * 1024
COOKIE_NAME = "rl_session"
SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 1000
LEVELS = ("low", "medium", "high")
SEVERITIES = ("low", "medium", "high")
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "runledger"
PUBLIC_URL_ENV = "RUNLEDGER_PUBLIC_URL"
_APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

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
<p>Create a team and its key with <code>runledger team create NAME</code>.</p>
</main></body></html>
"""


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


class _Sessions:
    """Dashboard sessions: random opaque tokens kept in memory, so the API key never
    sits in a cookie. A server restart signs everyone out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, Tuple[Dict[str, Any], float]]" = OrderedDict()

    def issue(self, team: Dict[str, Any]) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[token] = (team, time.time() + SESSION_TTL_SECONDS)
            while len(self._items) > MAX_SESSIONS:
                self._items.popitem(last=False)
        return token

    def team_for(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            team, expires = item
            if expires < time.time():
                del self._items[token]
                return None
            return team


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
    ) -> None:
        self.db = db
        self.log_requests = log_requests
        self.sessions = _Sessions()
        super().__init__(address, _Handler)
        # The base URL people see in approval links. The default is the bound address,
        # so set RUNLEDGER_PUBLIC_URL when the server sits behind a reverse proxy.
        host, port = self.server_address[:2]
        shown = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in shown:
            shown = f"[{shown}]"
        chosen = public_url or os.environ.get(PUBLIC_URL_ENV) or f"http://{shown}:{port}"
        self.public_url = chosen.strip().rstrip("/")


class _IPv6Server(RunLedgerServer):
    address_family = socket.AF_INET6


def make_server(
    db_path: str = "runledger.db",
    host: str = "127.0.0.1",
    port: int = 8787,
    log_requests: bool = False,
    public_url: Optional[str] = None,
) -> RunLedgerServer:
    """Open the database and bind the server. Port 0 picks a free port (see server_address).
    The caller owns the server: call shutdown(), server_close() and db.close() when done.
    public_url defaults to $RUNLEDGER_PUBLIC_URL, then to the bound address."""
    db = Database(db_path)
    cls = _IPv6Server if ":" in host else RunLedgerServer
    try:
        return cls((host, port), db, log_requests=log_requests, public_url=public_url)
    except Exception:
        db.close()
        raise


class _Handler(BaseHTTPRequestHandler):
    server_version = "RunLedger"
    sys_version = ""
    timeout = 30  # a stalled client cannot hold a thread forever

    def do_GET(self) -> None:
        self._route("GET")

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
            if path == "/health":
                self._only(method, ("GET",))
                self._send_json(200, {"ok": True, "version": __version__})
            elif path == "/":
                self._only(method, ("GET",))
                self._dashboard(query)
            elif path == "/api/runs":
                self._only(method, ("GET", "POST"))
                if method == "POST":
                    self._create_run()
                else:
                    self._list_runs(query)
            elif path == "/api/stats":
                self._only(method, ("GET",))
                self._stats(query)
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
            else:
                raise _HttpError(404, "not_found", "No such endpoint.")
        except _HttpError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, exc.headers)
        except Exception:  # never leak internals to the client
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "internal_error", "message": "Internal server error."}})

    def _only(self, method: str, allowed: Tuple[str, ...]) -> None:
        if method not in allowed:
            raise _HttpError(
                405, "method_not_allowed", f"Use {', '.join(allowed)} for this endpoint.",
                {"Allow": ", ".join(allowed)},
            )

    # Responses

    def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, obj: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8", headers)

    def _send_html(self, status: int, html: str, headers: Optional[Dict[str, str]] = None) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8", headers)

    # Auth

    def _bearer_team(self) -> Optional[Dict[str, Any]]:
        scheme, _, token = (self.headers.get("Authorization") or "").strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self.server.db.team_for_key(token.strip())

    def _cookie(self, name: str) -> Optional[str]:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name and value:
                return value
        return None

    def _reader_team(self) -> Dict[str, Any]:
        team = self._bearer_team() or self.server.sessions.team_for(self._cookie(COOKIE_NAME))
        if team is None:
            raise _unauthorized()
        return team

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

    def _create_run(self) -> None:
        length = self._content_length()
        raw = self.rfile.read(length) if length else b""
        team = self._bearer_team()
        if team is None:
            raise _unauthorized()
        if not raw:
            raise _HttpError(400, "empty_body", "Send the receipt as a JSON object in the request body.")
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise _HttpError(400, "invalid_json", "The body must be UTF-8 JSON.") from None
        if not isinstance(payload, dict):
            raise _HttpError(400, "invalid_json", "The body must be a JSON object.")
        run = receipt_to_run(payload)
        self.server.db.upsert_run(team["id"], run)
        self._send_json(201, {
            "id": run["id"],
            "url": "/runs/" + quote(run["id"], safe=""),
            "risk_score": run["risk_score"],
            "risk_level": run["risk_level"],
        })

    def _list_runs(self, query: Dict[str, List[str]]) -> None:
        team = self._reader_team()
        runs = self.server.db.list_runs(
            team["id"],
            user=_text_param(query, "user"),
            project=_text_param(query, "project"),
            min_risk=_int_param(query, "min_risk", None, 0, 100),
            limit=_int_param(query, "limit", 100, 1, 500),
        )
        self._send_json(200, {"runs": runs})

    def _stats(self, query: Dict[str, List[str]]) -> None:
        team = self._reader_team()
        stats = self.server.db.stats(team["id"], _int_param(query, "days", 30, 1, 3650))
        stats["team"] = team["name"]
        self._send_json(200, stats)

    def _get_run(self, raw_id: str) -> None:
        team = self._reader_team()
        run = self.server.db.get_run(team["id"], _run_id(raw_id))
        if run is None:
            raise _not_found()
        self._send_json(200, {"run": run})

    def _receipt(self, raw_id: str) -> None:
        team = self._reader_team()
        html = self.server.db.get_receipt_html(team["id"], _run_id(raw_id))
        if html is None:
            raise _not_found("Run not found, or it has no stored HTML receipt.")
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": RECEIPT_CSP})

    def _dashboard(self, query: Dict[str, List[str]]) -> None:
        if "key" in query:
            # Sign-in: trade the key for an opaque cookie and redirect so the key leaves the URL.
            team = self.server.db.team_for_key((query["key"][0] or "").strip())
            if team is None:
                self._send_html(401, SIGN_IN_HTML, {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
                return
            token = self.server.sessions.issue(team)
            self._send(302, b"", "text/plain; charset=utf-8", {
                "Location": "/",
                "Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}",
            })
            return
        team = self._bearer_team() or self.server.sessions.team_for(self._cookie(COOKIE_NAME))
        if team is None:
            self._send_html(401, SIGN_IN_HTML, {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
            return
        nonce = secrets.token_urlsafe(16)
        self._send_html(200, DASHBOARD_HTML.replace("__CSP_NONCE__", nonce), {"Content-Security-Policy": _page_csp(nonce)})

    # Approvals

    def _decider(self) -> Tuple[Dict[str, Any], str]:
        """Who is deciding: the team's API key ("api") or a dashboard session ("dashboard").
        A cookie decision must also send X-Requested-With, a header a cross-site form cannot set."""
        team = self._bearer_team()
        if team is not None:
            return team, "api"
        team = self.server.sessions.team_for(self._cookie(COOKIE_NAME))
        if team is None:
            raise _unauthorized()
        if (self.headers.get(CSRF_HEADER) or "").strip() != CSRF_VALUE:
            raise _HttpError(
                403, "csrf_required",
                f"Dashboard decisions must send the header '{CSRF_HEADER}: {CSRF_VALUE}'.",
            )
        return team, "dashboard"

    def _create_approval(self) -> None:
        length = self._content_length(approvals.CREATE_BODY_LIMIT, "Approval requests")
        raw = self.rfile.read(length) if length else b""
        team = self._bearer_team()  # agents create approvals with the team key, never with a dashboard session
        if team is None:
            raise _unauthorized()
        try:
            fields = approvals.validate_new_approval(_json_object(raw))
        except approvals.InvalidInput as exc:
            raise _HttpError(400, "invalid_approval", str(exc)) from None
        db = self.server.db
        settings = db.approval_settings(team["id"])
        approval_id = approvals.new_approval_id()
        view = db.create_approval(team["id"], approval_id, fields, settings["approval_ttl_s"])
        approvals.notify_new_approval(settings, view, self.server.public_url)
        self._send_json(201, {"id": approval_id, "status": "pending"})

    def _list_approvals(self, query: Dict[str, List[str]]) -> None:
        team = self._reader_team()
        status = _text_param(query, "status", 20)
        if status is not None and status not in approvals.APPROVAL_STATUSES:
            raise _HttpError(400, "bad_request", "'status' must be one of pending, approved, denied, expired.")
        db = self.server.db
        items = db.list_approvals(
            team["id"], status, db.approval_ttl_s(team["id"]), limit=_int_param(query, "limit", 100, 1, 200),
        )
        self._send_json(200, {"approvals": items})

    def _get_approval(self, raw_id: str) -> None:
        team = self._reader_team()
        approval_id = _approval_id(raw_id)
        db = self.server.db
        view = db.get_approval(team["id"], approval_id, db.approval_ttl_s(team["id"]))
        if view is None:
            raise _not_found("Approval not found.")
        self._send_json(200, view)

    def _decide_approval(self, raw_id: str) -> None:
        approval_id = _approval_id(raw_id)
        length = self._content_length(approvals.SMALL_BODY_LIMIT, "Decisions")
        raw = self.rfile.read(length) if length else b""
        team, source = self._decider()
        try:
            decision, reason, name = approvals.validate_decision(_json_object(raw))
        except approvals.InvalidInput as exc:
            raise _HttpError(400, "invalid_decision", str(exc)) from None
        status = "approved" if decision == "approve" else "denied"
        decided_by = f"{source}: {name}" if name else source
        db = self.server.db
        outcome, view = db.decide_approval(
            team["id"], approval_id, status, decided_by, reason, db.approval_ttl_s(team["id"]),
        )
        if outcome == "missing":
            raise _not_found("Approval not found.")
        if outcome == "expired":
            raise _HttpError(409, "expired", "This approval expired before it was decided.")
        if outcome == "conflict":
            raise _HttpError(409, "already_decided", f"This approval was already {view['status']}.")
        self._send_json(200, view)

    def _team_settings(self, method: str) -> None:
        raw = b""
        if method == "PUT":
            length = self._content_length(approvals.SMALL_BODY_LIMIT, "Settings")
            raw = self.rfile.read(length) if length else b""
        team = self._bearer_team()
        if team is None:
            raise _unauthorized()
        db = self.server.db
        if method == "PUT":
            try:
                changes = approvals.validate_settings(_json_object(raw))
            except approvals.InvalidInput as exc:
                raise _HttpError(400, "invalid_settings", str(exc)) from None
            db.update_approval_settings(team["id"], changes)
        self._send_json(200, {"team": team["name"], **db.approval_settings(team["id"])})

    def _approval_page(self, raw_id: str) -> None:
        approval_id = _approval_id(raw_id)
        team = self._bearer_team() or self.server.sessions.team_for(self._cookie(COOKIE_NAME))
        if team is None:
            self._send_html(401, SIGN_IN_HTML, {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
            return
        db = self.server.db
        if db.get_approval(team["id"], approval_id, db.approval_ttl_s(team["id"])) is None:
            raise _not_found("Approval not found.")
        nonce = secrets.token_urlsafe(16)
        self._send_html(200, APPROVAL_HTML.replace("__CSP_NONCE__", nonce), {"Content-Security-Policy": _page_csp(nonce)})


# Helpers


def _page_csp(nonce: str) -> str:
    return (
        "default-src 'none'; script-src 'nonce-" + nonce + "'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def _size_text(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit // 1024} KB"


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

    receipt = {k: v for k, v in payload.items() if k != "html"}
    receipt["user"] = user
    receipt["project"] = project

    return {
        "id": session_id,
        "user": user,
        "project": project,
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
        "receipt": receipt,
        "receipt_html": html,
    }


def _folder_name(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]
    return parts[-1] if parts else None
