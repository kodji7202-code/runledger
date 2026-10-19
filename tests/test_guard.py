"""Tests for runledger.guard: the PreToolUse policy guard. Covers the default and
custom rules, monitor and fail-open / fail-closed behaviour, the approval flow (a fake
approver, plus a local HTTP stand-in for the server), log redaction and the
settings.json installer. Every file lives in tmp_path; nothing touches the real
~/.claude. Every sample secret below is synthetic."""
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from runledger import guard
from runledger.cli import main as cli_main

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AWS_KEY = "AKIAABCDEFGHIJKLMNOP"             # synthetic: AKIA + 16 caps
ANTHROPIC_KEY = "sk-ant-api03-" + "x" * 30   # synthetic
APPROVAL = {"timeout_s": 5}   # the server is never read from the policy; see RUNLEDGER_SERVER
SERVER = "https://approvals.example.test"


def bash_event(cwd, command, session="sess-1"):
    return {"session_id": session, "transcript_path": "t.jsonl", "cwd": str(cwd),
            "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}


def write_event(cwd, name, content):
    return {"session_id": "sess-1", "cwd": str(cwd), "hook_event_name": "PreToolUse",
            "tool_name": "Write", "tool_input": {"file_path": str(Path(cwd) / name), "content": content}}


def policy(**guard_cfg):
    return {"guard": guard_cfg}


def boom(*_args, **_kwargs):
    raise RuntimeError("simulated internal bug")


class FakeApprover:
    """Returns a fixed status, or raises the given exception. Records each call."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, request, approval):
        self.calls.append((request, approval))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# ---------------------------------------------------------------- default rules

def test_default_denies_rm_rf_root(tmp_path):
    d = guard.decide(bash_event(tmp_path, "rm -rf /"), {})
    assert d["decision"] == "deny"
    assert "command" in d["codes"]


def test_default_denies_writing_an_aws_key_without_echoing_it(tmp_path):
    d = guard.decide(write_event(tmp_path, "config.py", f'AWS_KEY = "{AWS_KEY}"'), {})
    assert d["decision"] == "deny"
    assert "secret_in_content" in d["codes"]
    assert AWS_KEY not in json.dumps(d)


def test_default_asks_on_sudo(tmp_path):
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {})
    assert d["decision"] == "ask"
    assert guard.hook_output(d)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_default_still_denies_rm_rf_under_sudo(tmp_path):
    d = guard.decide(bash_event(tmp_path, "sudo rm -rf /var/data"), {})
    assert d["decision"] == "deny"


def test_default_denies_a_delete_after_a_multiline_ansi_c_string(tmp_path):
    """CORE-SCR-R2-002: a `$'...'` string may contain literal newlines; the `rm -rf` after its
    closing quote is still read, so the default deny stays in effect."""
    d = guard.decide(bash_event(tmp_path, "echo $'first\nsecond'\nrm -rf ./build"), {})
    assert d["decision"] == "deny"
    assert "command" in d["codes"]


def test_default_asks_again_for_an_operand_after_a_spaced_drive_path(tmp_path):
    """CORE-SCR-R2-004: `../outside.txt` is an argument of its own, so deleting it is still a
    high finding and the guard asks instead of allowing the command."""
    repo = tmp_path / "client work"
    repo.mkdir()
    command = "rm " + str(repo).replace(chr(92), "/") + "/a.txt ../outside.txt"
    d = guard.decide(bash_event(repo, command), {})
    assert d["policy_decision"] == "ask"
    assert d["decision"] == "ask"
    assert any(r["code"] == "shell_outside" and r["severity"] == "high" for r in d["risks"])


@pytest.mark.parametrize("template", [
    "rm {root}/sub/a.txt nested/../../outside.txt",
    "rm {root}/sub/a.txt > {root}/sub/log nested/../../outside.txt",
    "rm < {root}/sub/input nested/../../outside.txt",
])
def test_default_asks_again_for_a_traversing_operand_after_a_spaced_drive_path(tmp_path, template):
    """CORE-SCR-R3-001: `nested/../../outside.txt` climbs out of the project, so its high
    finding survives the display join and the default guard asks instead of deleting it."""
    repo = tmp_path / "client work"
    repo.mkdir()
    command = template.format(root=str(repo).replace(chr(92), "/"))
    d = guard.decide(bash_event(repo, command), {})
    assert d["policy_decision"] == "ask"
    assert d["decision"] == "ask"
    outside = [r for r in d["risks"] if r["code"] == "shell_outside"
               and r["severity"] == "high" and r["reason"].endswith("(nested/../../outside.txt)")]
    assert len(outside) == 1


def test_default_asks_and_names_the_traversing_operand_of_a_backslash_drive_path(tmp_path):
    """CORE-SCR-R3-001, the literal reproducer: `rm C:\\a b\\x nested/../../outside.txt` asks
    again, and the escaping operand is named next to the whole-path finding."""
    d = guard.decide(bash_event(tmp_path, r"rm C:\a b\x nested/../../outside.txt"), {})
    assert d["policy_decision"] == "ask"
    assert d["decision"] == "ask"
    reasons = [r["reason"] for r in d["risks"]
               if r["code"] == "shell_outside" and r["severity"] == "high"]
    assert any(reason.endswith("(nested/../../outside.txt)") for reason in reasons)


def test_default_asks_for_the_escaping_operand_of_an_explicit_rm_executable(tmp_path):
    """CORE-SCR-R4-001: the executable `bin/rm` and the plain argument `sub/echo` stay separate
    words, so deleting `../outside.txt` is a high finding and the guard asks instead of
    allowing an outside delete."""
    repo = tmp_path / "repo"
    repo.mkdir()
    command = str(repo).replace(chr(92), "/") + "/bin/rm sub/echo ../outside.txt"
    d = guard.decide(bash_event(repo, command), {})
    assert d["policy_decision"] == "ask"
    assert d["decision"] == "ask"
    assert [(r["severity"], r["code"]) for r in d["risks"] if r["code"] == "shell_outside"] == [
        ("high", "shell_outside")]


def test_the_spaced_path_label_does_not_change_the_guard_decision(tmp_path):
    """The label only rewrites the path a receipt shows: the unquoted spaced path asks exactly
    like the same command with the path quoted, with the same codes and severities."""
    repo = tmp_path / "repo"
    repo.mkdir()
    loose = guard.decide(bash_event(repo, r"echo hi > E:\Work\Client y\z.txt"), {})
    quoted = guard.decide(bash_event(repo, r"echo hi > 'E:\Work\Client y\z.txt'"), {})
    assert loose["decision"] == quoted["decision"]
    assert [(r["code"], r["severity"]) for r in loose["risks"]] == \
        [(r["code"], r["severity"]) for r in quoted["risks"]]


def test_default_denies_a_delete_after_a_queued_heredoc_past_a_multiline_ansi_c_string(tmp_path):
    """CORE-SCR-R3-002: a newline inside `$'...'` is part of that word, not the end of the
    command line, so the queued heredoc starts after the real terminator and the `rm -rf`
    following it is still read: the default guard denies it."""
    command = "echo <<EOF $'first\nsecond'\npayload\nEOF\nrm -rf ./build"
    d = guard.decide(bash_event(tmp_path, command), {})
    assert d["decision"] == "deny"
    assert "command" in d["codes"]


def test_default_allows_ls_and_prints_nothing(tmp_path):
    d = guard.decide(bash_event(tmp_path, "ls"), {})
    assert d["decision"] == "allow"
    assert guard.hook_output(d) is None


def test_default_allows_medium_risk(tmp_path):
    d = guard.decide(bash_event(tmp_path, "git push origin main"), {})
    assert d["decision"] == "allow"
    assert d["codes"] == ["command"]


def test_default_asks_on_other_high_risk(tmp_path):
    ev = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Read",
          "tool_input": {"file_path": str(tmp_path / ".env")}}
    assert guard.decide(ev, {})["decision"] == "ask"


def test_missing_guard_section_uses_defaults(tmp_path):
    assert guard.decide(bash_event(tmp_path, "rm -rf /"), {"ignore": []})["decision"] == "deny"


# ---------------------------------------------------------------- monitor mode

def test_monitor_mode_never_blocks_but_records_what_it_would_do(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"mode": "monitor"}})
    d = guard.decide(bash_event(tmp_path, "rm -rf /"), {})
    assert d["decision"] == "allow"
    assert d["policy_decision"] == "deny"
    assert d["mode"] == "monitor"
    assert guard.hook_output(d) is None


def test_monitor_mode_handle_prints_nothing_and_logs(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"mode": "monitor"}})
    assert guard.handle(json.dumps(bash_event(tmp_path, "rm -rf /"))) is None
    rec = _last_log(tmp_path)
    assert rec["decision"] == "allow" and rec["policy_decision"] == "deny"


# ---------------------------------------------------------------- custom rules

def test_custom_deny_reason_entry(tmp_path):
    d = guard.decide(bash_event(tmp_path, "npm install left-pad"), policy(deny=["reason:Installed packages"]))
    assert d["decision"] == "deny"


def test_custom_ask_code_entry(tmp_path):
    ev = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__github__create_issue",
          "tool_input": {"title": "x"}}
    assert guard.decide(ev, policy(ask=["mcp"]))["decision"] == "ask"
    assert guard.decide(ev, {})["decision"] == "allow"  # the default allows low-risk MCP calls


def test_custom_severity_and_code_severity_entries(tmp_path):
    push = bash_event(tmp_path, "git push origin main")  # medium
    assert guard.decide(push, policy(deny=["severity:medium"]))["decision"] == "deny"
    reset = bash_event(tmp_path, "git reset --hard")     # medium, code "command"
    assert guard.decide(reset, policy(deny=["command:high"]))["decision"] == "allow"


def test_exception_entry_keeps_matches_out_of_a_list(tmp_path):
    cfg = policy(deny=["command:high", "!reason:ran a command with sudo"], ask=["severity:high"])
    assert guard.decide(bash_event(tmp_path, "sudo apt-get update"), cfg)["decision"] == "ask"
    assert guard.decide(bash_event(tmp_path, "rm -rf /"), cfg)["decision"] == "deny"


def test_user_list_replaces_the_default_list(tmp_path, isolated_settings):
    # The user's deny: [] replaces the default deny list, so rm -rf falls to the default ask.
    write_user_config(isolated_settings, {"guard": {"deny": []}})
    assert guard.decide(bash_event(tmp_path, "rm -rf /"), {})["decision"] == "ask"


def test_invalid_entries_are_ignored_and_bad_mode_means_enforce(tmp_path):
    cfg = policy(mode="sometimes", deny=["severity:extreme", 42, "command:high"])
    assert guard.decide(bash_event(tmp_path, "rm -rf /"), cfg)["decision"] == "deny"


def test_user_ignore_and_severity_overrides_apply(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"ignore": ["Recursive force delete"]})
    assert guard.decide(bash_event(tmp_path, "rm -rf /"), {})["decision"] == "allow"
    write_user_config(isolated_settings, {"severity_overrides": {"command": "low"}})
    assert guard.decide(bash_event(tmp_path, "rm -rf /"), {})["decision"] == "allow"


# ---------------------------------------------------------------- hook output and failures

def test_deny_output_has_the_hook_contract_and_a_reason_for_claude(tmp_path):
    out = guard.handle(json.dumps(bash_event(tmp_path, "rm -rf /")))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    assert "Recursive force delete" in spec["permissionDecisionReason"]


def test_garbage_stdin_fails_open(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for raw in ["{not json", "", "[1, 2]", "null"]:
        assert guard.handle(raw) is None


def test_internal_error_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "decide", boom)
    assert guard.handle(json.dumps(bash_event(tmp_path, "rm -rf /"))) is None


def test_fail_closed_denies_on_internal_error(tmp_path, monkeypatch):
    (tmp_path / ".runledger.json").write_text(json.dumps(policy(fail_closed=True)), encoding="utf-8")
    monkeypatch.setattr(guard, "decide", boom)
    out = guard.handle(json.dumps(bash_event(tmp_path, "ls")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "fail_closed" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_fail_closed_denies_on_garbage_stdin(tmp_path, monkeypatch):
    (tmp_path / ".runledger.json").write_text(json.dumps(policy(fail_closed=True)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = guard.handle("this is not json")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_fail_closed_is_ignored_in_monitor_mode(tmp_path, monkeypatch, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"mode": "monitor"}})
    (tmp_path / ".runledger.json").write_text(json.dumps(policy(fail_closed=True)), encoding="utf-8")
    monkeypatch.setattr(guard, "decide", boom)
    assert guard.handle(json.dumps(bash_event(tmp_path, "ls"))) is None


def test_log_failure_does_not_change_the_decision(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")  # cannot create .runledger under a file
    out = guard.handle(json.dumps(bash_event(blocker, "rm -rf /")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------- approval flow (fake approver)

def test_approved_turns_ask_into_allow(tmp_path):
    fake = FakeApprover("approved")
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), policy(approval=APPROVAL), approver=fake)
    assert d["decision"] == "allow" and d["approval"] == "approved"
    out = guard.hook_output(d)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(fake.calls) == 1


def test_denied_turns_ask_into_deny(tmp_path):
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), policy(approval=APPROVAL),
                     approver=FakeApprover("denied"))
    assert d["decision"] == "deny" and d["approval"] == "denied"
    assert "Denied" in d["reason"]


def test_timeout_denies_by_default(tmp_path):
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), policy(approval=APPROVAL),
                     approver=FakeApprover("timeout"))
    assert d["decision"] == "deny" and d["approval"] == "timeout"
    assert "No approval" in d["reason"]


def test_timeout_can_fall_back_to_the_local_prompt(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"approval": {"on_timeout": "ask"}}})
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {}, approver=FakeApprover("timeout"))
    assert d["decision"] == "ask"


def test_approval_request_has_the_server_contract_and_redacted_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_SERVER", SERVER)
    fake = FakeApprover("approved")
    cmd = f"echo {ANTHROPIC_KEY} | sudo tee /etc/x"
    guard.decide(bash_event(tmp_path, cmd, session="abc"), policy(approval=APPROVAL), approver=fake)
    request, approval_cfg = fake.calls[0]
    assert set(request) == {"session_id", "tool", "summary", "risks", "cwd"}
    assert request["session_id"] == "abc" and request["tool"] == "Bash"
    assert request["cwd"] == str(tmp_path)
    assert set(request["risks"][0]) == {"severity", "code", "reason"}
    assert ANTHROPIC_KEY not in request["summary"]
    assert "[REDACTED]" in request["summary"]
    assert approval_cfg["server"] == SERVER


def test_approver_error_falls_back_to_a_local_ask(tmp_path):
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), policy(approval=APPROVAL),
                     approver=FakeApprover(guard.ApprovalError("server down")))
    assert d["decision"] == "ask" and d["approval"] == "unavailable"
    assert "server down" in d["reason"]


def test_no_approval_server_keeps_the_ask(tmp_path):
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {}, approver=None)
    assert d["decision"] == "ask" and d["approval"] is None


def test_monitor_mode_never_calls_the_approver(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"mode": "monitor"}})
    fake = FakeApprover("approved")
    guard.decide(bash_event(tmp_path, "sudo apt-get update"), policy(approval=APPROVAL), approver=fake)
    assert fake.calls == []


# ---------------------------------------------------------------- approval HTTP client

@pytest.fixture
def approval_server():
    """A local stand-in for the approval server. `state["statuses"]` is the sequence
    of answers to GETs; the last one repeats. Only the key "test-key" is accepted."""
    state = {"statuses": ["pending", "approved"], "posts": []}

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


def test_http_approver_polls_until_approved(tmp_path, approval_server, monkeypatch):
    url, state = approval_server
    monkeypatch.setenv("RUNLEDGER_SERVER", url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    monkeypatch.setattr(guard, "POLL_INTERVAL_S", 0.01)
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), policy(approval={"timeout_s": 10}))
    assert d["decision"] == "allow" and d["approval"] == "approved"
    assert state["posts"][0]["tool"] == "Bash"
    assert set(state["posts"][0]) == {"session_id", "tool", "summary", "risks", "cwd"}


def test_http_approver_reports_timeout_when_nobody_answers(tmp_path, approval_server, monkeypatch):
    url, state = approval_server
    state["statuses"] = ["pending"]
    monkeypatch.setenv("RUNLEDGER_SERVER", url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    monkeypatch.setattr(guard, "POLL_INTERVAL_S", 0.01)
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), policy(approval={"timeout_s": 0.3}))
    assert d["decision"] == "deny" and d["approval"] == "timeout"


def test_http_approver_wrong_key_falls_back_to_a_local_ask(tmp_path, approval_server, monkeypatch):
    url, _state = approval_server
    monkeypatch.setenv("RUNLEDGER_SERVER", url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "wrong-key")
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {})
    assert d["decision"] == "ask" and d["approval"] == "unavailable"
    assert "401" in d["reason"]


def test_unreachable_server_falls_back_to_a_local_ask(tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]          # now closed: nothing listens there
    monkeypatch.setenv("RUNLEDGER_SERVER", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {})
    assert d["decision"] == "ask" and d["approval"] == "unavailable"


def test_missing_api_key_falls_back_to_a_local_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_SERVER", "http://127.0.0.1:9")
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {})
    assert d["decision"] == "ask" and d["approval"] == "unavailable"
    assert "RUNLEDGER_API_KEY" in d["reason"]


# ---------------------------------------------------------------- log and redaction

def _last_log(cwd):
    lines = (Path(cwd) / ".runledger" / "guard.log").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def test_log_is_jsonl_with_the_decision_fields(tmp_path):
    event = bash_event(tmp_path, "rm -rf /", session="sess-9")
    event["tool_use_id"] = "toolu-guard-9"
    guard.handle(json.dumps(event))
    rec = _last_log(tmp_path)
    assert rec["session_id"] == "sess-9"
    assert rec["tool_use_id"] == "toolu-guard-9"
    assert rec["tool"] == "Bash" and rec["decision"] == "deny"
    assert "command" in rec["codes"]
    assert rec["ts"].endswith("+00:00")


def test_log_redacts_secrets_and_truncates_the_command(tmp_path):
    cmd = f"curl -H 'Authorization: Bearer {ANTHROPIC_KEY}' https://example.com/" + "a" * 300
    guard.handle(json.dumps(bash_event(tmp_path, cmd)))
    raw = (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8")
    assert ANTHROPIC_KEY not in raw
    rec = _last_log(tmp_path)
    assert "[REDACTED]" in rec["summary"]
    assert len(rec["summary"]) <= 200


def test_plain_allow_is_logged_but_not_shown_in_receipt(tmp_path, capsys):
    event = bash_event(tmp_path, "ls")
    event["tool_use_id"] = "toolu-plain-allow"
    assert guard.handle(json.dumps(event)) is None
    record = _last_log(tmp_path)
    assert record["decision"] == record["policy_decision"] == "allow"
    assert record["session_id"] == event["session_id"]
    assert record["tool_use_id"] == event["tool_use_id"]
    assert record["tool"] == "Bash" and record["summary"] == "ls"
    assert record["approval"] is None

    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({
        "type": "assistant", "sessionId": event["session_id"], "cwd": str(tmp_path),
        "message": {"id": "plain-allow-message", "content": [{
            "type": "tool_use", "id": event["tool_use_id"], "name": "Bash",
            "input": {"command": "ls"},
        }]},
    }) + "\n", encoding="utf-8")
    for fmt in ("html", "md", "json"):
        assert cli_main(["receipt", str(session), "--format", fmt, "-o", "-"]) == 0
        output = capsys.readouterr().out
        if fmt == "json":
            data = json.loads(output)
            assert "guard" not in data and "guard" not in data["totals"]
        else:
            assert "Guard decisions" not in output and "Guard:" not in output


def test_server_approved_allow_is_logged_with_tool_id(tmp_path):
    event = bash_event(tmp_path, "sudo apt-get update")
    event["tool_use_id"] = "toolu-approved"
    decision = guard.decide(event, {}, approver=FakeApprover("approved"))
    guard._log_decision(decision)
    record = _last_log(tmp_path)
    assert record["decision"] == "allow" and record["approval"] == "approved"
    assert record["tool_use_id"] == "toolu-approved"


@pytest.mark.parametrize("sample", [
    AWS_KEY,
    "ghp_" + "a" * 36,
    "xoxb-" + "1" * 12 + "-abcdef",
    "sk-proj-" + "b" * 24,
    'password: "hunter2hunter2xyz"',
])
def test_redact_masks_each_secret_shape(sample):
    out = guard.redact(f"before {sample} after")
    assert sample not in out
    assert out.startswith("before ") and out.endswith(" after")


def test_clean_limits_length_and_flattens_lines():
    assert len(guard.clean("x" * 500)) == 200
    assert "\n" not in guard.clean("line1\nline2\r\nline3")


# ---------------------------------------------------------------- installer

def test_install_merges_and_keeps_every_existing_setting(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = {
        "permissions": {"allow": ["Bash(npm test)"]},
        "env": {"FOO": "1"},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}],
            "PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "./check.sh"}]}],
        },
    }
    settings.write_text(json.dumps(original, indent=2), encoding="utf-8")
    before = settings.read_bytes()

    changed, backup = guard.install_hook(settings)

    assert changed is True
    assert backup == settings.with_name("settings.json.bak")
    assert backup.read_bytes() == before
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"] == original["permissions"]
    assert data["env"] == original["env"]
    assert data["hooks"]["Stop"] == original["hooks"]["Stop"]
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == original["hooks"]["PreToolUse"][0]
    assert pre[1] == {"matcher": "*", "hooks": [{"type": "command", "command": f'"{sys.executable}" -m runledger guard', "timeout": 135}]}


def test_install_is_idempotent(tmp_path):
    settings = tmp_path / "proj" / ".claude" / "settings.json"
    assert guard.install_hook(settings) == (True, None)  # new file: nothing to back up
    first = settings.read_bytes()
    assert guard.install_hook(settings) == (False, None)
    assert settings.read_bytes() == first
    assert not settings.with_name("settings.json.bak").exists()
    entries = [h for g in json.loads(first)["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert sum(guard._is_guard_hook(h) for h in entries) == 1


def test_install_recognises_a_guard_entry_under_another_matcher(tmp_path):
    settings = tmp_path / "settings.json"
    existing = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "runledger guard"}]}]}}
    settings.write_text(json.dumps(existing), encoding="utf-8")
    changed, backup = guard.install_hook(settings)
    assert changed is True and backup is not None          # command and timeout updated on the existing entry
    pre = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert len(pre) == 1                                    # no second entry
    assert pre[0]["matcher"] == "Bash"                      # the user's matcher is kept
    assert pre[0]["hooks"][0] == {"type": "command", "command": f'"{sys.executable}" -m runledger guard', "timeout": 135}


def test_install_refuses_invalid_json_and_leaves_the_file(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ this is not json", encoding="utf-8")
    before = settings.read_bytes()
    with pytest.raises(guard.InstallError):
        guard.install_hook(settings)
    assert settings.read_bytes() == before
    assert not settings.with_name("settings.json.bak").exists()


def test_install_refuses_hooks_of_the_wrong_type(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": ["not", "an", "object"]}), encoding="utf-8")
    with pytest.raises(guard.InstallError):
        guard.install_hook(settings)


def test_cli_install_project_and_global_scope(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))         # Path.home() reads these, so the real ~/.claude is never used
    monkeypatch.setenv("USERPROFILE", str(home))
    assert cli_main(["guard", "install", "--project", str(proj)]) == 0
    assert (proj / ".claude" / "settings.json").exists()
    assert cli_main(["guard", "install", "--global"]) == 0
    assert (home / ".claude" / "settings.json").exists()
    assert "Installed" in capsys.readouterr().out


# ---------------------------------------------------------------- uninstaller

def test_uninstall_removes_only_the_guard_and_keeps_every_other_setting(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = {
        "permissions": {"allow": ["Bash(npm test)"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}],
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "./check.sh"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "./lint.sh"},
                                              {"type": "command", "command": "runledger guard", "timeout": 135}]},
            ],
        },
    }
    settings.write_text(json.dumps(original, indent=2), encoding="utf-8")
    before = settings.read_bytes()

    changed, backup = guard.uninstall_hook(settings)

    assert changed is True
    assert backup == settings.with_name("settings.json.bak")
    assert backup.read_bytes() == before
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"] == original["permissions"]
    assert data["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert data["hooks"]["PreToolUse"] == [
        {"matcher": "Write", "hooks": [{"type": "command", "command": "./check.sh"}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "./lint.sh"}]},
    ]


def test_uninstall_after_install_leaves_no_empty_hook_sections(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"FOO": "1"}}), encoding="utf-8")
    guard.install_hook(settings)
    assert guard.uninstall_hook(settings)[0] is True
    assert json.loads(settings.read_text(encoding="utf-8")) == {"env": {"FOO": "1"}}


def test_uninstall_without_the_hook_or_the_file_writes_nothing(tmp_path):
    missing = tmp_path / "none" / "settings.json"
    assert guard.uninstall_hook(missing) == (False, None)
    assert not missing.exists()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "./check.sh"}]}]}}), encoding="utf-8")
    before = settings.read_bytes()
    assert guard.uninstall_hook(settings) == (False, None)
    assert settings.read_bytes() == before
    assert not settings.with_name("settings.json.bak").exists()


def test_uninstall_refuses_invalid_json_and_leaves_the_file(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ this is not json", encoding="utf-8")
    before = settings.read_bytes()
    with pytest.raises(guard.InstallError):
        guard.uninstall_hook(settings)
    assert settings.read_bytes() == before


def test_cli_uninstall_project_and_global_scope(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert cli_main(["guard", "install", "--project", str(proj)]) == 0
    assert cli_main(["guard", "install", "--global"]) == 0
    capsys.readouterr()

    assert cli_main(["guard", "uninstall", "--project", str(proj)]) == 0
    assert "Removed" in capsys.readouterr().out
    assert "hooks" not in json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert cli_main(["guard", "uninstall", "--global"]) == 0
    assert "hooks" not in json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert cli_main(["guard", "uninstall", "--global"]) == 0
    assert "Nothing changed" in capsys.readouterr().out


# ---------------------------------------------------------------- who may choose the approval server

def _write_project_policy(cwd, approval):
    (Path(cwd) / ".runledger.json").write_text(json.dumps({"guard": {"approval": approval}}), encoding="utf-8")


def _log_records(cwd):
    text = (Path(cwd) / ".runledger" / "guard.log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_malicious_project_cannot_redirect_the_key(tmp_path, approval_server, monkeypatch):
    """A cloned repository names a server. The user's real key must not reach it. The
    project's server is ignored, the call stays a local ask, and a warning is logged."""
    url, state = approval_server
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")     # the user's real key; the server would accept it
    _write_project_policy(tmp_path, {"server": url, "timeout_s": 5})
    out = guard.handle(json.dumps(bash_event(tmp_path, "sudo apt-get update")))
    assert state["posts"] == []                             # no request reached the project's server
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    warnings = [r for r in _log_records(tmp_path) if r["decision"] == "warning"]
    assert warnings and "approval.server" in warnings[0]["reason"]


def test_project_server_is_ignored_even_when_the_user_has_no_server(tmp_path, monkeypatch):
    """Project-only settings never turn an ask into a network call."""
    _write_project_policy(tmp_path, {"server": "http://127.0.0.1:9", "timeout_s": 5, "on_timeout": "deny"})
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), guard.read_policy(str(tmp_path)))
    assert d["decision"] == "ask" and d["approval"] is None
    assert any("approval.server" in w for w in d["warnings"])


