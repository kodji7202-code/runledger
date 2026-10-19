"""The per-agent hook layer: Droid and Codex payloads through the same decision path
(validation contract VAL-AGENT-013..029, VAL-MCP-005, VAL-RULES-019/022).

The payload shapes come from the recordings in `tests/fixtures/guard/hook_payloads.json`
(claude 2.1.293, droid 0.232.0, codex 0.159.0), copied from the mission's
`research/hook-payloads/` and sanitised: another user's home and the recording scratch
folders were replaced, while the key sets, session ids and tool ids are the recorded ones.
Real-shaped events for other tools reuse those key sets. Every secret below is synthetic.
"""
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

import pytest

from runledger import guard
from runledger.cli import main as cli_main

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "guard"
RECORDED = json.loads((FIXTURES / "hook_payloads.json").read_text(encoding="utf-8"))
RECORDED_SESSION = {
    "claude-code": "ad4fc5c6-5bc7-4c66-81c3-e4d6c2924991",
    "droid": "a96d6f95-c82e-463d-947c-e0cf04eb7fac",
    "codex": "01a148d0-fad4-7553-891d-c68363b89cf2",
}
ANTHROPIC_KEY = "sk-ant-api03-" + "x" * 30   # synthetic
AGENTS = ("claude-code", "droid", "codex")


# ---------------------------------------------------------------- helpers

def payload(agent, tool=None, tool_input=None, cwd=None, session=None, **extra):
    """A recorded event of this agent, with the tool call and folder replaced."""
    ev = copy.deepcopy(RECORDED[agent])
    if cwd is not None:
        ev["cwd"] = str(cwd)
    if session is not None:
        ev["session_id"] = session
    if tool is not None:
        ev["tool_name"] = tool
    if tool_input is not None:
        ev["tool_input"] = tool_input
    ev.update(extra)
    return ev


def bash(agent, command, cwd, **kw):
    """The agent's shell call. Droid uses `Execute`, Codex and Claude Code `Bash`."""
    tool = {"claude-code": "Bash", "droid": "Execute", "codex": "Bash"}[agent]
    inp = {"command": command}
    if agent == "droid":
        inp.update({"summary": "run a command", "riskLevel": "medium"})
    return payload(agent, tool, inp, cwd, **kw)


def decide(agent, tool, tool_input, cwd):
    return guard.decide(payload(agent, tool, tool_input, cwd), {}, agent=agent)


def handle(agent, tool, tool_input, cwd, **kw):
    return guard.handle(json.dumps(payload(agent, tool, tool_input, cwd, **kw)), agent=agent)


def permission(out):
    return out["hookSpecificOutput"]["permissionDecision"]


def log_records(cwd):
    path = Path(cwd) / ".runledger" / "guard.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def last_log(cwd):
    return log_records(cwd)[-1]


def write_user_config(home, data):
    folder = Path(home) / ".runledger"
    folder.mkdir(exist_ok=True)
    (folder / "config.json").write_text(json.dumps(data), encoding="utf-8")


def write_project(cwd, data):
    (Path(cwd) / ".runledger.json").write_text(json.dumps(data), encoding="utf-8")


class FakeApprover:
    """Returns a fixed status. Records each call."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, request, approval):
        self.calls.append(request)
        return self.answer


COMMANDS = [
    "rm -rf x", "sudo ls", "cat .env", "git push origin main", "pip install x", "echo hi",
    "git push origin --delete x", "git branch -D old", "find . -delete", "docker system prune -f",
    'gh repo delete o/r --yes', 'psql -c "DELETE FROM users"', "rsync --delete a b",
    "helm uninstall rel", "dd of=/dev/sda", "git checkout .", "git stash drop", "ls -la",
    "pytest -q", "curl https://example.com", "git status", "npm install", "mkdir sub",
    "Remove-Item -Recurse -Force x", "Get-Content .env", "Remove-Item -Recurse x",
    "reg delete HKCU\\Software\\x /f", "aws s3 rm s3://b/k", "rd /s /q x", "git rm -f x",
]
PATCH_SECRET = ("*** Begin Patch\n*** Add File: src/keys.py\n"
                "+ANTHROPIC = \"" + ANTHROPIC_KEY + "\"\n*** End Patch\n")


def hook_process(agent, stdin_text, cwd, extra_env=None):
    """Run the real hook command in a subprocess, as an agent would."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra_env or {})
    argv = [sys.executable, "-m", "runledger", "guard"]
    if agent is not None:
        argv += ["--agent", agent]
    return subprocess.run(argv, input=stdin_text.encode("utf-8"), capture_output=True,
                          cwd=str(cwd), env=env, timeout=60)


# ---------------------------------------------------------------- hook output per agent (VAL-AGENT-013, 014, 017)

def test_claude_hook_output_is_unchanged(tmp_path):
    deny = handle("claude-code", "Bash", {"command": "rm -rf x"}, tmp_path)
    assert deny == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": deny["hookSpecificOutput"]["permissionDecisionReason"]}}
    assert "Recursive force delete (rm -rf)" in deny["hookSpecificOutput"]["permissionDecisionReason"]

    assert permission(handle("claude-code", "Bash", {"command": "cat .env"}, tmp_path)) == "ask"
    assert handle("claude-code", "Bash", {"command": "echo rl-ready"}, tmp_path) is None

    approved = guard.decide(payload("claude-code", "Bash", {"command": "sudo ls"}, tmp_path), {},
                            approver=FakeApprover("approved"), agent="claude-code")
    assert permission(guard.hook_output(approved, "claude-code")) == "allow"


def test_claude_deny_is_logged_with_the_tool_use_id(tmp_path):
    handle("claude-code", "Bash", {"command": "rm -rf x"}, tmp_path)
    rec = last_log(tmp_path)
    assert rec["tool_use_id"] == RECORDED["claude-code"]["tool_use_id"]
    assert rec["decision"] == "deny" and rec["agent"] == "claude-code"


