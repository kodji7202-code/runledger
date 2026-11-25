"""FastAPI/uvicorn transport for the team server.

FastAPI owns HTTP method and path dispatch. The endpoint bodies still share the legacy
security-sensitive implementation through a small capture adapter, so the externally
visible API, dashboard and CLI behavior remain unchanged while the transport migration
is completed and verified.
"""
from __future__ import annotations

import asyncio
import io
import os
import queue
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import Response
from uvicorn import Config, Server

from .app import RunLedgerServer, _Handler

_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


class _ASGIBody:
    """Synchronous file-like access to an ASGI request body, started only when read."""

    def __init__(self, request: Request, closing: Any) -> None:
        self.request = request
        self.closing = closing
        self.loop = asyncio.get_running_loop()
        self.chunks: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.buffer = bytearray()
        self.eof = False
        self.started = False

    async def _pump(self) -> None:
        try:
            async for chunk in self.request.stream():
                self.chunks.put(chunk)
        except Exception:
            # A truncated upload is the same end-of-file a socket server would observe.
            pass
        self.chunks.put(None)

    def _start(self) -> None:
        if not self.started:
            self.started = True
            asyncio.run_coroutine_threadsafe(self._pump(), self.loop)

    def read(self, size: int = -1) -> bytes:
        self._start()
        while not self.eof and (size < 0 or len(self.buffer) < size):
            try:
                chunk = self.chunks.get(timeout=0.1)
            except queue.Empty:
                self.eof = bool(self.closing())
                continue
            if chunk is None:
                self.eof = True
            else:
                self.buffer.extend(chunk)
        if size < 0:
            out = bytes(self.buffer)
            self.buffer.clear()
            return out
        out = bytes(self.buffer[:size])
        del self.buffer[:size]
        return out

    def read_with_timeout(self, size: int, timeout: float) -> bytes:
        """Read at most `size` bytes, returning what is available when `timeout` expires."""
        self._start()
        deadline = time.monotonic() + timeout
        while not self.eof and len(self.buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                chunk = self.chunks.get(timeout=remaining)
            except queue.Empty:
                break
            if chunk is None:
                self.eof = True
            else:
                self.buffer.extend(chunk)
        out = bytes(self.buffer[:size])
        del self.buffer[:size]
        return out


class _HeadersAdapter:
    """The small email-message interface the existing handlers use."""

    def __init__(self, headers: Any) -> None:
        self.headers = headers

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.headers.get(name, default)

    def get_all(self, name: str) -> Optional[List[str]]:
        values = self.headers.getlist(name)
        return values or None

    def get_content_type(self) -> str:
        return (self.headers.get("content-type", "text/plain") or "text/plain").split(";", 1)[0].strip().lower()


class _CapturedHandler(_Handler):
    """A `_Handler` whose response is captured instead of written to a socket."""

    def __init__(self, server: RunLedgerServer, request: Request, body: Any) -> None:
        # Deliberately do not call BaseHTTPRequestHandler.__init__: it owns socket I/O in the
        # legacy implementation. The endpoint methods only use these request attributes.
        self.server = server
        self.command = request.method
        raw_path = request.scope.get("raw_path") or request.url.path.encode("ascii")
        raw_query = request.scope.get("query_string", b"")
        self.path = raw_path.decode("latin-1") + (("?" + raw_query.decode("latin-1")) if raw_query else "")
        self.headers = _HeadersAdapter(request.headers)
        self.rfile = body
        self.wfile = io.BytesIO()
        client = request.client
        self.client_address = (client.host if client else "", client.port if client else 0)
        self.close_connection = True
        self.status = 500
        self.response_headers: List[Tuple[str, str]] = []

    def send_response(self, code: int, message: Optional[str] = None) -> None:
        self.log_request(code)
        self.status = code

    def send_header(self, keyword: str, value: str) -> None:
        self.response_headers.append((keyword, value))

    def end_headers(self) -> None:
        pass

    def handle(self) -> None:
        if len(self.path.encode("utf-8")) > 65536:
            self.send_error(414, "Request-URI too long.")
            return
        if self.command not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            # BaseHTTPRequestHandler answers unimplemented methods before routing.
            self.send_error(501, f"Unsupported method ({self.command!r})")
            return
        self._route(self.command)

    def response(self) -> Response:
        body = self.wfile.getvalue() if self.command != "HEAD" else b""
        response = Response(content=body, status_code=self.status)
        response.raw_headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in self.response_headers
        ]
        return response