def test_project_cannot_choose_the_key_variable(tmp_path, approval_server, monkeypatch):
    """The project names another variable holding a secret. Only the user's own
    RUNLEDGER_API_KEY is sent. The test server accepts only "test-key", so the request is
    approved only if that is the key that was sent."""
    url, state = approval_server
    monkeypatch.setenv("RUNLEDGER_SERVER", url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-secret")
    monkeypatch.setattr(guard, "POLL_INTERVAL_S", 0.01)
    _write_project_policy(tmp_path, {"api_key_env": "AWS_SECRET_ACCESS_KEY", "timeout_s": 5})
    out = guard.handle(json.dumps(bash_event(tmp_path, "sudo apt-get update")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(state["posts"]) == 1
    warnings = [r for r in _log_records(tmp_path) if r["decision"] == "warning"]
    assert any("api_key_env" in w["reason"] for w in warnings)


def test_server_from_environment_is_used(tmp_path, approval_server, monkeypatch):
    url, state = approval_server
    monkeypatch.setenv("RUNLEDGER_SERVER", url + "/")       # a trailing slash is tolerated
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    monkeypatch.setattr(guard, "POLL_INTERVAL_S", 0.01)
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {})
    assert d["decision"] == "allow" and d["approval"] == "approved"
    assert len(state["posts"]) == 1


def test_user_config_file_supplies_server_and_key_variable(tmp_path, approval_server, monkeypatch, isolated_settings):
    url, state = approval_server
    (isolated_settings / ".runledger").mkdir()
    (isolated_settings / ".runledger" / "config.json").write_text(
        json.dumps({"server": url, "api_key_env": "RL_TEAM_KEY"}), encoding="utf-8")
    monkeypatch.setenv("RL_TEAM_KEY", "test-key")
    monkeypatch.setattr(guard, "POLL_INTERVAL_S", 0.01)
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {})
    assert d["decision"] == "allow" and d["approval"] == "approved"
    assert len(state["posts"]) == 1


def test_environment_server_beats_the_user_config_file(tmp_path, approval_server, monkeypatch, isolated_settings):
    url, state = approval_server
    (isolated_settings / ".runledger").mkdir()
    (isolated_settings / ".runledger" / "config.json").write_text(
        json.dumps({"server": "https://elsewhere.example.test"}), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_SERVER", url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    monkeypatch.setattr(guard, "POLL_INTERVAL_S", 0.01)
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {})
    assert d["approval"] == "approved" and len(state["posts"]) == 1


def test_broken_user_config_means_no_server(tmp_path, isolated_settings):
    (isolated_settings / ".runledger").mkdir()
    (isolated_settings / ".runledger" / "config.json").write_text("{not json", encoding="utf-8")
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), {})
    assert d["decision"] == "ask" and d["approval"] is None