def test_droid_output_is_json_exit_zero_and_silent_allow(tmp_path):
    deny = handle("droid", "Execute", {"command": "rm -rf x"}, tmp_path)
    assert json.loads(json.dumps(deny)) == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": deny["hookSpecificOutput"]["permissionDecisionReason"]}}
    assert "Recursive force delete (rm -rf)" in deny["hookSpecificOutput"]["permissionDecisionReason"]
    assert permission(handle("droid", "Read", {"file_path": str(tmp_path / ".env")}, tmp_path)) == "ask"
    assert handle("droid", "Execute", {"command": "echo rl-ready"}, tmp_path) is None

    approved = guard.decide(payload("droid", "Execute", {"command": "sudo ls"}, tmp_path), {},
                            approver=FakeApprover("approved"), agent="droid")
    assert permission(guard.hook_output(approved, "droid")) == "allow"


def test_droid_hook_process_never_exits_two(tmp_path):
    env = dict(os.environ)
    for command in ("rm -rf x", "echo rl-ready"):
        proc = hook_process("droid", json.dumps(bash("droid", command, tmp_path)), tmp_path)
        assert proc.returncode == 0, proc.stderr
        if command == "rm -rf x":
            assert json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"] == "deny"
        else:
            assert proc.stdout == b""
        assert proc.stderr == b""


def test_codex_deny_prints_json_and_allow_prints_nothing(tmp_path):
    deny = handle("codex", "Bash", {"command": "rm -rf x"}, tmp_path)
    assert permission(deny) == "deny"
    assert "Recursive force delete (rm -rf)" in deny["hookSpecificOutput"]["permissionDecisionReason"]
    assert permission(handle("codex", "Bash", {"command": "git push origin --delete x"}, tmp_path)) == "deny"

    out = handle("codex", "Bash", {"command": "echo rl-ready"}, tmp_path)
    assert out is None or (permission(out) == "allow"
                           and set(out["hookSpecificOutput"]) == {
                               "hookEventName", "permissionDecision", "permissionDecisionReason"})


def test_codex_ask_becomes_deny_with_the_explanation(tmp_path):
    for tool, tool_input in (("Bash", {"command": "cat .env"}),
                             ("mcp__supabase__delete_project", {})):
        out = handle("codex", tool, tool_input, tmp_path)
        assert permission(out) == "deny", (tool, out)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "cannot prompt" in reason
        assert "approval" in reason and "rule" in reason
        rec = last_log(tmp_path)
        assert rec["policy_decision"] == "ask" and rec["decision"] == "deny"
        assert rec["agent"] == "codex"
    assert "secret_file" in last_log(tmp_path)["codes"] or "mcp" in last_log(tmp_path)["codes"]


def test_codex_never_prints_ask_but_claude_and_droid_do(tmp_path):
    for agent in ("claude-code", "droid"):
        assert permission(handle(agent, "Bash" if agent == "claude-code" else "Execute",
                                 {"command": "cat .env"}, tmp_path)) == "ask"
    assert permission(handle("codex", "Bash", {"command": "cat .env"}, tmp_path)) == "deny"


# ---------------------------------------------------------------- normalisation and parity (VAL-AGENT-015, 016)

def test_droid_decisions_match_claude_for_the_same_commands(tmp_path):
    for command in COMMANDS:
        claude = decide("claude-code", "Bash", {"command": command}, tmp_path)
        droid = decide("droid", "Execute",
                       {"command": command, "summary": "run a command", "riskLevel": "medium"}, tmp_path)
        assert (droid["decision"], droid["policy_decision"], sorted(droid["codes"])) == \
               (claude["decision"], claude["policy_decision"], sorted(claude["codes"])), command


def test_droid_execute_is_logged_with_its_own_tool_name_and_canonical_tool(tmp_path):
    handle("droid", "Execute", {"command": "rm -rf x"}, tmp_path)
    rec = last_log(tmp_path)
    assert rec["tool"] == "Execute" and rec["agent"] == "droid" and rec["canonical_tool"] == "Bash"


def test_normalize_event_maps_the_droid_tools():
    cases = {
        "Execute": ("Bash", {"command": "ls"}),
        "Create": ("Write", {"file_path": "a.py", "content": "x"}),
        "Edit": ("Edit", {"file_path": "a.py", "old_str": "a", "new_str": "b"}),
        "Read": ("Read", {"file_path": "a.py"}),
        "LS": ("Search", {"path": "src"}),
        "Glob": ("Search", {"pattern": "*.py"}),
        "Grep": ("Search", {"pattern": "x"}),
        "FetchUrl": ("WebFetch", {"url": "https://example.test"}),
        "WebSearch": ("WebSearch", {"query": "x"}),
        "mcp__supabase__delete_project": ("mcp__supabase__delete_project", {"project_id": "p"}),
        "supabase__delete_project": ("supabase__delete_project", {"project_id": "p"}),
    }
    for name, (want_tool, tool_input) in cases.items():
        event = {"tool_name": name, "tool_input": tool_input}
        tool, inp, steps = guard.normalize_event(event, "droid")
        assert tool == name and inp == tool_input
        assert steps[0][0] == want_tool, name
    tool, _, steps = guard.normalize_event({"tool_name": "Frobnicate", "tool_input": {}}, "droid")
    assert (tool, steps) == ("Frobnicate", [("Frobnicate", {})])


