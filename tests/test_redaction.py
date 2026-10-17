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


def test_native_metadata_and_model_ids_are_redacted(tmp_path):
    doc = {
        "runledger_format": 1,
        "agent": GITHUB_TOKEN,
        "session_id": GITHUB_TOKEN,
        "cwd": f"/work/{GITHUB_TOKEN}",
        "git_branch": f"feature/{GITHUB_TOKEN}",
        "models": {GITHUB_TOKEN: {"input_tokens": 10}},
        "steps": [{"tool": "Read", "model": GITHUB_TOKEN,
                   "input": {"file_path": f"/work/{GITHUB_TOKEN}/a.py"},
                   "usage": {"input_tokens": 10}}],
    }
    path = tmp_path / "metadata.runledger.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    run, score, level, risks, _ = build(str(path))

    for fmt in ("html", "md", "json"):
        rendered = render(run, score, level, risks, fmt)
        assert GITHUB_TOKEN not in rendered
        assert "[REDACTED]" in rendered
    data = json.loads(render(run, score, level, risks, "json"))
    assert data["session_id"] == "[REDACTED]"
    assert list(data["models"]) == ["[REDACTED]"]
    assert data["steps"][0]["model"] == "[REDACTED]"


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


# ---------------------------------------------------------------- shared secrets module & parity (VAL-CORE-012..015, VAL-CORE-081)

@pytest.mark.parametrize("secret_str", [
    "sk-" + "proj-" + "A" * 48,
    "sk-ant-" + "api03-" + "B" * 40,
    "sk-" + "live-" + "C" * 32,
    "AKIA" + "IOSFODNN7EXAMPLE",
    "ASIA" + "IOSFODNN7EXAMPLE",
    "ghp_" + "a" * 36,
    "ghp_" + "a" * 30,
    "github_pat_" + "a" * 22 + "_" + "b" * 59,
    "glpat-" + "a" * 20,
    "xox" + "b-" + "1" * 11 + "-" + "2" * 11 + "-" + "A" * 24,
    "AIza" + "Sy" + "A" * 33,
    "npm_" + "a" * 36,
    "pypi-" + "AgEIcHlwaS5vcmc" + "a" * 50,
    "rl_" + "a" * 40,
    "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "postgres://admin:" + "s3cret" + "Pass" + "@db.example.com:5432/app",
    'export SECRET_TOKEN="' + "my_super_" + 'secret_val_1234"',
    "Authorization: Bearer " + "eyJ" + "hbGciOiJIUzI1NiJ9.aaa.bbb",
])
def test_secrets_module_detection_and_redaction_parity(secret_str):
    from runledger import llm, secrets
    from runledger.parser import Step
    from runledger.risk import assess_step

    found = secrets.find_secrets(secret_str)
    assert len(found) >= 1
    redacted = secrets.redact_text(secret_str)
    assert redacted != secret_str
    assert "[REDACTED]" in redacted
    assert guard.redact(secret_str) == redacted
    assert llm.redact(secret_str) == redacted
    step_risks = assess_step(
        Step(index=1, tool_use_id="t1", tool="Write", input={"file_path": "a.txt", "content": secret_str},
             model=None, timestamp=None),
        None,
    )
    assert any(r.code in ("secret", "secret_in_content") and r.severity == "high" for r in step_risks)


@pytest.mark.parametrize("n,expected", [(20, False), (30, True), (36, True), (40, True)])
def test_github_token_length_parity(n, expected):
    from runledger import secrets

    s = "ghp_" + "a" * n
    detected = bool(secrets.find_secrets(s))
    was_redacted = secrets.redact_text(s) != s
    assert detected == was_redacted == expected


def test_secrets_is_leaf_module():
    import subprocess
    import sys

    code = (
        "import sys; import runledger.secrets; "
        "others = [m for m in sys.modules if m.startswith('runledger.') and m != 'runledger.secrets']; "
        "assert not others, others"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("benign", [
    'password = "short"',
    'password = "${PASSWORD}"',
    "token = None",
    "secret = True",
    'api_key = os.environ["API_KEY"]',
    'key = "<YOUR_API_KEY>"',
    'example = "sk-ant-..."',
    'url = "postgres://localhost:5432/mydb"',
])
def test_benign_values_not_flagged_or_redacted(benign):
    from runledger import secrets
    from runledger.parser import Step
    from runledger.risk import assess_step

    assert secrets.find_secrets(benign) == []
    assert secrets.redact_text(benign) == benign
    assert guard.redact(benign) == benign
    step_risks = assess_step(
        Step(index=1, tool_use_id="t1", tool="Write", input={"file_path": "a.py", "content": benign},
             model=None, timestamp=None),
        None,
    )
    assert not any(r.code in ("secret", "secret_in_content") for r in step_risks)


def test_guard_standalone_fallback_when_secrets_unavailable():
    import subprocess
    import sys

    code = (
        "import importlib, sys; "
        "import runledger.guard as guard; "
        "sys.modules['runledger.secrets'] = None; "
        "importlib.reload(guard); "
        "s = 'key=sk-proj-' + 'A' * 48 + ' aws=AKIAIOSFODNN7EXAMPLE'; "
        "r = guard.redact(s); "
        "assert 'sk-proj-' not in r and 'AKIAIOSFODNN7EXAMPLE' not in r and '[REDACTED]' in r, r"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr


def test_conftest_isolation_fixture_clears_env_and_Redirects_home(tmp_path):
    import os
    from pathlib import Path
    from conftest import ISOLATED_ENV_VARS

    home = Path(os.environ["HOME"])
    userprofile = Path(os.environ["USERPROFILE"])
    assert home == userprofile
    assert tmp_path in home.parents or home.parent == tmp_path
    for var in ISOLATED_ENV_VARS:
        assert var not in os.environ