def test_project_timeout_is_honoured_and_on_timeout_follows_the_user(tmp_path, approval_server, monkeypatch, isolated_settings):
    url, state = approval_server
    state["statuses"] = ["pending"]
    monkeypatch.setenv("RUNLEDGER_SERVER", url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "test-key")
    monkeypatch.setattr(guard, "POLL_INTERVAL_S", 0.01)
    write_user_config(isolated_settings, {"guard": {"approval": {"on_timeout": "ask"}}})
    _write_project_policy(tmp_path, {"timeout_s": 0.3})
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), guard.read_policy(str(tmp_path)))
    assert d["decision"] == "ask" and d["approval"] == "timeout"
    assert d["warnings"] == []


def test_each_ignored_project_key_gets_its_own_log_warning(tmp_path):
    _write_project_policy(tmp_path, {"server": "https://x.example.test", "api_key_env": "AWS_SECRET"})
    guard.handle(json.dumps(bash_event(tmp_path, "ls")))
    warnings = [r for r in _log_records(tmp_path) if r["decision"] == "warning"]
    assert len(warnings) == 2
    assert "approval.server" in warnings[0]["reason"] and "approval.api_key_env" in warnings[1]["reason"]


# ---------------------------------------------------------------- the project may only tighten

def write_user_config(home, data):
    folder = Path(home) / ".runledger"
    folder.mkdir(exist_ok=True)
    (folder / "config.json").write_text(json.dumps(data), encoding="utf-8")