def test_normalize_event_maps_the_codex_tools():
    tool, _, steps = guard.normalize_event({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "codex")
    assert (tool, steps) == ("Bash", [("Bash", {"command": "ls"})])
    _, _, steps = guard.normalize_event(
        {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch\n"}},
        "codex")
    assert steps == [("Write", {"file_path": "a.py", "content": "x\n"})]
    _, _, steps = guard.normalize_event({"tool_name": "update_plan", "tool_input": {}}, "codex")
    assert steps == [("update_plan", {})]


def test_normalize_event_tolerates_odd_tool_input():
    for tool_input in (None, {}, "a plain string", 5, [1, 2]):
        tool, inp, steps = guard.normalize_event({"tool_name": "apply_patch", "tool_input": tool_input}, "codex")
        assert tool == "apply_patch" and isinstance(inp, dict)
        assert steps and all(isinstance(s[0], str) and isinstance(s[1], dict) for s in steps)
    tool, inp, steps = guard.normalize_event({"tool_name": "Execute"}, "droid")
    assert (tool, inp, steps) == ("Execute", {}, [("Bash", {})])


# ---------------------------------------------------------------- Droid file tools (VAL-AGENT-016)

def test_droid_create_outside_the_project_asks(tmp_path):
    outside = Path(tmp_path).parent / "elsewhere" / "x.py"
    d = decide("droid", "Create", {"file_path": str(outside), "content": "print(1)"}, tmp_path)
    assert d["decision"] == "ask" and "write_outside" in d["codes"]


def test_droid_create_with_a_secret_is_denied_without_leaking_the_body(tmp_path, capsys):
    inp = {"file_path": str(tmp_path / "config.py"), "content": f'KEY = "{ANTHROPIC_KEY}"'}
    out = handle("droid", "Create", inp, tmp_path)
    assert permission(out) == "deny"
    raw_log = (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8")
    assert ANTHROPIC_KEY not in raw_log
    assert ANTHROPIC_KEY not in json.dumps(out)
    assert "secret_in_content" in last_log(tmp_path)["codes"]


def test_droid_edit_and_read_of_a_secrets_file_ask(tmp_path):
    (tmp_path / ".env").write_text("A=1", encoding="utf-8")
    for tool, tool_input in (("Edit", {"file_path": str(tmp_path / ".env"), "old_str": "A=1", "new_str": "A=2"}),
                             ("Read", {"file_path": str(tmp_path / ".env")})):
        d = decide("droid", tool, tool_input, tmp_path)
        assert d["decision"] == "ask" and "secret_file" in d["codes"], tool


def test_droid_edits_of_normal_files_are_allowed(tmp_path):
    d = decide("droid", "Edit", {"file_path": str(tmp_path / "a.py"), "old_str": "a", "new_str": "b"}, tmp_path)
    assert d["decision"] == "allow" and d["codes"] == []
    assert handle("droid", "Edit", {"file_path": str(tmp_path / "a.py"),
                                    "old_str": "a", "new_str": "b"}, tmp_path) is None


@pytest.mark.parametrize("tool, tool_input", [
    ("Read", {"file_path": "a.py"}), ("LS", {"path": "src"}), ("Glob", {"pattern": "*.py"}),
    ("Grep", {"pattern": "x"}), ("Task", {"description": "look around"}),
    ("FetchUrl", {"url": "https://example.test"}), ("WebSearch", {"query": "x"}),
])
def test_droid_read_only_tools_allow_and_print_nothing(tmp_path, tool, tool_input):
    assert handle("droid", tool, tool_input, tmp_path) is None


@pytest.mark.parametrize("tool_input", [
    {"input": "*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch\n"},
    {"patch": "*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch\n"},
    {}, {"input": "not a patch at all"}, "a plain string", None,
])
def test_droid_apply_patch_never_crashes(tmp_path, tool_input, capsys):
    event = payload("droid", "ApplyPatch", tool_input, tmp_path)
    out = guard.handle(json.dumps(event), agent="droid")
    assert out is None or permission(out) in ("allow", "ask", "deny")
    assert "Traceback" not in capsys.readouterr().err


def test_droid_apply_patch_with_a_string_input_exits_zero(tmp_path):
    proc = hook_process("droid", json.dumps(payload("droid", "ApplyPatch", "a plain string", tmp_path)), tmp_path)
    assert proc.returncode == 0 and b"Traceback" not in proc.stderr
    proc = hook_process("droid", json.dumps(payload("droid", "ApplyPatch", {}, tmp_path)), tmp_path)
    assert proc.returncode == 0 and b"Traceback" not in proc.stderr


def test_droid_apply_patch_with_a_secret_is_denied(tmp_path):
    out = handle("droid", "ApplyPatch", {"input": PATCH_SECRET}, tmp_path)
    assert permission(out) == "deny"
    assert "secret_in_content" in last_log(tmp_path)["codes"]


def test_an_unknown_droid_tool_is_logged_with_canonical_tool_null(tmp_path):
    assert handle("droid", "Frobnicate", {"thing": "x"}, tmp_path) is None
    rec = last_log(tmp_path)
    assert rec["tool"] == "Frobnicate" and rec["canonical_tool"] is None
    assert rec["decision"] == "allow" and rec["codes"] == []


# ---------------------------------------------------------------- Codex apply_patch and MCP tools (VAL-AGENT-019)

def patch_event(patch, cwd):
    return payload("codex", "apply_patch", {"command": patch}, cwd)


def test_codex_apply_patch_add_with_a_secret_is_denied(tmp_path):
    out = guard.handle(json.dumps(patch_event(PATCH_SECRET, tmp_path)), agent="codex")
    assert permission(out) == "deny"
    assert "secret_in_content" in last_log(tmp_path)["codes"]


def test_codex_apply_patch_update_of_env_asks_then_denies_for_codex(tmp_path):
    patch = ("*** Begin Patch\n*** Update File: .env\n@@\n-A=1\n+A=2\n*** End Patch\n")
    assert decide("claude-code", "Edit", {"file_path": ".env", "old_string": "A=1", "new_string": "A=2"},
                  tmp_path)["decision"] == "ask"
    out = guard.handle(json.dumps(patch_event(patch, tmp_path)), agent="codex")
    assert permission(out) == "deny"
    assert last_log(tmp_path)["policy_decision"] == "ask"


def test_codex_apply_patch_deleting_a_test_file_is_denied(tmp_path):
    patch = "*** Begin Patch\n*** Delete File: tests/test_a.py\n*** End Patch\n"
    out = guard.handle(json.dumps(patch_event(patch, tmp_path)), agent="codex")
    assert permission(out) == "deny"
    assert "test_deleted" in last_log(tmp_path)["codes"]


def test_codex_apply_patch_of_a_harmless_file_is_allowed(tmp_path):
    patch = "*** Begin Patch\n*** Add File: src/new.py\n+print('hi')\n*** End Patch\n"
    assert guard.handle(json.dumps(patch_event(patch, tmp_path)), agent="codex") is None
    rec = last_log(tmp_path)
    assert rec["decision"] == "allow" and rec["summary"] == "src/new.py"


def test_codex_apply_patch_with_several_files_is_evaluated_for_all_of_them(tmp_path):
    patch = ("*** Begin Patch\n*** Add File: src/new.py\n+print('hi')\n"
             "*** Add File: cfg.py\n+KEY = \"" + ANTHROPIC_KEY + "\"\n*** End Patch\n")
    out = guard.handle(json.dumps(patch_event(patch, tmp_path)), agent="codex")
    assert permission(out) == "deny"
    rec = last_log(tmp_path)
    assert rec["summary"] == "src/new.py, cfg.py"
    raw_log = (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8")
    assert ANTHROPIC_KEY not in raw_log and "print('hi')" not in raw_log


def test_codex_unparseable_patch_does_not_crash(tmp_path):
    proc = hook_process("codex", json.dumps(patch_event("*** Begin Patch oops", tmp_path)), tmp_path)
    assert proc.returncode == 0 and b"Traceback" not in proc.stderr


def test_codex_mcp_tools_use_the_shared_classifier(tmp_path):
    deny = handle("codex", "mcp__supabase__delete_project", {}, tmp_path)
    assert permission(deny) == "deny" and "cannot prompt" in deny["hookSpecificOutput"]["permissionDecisionReason"]
    assert handle("codex", "mcp__supabase__get_project", {}, tmp_path) is None
    assert handle("codex", "update_plan", {"plan": []}, tmp_path) is None


# ---------------------------------------------------------------- guard.codex_ask (VAL-AGENT-020)

def test_codex_ask_allow_is_a_user_setting(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"codex_ask": "allow"}})
    for tool, tool_input in (("Bash", {"command": "cat .env"}), ("mcp__supabase__delete_project", {})):
        out = handle("codex", tool, tool_input, tmp_path)
        assert out is None or permission(out) != "ask", (tool, out)
    rec = last_log(tmp_path)
    assert rec["policy_decision"] == "ask" and rec["decision"] == "allow"
    assert "codex" in rec["reason"].lower()
    assert permission(handle("codex", "Bash", {"command": "rm -rf x"}, tmp_path)) == "deny"


def test_codex_ask_deny_is_the_default_and_allows_nothing_new(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"codex_ask": "deny"}})
    assert permission(handle("codex", "Bash", {"command": "cat .env"}, tmp_path)) == "deny"


