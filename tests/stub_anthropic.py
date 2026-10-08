"""A local stand-in for the Anthropic Messages API, for the llm and review tests.

Point ANTHROPIC_BASE_URL at StubAnthropic.url. Every request is recorded (path, lower-cased
headers and the parsed JSON body) and each one is answered with the next queued reply.
Nothing leaves 127.0.0.1, so the tests never reach the real API. Standard library only.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Synthetic secrets. They match the risk rules' patterns and are not real credentials.
ANTHROPIC_KEY = "sk-ant-api03-" + "x" * 30
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
GITHUB_TOKEN = "ghp_" + "a" * 36


class StubAnthropic:
    def __init__(self):
        self.requests = []
        self.replies = []  # queue of (status, body, headers); body is a dict or a str
        self.sleeps = []   # filled by tests that patch time.sleep
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def queue(self, status=200, body=None, headers=None):
        self.replies.append((status, body if body is not None else message_reply(), headers or {}))
        return self

    def bodies(self):
        return [r["body"] for r in self.requests]

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # keep test output quiet
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                stub.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                    "raw": raw.decode("utf-8", "replace"),
                })
                if stub.replies:
                    status, payload, headers = stub.replies.pop(0)
                else:
                    status, payload, headers = 500, error_body("stub: no reply queued"), {}
                data = (payload if isinstance(payload, str) else json.dumps(payload)).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

        return Handler


def message_reply(text="ok", usage=None, model="claude-haiku-5-5", stop_reason="end_turn"):
    return {"id": "msg_stub", "type": "message", "role": "assistant", "model": model,
            "stop_reason": stop_reason,
            "content": [{"type": "text", "text": text}],
            "usage": usage or {"input_tokens": 100, "output_tokens": 20}}


def tool_reply(tool_input, tool="record_review", usage=None, model="claude-sonnet-5-5",
               stop_reason="tool_use"):
    return {"id": "msg_stub", "type": "message", "role": "assistant", "model": model,
            "stop_reason": stop_reason,
            "content": [{"type": "tool_use", "id": "toolu_stub", "name": tool, "input": tool_input}],
            "usage": usage or {"input_tokens": 1000, "output_tokens": 200}}


def error_body(message, kind="api_error"):
    return {"type": "error", "error": {"type": kind, "message": message}}