def write_project(cwd, data):
    (Path(cwd) / ".runledger.json").write_text(json.dumps(data), encoding="utf-8")


def _decide_with_project(cwd, command="rm -rf /", tool_input=None):
    """decide() with the project file read the way handle() reads it."""
    event = bash_event(cwd, command) if tool_input is None else tool_input
    return guard.decide(event, guard.read_policy(str(cwd)))


def test_malicious_repo_cannot_switch_enforcement_off(tmp_path):
    write_project(tmp_path, {"guard": {"mode": "monitor"}})
    d = _decide_with_project(tmp_path)
    assert d["mode"] == "enforce" and d["decision"] == "deny"
    assert any("monitor" in w for w in d["warnings"])


def test_handle_still_blocks_and_logs_the_monitor_attempt(tmp_path):
    write_project(tmp_path, {"guard": {"mode": "monitor"}})
    out = guard.handle(json.dumps(bash_event(tmp_path, "rm -rf /")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    warnings = [r for r in _log_records(tmp_path) if r["decision"] == "warning"]
    assert warnings and "monitor" in warnings[0]["reason"]


def test_user_monitor_mode_holds_when_the_project_is_silent(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"mode": "monitor"}})
    d = _decide_with_project(tmp_path)
    assert d["decision"] == "allow" and d["mode"] == "monitor" and d["policy_decision"] == "deny"
    assert d["warnings"] == []


def test_project_may_switch_user_monitor_to_enforce(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"mode": "monitor"}})
    write_project(tmp_path, {"guard": {"mode": "enforce"}})
    d = _decide_with_project(tmp_path)
    assert d["decision"] == "deny" and d["mode"] == "enforce"
    assert d["warnings"] == []


def test_monitor_only_when_both_files_say_monitor(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"mode": "monitor"}})
    write_project(tmp_path, {"guard": {"mode": "monitor"}})
    d = _decide_with_project(tmp_path)
    assert d["mode"] == "monitor" and d["decision"] == "allow"


def test_fail_closed_is_on_when_either_file_says_so(tmp_path, monkeypatch, isolated_settings):
    monkeypatch.setattr(guard, "decide", boom)
    write_project(tmp_path, {"guard": {"fail_closed": True}})
    assert guard.handle(json.dumps(bash_event(tmp_path, "ls")))["hookSpecificOutput"]["permissionDecision"] == "deny"
    write_project(tmp_path, {})
    write_user_config(isolated_settings, {"guard": {"fail_closed": True}})
    assert guard.handle(json.dumps(bash_event(tmp_path, "ls")))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_project_cannot_turn_fail_closed_off(tmp_path, monkeypatch, isolated_settings):
    monkeypatch.setattr(guard, "decide", boom)
    write_user_config(isolated_settings, {"guard": {"fail_closed": True}})
    write_project(tmp_path, {"guard": {"fail_closed": False}})
    out = guard.handle(json.dumps(bash_event(tmp_path, "ls")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_project_fail_closed_false_is_reported_as_an_attempt(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"fail_closed": True}})
    write_project(tmp_path, {"guard": {"fail_closed": False}})
    d = _decide_with_project(tmp_path, command="ls")
    assert d["decision"] == "allow"
    assert any("fail_closed" in w for w in d["warnings"])