def test_a_project_cannot_set_codex_ask(tmp_path):
    write_project(tmp_path, {"guard": {"codex_ask": "allow"}})
    out = handle("codex", "Bash", {"command": "cat .env"}, tmp_path)
    assert permission(out) == "deny"
    rows = log_records(tmp_path)
    assert any(r["decision"] == "warning" and "codex_ask" in r["reason"] for r in rows)
    decision = guard.decide(payload("codex", "Bash", {"command": "cat .env"}, tmp_path),
                            guard.read_policy(str(tmp_path)), agent="codex")
    assert decision["warnings"] and "codex_ask" in " ".join(decision["warnings"])


def test_codex_ask_has_no_effect_on_the_other_agents(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"codex_ask": "allow"}})
    assert permission(handle("claude-code", "Bash", {"command": "cat .env"}, tmp_path)) == "ask"
    assert permission(handle("droid", "Execute", {"command": "cat .env"}, tmp_path)) == "ask"


# ---------------------------------------------------------------- approval server flow (VAL-AGENT-021)

@pytest.fixture
def approval_server():
    """A local stand-in for the approval server: one POST, then scripted GET answers."""
    state = {"statuses": ["approved"], "posts": [], "gets": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self):
            return self.headers.get("Authorization") == "Bearer test-key"

        def do_POST(self):
            if self.path != "/api/approvals" or not self._authorised():
                return self._send(401, {"error": "unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            state["posts"].append(json.loads(self.rfile.read(length) or b"{}"))
            self._send(201, {"id": "req-1", "status": "pending"})

        def do_GET(self):
            if not self._authorised():
                return self._send(401, {"error": "unauthorized"})
            if self.path != "/api/approvals/req-1":
                return self._send(404, {"error": "not found"})
            state["gets"] += 1
            seq = state["statuses"]
            status = seq.pop(0) if len(seq) > 1 else seq[0]
            self._send(200, {"id": "req-1", "status": status})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", state
    finally:
        srv.shutdown()
        srv.server_close()


def ask_event(agent, command, cwd):
    return payload(agent, "Bash" if agent != "droid" else "Execute", {"command": command}, cwd,
                   session="sess-approval")


def test_approved_ask_prints_allow_for_claude_and_droid(tmp_path):
    for agent in ("claude-code", "droid"):
        d = guard.decide(ask_event(agent, "sudo ls", tmp_path), {}, approver=FakeApprover("approved"),
                         agent=agent)
        assert d["decision"] == "allow" and d["approval"] == "approved"
        assert permission(guard.hook_output(d, agent)) == "allow"


def test_codex_approved_ask_is_allow_and_never_ask(tmp_path):
    d = guard.decide(ask_event("codex", "sudo ls", tmp_path), {}, approver=FakeApprover("approved"),
                     agent="codex")
    assert d["decision"] == "allow" and d["approval"] == "approved"
    assert guard.hook_output(d, "codex") is None


def test_codex_denied_and_timed_out_asks_deny_with_the_codex_reason(tmp_path):
    for answer, wording in (("denied", "Denied in the RunLedger approval queue"),
                            ("timeout", "No approval within")):
        d = guard.decide(ask_event("codex", "sudo ls", tmp_path), {}, approver=FakeApprover(answer),
                         agent="codex")
        assert d["decision"] == "deny" and d["approval"] == answer
        out = guard.hook_output(d, "codex")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert permission(out) == "deny" and "cannot prompt" in reason
        # The server's own answer stays in the reason, so the real cause is visible.
        assert wording in reason and "sudo" in reason


def test_codex_ask_allow_still_yields_to_an_approval_answer(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"codex_ask": "allow"}})
    d = guard.decide(ask_event("codex", "sudo ls", tmp_path), {}, approver=FakeApprover("denied"),
                     agent="codex")
    assert d["decision"] == "deny" and d["approval"] == "denied"
    assert "cannot prompt" in d["reason"] and "Denied in the RunLedger approval queue" in d["reason"]


