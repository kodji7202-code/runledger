"""The JSON receipt contract: every fixture validates against receipt.schema.json with safe mode
on and off, and the 0.5.x keys stay in place."""
import json
from pathlib import Path

import pytest

from runledger.cli import main
from runledger.receipt import SCHEMA_VERSION
from schema_check import SCHEMA, validate

FIXTURES = Path(__file__).parent / "fixtures"
def _guard_session(tmp_path):
    events = [json.loads(line) for line in
              (FIXTURES / "guard" / "session.jsonl").read_text(encoding="utf-8").splitlines()]
    events[0]["cwd"] = str(tmp_path)
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    log = tmp_path / ".runledger" / "guard.log"
    log.parent.mkdir()
    log.write_bytes((FIXTURES / "guard" / "guard.log").read_bytes())
    return path


SESSIONS = {
    "claude": lambda tmp: FIXTURES / "sample_session.jsonl",
    "codex": lambda tmp: FIXTURES / "codex_session.jsonl",
    "droid": lambda tmp: FIXTURES / "droid_session.jsonl",
    "droid_shell": lambda tmp: FIXTURES / "droid_shell_edits.jsonl",
    "aider": lambda tmp: FIXTURES / "aider" / ".aider.chat.history.md",
    "native": lambda tmp: FIXTURES / "native_session.json",
    "guard": _guard_session,
}


def _receipt(capsys, path, *flags):
    assert main(["receipt", str(path), "--format", "json", "-o", "-", *flags]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("flag", ["--safe", "--no-safe"])
@pytest.mark.parametrize("name", sorted(SESSIONS))
def test_every_fixture_matches_the_schema(tmp_path, capsys, name, flag):
    data = _receipt(capsys, SESSIONS[name](tmp_path), flag)
    assert validate(data, SCHEMA) == []
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["safe"] is (flag == "--safe")
    if name == "guard":
        assert data["guard"] and "guard" in data["totals"]


def test_validator_reports_contract_breaks(capsys):
    data = _receipt(capsys, SESSIONS["claude"](None))
    broken = json.loads(json.dumps(data))
    broken["risk"]["score"] = 101
    broken["steps"][0]["cost_usd"] = "0.1"
    del broken["totals"]["files_changed"]
    broken["files"].append({"path": 3})
    errors = validate(broken, SCHEMA)
    assert any("risk.score" in e for e in errors)
    assert any("steps[0].cost_usd" in e for e in errors)
    assert any("missing files_changed" in e for e in errors)
    assert any("files[" in e for e in errors)


# Keys present in 0.5.x receipts. Removing one, or changing its type, needs a new schema_version.
KEYS_05 = {"session_id", "rollback", "agent", "agent_id", "cwd", "git_branch", "started", "ended",
           "duration_seconds", "request", "overview", "risk", "totals", "models", "pricing_note",
           "files", "steps", "quality", "recommendations", "ai_review", "runledger_version"}
TOTALS_05 = {"steps", "tokens", "input_tokens", "output_tokens", "cache_write_tokens",
             "cache_read_tokens", "cost_usd", "reported_cost_usd", "files_changed", "usage_source"}
STEP_05 = {"n", "tool", "summary", "model", "tokens", "tokens_estimated", "cost_usd", "error", "risks"}
RISK_05 = {"score", "level", "reasons"}


def test_0_5_keys_are_kept(capsys):
    data = _receipt(capsys, SESSIONS["claude"](None))
    assert KEYS_05 <= set(data)
    assert TOTALS_05 <= set(data["totals"])
    assert RISK_05 <= set(data["risk"])
    assert all(STEP_05 <= set(s) for s in data["steps"])
    assert set(SCHEMA["required"]) >= KEYS_05


def test_schema_requires_what_the_receipt_always_has(capsys):
    """Every key the schema requires appears in a real receipt, and no top-level key is unknown."""
    data = _receipt(capsys, SESSIONS["claude"](None))
    assert set(SCHEMA["required"]) <= set(data)
    assert set(data) <= set(SCHEMA["properties"])


def test_markdown_on_stdout_ends_with_the_mode_comment(capsys):
    assert main(["receipt", str(SESSIONS["claude"](None)), "--format", "md", "-o", "-"]) == 0
    out = capsys.readouterr().out
    assert out.endswith(f"<!-- runledger: safe=true schema={SCHEMA_VERSION} -->\n")
    assert main(["receipt", str(SESSIONS["claude"](None)), "--format", "md", "-o", "-", "--no-safe"]) == 0
    assert capsys.readouterr().out.endswith("safe=false schema=1 -->\n")
