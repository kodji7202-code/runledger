"""New v0.7.0 rules as they appear in receipts (validation contract VAL-MCP-011, VAL-RULES-018).

The receipt re-runs the rules over the transcript, so an MCP verb class or a dangerous command
must show up in `risk.reasons` in every format, and the Guard decisions section must still
match the logged rows without leaking MCP arguments.
"""
import json
from pathlib import Path

import pytest

from runledger.cli import main

MODEL = "claude-sonnet-4-5-20250929"
MARKER = "SECRET-ARG-MARKER"


def _claude_session(tmp_path, calls, session_id="mcp-session"):
    """A minimal Claude Code transcript whose tool calls are `(name, input)` pairs."""
    rows = [{"cwd": str(tmp_path), "sessionId": session_id, "gitBranch": "main", "version": "2.1.0",
             "timestamp": "2026-10-19T10:00:00.000Z", "type": "user",
             "message": {"role": "user", "content": "Use the tools."}}]
    for i, (name, inp) in enumerate(calls):
        rows.append({
            "cwd": str(tmp_path), "sessionId": session_id, "gitBranch": "main", "version": "2.1.0",
            "timestamp": f"2026-10-19T10:00:{i + 1:02d}.000Z", "type": "assistant",
            "message": {"id": f"msg_{i}", "role": "assistant", "model": MODEL,
                        "content": [{"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}],
                        "usage": {"input_tokens": 10, "output_tokens": 5,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _droid_session(tmp_path, name, session_id="droid-session"):
    header = {"type": "session_start", "id": session_id, "cwd": str(tmp_path), "version": 1}
    message = {"type": "message", "id": "m1", "timestamp": "2026-10-19T10:00:00Z",
               "message": {"role": "assistant", "content": [
                   {"type": "tool_use", "id": "call-1", "name": name, "input": {}}]}}
    path = tmp_path / "droid.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (header, message)) + "\n", encoding="utf-8")
    return path


def _codex_session(tmp_path, name, session_id="codex-session"):
    rows = [
        {"timestamp": "2026-10-19T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": session_id, "cwd": str(tmp_path)}},
        {"timestamp": "2026-10-19T10:00:01.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": name, "arguments": "{}", "call_id": "c1"}},
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _receipt(capsys, path, fmt="json"):
    assert main(["receipt", str(path), "--format", fmt, "-o", "-"]) == 0
    return capsys.readouterr().out


def _reasons(capsys, path):
    return json.loads(_receipt(capsys, path))["risk"]["reasons"]


def _guard_log(tmp_path, session_id, tool_use_id, tool):
    log = tmp_path / ".runledger" / "guard.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text(json.dumps({
        "ts": "2026-10-19T10:00:05Z", "session_id": session_id, "tool": tool,
        "mode": "enforce", "tool_use_id": tool_use_id, "decision": "ask",
        "policy_decision": "ask", "codes": ["mcp"], "approval": None, "summary": "",
        "reason": "RunLedger guard needs confirmation: Called a destructive MCP tool",
    }) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- VAL-MCP-011

def test_receipt_renders_the_three_mcp_verb_classes(tmp_path, capsys):
    path = _claude_session(tmp_path, [
        ("mcp__supabase__delete_project", {"project_ref": MARKER}),
        ("mcp__x__create_post", {"text": "hello"}),
        ("mcp__x__get_me", {}),
    ])
    data = json.loads(_receipt(capsys, path))
    mcp = sorted((r for r in data["risk"]["reasons"] if r["code"] == "mcp"), key=lambda r: r["step"])
    assert [(r["severity"], r["reason"]) for r in mcp] == [
        ("high", "Called a destructive MCP tool (supabase: delete_project)"),
        ("medium", "Called a state-changing MCP tool (x: create_post)"),
        ("low", "Called an external MCP tool (get_me)"),
    ]
    assert data["risk"]["score"] >= 30
    assert data["risk"]["level"] != "Low"


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_receipt_formats_list_every_mcp_reason(tmp_path, capsys, fmt):
    path = _claude_session(tmp_path, [
        ("mcp__supabase__delete_project", {}), ("mcp__x__create_post", {}), ("mcp__x__get_me", {}),
    ])
    out = _receipt(capsys, path, fmt)
    for severity, label in (("high", "Called a destructive MCP tool (supabase: delete_project)"),
                            ("medium", "Called a state-changing MCP tool (x: create_post)"),
                            ("low", "Called an external MCP tool (get_me)")):
        marked = f"**{severity.upper()}**" if fmt == "md" else f'class="sev {severity}"'
        assert marked in out and label in out, (fmt, severity)


@pytest.mark.parametrize("builder", [_droid_session, _codex_session],
                         ids=["droid-connector", "codex-mcp"])
def test_other_agents_report_the_same_destructive_finding(tmp_path, capsys, builder):
    name = "supabase__delete_project" if builder is _droid_session else "mcp__supabase__delete_project"
    path = builder(tmp_path, name)
    mcp = [r for r in _reasons(capsys, path) if r["code"] == "mcp"]
    assert [(r["severity"], r["reason"]) for r in mcp] == [
        ("high", "Called a destructive MCP tool (supabase: delete_project)")]


def test_guard_decisions_section_matches_the_logged_mcp_row(tmp_path, capsys):
    path = _claude_session(tmp_path, [("mcp__supabase__delete_project", {"project_ref": MARKER})],
                           session_id="mcp-session")
    _guard_log(tmp_path, "mcp-session", "toolu_0", "mcp__supabase__delete_project")
    data = json.loads(_receipt(capsys, path))
    assert [(d["tool"], d["decision"], d["codes"]) for d in data["guard"]] == [
        ("mcp__supabase__delete_project", "ask", ["mcp"])]
    assert data["guard"][0]["outcome"]
    assert MARKER not in json.dumps(data["guard"])
    assert MARKER not in (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8")
    for fmt in ("md", "html"):
        out = _receipt(capsys, path, fmt)
        assert "Guard decisions" in out and "mcp__supabase__delete_project" in out
        assert MARKER not in out


# ---------------------------------------------------------------- VAL-RULES-018

COMMAND_CALLS = [
    ("Bash", {"command": "git push origin --delete x"}),
    ("Bash", {"command": "docker volume prune -f"}),
    ("Bash", {"command": "gh repo delete o/x --yes"}),
    ("Bash", {"command": "helm uninstall web"}),
    ("Bash", {"command": "aws s3 rm s3://b --recursive"}),
    ("Bash", {"command": "git branch -D x"}),
    ("Bash", {"command": "rsync -av --delete a/ b/"}),
]


def test_receipt_flags_the_new_command_rules(tmp_path, capsys):
    path = _claude_session(tmp_path, COMMAND_CALLS)
    data = json.loads(_receipt(capsys, path))
    found = {(r["severity"], r["reason"]) for r in data["risk"]["reasons"] if r["code"] == "command"}
    for expected in [
        ("high", "Deleted a remote git branch or tag"),
        ("high", "Pruned Docker volumes (data loss)"),
        ("high", "Deleted a GitHub repository"),
        ("high", "Uninstalled a Helm release"),
        ("high", "Deleted S3 objects/buckets recursively"),
        ("medium", "Force-deleted a git branch (git branch -D)"),
        ("medium", "rsync that deletes files"),
    ]:
        assert expected in found, sorted(found)
    assert data["risk"]["level"] == "High"


def test_receipt_md_lists_the_new_command_rules(tmp_path, capsys):
    path = _claude_session(tmp_path, COMMAND_CALLS)
    out = _receipt(capsys, path, "md")
    for reason in ["Deleted a remote git branch or tag", "Pruned Docker volumes (data loss)",
                   "Deleted a GitHub repository", "Uninstalled a Helm release",
                   "Deleted S3 objects/buckets recursively", "Force-deleted a git branch",
                   "rsync that deletes files"]:
        assert reason in out
