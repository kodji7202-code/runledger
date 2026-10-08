import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from runledger.cli import build
from runledger.github import MARKER, MAX_COMMENT_CHARS, Scored, _next_link, main, render_comment

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
# Fake token. It must never appear in stdout, stderr or the step outputs.
TOKEN = "fake-token-do-not-print-7f3c2a10"
_, SCORE, LEVEL, RISKS, _ = build(str(FIX))
_GITHUB_VARS = ("GITHUB_REPOSITORY", "GITHUB_EVENT_PATH", "GITHUB_API_URL",
                "GITHUB_TOKEN", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT")
_FOOTER_END = "costs are estimates at Claude API list prices."


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Tests must not depend on the variables of the CI runner they run on."""
    for name in _GITHUB_VARS:
        monkeypatch.delenv(name, raising=False)


class _Handler(BaseHTTPRequestHandler):
    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        stub = self.server.stub
        stub.requests.append((self.command, self.path, self.headers.get("Authorization"), body))
        status, headers, payload = stub.responder(self.command, self.path, body)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle
    do_PATCH = _handle

    def log_message(self, format, *args):  # keep test output quiet
        pass


class StubGitHub:
    """Local stand-in for the GitHub REST API. `responder(method, path, body)`
    returns (status, headers, json payload); every request is recorded."""

    def __init__(self):
        self.requests = []
        self.responder = lambda method, path, body: (404, {}, {"message": "Not Found"})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.stub = self
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = StubGitHub()
    monkeypatch.setenv("GITHUB_API_URL", server.url)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/demo")
    try:
        yield server
    finally:
        server.close()


def run_cli(capsys, *argv):
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def _created(comment_id=77):
    return (201, {}, {"id": comment_id, "html_url": f"https://github.com/octo/demo/pull/7#issuecomment-{comment_id}"})


def test_dry_run_renders_sample_session(capsys):
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--dry-run")
    assert rc == 0
    assert out.startswith(MARKER + "\n## RunLedger receipt · 🔴 High risk · 80/100\n")
    assert "| The payment retry logic gives up after the first failure." in out
    assert "| Sonnet 4.5, Haiku 4.5 | 10 | $0.108 | 🔴 80/100 High |" in out
    assert "<summary>Details for <code>7f3c2a10</code>: 5 risk reason(s), 3 file(s) changed</summary>" in out
    assert "- **HIGH** · step 4: Read a secrets file (.env.local)" in out
    assert "- `src/payments/retry.ts` +11 −1" in out
    assert out.count("<details>") == out.count("</details>") == 1
    assert out.endswith(_FOOTER_END + "\n")
    assert "Dry run" in err
    assert TOKEN not in out + err


@pytest.mark.parametrize("level, score, badge", [("Low", 10, "🟢"), ("Medium", 40, "🟡"), ("High", 80, "🔴")])
def test_header_badge_follows_level(level, score, badge):
    run = build(str(FIX))[0]
    md = render_comment([Scored(run, score, level, RISKS)])
    assert f"## RunLedger receipt · {badge} {level} risk · {score}/100" in md


def test_worst_run_sets_header(capsys):
    run = build(str(FIX))[0]
    md = render_comment([Scored(run, 10, "Low", []), Scored(run, 65, "High", RISKS)])
    assert "## RunLedger receipt · 🔴 High risk · 65/100" in md
    assert "**2 runs**" in md


def test_user_text_is_escaped():
    run = build(str(FIX))[0]
    run.prompts = ["<script>alert(1)</script> ping @alice | pipe"]
    md = render_comment([Scored(run, 80, "High", RISKS)])
    assert "<script>" not in md
    assert "&lt;script&gt;" in md
    assert "@alice" not in md and "@​alice" in md
    assert "pipe" in md and "\\| pipe" in md


def test_long_comment_is_truncated_safely():
    run = build(str(FIX))[0]
    md = render_comment([Scored(run, SCORE, LEVEL, RISKS)] * 300)
    assert len(md) <= MAX_COMMENT_CHARS
    assert md.startswith(MARKER)
    assert md.count("<details>") == md.count("</details>")
    assert "Shortened to fit GitHub's comment size limit" in md
    assert md.rstrip().endswith(_FOOTER_END)


def test_tiny_limit_still_fits():
    run = build(str(FIX))[0]
    md = render_comment([Scored(run, SCORE, LEVEL, RISKS)] * 50, limit=500)
    assert len(md) <= 500


def test_posts_new_comment_when_none_exists(stub, capsys):
    stub.responder = lambda m, p, b: (200, {}, [{"id": 1, "body": "unrelated"}]) if m == "GET" else _created()
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [(m, p) for m, p, _, _ in stub.requests] == [
        ("GET", "/repos/octo/demo/issues/7/comments?per_page=100"),
        ("POST", "/repos/octo/demo/issues/7/comments"),
    ]
    assert stub.requests[-1][3]["body"].startswith(MARKER)
    assert stub.requests[0][2] == f"Bearer {TOKEN}"
    assert "created" in out and "issuecomment-77" in out
    assert TOKEN not in out + err


def test_updates_existing_marked_comment(stub, capsys):
    def responder(method, path, body):
        if method == "GET":
            return 200, {}, [{"id": 5, "body": "hello"}, {"id": 42, "body": MARKER + "\nold receipt"}]
        return 200, {}, {"id": 42, "html_url": "https://github.com/octo/demo/pull/7#issuecomment-42"}

    stub.responder = responder
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [(m, p) for m, p, _, _ in stub.requests] == [
        ("GET", "/repos/octo/demo/issues/7/comments?per_page=100"),
        ("PATCH", "/repos/octo/demo/issues/comments/42"),
    ]
    assert "updated" in out


def test_follows_link_header_to_find_marker(stub, capsys):
    next_url = f"{stub.url}/repos/octo/demo/issues/7/comments?per_page=100&page=2"

    def responder(method, path, body):
        if method == "GET" and path.endswith("&page=2"):
            return 200, {}, [{"id": 99, "body": MARKER + " old"}]
        if method == "GET":
            return 200, {"Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"'}, [{"id": 1, "body": "a"}]
        return 200, {}, {"id": 99, "html_url": "u"}

    stub.responder = responder
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [(m, p) for m, p, _, _ in stub.requests] == [
        ("GET", "/repos/octo/demo/issues/7/comments?per_page=100"),
        ("GET", "/repos/octo/demo/issues/7/comments?per_page=100&page=2"),
        ("PATCH", "/repos/octo/demo/issues/comments/99"),
    ]


def test_full_pages_without_link_header_are_followed(stub, capsys):
    full_page = [{"id": i, "body": "x"} for i in range(100, 200)]

    def responder(method, path, body):
        if method == "GET" and path.endswith("&page=2"):
            return 200, {}, [{"id": 7, "body": MARKER}]
        if method == "GET":
            return 200, {}, full_page
        return 200, {}, {"id": 7, "html_url": "u"}

    stub.responder = responder
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [(m, p) for m, p, _, _ in stub.requests][-1] == ("PATCH", "/repos/octo/demo/issues/comments/7")
    assert len(stub.requests) == 3


def test_repo_and_pr_default_from_event_file(stub, tmp_path, monkeypatch, capsys):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "synchronize", "pull_request": {"number": 7}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    stub.responder = lambda m, p, b: (200, {}, []) if m == "GET" else _created()
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX))
    assert rc == 0
    assert stub.requests[0][1] == "/repos/octo/demo/issues/7/comments?per_page=100"
    assert stub.requests[-1][1] == "/repos/octo/demo/issues/7/comments"


def test_missing_pr_is_a_runtime_error(stub, capsys):
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX))
    assert rc == 3
    assert "no pull request number" in err
    assert stub.requests == []


def test_github_output_and_step_summary_are_written(tmp_path, monkeypatch, capsys):
    out_file = tmp_path / "github_output.txt"
    summary = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--dry-run")
    assert rc == 0
    assert out_file.read_text(encoding="utf-8") == f"risk_score={SCORE}\nrisk_level=High\n"
    assert summary.read_text(encoding="utf-8") == out


@pytest.mark.parametrize("extra, expected", [
    (["--fail-on", "80"], 1),
    (["--fail-on", "0"], 1),
    (["--fail-on", "81"], 0),
    ([], 0),
])
def test_fail_on_exit_codes(capsys, extra, expected):
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--dry-run", *extra)
    assert rc == expected


def test_token_is_not_printed_on_api_error(stub, capsys):
    stub.responder = lambda m, p, b: (500, {}, {"message": f"server echoed {TOKEN}"})
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--fail-on", "60")
    assert rc == 1  # the threshold is still enforced when posting fails
    assert "HTTP 500" in err and "***" in err
    assert TOKEN not in out + err


def test_token_is_not_printed_on_success(stub, capsys):
    stub.responder = lambda m, p, b: (200, {}, []) if m == "GET" else _created()
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert stub.requests[0][2] == f"Bearer {TOKEN}"
    assert TOKEN not in out + err


def test_dry_run_makes_no_requests(stub, capsys):
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--dry-run")
    assert rc == 0
    assert stub.requests == []


def test_sessions_dir_scores_every_jsonl(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    shutil.copy(FIX, sessions / "a.jsonl")
    shutil.copy(FIX, sessions / "b.jsonl")
    (sessions / "notes.txt").write_text("not a session", encoding="utf-8")
    rc, out, err = run_cli(capsys, "comment", "--sessions-dir", str(sessions), "--dry-run")
    assert rc == 0
    assert "**2 runs**" in out
    assert sum(1 for line in out.splitlines() if line.startswith("| ") and "/100" in line) == 2


def test_empty_sessions_dir_is_a_runtime_error(tmp_path, capsys):
    rc, out, err = run_cli(capsys, "comment", "--sessions-dir", str(tmp_path), "--dry-run")
    assert rc == 3
    assert "no session .jsonl files found" in err


def test_usage_errors(capsys):
    assert main(["comment"]) == 2  # no --session and no --sessions-dir
    with pytest.raises(SystemExit) as exc:
        main(["comment", "--session", str(FIX), "--fail-on", "101"])
    assert exc.value.code == 2


def test_invalid_repository_is_rejected(stub, capsys):
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--repo", "../evil")
    assert rc == 3
    assert "owner/name" in err
    assert stub.requests == []


def test_next_link_parsing():
    link = '<https://api.example/x?page=2>; rel="next", <https://api.example/x?page=5>; rel="last"'
    assert _next_link(link) == "https://api.example/x?page=2"
    assert _next_link('<https://api.example/x?page=1>; rel="prev"') is None
    assert _next_link("") is None