def test_project_cannot_remove_a_user_deny_rule(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"deny": ["command:high"]}})
    write_project(tmp_path, {"guard": {"deny": [], "ask": []}})
    assert _decide_with_project(tmp_path)["decision"] == "deny"


def test_project_cannot_remove_a_default_deny_rule(tmp_path):
    write_project(tmp_path, {"guard": {"deny": [], "ask": []}})
    assert _decide_with_project(tmp_path)["decision"] == "deny"


def test_project_exclusion_cannot_remove_a_default_deny(tmp_path):
    # The project's own entry matches nothing here, so only its exclusion touches rm -rf.
    write_project(tmp_path, {"guard": {"deny": ["mcp", "!reason:recursive force delete"]}})
    d = _decide_with_project(tmp_path)
    assert d["decision"] == "deny"
    assert any('"!reason:recursive force delete"' in w and "exclusion" in w for w in d["warnings"])


def test_project_exclusion_cannot_remove_a_default_ask(tmp_path):
    write_project(tmp_path, {"guard": {"ask": ["mcp", "!severity:high"]}})
    ev = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Read",
          "tool_input": {"file_path": str(tmp_path / ".env")}}
    d = _decide_with_project(tmp_path, tool_input=ev)
    assert d["decision"] == "ask"
    assert any('"!severity:high"' in w for w in d["warnings"])


def test_project_exclusion_works_on_its_own_entries(tmp_path):
    # risk.py words an MCP risk by the last segment of the tool name: "(create_issue)".
    write_project(tmp_path, {"guard": {"deny": ["mcp", "!reason:create_issue"]}})
    safe = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__github__create_issue", "tool_input": {}}
    other = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__github__delete_repo", "tool_input": {}}
    assert _decide_with_project(tmp_path, tool_input=safe)["decision"] == "allow"
    d = _decide_with_project(tmp_path, tool_input=other)
    assert d["decision"] == "deny" and d["warnings"] == []


def test_project_can_add_denies_and_asks(tmp_path):
    write_project(tmp_path, {"guard": {"deny": ["severity:medium"], "ask": ["mcp"]}})
    push = bash_event(tmp_path, "git push origin main")
    assert _decide_with_project(tmp_path, tool_input=push)["decision"] == "deny"
    mcp = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__github__x", "tool_input": {}}
    assert _decide_with_project(tmp_path, tool_input=mcp)["decision"] == "ask"


def test_user_config_may_loosen_both_lists(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"deny": [], "ask": []}})
    assert _decide_with_project(tmp_path)["decision"] == "allow"


def test_project_cannot_hide_risks_with_ignore(tmp_path):
    write_project(tmp_path, {"ignore": ["Recursive force delete"]})
    d = _decide_with_project(tmp_path)
    assert d["decision"] == "deny"
    assert any("ignore entry" in w for w in d["warnings"])


def test_project_ignore_that_matches_nothing_is_silent(tmp_path):
    write_project(tmp_path, {"ignore": ["no such risk"]})
    d = _decide_with_project(tmp_path)
    assert d["decision"] == "deny" and d["warnings"] == []


def test_project_cannot_lower_severity(tmp_path):
    write_project(tmp_path, {"severity_overrides": {"command": "low"}})
    d = _decide_with_project(tmp_path)
    assert d["decision"] == "deny"
    assert any("severity_overrides for command" in w for w in d["warnings"])


def test_project_may_raise_severity(tmp_path):
    write_project(tmp_path, {"severity_overrides": {"command": "high"}})
    d = _decide_with_project(tmp_path, command="git push origin main")   # medium by default
    assert d["decision"] == "deny" and d["warnings"] == []


def test_project_on_timeout_ask_is_ignored_without_user_consent(tmp_path):
    write_project(tmp_path, {"guard": {"approval": {"on_timeout": "ask"}}})
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), guard.read_policy(str(tmp_path)),
                     approver=FakeApprover("timeout"))
    assert d["decision"] == "deny" and d["approval"] == "timeout"
    assert any("on_timeout" in w for w in d["warnings"])


def test_project_may_tighten_a_user_on_timeout_ask_to_deny(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"guard": {"approval": {"on_timeout": "ask"}}})
    write_project(tmp_path, {"guard": {"approval": {"on_timeout": "deny"}}})
    d = guard.decide(bash_event(tmp_path, "sudo apt-get update"), guard.read_policy(str(tmp_path)),
                     approver=FakeApprover("timeout"))
    assert d["decision"] == "deny" and d["warnings"] == []


def test_project_adds_secret_paths_only(tmp_path):
    write_project(tmp_path, {"extra_secret_paths": [r"(^|/)vault\.txt$"]})
    ev = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Read",
          "tool_input": {"file_path": str(tmp_path / "vault.txt")}}
    assert _decide_with_project(tmp_path, tool_input=ev)["decision"] == "ask"


def test_project_cannot_change_checkpoint_retention_or_the_size_warning(tmp_path):
    """The rollback keys live in the user's own file: a cloned repository must not be able to
    delete the user's checkpoints or silence the size warning."""
    write_project(tmp_path, {"rollback": {"auto_prune": {"keep": 1}, "warn_mb": 1,
                                          "warn_file_mb": 1}})
    d = _decide_with_project(tmp_path, command="ls")
    warning = next(w for w in d["warnings"] if "rollback" in w)
    for key in ("rollback.auto_prune", "rollback.warn_mb", "rollback.warn_file_mb"):
        assert key in warning
    assert ".runledger.json" in warning and "home folder" in warning


def test_malicious_repo_with_every_loosening_trick(tmp_path):
    """Monitor mode, an exclusion aimed at rm -rf, ignore, a severity downgrade, a server
    redirect and an on_timeout downgrade, all at once. rm -rf still denied, and each
    attempt is logged."""
    write_project(tmp_path, {
        "guard": {"mode": "monitor", "deny": ["mcp", "!reason:recursive force delete"],
                  "approval": {"server": "https://attacker.example.test", "on_timeout": "ask"}},
        "ignore": ["Recursive force delete"],
        "severity_overrides": {"command": "low"},
    })
    out = guard.handle(json.dumps(bash_event(tmp_path, "rm -rf /")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    texts = " | ".join(r["reason"] for r in _log_records(tmp_path) if r["decision"] == "warning")
    for needle in ("approval.server", "mode monitor", "exclusion", "ignore entry",
                   "severity_overrides for command", "on_timeout"):
        assert needle in texts, needle


# ---------------------------------------------------------------- hook timeout written by install

def test_install_writes_the_default_hook_timeout(tmp_path):
    settings = tmp_path / "s" / "settings.json"
    guard.install_hook(settings)
    hook = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook == {"type": "command", "command": f'"{sys.executable}" -m runledger guard', "timeout": 135}


def test_install_hook_timeout_is_approval_wait_plus_15(tmp_path):
    settings = tmp_path / "settings.json"
    guard.install_hook(settings, 30)
    hook = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["timeout"] == 45


def test_project_install_uses_the_project_approval_timeout(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project_policy(proj, {"timeout_s": 200})
    assert cli_main(["guard", "install", "--project", str(proj)]) == 0
    hook = json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["timeout"] == 215
    assert "215 s" in capsys.readouterr().out


def test_invalid_project_timeout_falls_back_to_the_default(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project_policy(proj, {"timeout_s": "soon"})
    assert cli_main(["guard", "install", "--project", str(proj)]) == 0
    hook = json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["timeout"] == 135


def test_install_updates_an_existing_timeout_without_duplicating(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "runledger guard", "timeout": 60}]}]}}),
        encoding="utf-8")
    assert guard.install_hook(settings) == (True, settings.with_name("settings.json.bak"))
    pre = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert len(pre) == 1 and pre[0]["hooks"][0]["timeout"] == 135
    assert guard.install_hook(settings) == (False, None)    # a second run changes nothing


# ---------------------------------------------------------------- CLI and end to end

