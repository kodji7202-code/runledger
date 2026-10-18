import io
import json
import re
import shutil
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from runledger.cli import build
from runledger.github import (GITHUB_COMMENT_LIMIT, MARKER, MAX_COMMENT_CHARS, GitHubClient,
                              GitHubError, Scored, _code, _next_link, _text, build_parser, main,
                              render_comment)

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
FIXTURES = Path(__file__).parent / "fixtures"
GUARD_FIX = FIXTURES / "guard"
DROID_FIX = FIXTURES / "droid_session.jsonl"
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


def _comment(comment_id, login="me", kind="User", body=MARKER + "\nold receipt"):
    return {"id": comment_id, "body": body, "user": {"login": login, "type": kind}}


def _api(comments=(), user=("me", "User"), patch_status=200, post_status=201, post_message=""):
    """A stub GitHub API: `GET /user`, list the pull request comments, PATCH and POST one
    comment. `user=None` answers `GET /user` with the 403 a GITHUB_TOKEN gets."""
    def responder(method, path, body):
        if path == "/user":
            if user is None:
                return 403, {}, {"message": "Resource not accessible by integration"}
            return 200, {}, {"login": user[0], "type": user[1]}
        if method == "GET":
            return 200, {}, list(comments)
        if method == "PATCH":
            if patch_status >= 400:
                return patch_status, {}, {"message": "Forbidden"}
            comment_id = int(path.rsplit("/", 1)[-1])
            return 200, {}, {"id": comment_id,
                             "html_url": f"https://github.com/octo/demo/pull/7#issuecomment-{comment_id}"}
        if post_status >= 400:
            return post_status, {}, {"message": post_message or "Server Error"}
        return _created()
    return responder


def _native_session(folder, name="session.runledger.json", prompts=None, steps=(), **extra):
    doc = {"runledger_format": 1, "agent": "demo-agent", "session_id": "gh-native-1",
           "cwd": "/work/app", "prompts": prompts or ["Do the work."], "final_message": "",
           "models": {"claude-haiku-5-5": {"input_tokens": 1000, "output_tokens": 100}},
           "steps": list(steps)}
    doc.update(extra)
    path = folder / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _write_step(path, content="x"):
    return {"tool": "Write", "input": {"file_path": path, "content": content},
            "model": "claude-haiku-5-5", "result_text": "", "is_error": False}


def _shell_step(command, result="", is_error=False):
    return {"tool": "Bash", "input": {"command": command}, "model": "claude-haiku-5-5",
            "result_text": result, "is_error": is_error}


def _guard_session(tmp_path):
    """The guard fixture session, moved into a folder with its guard log."""
    events = [json.loads(line) for line in
              (GUARD_FIX / "session.jsonl").read_text(encoding="utf-8").splitlines()]
    events[0]["cwd"] = str(tmp_path)
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    log = tmp_path / ".runledger" / "guard.log"
    log.parent.mkdir()
    log.write_bytes((GUARD_FIX / "guard.log").read_bytes())
    return path


def test_dry_run_renders_sample_session(capsys):
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--dry-run")
    assert rc == 0
    assert out.startswith(MARKER + "\n## RunLedger receipt · 🔴 High risk · 80/100\n")
    assert "| The payment retry logic gives up after the first failure." in out
    assert "| Claude Code · Sonnet 4.5, Haiku 4.5 | 10 | $0.108 | 🔴 80/100 High |" in out
    assert "<summary>Details for <code>7f3c2a10</code>: 5 risk reason(s), 3 file(s) changed</summary>" in out
    assert "- **HIGH** · step 4: Read a secrets file \\(.env.local\\)" in out
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
    stub.responder = _api(comments=[{"id": 1, "body": "unrelated"}])
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [(m, p) for m, p, _, _ in stub.requests] == [
        ("GET", "/user"),
        ("GET", "/repos/octo/demo/issues/7/comments?per_page=100"),
        ("POST", "/repos/octo/demo/issues/7/comments"),
    ]
    assert stub.requests[-1][3]["body"].startswith(MARKER)
    assert all(auth == f"Bearer {TOKEN}" for _, _, auth, _ in stub.requests)
    assert "created" in out and "issuecomment-77" in out
    assert TOKEN not in out + err


