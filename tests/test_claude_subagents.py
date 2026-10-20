"""Claude Code sub-agent transcripts merge into parent session usage and cost (VAL-CORE-060)."""
import json
import os
from pathlib import Path

import pytest

from runledger import parser
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
    cfg = tmp_path / "c"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    prj = tmp_path / "p"
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
    legacy_sub = proj_dir / "subagents" / "agent-1.jsonl"
    legacy_sub.parent.mkdir(parents=True)
    legacy_sub.write_text(
        # Duplicate msg_p1 should NOT be double counted; msg_s1 is added once
        _assistant_line("msg_p1", "claude-sonnet-4-5-20250929", 500_000, session_id=sid)
        + _assistant_line("msg_s1", "claude-sonnet-4-5-20250929", 250_000, session_id=sid),
        encoding="utf-8",
    )

    # Another session's legacy subagent should NOT be merged into sid
    other_sub = proj_dir / "subagents" / "agent-2.jsonl"
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


def flaky_scandir(folder, failures, real=None):
    """The real `os.scandir`, refusing one folder the way an unreadable directory does."""
    real = real or os.scandir

    def scandir(path=None, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == folder:
            failures.append(str(path))
            raise PermissionError("synthetic unlistable folder")
        return real(path, **kwargs) if path is not None else real(**kwargs)

    return scandir


def test_folder_watch_says_whether_it_saw_the_whole_tree(tmp_path):
    """LOCAL-R3-STATS-01: the watch is capped for speed, so it reports whether it listed every
    folder. A capped walk means the parse cannot promise that a later transcript is seen."""
    root = tmp_path / "subagents"
    for index in range(parser._MAX_SUBAGENT_FOLDERS - 2):
        (root / f"f{index:02d}").mkdir(parents=True)
    folders, transcripts, complete = parser._walk_subagents(root)
    assert complete is True and len(folders) == parser._MAX_SUBAGENT_FOLDERS - 1
    assert transcripts == []

    (root / "one-more").mkdir()
    folders, _transcripts, complete = parser._walk_subagents(root)
    assert complete is False and len(folders) >= parser._MAX_SUBAGENT_FOLDERS


def test_the_walk_finds_every_transcript_it_walks_over(tmp_path):
    """The same walk that decides what is watched decides what is read: the transcripts of the
    whole tree, at any depth (the layout `rglob` used to cover)."""
    root = tmp_path / "subagents"
    (root / "workflows" / "wf1").mkdir(parents=True)
    (root / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "workflows" / "wf1" / "agent-2.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "workflows" / "notes.txt").write_text("not a transcript\n", encoding="utf-8")
    folders, transcripts, complete = parser._walk_subagents(root)
    assert complete is True
    assert sorted(path.name for path in transcripts) == ["agent-1.jsonl", "agent-2.jsonl"]
    assert sorted(folder.name for folder in folders) == ["subagents", "wf1", "workflows"]


def test_an_unlistable_folder_makes_the_watch_incomplete(tmp_path, monkeypatch):
    """A folder that cannot be listed is a folder whose sub-agent transcripts are unknown, so
    the walk reports it instead of quietly watching less than the tree holds."""
    root = tmp_path / "subagents"
    (root / "known").mkdir(parents=True)
    failures = []
    monkeypatch.setattr(os, "scandir", flaky_scandir(root / "known", failures))
    folders, transcripts, complete = parser._walk_subagents(root)
    assert [folder.name for folder in folders] == ["subagents", "known"]
    assert transcripts == []
    assert complete is False and failures


def test_a_failed_transcript_scan_makes_the_parse_incomplete(tmp_path, monkeypatch):
    """LOCAL-R4-STATS-01: the transcript scan is part of dependency discovery. A folder it
    cannot list is a folder whose transcripts it never saw, so the parse is incomplete - the
    same answer a capped folder watch gives, and what keeps `stats` from caching the totals."""
    cfg = tmp_path / "c"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    prj, sid = "/work/scan", "scan"
    proj_dir = cfg / "projects" / encode_project_path(prj)
    proj_dir.mkdir(parents=True)
    parent_file = proj_dir / f"{sid}.jsonl"
    parent_file.write_text(
        json.dumps({"type": "user", "sessionId": sid, "cwd": prj,
                    "message": {"content": "run subagents"}}) + "\n"
        + _assistant_line("msg_p1", "claude-sonnet-4-5-20250929", 1000, session_id=sid),
        encoding="utf-8",
    )
    tree = proj_dir / sid / "subagents"
    tree.mkdir(parents=True)
    (tree / "agent-1.jsonl").write_text(
        _assistant_line("msg_s1", "claude-sonnet-4-5-20250929", 500, session_id=sid),
        encoding="utf-8",
    )
    assert parse_session(parent_file).dependencies_complete is True
    assert parse_session(parent_file).usage.input_tokens == 1500

    with monkeypatch.context() as scan_error:
        scan_error.setattr(os, "scandir", flaky_scandir(tree, []))
        broken = parse_session(parent_file)
    assert broken.dependencies_complete is False
    assert broken.usage.input_tokens == 1000  # the transcript under the folder never listed

    recovered = parse_session(parent_file)
    assert recovered.dependencies_complete is True
    assert recovered.usage.input_tokens == 1500


def test_a_session_whose_folder_watch_was_capped_is_not_cacheable(tmp_path, monkeypatch):
    """The parse carries the watch's completeness, which is what `stats` refuses to cache on:
    a session whose sub-agent tree outgrows the watch is re-parsed, never trusted to a record."""
    cfg = tmp_path / "c"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    prj, sid = "/work/capped", "capped"
    proj_dir = cfg / "projects" / encode_project_path(prj)
    proj_dir.mkdir(parents=True)
    parent_file = proj_dir / f"{sid}.jsonl"
    parent_file.write_text(
        json.dumps({"type": "user", "sessionId": sid, "cwd": prj,
                    "message": {"content": "run subagents"}}) + "\n"
        + _assistant_line("msg_p1", "claude-sonnet-4-5-20250929", 1000, session_id=sid),
        encoding="utf-8",
    )
    assert parse_session(parent_file).dependencies_complete is True

    tree = proj_dir / sid / "subagents"
    for index in range(parser._MAX_SUBAGENT_FOLDERS):
        (tree / f"workflow-{index:02d}").mkdir(parents=True)
    assert parse_session(parent_file).dependencies_complete is False