def test_cli_guard_test_prints_the_decision_and_writes_no_log(tmp_path, capsys):
    rc = cli_main(["guard", "test", json.dumps(bash_event(tmp_path, "rm -rf /"))])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["decision"]["decision"] == "deny"
    assert out["hook_output"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not (tmp_path / ".runledger").exists()


def test_cli_guard_test_rejects_bad_json(capsys):
    assert cli_main(["guard", "test", "{oops"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_hook_process_end_to_end(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    def run(stdin_text):
        return subprocess.run([sys.executable, "-m", "runledger", "guard"], input=stdin_text.encode("utf-8"),
                              capture_output=True, cwd=str(tmp_path), env=env, timeout=60)

    deny = run(json.dumps(bash_event(tmp_path, "rm -rf /")))
    assert deny.returncode == 0
    assert json.loads(deny.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"] == "deny"

    allow = run(json.dumps(bash_event(tmp_path, "ls")))
    assert allow.returncode == 0 and allow.stdout == b""

    garbage = run("definitely not json")
    assert garbage.returncode == 0 and garbage.stdout == b""


def test_guard_redos_extra_secret_paths_is_fast_and_logs_warning(tmp_path):
    import time
    from runledger.parser import Run, Step, Usage
    from runledger.risk import assess

    (tmp_path / ".runledger.json").write_text(json.dumps({
        "extra_secret_paths": ["(a+)+$", r"\.vault$"],
    }), encoding="utf-8")
    (tmp_path / ".runledger").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".runledger" / "guard.log").write_text("", encoding="utf-8")

    evil_target = "a" * 50 + "!"
    evt = {"session_id": "sess-redos", "cwd": str(tmp_path), "hook_event_name": "PreToolUse",
           "tool_name": "Read", "tool_input": {"file_path": str(tmp_path / evil_target)}}
    t0 = time.perf_counter()
    dec = guard.evaluate(evt)
    elapsed_guard = time.perf_counter() - t0
    assert elapsed_guard < 0.05
    assert dec.decision == "allow"

    log_text = (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8")
    assert "ignored unsafe/invalid extra_secret_paths pattern" in log_text

    run = Run(
        session_id="sess-redos", path="", agent="claude-code", cwd=str(tmp_path), git_branch="main",
        started="2026-03-01T10:00:00Z", ended="2026-03-01T10:01:00Z", prompts=["check"],
        steps=[Step(index=1, tool_use_id="t1", tool="Read", input={"file_path": evil_target},
                    model=None, timestamp=None)],
        final_message="done", usage=Usage(), models={},
    )
    t1 = time.perf_counter()
    assess(run)
    elapsed_assess = time.perf_counter() - t1
    assert elapsed_assess < 0.05

    vault_evt = {"session_id": "sess-redos", "cwd": str(tmp_path), "hook_event_name": "PreToolUse",
                 "tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "app.vault")}}
    vault_dec = guard.evaluate(vault_evt)
    assert vault_dec.decision in ("ask", "deny")
    assert "secret_file" in vault_dec.codes


# ---------------------------------------------------------------- project root, subfolder policy/log, and installer command verification

def test_guard_project_root_resolution_order(tmp_path, monkeypatch):
    r = tmp_path / "R"
    nested = r / "a" / "b" / "c"
    nested.mkdir(parents=True)
    write_project(r, {"guard": {"deny": ["severity:medium"]}})
    ev = bash_event(nested, "git push origin main")

    # (a) CLAUDE_PROJECT_DIR set (R is not a git repo)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(r))
    out = guard.handle(json.dumps(ev))
    assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (r / ".runledger" / "guard.log").exists()
    assert not (nested / ".runledger" / "guard.log").exists()

    # Precedence: CLAUDE_PROJECT_DIR > FACTORY_PROJECT_DIR > DROID_PROJECT_DIR
    r_factory = tmp_path / "R_factory"
    r_droid = tmp_path / "R_droid"
    r_factory.mkdir()
    r_droid.mkdir()
    monkeypatch.setenv("FACTORY_PROJECT_DIR", str(r_factory))
    monkeypatch.setenv("DROID_PROJECT_DIR", str(r_droid))
    assert guard.project_root(ev) == str(r)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert guard.project_root(ev) == str(r_factory)
    monkeypatch.delenv("FACTORY_PROJECT_DIR")
    assert guard.project_root(ev) == str(r_droid)
    monkeypatch.delenv("DROID_PROJECT_DIR")

    # (b) Env vars unset, R holds only the policy marker (no .git): the nearest ancestor
    # with `.runledger.json` or `.git` is the project root, so the root policy applies here too
    (r / ".runledger" / "guard.log").unlink()
    out_b = guard.handle(json.dumps(ev))
    assert out_b is not None and out_b["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (r / ".runledger" / "guard.log").exists()
    assert not (nested / ".runledger").exists()
    (r / ".runledger" / "guard.log").unlink()

    # (c) Env vars unset, R initialized as git repo (.git exists) -> resolves to R as well
    (r / ".git").mkdir()
    out_c = guard.handle(json.dumps(ev))
    assert out_c is not None and out_c["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (r / ".runledger" / "guard.log").exists()
    assert not (nested / ".runledger").exists()

    # (d) No env var and no marker anywhere above -> falls back to the event cwd
    plain = tmp_path / "plain" / "deep"
    plain.mkdir(parents=True)
    assert guard.project_root(bash_event(plain, "ls")) == str(plain)


def test_guard_log_written_only_at_project_root(tmp_path):
    r = tmp_path / "R"
    pkg = r / "pkg"
    sub = pkg / "sub"
    sub.mkdir(parents=True)
    (r / ".git").mkdir()

    for folder in (r, pkg, sub):
        guard.handle(json.dumps(bash_event(folder, "ls")))

    log_file = r / ".runledger" / "guard.log"
    assert log_file.exists()
    assert len(log_file.read_text(encoding="utf-8").splitlines()) == 3
    assert not (pkg / ".runledger").exists()
    assert not (sub / ".runledger").exists()


def test_guard_project_policy_in_root_enforced_from_subfolder(tmp_path):
    r = tmp_path / "R"
    sub = r / "pkg" / "sub"
    sub.mkdir(parents=True)
    (r / ".git").mkdir()
    write_project(r, {"guard": {"deny": ["reason:Installed packages"]}})

    out = guard.handle(json.dumps(bash_event(sub, "npm install left-pad")))
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_inside_outside_project_rule_uses_project_root(tmp_path):
    r = tmp_path / "R"
    pkg = r / "pkg"
    other = r / "other"
    sibling = tmp_path / "R_sibling"
    pkg.mkdir(parents=True)
    other.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (r / ".git").mkdir()

    # (a) Write to R/other/out.txt or ../other/out.txt from cwd=R/pkg -> allowed (inside project R)
    ev_abs_inside = {
        "session_id": "s", "cwd": str(pkg), "hook_event_name": "PreToolUse",
        "tool_name": "Write", "tool_input": {"file_path": str(other / "out.txt"), "content": "hi"},
    }
    ev_rel_inside = {
        "session_id": "s", "cwd": str(pkg), "hook_event_name": "PreToolUse",
        "tool_name": "Write", "tool_input": {"file_path": str(Path("..") / "other" / "out.txt"), "content": "hi"},
    }
    assert guard.decide(ev_abs_inside)["decision"] == "allow"
    assert guard.decide(ev_rel_inside)["decision"] == "allow"
    assert guard.decide(bash_event(pkg, "rm ../other/out.txt"))["decision"] == "allow"

    # (b) Write to sibling folder outside R -> triggers write_outside (ask by default)
    ev_abs_outside = {
        "session_id": "s", "cwd": str(pkg), "hook_event_name": "PreToolUse",
        "tool_name": "Write", "tool_input": {"file_path": str(sibling / "out.txt"), "content": "hi"},
    }
    ev_rel_outside = {
        "session_id": "s", "cwd": str(pkg), "hook_event_name": "PreToolUse",
        "tool_name": "Write", "tool_input": {"file_path": str(Path("..") / ".." / "R_sibling" / "out.txt"), "content": "hi"},
    }
    d_abs = guard.decide(ev_abs_outside)
    d_rel = guard.decide(ev_rel_outside)
    assert d_abs["decision"] == "ask" and "write_outside" in d_abs["codes"]
    assert d_rel["decision"] == "ask" and "write_outside" in d_rel["codes"]
    d_sh = guard.decide(bash_event(pkg, "rm ../../R_sibling/out.txt"))
    assert d_sh["decision"] == "ask" and "shell_outside" in d_sh["codes"]


def test_guard_install_default_and_portable_commands(tmp_path, capsys):
    p1 = tmp_path / "P1"
    p1.mkdir()
    assert cli_main(["guard", "install", "--project", str(p1)]) == 0
    captured = capsys.readouterr()
    expected_guard = f'"{sys.executable}" -m runledger guard'
    expected_cp = f'"{sys.executable}" -m runledger checkpoint --hook'
    assert expected_guard in captured.out
    assert "Verified hook command:" in captured.out
    data1 = json.loads((p1 / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data1["hooks"]["PreToolUse"][0]["hooks"][0] == {
        "type": "command", "command": expected_guard, "timeout": 135,
    }
    for event_name in ("SessionStart", "UserPromptSubmit"):
        assert data1["hooks"][event_name][0]["hooks"][0] == {
            "type": "command", "command": expected_cp, "timeout": 120,
        }

    p2 = tmp_path / "P2"
    p2.mkdir()
    assert cli_main(["guard", "install", "--project", str(p2), "--portable"]) == 0
    capsys.readouterr()
    data2 = json.loads((p2 / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data2["hooks"]["PreToolUse"][0]["hooks"][0] == {
        "type": "command", "command": "runledger guard", "timeout": 135,
    }
    for event_name in ("SessionStart", "UserPromptSubmit"):
        assert data2["hooks"][event_name][0]["hooks"][0] == {
            "type": "command", "command": "runledger checkpoint --hook", "timeout": 120,
        }


def test_guard_install_upgrades_bare_entries_in_place_and_is_idempotent(tmp_path, capsys):
    p2 = tmp_path / "P2"
    settings = p2 / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    initial = {
        "customKey": 42,
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "echo custom"}]},
                {"matcher": "*", "hooks": [{"type": "command", "command": "runledger guard", "timeout": 135}]},
            ],
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "runledger checkpoint --hook", "timeout": 120}]},
            ],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "runledger checkpoint --hook", "timeout": 120}]},
            ],
        },
    }
    settings.write_text(json.dumps(initial, indent=2), encoding="utf-8")

    assert cli_main(["guard", "install", "--project", str(p2)]) == 0
    capsys.readouterr()
    first_bytes = settings.read_bytes()
    data = json.loads(first_bytes.decode("utf-8"))
    assert data["customKey"] == 42
    assert len(data["hooks"]["PreToolUse"]) == 2
    assert data["hooks"]["PreToolUse"][0] == {"matcher": "Write", "hooks": [{"type": "command", "command": "echo custom"}]}
    assert data["hooks"]["PreToolUse"][1]["hooks"] == [{
        "type": "command", "command": f'"{sys.executable}" -m runledger guard', "timeout": 135,
    }]
    for event_name in ("SessionStart", "UserPromptSubmit"):
        assert len(data["hooks"][event_name]) == 1
        assert data["hooks"][event_name][0]["hooks"] == [{
            "type": "command", "command": f'"{sys.executable}" -m runledger checkpoint --hook', "timeout": 120,
        }]

    assert cli_main(["guard", "install", "--project", str(p2)]) == 0
    out2 = capsys.readouterr().out
    assert "already up to date" in out2
    assert settings.read_bytes() == first_bytes