def test_updates_existing_marked_comment(stub, capsys):
    stub.responder = _api(comments=[{"id": 5, "body": "hello"}, _comment(42)])
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [(m, p) for m, p, _, _ in stub.requests] == [
        ("GET", "/user"),
        ("GET", "/repos/octo/demo/issues/7/comments?per_page=100"),
        ("PATCH", "/repos/octo/demo/issues/comments/42"),
    ]
    assert "updated" in out


def test_finds_the_bot_comment_when_the_user_lookup_is_forbidden(stub, capsys):
    stub.responder = _api(user=None, comments=[_comment(11, "human", "User"), _comment(12, "github-actions[bot]", "Bot")])
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert stub.requests[-1][0] == "PATCH"
    assert stub.requests[-1][1] == "/repos/octo/demo/issues/comments/12"


def test_a_humans_marker_comment_is_ignored_without_the_user_lookup(stub, capsys):
    stub.responder = _api(user=None, comments=[_comment(11, "human", "User")])
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert stub.requests[-1][0] == "POST"
    assert all("comments/11" not in path for _, path, _, _ in stub.requests)


def test_follows_link_header_to_find_marker(stub, capsys):
    next_url = f"{stub.url}/repos/octo/demo/issues/7/comments?per_page=100&page=2"

    def responder(method, path, body):
        if path == "/user":
            return 200, {}, {"login": "me", "type": "User"}
        if method == "GET" and path.endswith("&page=2"):
            return 200, {}, [_comment(99)]
        if method == "GET":
            return 200, {"Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"'}, [{"id": 1, "body": "a"}]
        return 200, {}, {"id": 99, "html_url": "u"}

    stub.responder = responder
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [(m, p) for m, p, _, _ in stub.requests] == [
        ("GET", "/user"),
        ("GET", "/repos/octo/demo/issues/7/comments?per_page=100"),
        ("GET", "/repos/octo/demo/issues/7/comments?per_page=100&page=2"),
        ("PATCH", "/repos/octo/demo/issues/comments/99"),
    ]


def test_full_pages_without_link_header_are_followed(stub, capsys):
    full_page = [{"id": i, "body": "x"} for i in range(100, 200)]

    def responder(method, path, body):
        if path == "/user":
            return 200, {}, {"login": "me", "type": "User"}
        if method == "GET" and path.endswith("&page=2"):
            return 200, {}, [_comment(7)]
        if method == "GET":
            return 200, {}, full_page
        return 200, {}, {"id": 7, "html_url": "u"}

    stub.responder = responder
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [(m, p) for m, p, _, _ in stub.requests][-1] == ("PATCH", "/repos/octo/demo/issues/comments/7")
    assert len(stub.requests) == 4


def test_repo_and_pr_default_from_event_file(stub, tmp_path, monkeypatch, capsys):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "synchronize", "pull_request": {"number": 7}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    stub.responder = _api()
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX))
    assert rc == 0
    assert stub.requests[1][1] == "/repos/octo/demo/issues/7/comments?per_page=100"
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
    stub.responder = _api(post_status=500, post_message=f"server echoed {TOKEN}")
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--fail-on", "60")
    assert rc == 3  # a failed post wins over the threshold
    assert "HTTP 500" in err and "***" in err
    assert "at or above --fail-on 60" in err
    assert TOKEN not in out + err


def test_token_is_not_printed_on_success(stub, capsys):
    stub.responder = _api()
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert all(auth == f"Bearer {TOKEN}" for _, _, auth, _ in stub.requests)
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
    assert "no session files were found" in err