def test_denied_ask_denies_for_droid_and_claude(tmp_path):
    for agent in ("claude-code", "droid"):
        d = guard.decide(ask_event(agent, "sudo ls", tmp_path), {}, approver=FakeApprover("denied"),
                         agent=agent)
        assert d["decision"] == "deny" and permission(guard.hook_output(d, agent)) == "deny"


def test_the_approval_request_carries_the_contract(tmp_path, approval_server, monkeypatch):
    url, state = approval_server
    monkeypatch.setenv("RUNLEDGER_SERVER", url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    monkeypatch.setattr(guard, "POLL_INTERVAL_S", 0.01)
    d = guard.decide(ask_event("codex", "sudo curl -H 'Authorization: Bearer " + ANTHROPIC_KEY + "' x", tmp_path),
                     {}, agent="codex")
    assert d["decision"] == "allow" and d["approval"] == "approved"
    assert len(state["posts"]) == 1
    post = state["posts"][0]
    assert set(post) == {"session_id", "tool", "summary", "risks", "cwd"}
    assert post["session_id"] == "sess-approval" and post["tool"] == "Bash"
    assert post["cwd"] == str(tmp_path)
    assert ANTHROPIC_KEY not in json.dumps(post)
    assert "test-key" not in json.dumps(post)


def test_plain_allow_and_deny_never_contact_the_approval_server(tmp_path, approval_server, monkeypatch):
    url, state = approval_server
    monkeypatch.setenv("RUNLEDGER_SERVER", url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    for agent in AGENTS:
        guard.decide(ask_event(agent, "echo hi", tmp_path), {}, agent=agent)
        guard.decide(ask_event(agent, "rm -rf x", tmp_path), {}, agent=agent)
    assert state["posts"] == [] and state["gets"] == 0


# ---------------------------------------------------------------- PowerShell under Bash / Execute (VAL-AGENT-022)

@pytest.mark.parametrize("agent", ["droid", "codex"])
def test_powershell_recursive_force_delete_is_denied_under_bash_and_execute(tmp_path, agent):
    for command in ("Remove-Item -Recurse -Force .\\victim", "Remove-Item -Recurse -Force victim"):
        d = decide(agent, "Bash" if agent == "codex" else "Execute", {"command": command}, tmp_path)
        assert d["decision"] == "deny", command
        assert "Recursive force delete (Remove-Item -Recurse -Force)" in d["reason"]


@pytest.mark.parametrize("agent", ["droid", "codex"])
def test_powershell_recursive_delete_without_force_is_medium(tmp_path, agent):
    d = decide(agent, "Bash" if agent == "codex" else "Execute",
               {"command": "Remove-Item -Recurse victim"}, tmp_path)
    assert d["decision"] == "allow" and d["codes"] == ["command"]


def test_powershell_get_content_of_env_asks_for_droid_and_denies_for_codex(tmp_path):
    droid = decide("droid", "Execute", {"command": "Get-Content .env"}, tmp_path)
    assert droid["decision"] == "ask"
    codex = guard.decide(payload("codex", "Bash", {"command": "Get-Content .env"}, tmp_path), {}, agent="codex")
    assert codex["decision"] == "deny" and "cannot prompt" in codex["reason"]


def test_posix_and_powershell_deletes_are_caught_under_every_tool_name(tmp_path):
    assert permission(handle("droid", "Execute", {"command": "rm -rf victim"}, tmp_path)) == "deny"
    assert permission(handle("codex", "Bash", {"command": "rm -rf victim"}, tmp_path)) == "deny"
    assert permission(handle("claude-code", "PowerShell", {"command": "Remove-Item -Recurse -Force victim"},
                             tmp_path)) == "deny"


# ---------------------------------------------------------------- unknown agent and inference (VAL-AGENT-023)

def test_an_unknown_agent_warns_on_stderr_and_never_blocks(tmp_path):
    benign = hook_process("bogus", json.dumps(bash("claude-code", "echo hi", tmp_path)), tmp_path)
    assert benign.returncode == 0
    assert benign.stdout == b""
    assert b"bogus" in benign.stderr

    deny = hook_process("bogus", json.dumps(bash("claude-code", "rm -rf x", tmp_path)), tmp_path)
    assert deny.returncode == 0
    assert json.loads(deny.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert b"bogus" in deny.stderr


def test_the_agent_is_inferred_from_the_payload_shape(tmp_path):
    assert guard.infer_agent(payload("droid", cwd=tmp_path)) == "droid"
    assert guard.infer_agent(payload("codex", cwd=tmp_path)) == "codex"
    assert guard.infer_agent(payload("claude-code", cwd=tmp_path)) == "claude-code"
    assert guard.normalize_agent("droid") == "droid" and guard.normalize_agent("bogus") is None

    out = guard.handle(json.dumps(bash("droid", "rm -rf x", tmp_path)))
    assert permission(out) == "deny"
    assert last_log(tmp_path)["agent"] == "droid"

    out = guard.handle(json.dumps(bash("codex", "cat .env", tmp_path)))
    assert permission(out) == "deny"
    assert last_log(tmp_path)["agent"] == "codex"


# ---------------------------------------------------------------- garbage, fail-open, fail-closed (VAL-AGENT-024)

@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("raw", ["", "not json", "[]", '"str"'])
def test_unparseable_input_fails_open_for_every_agent(tmp_path, agent, raw):
    assert guard.handle(raw, agent=agent) is None
    proc = hook_process(agent, raw, tmp_path)
    assert proc.returncode == 0 and proc.stdout == b""


@pytest.mark.parametrize("agent", AGENTS)
def test_a_missing_tool_input_fails_open(tmp_path, agent):
    event = payload(agent, cwd=tmp_path)
    del event["tool_input"]
    assert guard.handle(json.dumps(event), agent=agent) is None
    assert not any(r["decision"] == "error" for r in log_records(tmp_path))


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("raw", ["", "not json", "[]", '"str"'])
def test_fail_closed_denies_the_unparseable_inputs_for_every_agent(tmp_path, isolated_settings, agent, raw):
    write_user_config(isolated_settings, {"guard": {"fail_closed": True}})
    out = guard.handle(raw, agent=agent)
    assert permission(out) == "deny"
    assert handle(agent, "Bash" if agent != "droid" else "Execute", {"command": "echo hi"}, tmp_path) is None


@pytest.mark.parametrize("agent", AGENTS)
def test_monitor_mode_blocks_nothing_for_any_agent(tmp_path, isolated_settings, agent):
    write_user_config(isolated_settings, {"guard": {"mode": "monitor", "fail_closed": True}})
    assert guard.handle("not json", agent=agent) is None
    assert handle(agent, "Bash" if agent != "droid" else "Execute", {"command": "rm -rf x"}, tmp_path) is None
    rec = last_log(tmp_path)
    assert rec["policy_decision"] == "deny" and rec["decision"] == "allow" and rec["mode"] == "monitor"


# ---------------------------------------------------------------- project root and log per agent (VAL-AGENT-025)

def test_droid_uses_factory_project_dir_for_the_policy_and_the_log(tmp_path, monkeypatch):
    root = tmp_path / "p"
    deep = root / "sub" / "deep"
    deep.mkdir(parents=True)
    write_project(root, {"guard": {"deny": ["severity:medium"]}})
    monkeypatch.setenv("FACTORY_PROJECT_DIR", str(root))
    out = handle("droid", "Execute", {"command": "docker system prune -f"}, deep)
    assert permission(out) == "deny"
    assert (root / ".runledger" / "guard.log").exists()
    assert not (deep / ".runledger").exists()


def test_codex_logs_at_the_git_top_level_and_at_cwd_outside_a_repo(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    handle("codex", "Bash", {"command": "echo hi"}, sub)
    assert (repo / ".runledger" / "guard.log").exists()
    assert not (sub / ".runledger").exists()

    plain = tmp_path / "plain"
    plain.mkdir()
    handle("codex", "Bash", {"command": "echo hi"}, plain)
    assert (plain / ".runledger" / "guard.log").exists()


def test_claude_project_dir_beats_the_payload_cwd(tmp_path, monkeypatch):
    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    handle("claude-code", "Bash", {"command": "echo hi"}, other)
    assert (root / ".runledger" / "guard.log").exists()
    assert not (other / ".runledger").exists()


@pytest.mark.parametrize("agent", AGENTS)
def test_an_in_project_path_is_not_outside_when_cwd_is_a_subfolder(tmp_path, monkeypatch, agent):
    root = tmp_path / "p"
    sub = root / "sub"
    sub.mkdir(parents=True)
    write_project(root, {})
    if agent == "droid":
        monkeypatch.setenv("FACTORY_PROJECT_DIR", str(root))
    (root / "data.txt").write_text("x", encoding="utf-8")
    d = decide(agent, "Read", {"file_path": str(root / "data.txt")}, sub) if agent != "codex" else \
        guard.decide(payload("codex", "Read", {"file_path": str(root / "data.txt")}, sub), {}, agent=agent)
    assert d["decision"] == "allow"
    assert not {"read_outside", "write_outside", "shell_outside"} & set(d["codes"])


def test_a_project_policy_in_the_root_applies_from_a_subfolder_for_codex(tmp_path):
    root = tmp_path / "p"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / ".git").mkdir()
    write_project(root, {"guard": {"deny": ["severity:medium"]}})
    d = guard.decide(payload("codex", "Bash", {"command": "docker system prune -f"}, sub), None, agent="codex")
    assert d["decision"] == "deny" and d["cwd"] == str(root)


# ---------------------------------------------------------------- log rows (VAL-AGENT-026, 029)

@pytest.mark.parametrize("agent", AGENTS)
def test_log_rows_carry_the_agent_the_session_id_and_the_contract_fields(tmp_path, agent):
    handle(agent, "Bash" if agent != "droid" else "Execute", {"command": "echo rl-ready"}, tmp_path)
    rec = last_log(tmp_path)
    assert rec["session_id"] == RECORDED_SESSION[agent]
    assert rec["agent"] == agent
    assert rec["tool"] == ("Execute" if agent == "droid" else "Bash")
    assert rec["canonical_tool"] == "Bash"
    assert rec["tool_use_id"] == ("" if agent == "droid" else RECORDED[agent]["tool_use_id"])
    assert set(rec) >= {"ts", "session_id", "tool", "agent", "canonical_tool", "decision",
                        "policy_decision", "codes", "summary", "reason", "mode", "tool_use_id"}
    assert all(len(v) <= 200 for v in rec.values() if isinstance(v, str))


def test_the_recorded_session_id_is_the_transcript_stem_and_reaches_the_log(tmp_path):
    """VAL-AGENT-029's chain, on the recorded payloads: the agent's own session id (the
    transcript file stem; for Codex the rollout id that ends the file name) is the hook
    `session_id` and the log row."""
    for agent in AGENTS:
        ev = payload(agent, cwd=tmp_path)
        # The recorded payloads carry Windows transcript paths, so parse them as such: on
        # POSIX `Path` would treat the backslashes as part of the file name.
        stem = PureWindowsPath(ev["transcript_path"]).stem
        assert stem == ev["session_id"] if agent != "codex" else stem.endswith(ev["session_id"]), agent
        handle(agent, "Bash" if agent != "droid" else "Execute", {"command": "echo hi"}, tmp_path)
        assert last_log(tmp_path)["session_id"] == ev["session_id"], agent


def test_warning_rows_carry_the_session_id_and_a_reason(tmp_path):
    write_project(tmp_path, {"guard": {"mode": "monitor"}})
    handle("droid", "Execute", {"command": "echo hi"}, tmp_path)
    warnings = [r for r in log_records(tmp_path) if r["decision"] == "warning"]
    assert warnings
    for rec in warnings:
        assert rec["ts"] and rec["session_id"] == RECORDED_SESSION["droid"] and rec["reason"]


def test_a_v060_log_row_without_agent_is_still_read_by_receipts(tmp_path, capsys):
    log = tmp_path / ".runledger" / "guard.log"
    log.parent.mkdir()
    log.write_text(json.dumps({
        "ts": "2026-10-01T00:00:00+00:00", "session_id": "old-session", "tool": "Bash",
        "summary": "rm -rf x", "decision": "deny", "policy_decision": "deny",
        "codes": ["command"], "reason": "Blocked by RunLedger guard", "mode": "enforce",
        "approval": None, "tool_use_id": "",
    }) + "\n", encoding="utf-8")
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({
        "type": "assistant", "sessionId": "old-session", "cwd": str(tmp_path),
        "message": {"id": "m1", "content": [{"type": "tool_use", "id": "", "name": "Bash",
                                             "input": {"command": "rm -rf x"}}]},
    }) + "\n", encoding="utf-8")
    assert cli_main(["receipt", str(session), "--format", "json", "-o", "-"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["guard"][0]["decision"] == "deny"


# ---------------------------------------------------------------- the installed hook command form

@pytest.mark.parametrize("command", [
    '"C:\\py\\python.exe" -m runledger guard --agent droid',
    '"C:\\py\\python3.13t.exe" -m runledger guard --agent codex',
    "runledger guard --agent claude-code",
    "/usr/bin/runledger guard --agent droid",
])
def test_a_guard_command_with_an_agent_counts_as_ours(command):
    assert guard._owned_command_parts(command) is not None
    assert guard._is_guard_hook({"type": "command", "command": command})


@pytest.mark.parametrize("command", [
    "runledger guard --agent bogus", "echo runledger guard --agent droid",
    "other.sh && runledger guard --agent droid", "runledger guard --agent",
])
def test_a_foreign_command_that_mentions_an_agent_is_not_ours(command):
    assert guard._owned_command_parts(command) is None


def test_the_installed_droid_command_run_as_stored_behaves_the_same(tmp_path):
    command = f'"{sys.executable}" -m runledger guard --agent droid'
    ok, message = guard.verify_hook_command(command, cwd=str(tmp_path))
    assert ok, message
    deny = hook_process_command(command, json.dumps(bash("droid", "rm -rf x", tmp_path)), tmp_path)
    assert deny.returncode == 0
    assert permission(json.loads(deny.stdout.decode("utf-8"))) == "deny"


def hook_process_command(command, stdin_text, cwd):
    """Run one stored hook command string the way the agent would (through its shell)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(command, input=stdin_text.encode("utf-8"), capture_output=True,
                          cwd=str(cwd), env=env, timeout=60, shell=True)


# ---------------------------------------------------------------- speed and offline default path (VAL-AGENT-027)

def test_the_hook_is_fast_and_offline_on_the_default_path(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNLEDGER_SERVER", raising=False)
    events = []
    for agent in AGENTS:
        events.append(json.dumps(bash(agent, "rm -rf x", tmp_path)))
        events.append(json.dumps(bash(agent, "echo rl-ready", tmp_path)))
    started = time.perf_counter()
    times = []
    for _ in range(20):
        for raw in events:
            t0 = time.perf_counter()
            guard.handle(raw)
            times.append(time.perf_counter() - t0)
    elapsed = time.perf_counter() - started
    times.sort()
    assert times[len(times) // 2] < 0.5
    assert elapsed < 20
    assert all(r["approval"] is None for r in log_records(tmp_path))


def test_no_request_is_ever_sent_to_a_host_from_the_project_file(tmp_path, approval_server, monkeypatch):
    url, state = approval_server
    write_project(tmp_path, {"guard": {"approval": {"server": url, "api_key_env": "RUNLEDGER_API_KEY"}}})
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    monkeypatch.delenv("RUNLEDGER_SERVER", raising=False)
    d = decide("droid", "Execute", {"command": "sudo ls"}, tmp_path)
    assert d["decision"] == "ask"
    assert state["posts"] == []


# ---------------------------------------------------------------- MCP names and user rules per agent (VAL-MCP-005, VAL-RULES-019/022)

def guard_test_output(capsys, argv):
    assert cli_main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_droid_connector_names_and_codex_mcp_names_use_the_same_classifier(tmp_path, capsys):
    for tool, tool_input, decision, severity, named in (
        ("supabase__delete_project", {"project_id": "p"}, "ask", "high", "supabase: delete_project"),
        ("mcp__supabase__delete_project", {"project_id": "p"}, "ask", "high", "supabase: delete_project"),
        ("vercel__create_deployment", {"project": "p"}, "allow", "medium", "vercel: create_deployment"),
        ("x__get_me", {}, "allow", "low", "get_me"),
    ):
        event = payload("droid", tool, tool_input, tmp_path, session="sess-mcp")
        out = guard_test_output(capsys, ["guard", "test", "--agent", "droid", json.dumps(event)])
        assert out["decision"]["decision"] == decision, tool
        mcp = [r for r in out["decision"]["risks"] if r["code"] == "mcp"]
        assert mcp and mcp[0]["severity"] == severity, tool
        assert named in mcp[0]["reason"], tool
    for tool in ("Execute", "Read", "Create", "Edit", "LS", "Glob", "Grep", "Task", "FetchUrl", "WebSearch"):
        event = payload("droid", tool, {"file_path": "a.py"}, tmp_path, session="sess-mcp")
        out = guard_test_output(capsys, ["guard", "test", "--agent", "droid", json.dumps(event)])
        assert not [r for r in out["decision"]["risks"] if r["code"] == "mcp"], tool
    event = payload("codex", "mcp__supabase__delete_project", {}, tmp_path, session="sess-mcp")
    out = guard_test_output(capsys, ["guard", "test", "--agent", "codex", json.dumps(event)])
    assert out["hook_output"]["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_test_without_agent_keeps_the_claude_shape_and_writes_no_log(tmp_path, capsys):
    out = guard_test_output(capsys, ["guard", "test", json.dumps(bash("claude-code", "rm -rf x", tmp_path))])
    assert out["decision"]["decision"] == "deny"
    assert out["hook_output"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not (tmp_path / ".runledger").exists()


def test_a_user_command_rule_applies_to_a_droid_execute_payload(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"command_rules": {
        "deny": [r"\bmyfancytool\s+wipe\b"],
        "ask": [{"pattern": "secretword", "raw": True, "reason": "raw match"}]}})
    d = decide("droid", "Execute", {"command": "myfancytool wipe all"}, tmp_path)
    assert d["decision"] == "deny" and d["codes"] == ["rule_deny"]
    assert decide("droid", "Execute", {"command": 'echo "myfancytool wipe"'}, tmp_path)["decision"] == "allow"
    assert permission(handle("droid", "Execute", {"command": "echo secretword"}, tmp_path)) == "ask"
    assert guard.hook_output(d, "codex") is not None  # the rule hit survives hook_output


def test_tool_rules_apply_to_droid_edit_and_codex_ask_hits_become_deny(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"tool_rules": {"deny": ["Edit"], "ask": ["mcp__vercel__*"]}})
    d = decide("droid", "Edit", {"file_path": str(tmp_path / "a.py"), "old_str": "a", "new_str": "b"}, tmp_path)
    assert d["decision"] == "deny" and "rule_deny" in d["codes"]
    assert permission(handle("droid", "Edit", {"file_path": str(tmp_path / "a.py"),
                                               "old_str": "a", "new_str": "b"}, tmp_path)) == "deny"
    out = handle("codex", "mcp__vercel__get_project", {"project": "p"}, tmp_path)
    assert permission(out) == "deny"
    assert "cannot prompt" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_tool_rules_match_a_droid_bare_connector_name(tmp_path, isolated_settings):
    """GE-006: a rule written for the canonical `mcp__<server>__<tool>` name also covers the
    bare connector name Droid reports, exactly as receipts read it, while the log keeps the
    agent's own spelling."""
    write_user_config(isolated_settings, {"tool_rules": {"deny": ["mcp__supabase__execute_query"],
                                                         "ask": ["mcp__vercel__*"]}})
    denied = decide("droid", "supabase__execute_query", {"query": "SELECT 1"}, tmp_path)
    assert denied["decision"] == "deny" and "rule_deny" in denied["codes"]
    asked = decide("droid", "vercel__get_project", {"project": "p"}, tmp_path)
    assert asked["decision"] == "ask" and "rule_ask" in asked["codes"]

    assert permission(handle("droid", "supabase__execute_query", {"query": "SELECT 1"},
                             tmp_path, session="sess-bare")) == "deny"
    row = [r for r in log_records(tmp_path) if r["session_id"] == "sess-bare"][-1]
    assert row["tool"] == "supabase__execute_query"
    assert row["canonical_tool"] == "mcp__supabase__execute_query"
    assert "rule_deny" in row["codes"]


def test_a_droid_bare_connector_name_rule_still_matches_its_own_spelling(tmp_path, isolated_settings):
    """The bare spelling a user may already have written keeps working next to the canonical one."""
    write_user_config(isolated_settings, {"tool_rules": {"deny": ["supabase__execute_query"]}})
    d = decide("droid", "supabase__execute_query", {"query": "SELECT 1"}, tmp_path)
    assert d["decision"] == "deny" and "rule_deny" in d["codes"]
    assert decide("droid", "vercel__get_project", {"project": "p"}, tmp_path)["decision"] == "allow"
