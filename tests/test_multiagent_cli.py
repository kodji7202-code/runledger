"""Several agents through the CLI: agent labels in receipts, push payloads and the
GitHub comment, and --agent on list, receipt and push. Claude Code and Codex
folders are pointed at empty temp directories, so the machine's real sessions
never change the results."""
import json
import os
import shutil
from pathlib import Path

import pytest

from runledger.cli import build, main
from runledger.client import PushError, build_payload
from runledger.github import Scored, main as github_main, render_comment
from runledger.parser import encode_project_path

FIX_NATIVE = Path(__file__).parent / "fixtures" / "native_session.json"
FIX_CLAUDE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
UNPRICED = {"runledger_format": 1, "agent": "lab-bot", "session_id": "lab-run-1",
            "cwd": "/work/lab", "prompts": ["Try the experiment"],
            "models": {"mystery-model-9": {"input_tokens": 500, "output_tokens": 50}},
            "reported_cost": 0.42,
            "steps": [{"tool": "Read", "input": {"file_path": "/work/lab/data.csv"}, "model": "mystery-model-9"}]}


@pytest.fixture(autouse=True)
def isolated_agent_dirs(tmp_path, monkeypatch):
    """No real Claude Code or Codex sessions may leak into these tests."""
    claude_dir = tmp_path / "claude-config"
    codex_dir = tmp_path / "codex-home"
    claude_dir.mkdir()
    codex_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    for name in ("GITHUB_REPOSITORY", "GITHUB_EVENT_PATH", "GITHUB_API_URL", "GITHUB_TOKEN",
                 "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "RUNLEDGER_SERVER", "RUNLEDGER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _project(tmp_path: Path, name: str, source: Path = None, text: str = None) -> Path:
    """A project folder with one session file in .runledger/runs/."""
    project = tmp_path / "project"
    runs = project / ".runledger" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    content = text if text is not None else source.read_text(encoding="utf-8")
    (runs / name).write_text(content, encoding="utf-8")
    return project


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- labels in receipts

def test_json_receipt_names_the_agent(capsys):
    assert main(["receipt", str(FIX_NATIVE), "--format", "json", "-o", "-"]) == 0
    data = _json_out(capsys)
    assert data["agent"] == "demo-agent"
    assert data["agent_id"] == "demo-agent"
    assert data["totals"]["cost_usd"] == pytest.approx(0.08255)  # estimate from the price table
    assert data["totals"]["reported_cost_usd"] == pytest.approx(0.0871)


def test_claude_code_receipt_uses_the_adapter_label(capsys):
    assert main(["receipt", str(FIX_CLAUDE), "--format", "json", "-o", "-"]) == 0
    data = _json_out(capsys)
    assert data["agent"] == "Claude Code"
    assert data["agent_id"] == "claude-code"
    assert data["totals"]["reported_cost_usd"] is None


def test_markdown_and_html_show_agent_and_both_costs(tmp_path):
    md, html = tmp_path / "r.md", tmp_path / "r.html"
    assert main(["receipt", str(FIX_NATIVE), "--format", "md", "-o", str(md)]) == 0
    assert main(["receipt", str(FIX_NATIVE), "-o", str(html)]) == 0
    md_text = md.read_text(encoding="utf-8")
    assert "**Agent:** demo-agent" in md_text
    assert "**Cost:** $0.083 est. · $0.087 reported by agent" in md_text
    html_text = html.read_text(encoding="utf-8")
    assert "demo-agent" in html_text
    assert "<small>est. · reported by agent $0.087</small>" in html_text
    assert "Reported cost comes from the agent itself." in html_text


def test_reported_cost_alone_when_the_model_has_no_price(tmp_path):
    path = tmp_path / "lab.runledger.json"
    path.write_text(json.dumps(UNPRICED), encoding="utf-8")
    run, score, level, risks, _ = build(str(path))
    assert run.cost is None and run.reported_cost == 0.42
    md = tmp_path / "lab.md"
    html = tmp_path / "lab.html"
    assert main(["receipt", str(path), "--format", "md", "-o", str(md)]) == 0
    assert main(["receipt", str(path), "-o", str(html)]) == 0
    assert "**Cost:** $0.420 reported by agent" in md.read_text(encoding="utf-8")
    assert "<b>$0.420</b><small>reported by agent</small>" in html.read_text(encoding="utf-8")
    assert "**Agent:** lab-bot" in md.read_text(encoding="utf-8")


def test_no_cost_at_all_is_n_a(tmp_path, capsys):
    path = tmp_path / "bare.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "agent": "bare"}), encoding="utf-8")
    assert main(["receipt", str(path), "--format", "json", "-o", "-"]) == 0
    data = _json_out(capsys)
    assert data["totals"]["cost_usd"] is None and data["totals"]["reported_cost_usd"] is None
    assert main(["receipt", str(path), "--format", "md", "-o", "-"]) == 0
    assert "**Cost:** n/a" in capsys.readouterr().out


def test_default_file_name_is_safe_for_any_session_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "evil.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "session_id": "../../etc/passwd"}), encoding="utf-8")
    assert main(["receipt", str(path), "--format", "md"]) == 0
    written = list(tmp_path.glob("runledger-*.md"))
    assert len(written) == 1
    assert "/" not in written[0].name and "\\" not in written[0].name


def test_invalid_native_file_is_reported_not_raised(tmp_path, capsys):
    bad = tmp_path / "bad.runledger.jsonl"
    bad.write_text(json.dumps({"runledger_format": 1}) + "\n" + json.dumps({"tool": ""}) + "\n", encoding="utf-8")
    assert main(["receipt", str(bad), "-o", str(tmp_path / "out.html")]) == 1
    err = capsys.readouterr().err
    assert "line 2" in err and "'tool' must be a non-empty string" in err


def test_missing_session_file_is_reported(tmp_path, capsys):
    assert main(["receipt", str(tmp_path / "nope.runledger.json"), "-o", str(tmp_path / "x.html")]) == 1
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------- --agent on list / receipt

def test_list_shows_an_agent_column_and_filters(tmp_path, capsys):
    project = _project(tmp_path, "demo.runledger.json", source=FIX_NATIVE)
    claude_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / encode_project_path(str(project))
    claude_dir.mkdir(parents=True)
    shutil.copy(FIX_CLAUDE, claude_dir / "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42.jsonl")

    assert main(["list", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split()[2] == "agent"
    assert "claude-code" in out and "demo-agent" in out

    assert main(["list", "--project", str(project), "--agent", "native"]) == 0
    out = capsys.readouterr().out
    assert "demo-agent" in out and "claude-code" not in out

    assert main(["list", "--project", str(project), "--agent", "claude-code"]) == 0
    out = capsys.readouterr().out
    assert "claude-code" in out and "demo-agent" not in out


def test_list_with_no_session_for_the_agent_exits_1(tmp_path, capsys):
    project = _project(tmp_path, "demo.runledger.json", source=FIX_NATIVE)
    assert main(["list", "--project", str(project), "--agent", "claude-code"]) == 1
    assert "Claude Code sessions found" in capsys.readouterr().err


def test_receipt_latest_honours_agent(tmp_path, capsys):
    project = _project(tmp_path, "demo.runledger.json", source=FIX_NATIVE)
    assert main(["receipt", "--project", str(project), "--agent", "native",
                 "--format", "json", "-o", "-"]) == 0
    assert _json_out(capsys)["agent"] == "demo-agent"
    assert main(["receipt", "--project", str(project), "--agent", "claude-code", "-o", "-"]) == 1
    assert "Claude Code sessions found" in capsys.readouterr().err


def test_receipt_defaults_to_the_current_folder(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, "demo.runledger.json", source=FIX_NATIVE)
    monkeypatch.chdir(project)
    assert main(["receipt", "--format", "json", "-o", "-"]) == 0
    assert _json_out(capsys)["agent"] == "demo-agent"


def test_agent_is_ignored_with_an_explicit_file(capsys):
    assert main(["receipt", str(FIX_NATIVE), "--agent", "claude-code", "--format", "json", "-o", "-"]) == 0
    captured = capsys.readouterr()
    assert "--agent is ignored" in captured.err
    assert json.loads(captured.out)["agent"] == "demo-agent"


def test_unknown_agent_name_is_rejected_by_the_parser():
    with pytest.raises(SystemExit) as exc:
        main(["list", "--agent", "cursor"])
    assert exc.value.code == 2


# ---------------------------------------------------------------- push and GitHub

def test_push_payload_carries_the_agent_label():
    payload = build_payload(FIX_NATIVE, user="dev", project="demo")
    assert payload["agent"] == "demo-agent"
    assert payload["agent_id"] == "demo-agent"
    assert payload["models"]["claude-sonnet-5-5"]["cost_usd"] > 0
    assert build_payload(FIX_CLAUDE, user="dev")["agent"] == "Claude Code"


def test_push_of_an_invalid_native_file_is_a_push_error(tmp_path):
    bad = tmp_path / "bad.runledger.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(PushError, match="top level must be a JSON object"):
        build_payload(bad)


def test_github_table_has_an_agent_and_model_column():
    run, score, level, risks, _ = build(str(FIX_NATIVE))
    markdown = render_comment([Scored(run, score, level, risks)])
    assert "| Run | Agent · Model | Steps | Cost | Risk |" in markdown
    assert "| demo-agent · Sonnet 5.5 |" in markdown
    assert "$0.083" in markdown


def test_github_table_shows_a_reported_cost_when_there_is_no_estimate(tmp_path):
    path = tmp_path / "lab.runledger.json"
    path.write_text(json.dumps(UNPRICED), encoding="utf-8")
    run, score, level, risks, _ = build(str(path))
    markdown = render_comment([Scored(run, score, level, risks)])
    assert "| lab-bot · mystery-model-9 |" in markdown
    assert "$0.420 reported" in markdown


def test_github_sessions_dir_scores_native_files(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    shutil.copy(FIX_NATIVE, sessions / "demo.runledger.json")
    assert github_main(["comment", "--sessions-dir", str(sessions), "--dry-run"]) == 0
    assert "demo-agent" in capsys.readouterr().out
