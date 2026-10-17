"""Claude Code sub-agent transcripts merge into parent session usage and cost (VAL-CORE-060)."""
import json

import pytest

from runledger.cli import main
from runledger.parser import encode_project_path, find_sessions, parse_session


def _assistant_line(msg_id: str, model: str, input_tokens: int, tool_id: str = "", cmd: str = "",
                    session_id: str = "") -> str:
    content = [{"type": "text", "text": "ok"}]
    if tool_id:
        content.append({"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": cmd}})
    ev = {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            "content": content,
        },
    }
    if session_id:
        ev["sessionId"] = session_id
    return json.dumps(ev) + "\n"


def test_subagent_transcripts_merged_into_parent_cost_and_tokens(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    prj = "/work/proj"
    enc = encode_project_path(prj)
    proj_dir = cfg / "projects" / enc
    proj_dir.mkdir(parents=True)

    sid = "s1"
    parent_file = proj_dir / f"{sid}.jsonl"
    parent_file.write_text(
        json.dumps({"type": "user", "sessionId": sid, "cwd": prj,
                    "message": {"content": "run subagents"}}) + "\n"
        + _assistant_line("msg_p1", "claude-sonnet-4-5-20250929", 1_000_000, tool_id="t_p1", cmd="echo parent",
                          session_id=sid),
        encoding="utf-8",
    )

    sub1 = proj_dir / sid / "subagents" / "agent-1.jsonl"
    sub1.parent.mkdir(parents=True)
    sub1.write_text(
        _assistant_line("msg_s1", "claude-sonnet-4-5-20250929", 1_000_000, tool_id="t_s1", cmd="echo sub1",
                        session_id=sid),
        encoding="utf-8",
    )

    sub2 = proj_dir / sid / "subagents" / "workflows" / "wf1" / "agent-2.jsonl"
    sub2.parent.mkdir(parents=True)
    sub2.write_text(
        _assistant_line("msg_s2", "claude-sonnet-4-5-20250929", 1_000_000, tool_id="t_s2", cmd="echo sub2",
                        session_id=sid),
        encoding="utf-8",
    )

    assert main(["receipt", str(parent_file), "--format", "json", "-o", "-"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["cost_usd"] == pytest.approx(9.0)
    assert data["totals"]["tokens"] == 3_000_000
    assert data["totals"]["input_tokens"] == 3_000_000
    assert data["models"]["claude-sonnet-4-5-20250929"]["tokens"] == 3_000_000
    assert len(data["steps"]) == 1
    assert data["steps"][0]["summary"] == "Ran `echo parent`"
    assert any("2 sub-agent transcripts" in n for n in data.get("notes", []))

    assert main(["receipt", str(parent_file), "--format", "md", "-o", "-"]) == 0
    md = capsys.readouterr().out
    assert "includes 2 sub-agent transcripts" in md


def test_legacy_subagents_merged_and_duplicate_message_ids_deduped(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    prj = tmp_path / "work" / "legacy"
    prj.mkdir(parents=True)
    proj_dir = cfg / "projects" / encode_project_path(str(prj))
    proj_dir.mkdir(parents=True)

    sid = "sess-legacy"
    parent_file = proj_dir / f"{sid}.jsonl"
    parent_file.write_text(
        _assistant_line("msg_p1", "claude-sonnet-4-5-20250929", 500_000, tool_id="t1", cmd="ls", session_id=sid),
        encoding="utf-8",
    )

    # Legacy layout: <project>/subagents/agent-*.jsonl
    legacy_sub = proj_dir / "subagents" / "agent-legacy.jsonl"
    legacy_sub.parent.mkdir(parents=True)
    legacy_sub.write_text(
        # Duplicate msg_p1 should NOT be double counted; msg_s1 is added once
        _assistant_line("msg_p1", "claude-sonnet-4-5-20250929", 500_000, session_id=sid)
        + _assistant_line("msg_s1", "claude-sonnet-4-5-20250929", 250_000, session_id=sid),
        encoding="utf-8",
    )

    # Another session's legacy subagent should NOT be merged into sid
    other_sub = proj_dir / "subagents" / "agent-other.jsonl"
    other_sub.write_text(
        _assistant_line("msg_other", "claude-sonnet-4-5-20250929", 900_000, session_id="other-session"),
        encoding="utf-8",
    )

    run = parse_session(parent_file)
    assert run.usage.input_tokens == 750_000
    assert len(run.steps) == 1
    assert any("1 sub-agent transcript" in n for n in run.notes)

    # Sub-agent files are never listed separately
    assert find_sessions(str(prj)) == [parent_file]
    assert find_sessions(None) == [parent_file]
