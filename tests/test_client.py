"""runledger.client: what the push client does with a server error. The body of an
HTTPError must be read and closed (a leaked response is a ResourceWarning under
`-W error::ResourceWarning`), and the server's own message is what the user sees."""
import io
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from runledger import client

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


class _RecordingBody(io.BytesIO):
    """An error body that remembers being closed, the way a real response must be."""

    def __init__(self, data: bytes = b""):
        super().__init__(data)
        self.closed_by_us = False

    def close(self):
        self.closed_by_us = True
        super().close()


def _http_error(status: int, payload: bytes, reason: str = "Error"):
    body = _RecordingBody(payload)
    return urllib.error.HTTPError("http://127.0.0.1:8792/api/runs", status, reason, {}, body), body


def test_server_message_http_error_body_closed():
    exc, body = _http_error(401, json.dumps({"error": {"message": "wrong key"}}).encode("utf-8"))
    assert client._server_message(exc) == "wrong key"
    assert body.closed_by_us


def test_server_message_falls_back_to_the_reason_and_closes():
    exc, body = _http_error(500, b"<html>not json</html>", reason="Server Error")
    assert client._server_message(exc) == "Server Error"
    assert body.closed_by_us


def test_server_message_closes_a_body_that_cannot_be_read():
    class _BrokenBody(_RecordingBody):
        def read(self, *args, **kwargs):
            raise OSError("connection reset")

    body = _BrokenBody()
    exc = urllib.error.HTTPError("http://127.0.0.1:8792/api/runs", 500, "Server Error", {}, body)
    assert client._server_message(exc) == "Server Error"
    assert body.closed_by_us


class _StubPushServer:
    """A stand-in for the team server: every POST is answered with one status and body."""

    def __init__(self, status: int = 401, payload: dict = None):
        stub = self
        self.requests = []
        answer = json.dumps(payload if payload is not None else {"error": {"message": "wrong key"}})

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # keep test output quiet
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                stub.requests.append((self.path, self.rfile.read(length)))
                data = answer.encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def stub_server():
    server = _StubPushServer()
    try:
        yield server
    finally:
        server.close()


def test_push_reports_the_server_message(stub_server):
    with pytest.raises(client.PushError, match="wrong key"):
        client.push(stub_server.url, "wrong-key", FIX, user="dev")
    assert [path for path, _body in stub_server.requests] == ["/api/runs"]