def test_empty_sessions_dir_is_allowed_with_a_summary_note(tmp_path, monkeypatch, stub, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rc, out, err = run_cli(capsys, "comment", "--sessions-dir", str(tmp_path), "--pr", "7",
                           "--allow-empty")
    assert rc == 0
    assert stub.requests == []  # nothing is posted
    assert "no session" in err.lower()
    assert summary.read_text(encoding="utf-8").strip()
    assert len(summary.read_text(encoding="utf-8").strip().splitlines()) == 1
    with pytest.raises(SystemExit):
        main(["comment", "--help"])
    assert "--allow-empty" in capsys.readouterr().out


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


class _RecordingBody(io.BytesIO):
    """An error body that remembers being closed, the way a real response must be."""

    def __init__(self, data: bytes = b""):
        super().__init__(data)
        self.closed_by_us = False

    def close(self):
        self.closed_by_us = True
        super().close()


def test_http_error_body_closed(monkeypatch):
    body = _RecordingBody(json.dumps({"message": "Forbidden"}).encode("utf-8"))
    error = urllib.error.HTTPError("https://api.github.com/repos/o/r/issues/1/comments", 403,
                                   "Forbidden", {}, body)

    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    client = GitHubClient("https://api.github.com", TOKEN, "o/r", 1)
    with pytest.raises(GitHubError, match="HTTP 403"):
        client._send("POST", "https://api.github.com/repos/o/r/issues/1/comments", {"body": "x"})
    assert body.closed_by_us


# ---------------------------------------------------------------- the sticky comment

def test_marker_lookup_only_accepts_the_tokens_own_comment(stub, capsys):
    stub.responder = _api(comments=[_comment(11, "other"), _comment(12, "me")])
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [m for m, _, _, _ in stub.requests] == ["GET", "GET", "PATCH"]
    assert stub.requests[-1][1] == "/repos/octo/demo/issues/comments/12"
    assert all("comments/11" not in path for _, path, _, _ in stub.requests)


def test_only_another_users_marker_leads_to_a_new_comment(stub, capsys):
    stub.responder = _api(comments=[_comment(11, "other")])
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [m for m, _, _, _ in stub.requests] == ["GET", "GET", "POST"]


@pytest.mark.parametrize("status", [403, 404])
def test_a_refused_patch_falls_back_to_a_new_comment(stub, capsys, status):
    stub.responder = _api(comments=[_comment(42)], patch_status=status)
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert [m for m, _, _, _ in stub.requests] == ["GET", "GET", "PATCH", "POST"]
    assert "creating a new comment" in err
    assert "created" in out


# ---------------------------------------------------------------- markdown escaping

def test_text_escapes_links_images_mentions_and_html():
    text = _text("[click me](https://evil.example/x) ![img](https://evil.example/i.png) "
                 "@octocat #12 *bold* _it_ ~~s~~ <b>x</b> | pipe")
    assert "](https://evil.example" not in text
    assert "![img]" not in text
    assert "@\u200boctocat" in text and "@octocat" not in text
    assert "\\#12" in text
    assert "\\[click me\\]\\(" in text
    assert "\\*bold\\*" in text and "\\_it\\_" in text and "\\~\\~s\\~\\~" in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "\\| pipe" in text
    assert "\n" not in text


def test_text_escapes_markdown_at_the_start_of_a_line():
    assert _text("# heading") == "\\# heading"
    assert _text("> quote") == "&gt; quote"
    assert _text("- item") == "\\- item"
    assert _text("+ item") == "\\+ item"
    assert _text("1. item") == "\\1. item"
    assert _text("plain - dash") == "plain - dash"


def test_text_neutralizes_bare_urls_github_would_autolink():
    text = _text("See https://evil.example/x, http://evil.example/y and www.evil.example now")
    assert "https://evil.example" not in text
    assert "http://evil.example" not in text
    assert "www.evil.example" not in text
    assert "HTTPS://Evil.example" not in _text("Mixed case HTTPS://Evil.example here")
    # the address stays readable, only the autolink trigger is gone
    assert "evil.example" in text and "www" in text
    assert "\n" not in text


def test_code_keeps_backticks_in_a_wider_fence():
    assert _code("src/app.py") == "`src/app.py`"
    assert _code("dir/we`ird.py") == "`` dir/we`ird.py ``"
    assert _code("a``b") == "``` a``b ```"
    assert "`" in _code("trailing`")


def test_comment_has_no_live_links_from_session_text(tmp_path, capsys):
    injection = ("<b>x</b> @octocat #12 *b* _i_ ~~s~~ | pipe [c](https://e/x) ![i](https://e/i.png)")
    path = _native_session(tmp_path, "injected.runledger.json", prompts=[injection],
                           steps=[_write_step("/work/app/a[1](2).py"),
                                  _write_step("/work/app/dir/we`ird.py")])
    rc, out, err = run_cli(capsys, "comment", "--session", str(path), "--dry-run")
    assert rc == 0
    assert "](https://evil.example" not in out
    assert "![" not in out
    assert "<b>" not in out and "&lt;b&gt;x&lt;/b&gt;" in out
    assert "@\u200boctocat" in out and "@octocat" not in out
    assert "\\#12" in out and "\\| pipe" in out
    assert out.count("](http") == 1  # the footer link is the only live Markdown link
    assert "[RunLedger](https://runledger.site)" in out
    # `](http` counts bracket links only: GitHub also links a bare URL on its own, so no
    # autolink trigger may be left outside the footer either.
    triggers = re.findall(r"(?i)https?://|www\.", out)
    footer = next(line for line in out.splitlines() if line.startswith("Generated by "))
    assert triggers == re.findall(r"(?i)https?://|www\.", footer) and triggers
    backtick_line, = [line for line in out.splitlines() if "we`ird.py" in line]
    assert "`` dir/we`ird.py ``" in backtick_line
    assert "'" not in backtick_line
    assert "`a[1](2).py`" in out


def test_comment_autolinks_no_bare_url_from_session_text(tmp_path, capsys):
    prompt = "Look at https://evil.example/x and www.evil.example before you continue"
    path = _native_session(tmp_path, "urls.runledger.json", prompts=[prompt],
                           steps=[_write_step("/work/app/retry.py")])
    rc, out, err = run_cli(capsys, "comment", "--session", str(path), "--dry-run")
    assert rc == 0
    assert "https://evil.example" not in out
    assert "www.evil.example" not in out
    assert "evil.example" in out  # the reader still sees the address
    # GitHub turns a bare scheme or a www. prefix into a live link: every trigger in the
    # comment must live in the trusted footer, where RunLedger's own link stays clickable.
    triggers = re.findall(r"(?i)https?://|www\.", out)
    footer = next(line for line in out.splitlines() if line.startswith("Generated by "))
    assert triggers == re.findall(r"(?i)https?://|www\.", footer)
    assert triggers and "[RunLedger](https://runledger.site)" in out


# ---------------------------------------------------------------- agent, guard, tests, safe mode

def test_comment_shows_droid_and_safe_mode(tmp_path, capsys):
    import shutil
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    shutil.copy(FIX, sessions / "a.jsonl")
    shutil.copy(DROID_FIX, sessions / "c.jsonl")
    shutil.copy(DROID_FIX.with_suffix(".settings.json"), sessions / "c.settings.json")
    rc, out, err = run_cli(capsys, "comment", "--sessions-dir", str(sessions), "--dry-run")
    assert rc == 0
    assert "**2 runs**" in out
    assert "| Factory Droid · Sonnet 4.5 | 5 | $0.0066 | 🟢 0/100 Low |" in out
    assert "Claude Code · Sonnet 4.5, Haiku 4.5" in out
    assert "$0.000" not in out
    assert "Safe mode" in out and "masked" in out


def test_comment_masks_home_folders(tmp_path, capsys):
    home = "C:\\Users\\example"
    path = _native_session(tmp_path, "home.runledger.json",
                           prompts=[f"Fix the build in {home}\\app\\src."],
                           steps=[_write_step(f"{home}\\app\\src\\main.py")],
                           cwd=f"{home}\\app")
    rc, out, err = run_cli(capsys, "comment", "--session", str(path), "--dry-run")
    assert rc == 0
    assert home not in out
    assert "Safe mode" in out


def test_comment_shows_the_guard_line(tmp_path, capsys):
    path = _guard_session(tmp_path)
    rc, out, err = run_cli(capsys, "comment", "--session", str(path), "--dry-run")
    assert rc == 0
    guard_line, = [line for line in out.splitlines() if "Guard:" in line]
    assert "2 blocked" in guard_line
    assert "3 warned (2 monitor mode, 1 log warning)" in guard_line
    plain = _native_session(tmp_path, "plain.runledger.json", steps=[_write_step("/work/app/a.py")])
    rc2, out2, err2 = run_cli(capsys, "comment", "--session", str(plain), "--dry-run")
    assert rc2 == 0
    assert "Guard:" not in out2


def test_comment_shows_the_test_verdict(tmp_path, capsys):
    passed = _native_session(tmp_path, "passed.runledger.json",
                             steps=[_shell_step("python -m pytest -q", "3 passed in 0.42s")])
    failed = _native_session(tmp_path, "failed.runledger.json",
                             steps=[_shell_step("python -m pytest -q", "2 failed, 1 passed in 1.04s",
                                                is_error=True)])
    none = _native_session(tmp_path, "none.runledger.json", steps=[_write_step("/work/app/a.py")])
    rc, out, err = run_cli(capsys, "comment", "--session", str(passed), "--session", str(failed),
                           "--session", str(none), "--dry-run")
    assert rc == 0
    lines = [line for line in out.splitlines() if line.startswith("- **Tests:**")]
    assert len(lines) == 3
    assert "1 test command(s) ran" in lines[0]
    assert "3 passed, 0 failed" in lines[0] and "verdict: passed" in lines[0]
    assert "2 failed" in lines[1] and "verdict: failed" in lines[1]
    assert "no tests run" in lines[2]


def test_tests_line_reports_an_unreadable_final_run_as_unknown(tmp_path, capsys):
    path = _native_session(tmp_path, "unread.runledger.json", steps=[
        _shell_step("python -m pytest -q", "3 passed in 0.42s"),
        _write_step("/work/app/retry.py", "x = 1"),
        _shell_step("python -m pytest -q", "still running, no summary yet"),
    ])
    rc, out, err = run_cli(capsys, "comment", "--session", str(path), "--dry-run")
    assert rc == 0
    line, = [line for line in out.splitlines() if line.startswith("- **Tests:**")]
    assert "2 test command(s) ran" in line
    assert "verdict: unknown" in line
    assert "verdict: passed" not in line
    assert "last run 3 passed" not in line  # an earlier pass is never the last run
    assert "earlier" in line and "3 passed, 0 failed" in line


def test_tests_line_is_unknown_when_no_run_is_readable(tmp_path, capsys):
    path = _native_session(tmp_path, "opaque.runledger.json",
                           steps=[_shell_step("python -m pytest -q", "output without a summary")])
    rc, out, err = run_cli(capsys, "comment", "--session", str(path), "--dry-run")
    assert rc == 0
    line, = [line for line in out.splitlines() if line.startswith("- **Tests:**")]
    assert "1 test command(s) ran" in line
    assert "verdict: unknown" in line
    assert "passed" not in line


# ---------------------------------------------------------------- size, unreadable sessions

def test_oversized_comment_is_cut_and_posted(tmp_path, stub, capsys):
    sessions = tmp_path / "many"
    sessions.mkdir()
    for i in range(40):
        steps = [_shell_step("rm -rf build") for _ in range(40)]
        steps += [_write_step(f"/work/app/f{j}.py") for j in range(40)]
        _native_session(sessions, f"s{i}.runledger.json", steps=steps)
    stub.responder = _api()
    rc, out, err = run_cli(capsys, "comment", "--sessions-dir", str(sessions), "--pr", "7")
    assert rc == 0
    body = stub.requests[-1][3]["body"]
    assert MAX_COMMENT_CHARS <= GITHUB_COMMENT_LIMIT
    assert len(body) <= GITHUB_COMMENT_LIMIT
    assert body.startswith(MARKER)
    assert body.count("<details>") == body.count("</details>")
    assert "Shortened to fit GitHub's comment size limit" in body
    assert body.rstrip().endswith(_FOOTER_END)


def test_comment_skips_unreadable_sessions_and_posts_the_rest(tmp_path, capsys):
    sessions = tmp_path / "mixed"
    sessions.mkdir()
    import shutil
    shutil.copy(FIX, sessions / "good.jsonl")
    shutil.copy(DROID_FIX, sessions / "droid.jsonl")
    shutil.copy(DROID_FIX.with_suffix(".settings.json"), sessions / "droid.settings.json")
    (sessions / "bad.jsonl").write_text("not a session at all\nrandom text\n", encoding="utf-8")
    (sessions / "broken.runledger.json").write_text("{not json", encoding="utf-8")
    rc, out, err = run_cli(capsys, "comment", "--sessions-dir", str(sessions), "--dry-run")
    assert rc == 0
    assert "**2 runs**" in out
    assert "2 session(s) could not be read" in out
    assert "::warning file=" in err and "bad.jsonl" in err and "broken.runledger.json" in err


def test_comment_fails_when_no_session_can_be_read(tmp_path, stub, capsys):
    sessions = tmp_path / "bad"
    sessions.mkdir()
    (sessions / "bad.jsonl").write_text("not a session", encoding="utf-8")
    (sessions / "broken.runledger.json").write_text("{not json", encoding="utf-8")
    rc, out, err = run_cli(capsys, "comment", "--sessions-dir", str(sessions), "--pr", "7")
    assert rc == 3
    assert "no session could be read" in err
    assert stub.requests == []  # nothing is posted


# ---------------------------------------------------------------- exit codes

def test_fail_on_exit_codes_when_the_comment_is_posted(stub, capsys):
    stub.responder = _api()
    assert run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--fail-on", "80")[0] == 1
    assert run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--fail-on", "81")[0] == 0
    assert run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")[0] == 0
    assert [m for m, _, _, _ in stub.requests].count("POST") == 3


def test_failed_posting_wins_over_the_threshold(stub, capsys):
    stub.responder = _api(post_status=500)
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--fail-on", "10")
    assert rc == 3
    assert "HTTP 500" in err and "at or above --fail-on 10" in err
    stub.responder = _api(post_status=500)
    rc2, out2, err2 = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--fail-on", "90")
    assert rc2 == 3
    assert "HTTP 500" in err2


# ---------------------------------------------------------------- forks

def test_fork_forbidden_post_falls_back_to_the_step_summary(tmp_path, monkeypatch, stub, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    stub.responder = _api(post_status=403, post_message="Resource not accessible by integration")
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 0
    assert "fork" in err.lower()
    text = summary.read_text(encoding="utf-8")
    assert MARKER in text and "| Run | Agent · Model |" in text


def test_fork_forbidden_post_still_honours_the_threshold(tmp_path, monkeypatch, stub, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    stub.responder = _api(post_status=403, post_message="Resource not accessible by integration")
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7", "--fail-on", "10")
    assert rc == 1
    assert MARKER in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("fork, posts", [(True, False), (False, True)])
def test_fork_event_skips_posting_and_writes_the_summary(tmp_path, monkeypatch, stub, capsys, fork, posts):
    summary = tmp_path / "summary.md"
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"number": 7, "pull_request": {"head": {"repo": {"fork": fork}}}}),
                     encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    stub.responder = _api()
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX))
    assert rc == 0
    assert any(m == "POST" for m, _, _, _ in stub.requests) is posts
    assert MARKER in summary.read_text(encoding="utf-8")


