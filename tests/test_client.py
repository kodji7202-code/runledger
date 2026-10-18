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
from runledger.cli import main

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"

SAFE_CWD = "C:\\Users\\claud\\proj"
SAFE_FILE = SAFE_CWD + "\\secret\\x.py"
SAFE_EMAIL = "me@example.com"


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


# ---------------------------------------------------------------- safe mode

@pytest.fixture
def push_session(tmp_path):
    """A native session that mentions a home folder and an e-mail address."""
    doc = {
        "runledger_format": 1,
        "agent": "demo-agent",
        "session_id": "safe-0001-native",
        "cwd": SAFE_CWD,
        "git_branch": "main",
        "started": "2026-10-08T09:00:00Z",
        "ended": "2026-10-08T09:04:30Z",
        "prompts": [f"Ship the release and mail {SAFE_EMAIL} when it is done."],
        "final_message": f"Read {SAFE_FILE} and wrote the summary.",
        "reported_cost": 0.02,
        "steps": [
            {"tool": "Read", "input": {"file_path": SAFE_FILE}, "result_text": "ok"},
            {"tool": "Write", "input": {"file_path": SAFE_FILE, "content": "k = 1\n"},
             "result_text": "ok"},
            {"tool": "Bash", "input": {"command": "pytest -q"}, "result_text": "12 passed"},
        ],
    }
    path = tmp_path / "safe_native.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _names_home_folder(value) -> bool:
    return any("C:\\Users\\claud" in text or "C:/Users/claud" in text for text in _strings(value))


def _without_user(value) -> dict:
    return {k: v for k, v in value.items() if k != "user"}


def test_push_safe_masks_paths_and_emails(push_session):
    payload = client.build_payload(push_session, user=SAFE_EMAIL, safe=True)
    assert payload["safe"] is True
    assert not _names_home_folder(payload)
    assert SAFE_EMAIL not in json.dumps(_without_user(payload))
    assert SAFE_EMAIL in payload["user"]        # the explicit developer name is used as given
    assert payload["cwd"].replace("\\", "/") == "proj"
    assert [f["path"].replace("\\", "/") for f in payload["files"]] == ["secret/x.py"]
    assert "[EMAIL]" in payload["request"][0]
    assert "x.py" in payload["steps"][0]["summary"]
    assert payload["session_id"] == "safe-0001-native"
    assert len(payload["steps"]) == 3


def test_push_default_unchanged(push_session):
    payload = client.build_payload(push_session, user="dev")
    assert payload["safe"] is False
    assert _names_home_folder(payload)
    assert any(SAFE_EMAIL in prompt for prompt in payload["request"])
    assert [f["path"].replace("\\", "/") for f in payload["files"]] == ["secret/x.py"]
    assert payload["project"] == "proj" and payload["user"] == "dev"
    assert len(payload["steps"]) == 3


def test_push_sends_the_safe_receipt(push_session):
    server = _StubPushServer(status=201, payload={"id": "safe-0001-native", "url": "/runs/safe-0001-native",
                                                  "risk_score": 5, "risk_level": "low", "revision": 1})
    try:
        client.push(server.url, "rl_test", push_session, user=SAFE_EMAIL, safe=True)
    finally:
        server.close()
    body = json.loads(server.requests[-1][1].decode("utf-8"))
    assert body["safe"] is True
    assert not _names_home_folder(_without_user(body))
    assert SAFE_EMAIL not in json.dumps(_without_user(body))


@pytest.mark.parametrize("url,hint", [
    ("https://runledger.example.com", True),
    ("https://runledger.example.com/team/../x", True),
    ("https://team.example.com:8443", True),
    ("https://127.0.0.1:8792", False),
    ("https://localhost", False),
    ("https://[::1]:8792", False),
    ("http://runledger.example.com", False),   # not HTTPS
    ("http://192.168.1.10:8792", False),       # not HTTPS
    ("", False),
])
def test_safe_hint_only_for_a_remote_https_server(url, hint):
    text = client.safe_hint(url, False)
    if hint:
        assert text and text.startswith("tip: --safe")
    else:
        assert text is None
    assert client.safe_hint(url, True) is None   # nothing to tip about in safe mode


