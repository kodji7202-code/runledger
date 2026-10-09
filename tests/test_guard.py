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


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """HOME and USERPROFILE point at an empty folder, and the approval variables are
    cleared, so no test reads the real ~/.runledger/config.json or the environment."""
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("RUNLEDGER_SERVER", raising=False)
    monkeypatch.delenv("RUNLEDGER_API_KEY", raising=False)
    return home


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
    guard.handle(json.dumps(bash_event(tmp_path, "rm -rf /", session="sess-9")))
    rec = _last_log(tmp_path)
    assert rec["session_id"] == "sess-9"
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
    assert pre[1] == {"matcher": "*", "hooks": [{"type": "command", "command": "runledger guard", "timeout": 135}]}


def test_install_is_idempotent(tmp_path):
    settings = tmp_path / "proj" / ".claude" / "settings.json"
    assert guard.install_hook(settings) == (True, None)  # new file: nothing to back up
    first = settings.read_bytes()
    assert guard.install_hook(settings) == (False, None)
    assert settings.read_bytes() == first
    assert not settings.with_name("settings.json.bak").exists()
    entries = [h for g in json.loads(first)["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert sum("runledger guard" in h["command"] for h in entries) == 1


def test_install_recognises_a_guard_entry_under_another_matcher(tmp_path):
    settings = tmp_path / "settings.json"
    existing = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "runledger guard"}]}]}}
    settings.write_text(json.dumps(existing), encoding="utf-8")
    changed, backup = guard.install_hook(settings)
    assert changed is True and backup is not None          # timeout added to the existing entry
    pre = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert len(pre) == 1                                    # no second entry
    assert pre[0]["matcher"] == "Bash"                      # the user's matcher is kept
    assert pre[0]["hooks"][0] == {"type": "command", "command": "runledger guard", "timeout": 135}


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
    assert hook == {"type": "command", "command": "runledger guard", "timeout": 135}


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