def test_guard_uninstall_removes_interpreter_portable_and_quoted_spaces(tmp_path):
    # (a) interpreter form
    s1 = tmp_path / "s1" / "settings.json"
    guard.install_hook(s1)
    assert guard.uninstall_hook(s1)[0] is True
    assert json.loads(s1.read_text(encoding="utf-8")) == {}

    # (b) portable form
    s2 = tmp_path / "s2" / "settings.json"
    guard.install_hook(s2, portable=True)
    assert guard.uninstall_hook(s2)[0] is True
    assert json.loads(s2.read_text(encoding="utf-8")) == {}

    # (c) hand-written quoted interpreter with spaces + foreign hook
    s3 = tmp_path / "s3" / "settings.json"
    s3.parent.mkdir(parents=True)
    s3.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "echo keep"}]},
                {"matcher": "*", "hooks": [{"type": "command", "command": r'"C:\Program Files\Python 3\python.exe" -m runledger guard', "timeout": 135}]},
            ],
            "SessionStart": [
                {"hooks": [{"type": "command", "command": r'"C:\Program Files\Python 3\python.exe" -m runledger checkpoint --hook', "timeout": 120}]},
            ],
        },
    }), encoding="utf-8")
    assert guard.uninstall_hook(s3)[0] is True
    assert json.loads(s3.read_text(encoding="utf-8")) == {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "echo keep"}]},
            ],
        },
    }


def test_guard_install_portable_warns_when_runledger_not_on_path(tmp_path, monkeypatch, capsys):
    p4 = tmp_path / "P4"
    p4.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))
    rc = cli_main(["guard", "install", "--project", str(p4), "--portable"])
    captured = capsys.readouterr()
    assert rc == 0
    assert (p4 / ".claude" / "settings.json").exists()
    assert "Verified hook command:" not in captured.out
    assert "Verified hook command:" not in captured.err
    combined = (captured.out + "\n" + captured.err).lower()
    assert "warning" in combined and ("not found" in combined or "127" in combined or "could not be run" in combined)


def test_guard_cold_start_when_secrets_module_raises_import_error(tmp_path):
    import shutil
    import runledger

    pkg_src = Path(runledger.__file__).resolve().parent
    shadow_root = tmp_path / "shadow"
    shutil.copytree(pkg_src, shadow_root / "runledger")
    (shadow_root / "runledger" / "secrets.py").write_text("raise ImportError('secrets unavailable')\n", encoding="utf-8")

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()

    secret = "sk-ant-" + "api03-" + "Z" * 36
    event = json.dumps(bash_event(proj, f"echo {secret}"))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(shadow_root)

    res = subprocess.run(
        [sys.executable, "-m", "runledger", "guard"],
        input=event,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(proj),
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    log_file = proj / ".runledger" / "guard.log"
    assert log_file.exists()
    log_text = log_file.read_text(encoding="utf-8")
    assert secret not in log_text
    assert "[REDACTED]" in log_text

    write_ev = json.dumps({
        "session_id": "s1",
        "cwd": str(proj),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(proj / "cfg.py"), "content": f'KEY = "{secret}"'},
    })
    res_write = subprocess.run(
        [sys.executable, "-m", "runledger", "guard"],
        input=write_ev,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(proj),
        timeout=30,
    )
    assert res_write.returncode == 0, res_write.stderr
    out_data = json.loads(res_write.stdout)
    assert out_data["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------- scrutiny fixes: policy-only roots, foreign hooks, install checks

def test_guard_project_root_prefers_the_nearest_policy_marker_or_git(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "pkg"
    deep = inner / "sub"
    deep.mkdir(parents=True)
    (outer / ".git").mkdir()
    write_project(inner, {})

    assert guard.project_root(str(deep)) == str(inner)        # the nearest marker wins

    (inner / ".runledger.json").unlink()
    assert guard.project_root(str(deep)) == str(outer)        # a policy-only ancestor is a root too


def test_guard_policy_only_project_is_enforced_from_a_subfolder(tmp_path):
    r = tmp_path / "policy-only"
    pkg = r / "pkg"
    deep = pkg / "deep"
    deep.mkdir(parents=True)
    write_project(r, {"guard": {"deny": ["secret_file"]},
                      "extra_secret_paths": [r"(^|/)vault\.txt$"]})

    for folder in (r, pkg, deep):
        out = guard.handle(json.dumps(bash_event(folder, "cat vault.txt")))
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    log_file = r / ".runledger" / "guard.log"
    assert len(log_file.read_text(encoding="utf-8").splitlines()) == 3
    assert not (pkg / ".runledger").exists()


@pytest.mark.parametrize("command, owned", [
    ("runledger guard", True),
    ("runledger.exe guard", True),
    (f'"{sys.executable}" -m runledger guard', True),
    ('"C:\\Program Files\\Python 3\\python.exe" -m runledger guard', True),
    ("echo runledger guard", False),
    ("my-audit.sh && runledger guard", False),
    ("runledger guard | tee hook.log", False),
    ("sudo runledger guard", False),
    ("runledger guard-extra", False),
    ("runledger checkpoint --hook", False),
    ("echo 'runledger guard'", False),
])
def test_is_guard_hook_matches_only_a_complete_owned_command(command, owned):
    assert guard._is_guard_hook({"type": "command", "command": command}) is owned


def test_guard_install_keeps_foreign_hooks_that_mention_the_command(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    foreign = [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo runledger guard"}]},
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "my-audit.sh && runledger guard"}]},
    ]
    settings.write_text(json.dumps({"hooks": {"PreToolUse": foreign}}), encoding="utf-8")

    changed, _ = guard.install_hook(settings)
    assert changed is True
    pre = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert pre[:2] == foreign                                  # neither foreign hook was touched
    owned = [h for g in pre for h in g["hooks"] if guard._is_guard_hook(h)]
    assert len(owned) == 1
    assert owned[0]["command"] == guard.guard_hook_command()

    assert guard.uninstall_hook(settings)[0] is True
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"] == foreign