def test_other_forbidden_responses_are_still_errors(tmp_path, monkeypatch, stub, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    stub.responder = _api(post_status=403, post_message="Must have admin rights")
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 3
    assert "Must have admin rights" in err


def test_bad_credentials_is_an_error_and_the_token_is_not_printed(stub, capsys):
    stub.responder = _api(post_status=401, post_message="Bad credentials")
    rc, out, err = run_cli(capsys, "comment", "--session", str(FIX), "--pr", "7")
    assert rc == 3
    assert "401" in err
    assert TOKEN not in out + err


# ---------------------------------------------------------------- discovery

def test_action_yml_is_multi_agent_and_exposes_the_new_inputs():
    action = (Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8")
    assert action.splitlines()[1].startswith("description:")
    assert "AI coding agent sessions (.jsonl, .runledger.json)" in action.splitlines()[1]
    sessions_dir = action.split("  sessions-dir:", 1)[1].split("  fail-on:", 1)[0]
    assert "Claude Code, Codex and Factory Droid" in sessions_dir
    assert ".runledger.json" in sessions_dir
    assert ".jsonl" in action and ".runledger.json" in action
    assert ".settings.json" in action  # the Droid sidecar carries usage and cost
    assert 'allow-empty:' in action and 'default: "false"' in action
    assert 'python-version:' in action and 'default: "3.11"' in action
    assert "python-version: ${{ inputs.python-version }}" in action
    assert 'if [ "$RUNLEDGER_ALLOW_EMPTY" = "true" ]; then' in action
    assert "args+=(--allow-empty)" in action
    assert "risk_score:" in action and "risk_level:" in action
    assert "steps.receipt.outputs.risk_score" in action
    assert "steps.receipt.outputs.risk_level" in action


def test_sessions_dir_reads_jsonl_and_runledger_json_with_droid_sidecars(tmp_path, capsys):
    import shutil
    sessions = tmp_path / "found"
    sessions.mkdir()
    shutil.copy(FIX, sessions / "a.jsonl")
    shutil.copy(FIXTURES / "native_session.json", sessions / "b.runledger.json")
    shutil.copy(DROID_FIX, sessions / "c.jsonl")
    shutil.copy(DROID_FIX.with_suffix(".settings.json"), sessions / "c.settings.json")
    (sessions / "ignore.txt").write_text("not a session", encoding="utf-8")
    rc, out, err = run_cli(capsys, "comment", "--sessions-dir", str(sessions), "--dry-run")
    assert rc == 0
    assert "**3 runs**" in out
    assert "Factory Droid · Sonnet 4.5" in out
    assert "Demo Agent" in out or "demo-agent" in out
    assert "not a session" not in out