class _MethodCompatibility:
    """Answer every unimplemented HTTP method through the legacy handler, not Starlette's 405."""

    def __init__(self, app: Any, server: RunLedgerServer) -> None:
        self.app = app
        self.server = server

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") in _METHODS:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        handler = _CapturedHandler(self.server, request, _ASGIBody(request, self.server.closing.is_set))
        await asyncio.to_thread(handler.handle)
        await handler.response()(scope, receive, send)


def create_app(server: RunLedgerServer) -> Any:
    """The ASGI application for one configured team server."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def endpoint(request: Request) -> Response:
        handler = _CapturedHandler(server, request, _ASGIBody(request, server.closing.is_set))
        # The existing database object is already shared safely across request threads. Run the
        # synchronous endpoint off the event loop. Its body stream starts only when the
        # authenticated endpoint actually reads it.
        await asyncio.to_thread(handler.handle)
        return handler.response()

    # FastAPI dispatches the public surface explicitly. Dynamic identifiers use the path
    # converter because run ids are user-controlled text in the underlying API and may
    # contain an encoded slash. The final catch-all preserves the legacy JSON 404 rather
    # than exposing Starlette's default error schema.
    routes = (
        "/health",
        "/scim/v2",
        "/scim/v2/{rest:path}",
        "/api/scim/token",
        "/auth/oidc/team",
        "/auth/oidc/{provider}/{action}",
        "/auth/saml/team",
        "/auth/saml/{provider}/{action}",
        "/",
        "/session",
        "/session/logout",
        "/sessions",
        "/sessions/{raw_id:path}",
        "/api/me",
        "/api/runs",
        "/api/stats",
        "/api/insights",
        "/api/runs/{raw_id:path}",
        "/runs/{raw_id:path}",
        "/api/approvals",
        "/api/approvals/{approval_id}/decision",
        "/api/approvals/{approval_id}",
        "/api/team/settings",
        "/approvals/{approval_id:path}",
        "/api/keys",
        "/api/members",
        "/api/members/{user_id:path}",
        "/api/keys/{key_id}/revoke",
        "/api/keys/{key_id}/rotate",
        "/api/audit",
        "/api/budgets",
        "/api/budgets/status",
        "/api/export/runs.csv",
        "/api/export/audit.csv",
        "/api/export/report.html",
        "/{full_path:path}",
    )
    for path in routes:
        app.add_api_route(path, endpoint, methods=_METHODS, include_in_schema=False)

    return _MethodCompatibility(app, server)


class UvicornServer:
    """The small `serve_forever`/`shutdown` surface the CLI and tests already use."""

    def __init__(
        self,
        address: Tuple[str, int],
        app: Any,
        config_values: Optional[Dict[str, Any]] = None,
        tls_context: Optional[Any] = None,
    ) -> None:
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.socket = socket.socket(family, socket.SOCK_STREAM)
        # On Windows, SO_REUSEADDR lets a second process take over a bound port.
        if os.name != "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(address)
        self.socket.listen(100)
        self.server_address = self.socket.getsockname()
        values: Dict[str, Any] = {"log_level": "error", "access_log": False, "http": "h11",
                                  "proxy_headers": False, "h11_max_incomplete_event_size": 131072,
                                  "headers": [("server", "RunLedger")]}
        values.update(config_values or {})
        config = Config(app, **values)
        if tls_context is not None:
            # uvicorn only grew ssl_context_factory in 0.47, the first release that requires
            # Python 3.10. Pre-loading the configuration and installing the already validated
            # context keeps TLS 1.2+ behavior identical on the Python 3.9-compatible releases.
            config.load()
            config.ssl = tls_context
        self.uvicorn = Server(config)
        self._stopped = threading.Event()

    def serve_forever(self) -> None:
        try:
            asyncio.run(self.uvicorn.serve(sockets=[self.socket]))
        finally:
            self._stopped.set()

    def shutdown(self) -> None:
        self.uvicorn.should_exit = True
        self._stopped.wait(5)

    def server_close(self) -> None:
        self.socket.close()