def test_guard_install_verification_uses_the_project_environment(tmp_path, monkeypatch, capsys):
    """The self-test must probe the stored command where the hook runs: no injected
    PYTHONPATH and no installer cwd, so an interpreter that cannot import RunLedger
    from the project is reported as a warning instead of a false success."""
    shadow = tmp_path / "shadow"
    (shadow / "runledger").mkdir(parents=True)
    (shadow / "runledger" / "__init__.py").write_text(
        "raise ImportError('runledger is not installed here')\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(shadow))

    proj = tmp_path / "proj"
    proj.mkdir()
    assert cli_main(["guard", "install", "--project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert (proj / ".claude" / "settings.json").exists()
    assert "Verified hook command:" not in out
    assert "Warning" in out and "exit" in out


def test_guard_install_default_writes_settings_at_the_project_root(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    sub = repo / "pkg" / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.chdir(sub)

    assert cli_main(["guard", "install"]) == 0
    capsys.readouterr()
    assert (repo / ".claude" / "settings.json").exists()
    assert not (sub / ".claude").exists()
    assert not (repo / "pkg" / ".claude").exists()

    assert cli_main(["guard", "uninstall"]) == 0
    assert "hooks" not in json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_guard_install_default_uses_a_policy_only_project_root(tmp_path, monkeypatch):
    policy_only = tmp_path / "policy"
    sub = policy_only / "pkg"
    sub.mkdir(parents=True)
    write_project(policy_only, {})
    monkeypatch.chdir(sub)

    assert cli_main(["guard", "install"]) == 0
    assert (policy_only / ".claude" / "settings.json").exists()
    assert not (sub / ".claude").exists()


@pytest.mark.parametrize("command, owned", [
    (r'"C:\Python313\python3.13t.exe" -m runledger guard', True),
    ('"/opt/py313t/bin/python3.13t" -m runledger guard', True),
    ('"python3.14t" -m runledger guard', True),
    (r'"C:\Python313\pythonw.exe" -m runledger guard', True),
    (r'"C:\Python313\node.exe" -m runledger guard', False),
    ('"python3.13t" -m runledger checkpoint --hook', False),
])
def test_is_guard_hook_accepts_the_interpreter_names_the_installer_can_write(command, owned):
    """CORE-SCR-R2-007: `guard_hook_command` stores the literal `sys.executable`, so a
    free-threaded or versioned interpreter name is still our own entry, while a foreign
    executable or another hook stays foreign."""
    assert guard._is_guard_hook({"type": "command", "command": command}) is owned


def test_guard_install_round_trips_with_a_free_threaded_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", r"C:\Python313\python3.13t.exe")
    settings = tmp_path / ".claude" / "settings.json"

    changed, _ = guard.install_hook(settings)
    assert changed is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    stored = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert stored == r'"C:\Python313\python3.13t.exe" -m runledger guard'
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == \
        r'"C:\Python313\python3.13t.exe" -m runledger checkpoint --hook'

    assert guard.install_hook(settings) == (False, None)      # a second install changes nothing
    _, message = guard.verify_hook_command(stored)            # the self-test recognises it
    assert "not a RunLedger guard command" not in message

    assert guard.uninstall_hook(settings)[0] is True          # nothing is left behind
    assert json.loads(settings.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("command", [
    "true;/usr/bin/python3.13t -m runledger guard",
    "true&&/usr/bin/python3.13t -m runledger guard",
    "true||/usr/bin/python3.13t -m runledger guard",
    "true|/usr/bin/python3.13t -m runledger guard",
    "true&/usr/bin/python3.13t -m runledger guard",
    '"/usr/bin/python3.13t" -m runledger guard > hook.log',
    '"/usr/bin/python3.13t" -m runledger guard 2>&1',
    "(/usr/bin/python3.13t -m runledger guard)",
    "/usr/bin/python3.13t -m runledger guard\nrm -rf ./build",
    "/usr/bin/python3.13t -m runledger guard;echo hi",
    "true;runledger guard",
])
def test_is_guard_hook_rejects_attached_shell_operators(command):
    """CORE-SCR-R3-004: an operator written without spaces is still an operator, so the
    basename of the first word must never decide ownership of a compound command."""
    assert guard._is_guard_hook({"type": "command", "command": command}) is False


@pytest.mark.parametrize("command", [
    '"/usr/$(printf bin)/python3.13t" -m runledger guard',
    '"/usr/`printf bin`/python3.13t" -m runledger guard',
    '"$PYTHON3" -m runledger guard',
    r'"/usr/\$python/python3.13t" -m runledger guard',
])
def test_is_guard_hook_rejects_executable_substitutions(command):
    """CORE-SCR-R4-004: POSIX `$(...)` and backticks stay executable inside double quotes, so a
    quoted executable that computes its own path is not the single command the installer
    writes: it must never be replaced or removed (VAL-CORE-036/037). A backslash-escaped `$` is
    read as an operator too: an entry that spells a `$` at all is not the command install
    writes, and the conservative reading never touches someone else's hook."""
    assert guard._is_guard_hook({"type": "command", "command": command}) is False


def test_guard_install_preserves_a_substitution_bearing_hook(tmp_path):
    """The round-4 reproducer: v0.6.0 preserved this foreign entry, so install appends beside it
    and uninstall leaves it in place."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    foreign = [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": '"/usr/$(printf bin)/python3.13t" -m runledger guard'}]}]
    settings.write_text(json.dumps({"hooks": {"PreToolUse": foreign}}), encoding="utf-8")

    assert guard.install_hook(settings)[0] is True
    pre = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert pre[0] == foreign[0]
    owned = [h for group in pre for h in group["hooks"] if guard._is_guard_hook(h)]
    assert len(owned) == 1 and owned[0]["command"] == guard.guard_hook_command()

    assert guard.uninstall_hook(settings)[0] is True
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"] == foreign


def test_guard_install_preserves_a_compound_interpreter_hook(tmp_path):
    """The installer writes one simple command, so a compound entry that mentions ours is a
    foreign hook: install appends beside it and uninstall leaves it in place (VAL-CORE-036)."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    foreign = [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "true;/usr/bin/python3.13t -m runledger guard"}]}]
    settings.write_text(json.dumps({"hooks": {"PreToolUse": foreign}}), encoding="utf-8")

    assert guard.install_hook(settings)[0] is True
    pre = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert pre[0] == foreign[0]
    owned = [h for group in pre for h in group["hooks"] if guard._is_guard_hook(h)]
    assert len(owned) == 1 and owned[0]["command"] == guard.guard_hook_command()

    assert guard.uninstall_hook(settings)[0] is True
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"] == foreign


@pytest.mark.parametrize("executable, command, owned", [
    (r"D:\tools\bin\agent-python.exe", r'"D:\tools\bin\agent-python.exe" -m runledger guard', True),
    (r"D:\tools\bin\agent-python.exe", '"D:/tools/bin/agent-python.exe" -m runledger guard', True),
    (r"D:\Tools\Agent Python\agent-python.exe",
     r'"d:\tools\agent python\AGENT-PYTHON.EXE" -m runledger guard', True),
    ("/opt/agents/bin/agent-python", '"/opt/agents/bin/agent-python" -m runledger guard', True),
    (r"D:\Tools & Runtimes\agent-python.exe",
     r'"D:\Tools & Runtimes\agent-python.exe" -m runledger guard', True),
    ("/opt/agents/bin/agent-python", '"/opt/Agents/bin/agent-python" -m runledger guard', False),
    (r"D:\tools\bin\agent-python.exe", r'"D:\other\bin\agent-python.exe" -m runledger guard', False),
    (r"D:\tools\bin\agent-python.exe",
     r'"D:\tools\bin\agent-python.exe" -m runledger checkpoint --hook', False),
])
def test_is_guard_hook_follows_the_current_interpreter(monkeypatch, executable, command, owned):
    """CORE-SCR-R3-003: the installer writes the literal `sys.executable`, so a copied
    interpreter that does not look like python is our own entry, and another path is not."""
    monkeypatch.setattr(sys, "executable", executable)
    assert guard._is_guard_hook({"type": "command", "command": command}) is owned


def test_guard_install_round_trips_with_a_renamed_interpreter(tmp_path, monkeypatch):
    """The whole lifecycle of an installer-emitted command for a renamed interpreter:
    idempotent install, recognised verification and uninstall without leftovers."""
    renamed = r"D:\tools\bin\agent-python.exe"
    monkeypatch.setattr(sys, "executable", renamed)
    settings = tmp_path / ".claude" / "settings.json"

    assert guard.install_hook(settings)[0] is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    stored = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert stored == fr'"{renamed}" -m runledger guard'
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == \
        fr'"{renamed}" -m runledger checkpoint --hook'

    assert guard.install_hook(settings) == (False, None)      # no duplicate entry
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert len(data["hooks"]["PreToolUse"]) == 1
    assert len(data["hooks"]["SessionStart"]) == 1

    _, message = guard.verify_hook_command(stored)            # recognised, not refused
    assert "not a RunLedger guard command" not in message

    assert guard.uninstall_hook(settings)[0] is True
    assert json.loads(settings.read_text(encoding="utf-8")) == {}