def _recording_push(monkeypatch):
    calls = []

    def fake_push(server_url, api_key, session_path, user=None, project=None, timeout=60.0, **kwargs):
        calls.append(dict(kwargs, server=server_url, user=user))
        return {"id": "safe-0001-native", "url": "/runs/safe-0001-native",
                "risk_score": 5, "risk_level": "low", "revision": 1}

    monkeypatch.setattr(client, "push", fake_push)
    return calls


def _cli_push(push_session, extra=()):
    return main(["push", str(push_session), "--server", "http://127.0.0.1:8792",
                 "--key", "rl_test", "--user", "dev", *extra])


@pytest.mark.parametrize("flag,env,expected", [
    ([], None, False),          # default: what the server gets does not change
    (["--safe"], None, True),
    (["--no-safe"], None, False),
    ([], "1", True),
    ([], "0", False),
    ([], "", False),
    (["--safe"], "0", True),    # the flag wins over the environment
    (["--no-safe"], "1", False),
])
def test_push_cli_safe_flag_and_environment(push_session, monkeypatch, flag, env, expected):
    calls = _recording_push(monkeypatch)
    if env is not None:
        monkeypatch.setenv("RUNLEDGER_PUSH_SAFE", env)
    assert _cli_push(push_session, flag) == 0
    assert calls[-1]["safe"] is expected


@pytest.mark.parametrize("flag,env,configured,expected", [
    ([], None, True, True),     # the user config can turn safe mode on
    ([], "0", True, False),     # the environment wins over the config
    (["--no-safe"], None, True, False),
    ([], "1", False, True),
    (["--safe"], None, False, True),
])
def test_push_cli_safe_comes_from_the_user_config(push_session, monkeypatch, tmp_path,
                                                  flag, env, configured, expected):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"push_safe": configured}), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(config))
    if env is not None:
        monkeypatch.setenv("RUNLEDGER_PUSH_SAFE", env)
    calls = _recording_push(monkeypatch)
    assert _cli_push(push_session, flag) == 0
    assert calls[-1]["safe"] is expected


def test_push_cli_hint_for_a_remote_https_server_only(push_session, monkeypatch, capsys):
    _recording_push(monkeypatch)
    assert _cli_push(push_session) == 0
    assert "tip:" not in capsys.readouterr().err
    assert main(["push", str(push_session), "--server", "https://runledger.example.com",
                 "--key", "rl_test", "--user", "dev"]) == 0
    assert "tip: --safe" in capsys.readouterr().err
    assert main(["push", str(push_session), "--server", "https://runledger.example.com",
                 "--key", "rl_test", "--user", "dev", "--safe"]) == 0
    assert "tip:" not in capsys.readouterr().err


def test_push_help_lists_safe(capsys):
    with pytest.raises(SystemExit) as exit_code:
        main(["push", "--help"])
    assert exit_code.value.code == 0
    out = capsys.readouterr().out
    assert "--safe" in out and "--no-safe" in out and "RUNLEDGER_PUSH_SAFE" in out


def test_push_cli_reports_a_same_key_update_as_success(push_session, capsys):
    server = _StubPushServer(status=200, payload={"status": "updated", "id": "safe-0001-native",
                                                  "revision": 2})
    try:
        code = main(["push", str(push_session), "--server", server.url, "--key", "rl_test",
                     "--user", "dev"])
    finally:
        server.close()
    out, err = capsys.readouterr()
    assert code == 0
    assert "safe-0001-native" in out and "revision 2" in out and "error" not in out
    assert err.strip() == ""


def test_push_cli_reports_an_other_key_conflict_as_an_error(push_session, capsys):
    message = ("A different key already pushed this session, or the new receipt is older. "
               "Push the newest version of a session with the key that pushed it first.")
    server = _StubPushServer(status=409, payload={"error": {"code": "run_conflict", "message": message}})
    try:
        code = main(["push", str(push_session), "--server", server.url, "--key", "rl_other",
                     "--user", "dev"])
    finally:
        server.close()
    out, err = capsys.readouterr()
    assert code == 1
    assert "different key already pushed this session" in err
    assert "error" in err
    assert "Traceback" not in err and "Traceback" not in out
