"""Secrets are masked in everything a receipt shows: the HTML, Markdown and JSON receipt,
the payload pushed to a team server, the PR comment and `runledger list`. The risk rules
still see the raw session, so a hardcoded secret is still flagged.

Every secret below is synthetic and assembled at run time.
"""
import json
import time

import pytest

from runledger import guard
from runledger.cli import build, main
from runledger.client import build_payload
from runledger.github import Scored, render_comment
from runledger.receipt import render

CWD = "/home/dev/shop"
ANTHROPIC_KEY = "sk-ant-api03-" + "q" * 30
GITHUB_TOKEN = "ghp_" + "b" * 36
STRIPE_KEY = "sk_" + "live_" + "c1" * 10
TEAM_KEY = "rl_" + "d2" * 20
DB_PASSWORD = "hunter2" + "hunter"
URL_PASSWORD = "s3cret" + "Pass"
QUERY_TOKEN = "abc123" + "def456"
SECRETS = (ANTHROPIC_KEY, GITHUB_TOKEN, STRIPE_KEY, TEAM_KEY, DB_PASSWORD, URL_PASSWORD, QUERY_TOKEN)


def _line(kind, ts, **extra):
    base = {"cwd": CWD, "sessionId": "5ec12e75-0000-4000-8000-000000000001", "gitBranch": "main",
            "timestamp": ts, "type": kind}
    base.update(extra)
    return json.dumps(base)


def _tool(n, name, **inp):
    return _line("assistant", f"2026-10-09T10:00:{n:02d}.000Z", message={
        "id": f"msg_{n}", "role": "assistant", "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": inp}],
        "usage": {"input_tokens": 10, "output_tokens": 20}})


def _result(n, text="ok"):
    return _line("user", f"2026-10-09T10:00:{n:02d}.500Z", message={
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": text}]})


@pytest.fixture
def session(tmp_path):
    lines = [
        _line("user", "2026-10-09T10:00:00.000Z", message={
            "role": "user", "content": f"Deploy the shop. Use the key {ANTHROPIC_KEY} and push with {TEAM_KEY}."}),
        _tool(1, "Bash", command=f"export DB_PASSWORD={DB_PASSWORD} && npm run migrate"),
        _result(1),
        # The secret starts before the 120-character cut of the step summary.
        _tool(2, "Bash", command="echo " + "x" * 100 + " " + GITHUB_TOKEN),
        _result(2),
        _tool(3, "Bash", command=f"psql postgres://app:{URL_PASSWORD}@db.example.com/shop -c 'select 1'"),
        _result(3),
        _tool(4, "WebFetch", url=f"https://api.example.com/v1/orders?access_token={QUERY_TOKEN}&page=2"),
        _result(4),
        _tool(5, "Write", file_path=CWD + "/config.py", content=f'ANTHROPIC_API_KEY = "{ANTHROPIC_KEY}"\n'),
        _result(5, "File created successfully"),
        _tool(6, "Bash", command=f"STRIPE_SECRET_KEY={STRIPE_KEY} npm start"),
        _result(6),
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _assert_clean(text):
    for secret in SECRETS:
        assert secret not in text
    assert "[REDACTED]" in text


@pytest.mark.parametrize("fmt", ["html", "md", "json"])
def test_receipt_formats_do_not_show_secrets(session, fmt):
    run, score, level, risks, _ = build(str(session))
    _assert_clean(render(run, score, level, risks, fmt))


def test_rules_still_score_the_raw_session(session):
    _, score, _, risks, _ = build(str(session))
    assert any(r.code == "secret_in_content" and r.severity == "high" for r in risks)
    assert score >= 30


def test_summary_is_redacted_before_it_is_cut(session):
    run, *_ = build(str(session))
    summary = run.steps[1].summary
    assert "ghp_" not in summary
    assert summary.endswith("[REDACTED]`")


def test_files_changed_and_steps_keep_their_shape(session):
    run, score, level, risks, _ = build(str(session))
    data = json.loads(render(run, score, level, risks, "json"))
    assert [f["path"] for f in data["files"]] == ["config.py"]
    assert data["files"][0]["added"] == 1
    assert len(data["steps"]) == 6
    assert "npm run migrate" in data["steps"][0]["summary"]


def test_push_payload_does_not_carry_secrets(session):
    payload = build_payload(session, user="dev@example.com", project="shop")
    _assert_clean(json.dumps(payload))


def test_pr_comment_does_not_carry_secrets(session):
    run, score, level, risks, _ = build(str(session))
    comment = render_comment([Scored(run, score, level, risks)])
    for secret in SECRETS:
        assert secret not in comment


def test_list_does_not_print_secrets(session, tmp_path, capsys, monkeypatch):
    projects = tmp_path / "claude" / "projects" / "-home-dev-shop"
    projects.mkdir(parents=True)
    (projects / session.name).write_bytes(session.read_bytes())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert main(["list", "--project", CWD, "--agent", "claude-code"]) == 0
    out = capsys.readouterr().out
    assert ANTHROPIC_KEY not in out and "Deploy the shop" in out


# ---------------------------------------------------------------- the patterns

@pytest.mark.parametrize("text, secret", [
    (f"export OPENAI_API_KEY=sk-proj-{'e' * 24}", "sk-proj-" + "e" * 24),
    (f"export DB_PASSWORD={DB_PASSWORD}", DB_PASSWORD),
    (f"STRIPE_SECRET_KEY={STRIPE_KEY} npm start", STRIPE_KEY),
    (f"curl 'https://api.example.com/x?access_token={QUERY_TOKEN}&page=2'", QUERY_TOKEN),
    (f"curl https://admin:{URL_PASSWORD}@db.example.com/dump", URL_PASSWORD),
    (f"gh auth login --token {GITHUB_TOKEN}", GITHUB_TOKEN),
    ("mytool --password hunter22 --verbose", "hunter22"),
    (f"runledger push --key {TEAM_KEY}", TEAM_KEY),
    ("curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA=='", "dXNlcjpwYXNzd29yZA=="),
    ("glpat-" + "f" * 20, "glpat-" + "f" * 20),
    ("npm_" + "g" * 36, "npm_" + "g" * 36),
    ("hf_" + "h" * 34, "hf_" + "h" * 34),
    ("AIza" + "i" * 35, "AIza" + "i" * 35),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
     "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"),
])
def test_redact_masks_unquoted_and_provider_secrets(text, secret):
    out = guard.redact(text)
    assert secret not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize("text", [
    "npm test -- --watch",
    "llm --max_tokens=100000 --model x",
    "TOKEN=$GITHUB_TOKEN gh pr view",
    "python -m runledger.github comment --token-env=GITHUB_TOKEN",
    "git commit -m 'fix token=refresh logic'",
    "cd /home/dev/project-2024 && pytest -q",
    "echo PASSWORD=<your password>",
])
def test_redact_keeps_ordinary_text(text):
    assert guard.redact(text) == text


def test_redact_is_idempotent():
    text = f"export DB_PASSWORD={DB_PASSWORD} && curl https://a:{URL_PASSWORD}@h/x --token {GITHUB_TOKEN}"
    once = guard.redact(text)
    assert guard.redact(once) == once


def test_redact_stays_fast_on_long_text_without_secrets():
    started = time.monotonic()
    for blob in ("a" * 60000, "a." * 30000, "token" * 12000, "x_" * 30000):
        guard.redact(blob)
    assert time.monotonic() - started < 10
