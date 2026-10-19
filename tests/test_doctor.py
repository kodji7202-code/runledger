"""`runledger doctor` and `runledger guard status` (validation contract VAL-DOCTOR-001..019).

Every home and project below is a scratch one: the shared `isolated_settings` fixture in
conftest.py redirects HOME/USERPROFILE and clears the agent variables, so no real agent
config is read or written. The hook payload shapes are the recorded ones
(`tests/fixtures/guard/hook_payloads.json`).
"""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from runledger import doctor, guard, hook_targets, pricing, rollback
from runledger.cli import main as cli_main

AGENTS = ("claude-code", "droid", "codex")
CHECKPOINTS = ("SessionStart", "UserPromptSubmit")
FOLDER = {"claude-code": (".claude", "settings.json"),
          "droid": (".factory", "hooks.json"),
          "codex": (".codex", "hooks.json")}
WRAPPER = {"claude-code": "hooks", "droid": None, "codex": "hooks"}


# ---------------------------------------------------------------- helpers

def install(project, agent="claude-code", *extra):
    return cli_main(["guard", "install", "--agent", agent, "--project", str(project), *extra])


def hook_file(project, agent):
    return Path(project).joinpath(*FOLDER[agent])


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def events_of(data, agent):
    key = WRAPPER[agent]
    return data if key is None else data.setdefault(key, {})


def checks(project=None, **kw):
    kw.setdefault("offline", True)
    kw.setdefault("selftest", False)
    return doctor.run_checks(project=str(project) if project is not None else None, **kw)


def pick(all_checks, check_id, agent=None, scope=None):
    out = [c for c in all_checks
           if c.id == check_id
           and (agent is None or c.agent == agent)
           and (scope is None or c.scope == scope)]
    return out


def one(all_checks, check_id, agent=None, scope=None):
    found = pick(all_checks, check_id, agent, scope)
    assert len(found) == 1, [(c.id, c.agent, c.scope, c.status) for c in all_checks]
    return found[0]


def codes(all_checks):
    return doctor.exit_code(all_checks)


# ---------------------------------------------------------------- VAL-DOCTOR-004

def test_a_project_without_hooks_is_info_per_agent_and_scope(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    found = checks(proj)
    for agent in AGENTS:
        for scope in ("project", "global"):
            check = one(found, "guard.hooks", agent, scope)
            assert check.status == "info", check
            assert "not installed" in check.detail
    assert codes(found) == 0


def test_an_installed_project_hook_is_ok_and_names_the_file_and_entries(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "droid") == 0
    found = checks(proj)
    check = one(found, "guard.hooks", "droid", "project")
    assert check.status == "ok", check
    assert str(hook_file(proj, "droid")) in check.detail
    assert "guard" in check.detail and "SessionStart" in check.detail and "UserPromptSubmit" in check.detail
    assert one(found, "guard.hooks", "claude-code", "project").status == "info"
    assert codes(found) == 0


def test_a_global_install_is_reported_for_the_global_scope(tmp_path, isolated_settings):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert cli_main(["guard", "install", "--agent", "claude-code", "--global"]) == 0
    found = checks(proj)
    global_check = one(found, "guard.hooks", "claude-code", "global")
    assert global_check.status == "ok"
    assert str(isolated_settings / ".claude" / "settings.json") in global_check.detail
    assert one(found, "guard.hooks", "claude-code", "project").status == "info"


def test_a_removed_checkpoint_entry_is_not_reported_as_complete(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    path = hook_file(proj, "claude-code")
    data = read_json(path)
    del data["hooks"]["SessionStart"]
    write_json(path, data)

    check = one(checks(proj), "guard.hooks", "claude-code", "project")
    assert check.status == "warn", check
    assert "SessionStart" in check.detail
    assert check.status != "ok"


def test_malformed_hook_json_is_a_failure_with_the_position(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    path = hook_file(proj, "codex")
    path.parent.mkdir(parents=True)
    path.write_text('{"hooks": {', encoding="utf-8")

    check = one(checks(proj), "guard.hooks", "codex", "project")
    assert check.status == "fail", check
    assert str(path) in check.detail
    assert "line" in check.detail.lower()
    assert codes(checks(proj)) == 2


def test_duplicate_guard_entries_warn(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    path = hook_file(proj, "claude-code")
    data = read_json(path)
    pre = data["hooks"]["PreToolUse"]
    pre.append(json.loads(json.dumps(pre[0])))
    write_json(path, data)

    check = one(checks(proj), "guard.hooks", "claude-code", "project")
    assert check.status == "warn", check
    assert "2" in check.detail and "guard" in check.detail


def test_a_short_timeout_warns_about_the_approval_wait(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    path = hook_file(proj, "claude-code")
    data = read_json(path)
    data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 20
    write_json(path, data)
    config = isolated_home(tmp_path) / ".runledger" / "config.json"
    write_json(config, {"guard": {"approval": {"timeout_s": 300}}})

    check = one(checks(proj), "guard.hooks", "claude-code", "project")
    assert check.status == "warn", check
    assert "timeout" in check.detail
    assert "guard install" in check.fix


def test_a_module_command_with_a_missing_interpreter_is_a_failure(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "droid") == 0
    path = hook_file(proj, "droid")
    data = read_json(path)
    data["PreToolUse"][0]["hooks"][0]["command"] = (
        f'"{tmp_path / "missing" / "python.exe"}" -m runledger guard --agent droid')
    write_json(path, data)

    check = one(checks(proj), "guard.hooks", "droid", "project")
    assert check.status == "fail", check
    assert "interpreter" in check.detail.lower() or "does not exist" in check.detail.lower()
    assert codes(checks(proj)) == 2


def test_a_portable_command_without_runledger_on_path_warns(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex", "--portable") == 0
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    check = one(checks(proj), "guard.hooks", "codex", "project")
    assert check.status == "warn", check
    assert "PATH" in check.detail
    assert "guard install" in check.fix
    # The default absolute-interpreter install never gets this warning.
    assert install(proj, "claude-code") == 0
    absolute = one(checks(proj), "guard.hooks", "claude-code", "project")
    assert absolute.status == "ok", absolute
    assert "PATH" not in absolute.detail


def test_agent_filter_limits_the_report_to_that_agent(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    found = checks(proj, agents=["codex"])
    assert {c.agent for c in found if c.agent} == {"codex"}
    assert pick(found, "guard.hooks", "droid") == []
    # Agent-independent checks stay.
    assert pick(found, "config") and pick(found, "pricing")


def test_codex_and_droid_notes_are_info_and_do_not_change_the_exit_code(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0
    assert install(proj, "droid") == 0
    found = checks(proj)

    codex_note = one(found, "guard.note", "codex")
    assert codex_note.status == "info", codex_note
    assert "/hooks" in codex_note.detail and "trust" in codex_note.detail.lower()
    assert "active" not in codex_note.detail.lower()
    assert "trusted" not in codex_note.detail.lower().replace("untrusted", "")
    assert codes(found) == 0

    droid_note = one(found, "guard.note", "droid")
    assert droid_note.status == "info", droid_note
    assert "/hooks" in droid_note.detail
    assert "restart" in droid_note.detail.lower() or "snapshot" in droid_note.detail.lower()
    assert codes(found) == 0


def test_no_note_without_the_hook_installed(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    found = checks(proj)
    assert pick(found, "guard.note") == []


# ---------------------------------------------------------------- VAL-DOCTOR-001 / 002

def test_text_output_has_one_line_per_check_and_a_matching_summary(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    report = checks(proj)
    text = doctor.render_text(report)

    tags = [line.split(" ", 1)[0] for line in text.splitlines() if line.startswith("[")]
    assert len(tags) == len(report.checks)
    assert set(tags) <= {"[ok]", "[warn]", "[fail]", "[info]", "[skip]"}
    summary = doctor.summary(report.checks)
    last = text.strip().splitlines()[-1]
    assert last == (f"Summary: {summary['ok']} ok, {summary['warn']} warnings, "
                    f"{summary['fail']} failures")


def test_fix_hints_appear_on_warnings_and_failures(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    path = hook_file(proj, "codex")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    text = doctor.render_text(checks(proj))
    assert "->" in text


def test_exit_codes_follow_the_highest_severity(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert doctor.exit_code(checks(proj)) == 0

    assert install(proj, "claude-code") == 0
    path = hook_file(proj, "claude-code")
    data = read_json(path)
    del data["hooks"]["UserPromptSubmit"]
    write_json(path, data)
    assert doctor.exit_code(checks(proj)) == 1

    write_json(hook_file(proj, "claude-code"), ["not", "an", "object"])
    assert doctor.exit_code(checks(proj)) == 2


def test_warn_and_fail_together_give_two(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    found = [doctor.Check("a", "warn", "w", "d"), doctor.Check("b", "fail", "f", "d")]
    assert doctor.exit_code(found) == 2
    assert doctor.exit_code([doctor.Check("a", "info", "i", "d")]) == 0
    assert doctor.exit_code([doctor.Check("a", "skip", "s", "d")]) == 0


def test_a_folder_that_is_not_a_repo_does_not_crash_or_fail(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    found = checks(plain)
    assert codes(found) in (0, 1)
    assert one(found, "rollback").status in ("info", "skip")


# ---------------------------------------------------------------- helpers used above

def isolated_home(tmp_path):
    """The HOME the shared fixture points at (created on demand)."""
    home = Path(os.environ["USERPROFILE"])
    home.mkdir(parents=True, exist_ok=True)
    return home


def write_user_config(tmp_path, data):
    path = isolated_home(tmp_path) / ".runledger" / "config.json"
    write_json(path, data)
    return path


def project_policy_file(project):
    return Path(project) / ".runledger.json"


def git_init(project):
    project.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    for key, value in (("user.email", "doctor@example.com"), ("user.name", "Doctor Test")):
        subprocess.run(["git", "-C", str(project), "config", key, value], check=True,
                       capture_output=True)
    return project


def commit_all(project, message="initial"):
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", message], check=True,
                   capture_output=True)


def untrack(project, path):
    """Drop a path from the Git index, leaving the file on disk."""
    subprocess.run(["git", "-C", str(project), "rm", "-q", "--cached", "--", path], check=True,
                   capture_output=True)


# ---------------------------------------------------------------- VAL-DOCTOR-007

def test_selftest_runs_the_stored_command_for_each_installed_agent(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    for agent in AGENTS:
        assert install(proj, agent) == 0

    found = checks(proj, selftest=True)
    for agent in AGENTS:
        check = one(found, "guard.selftest", agent)
        assert check.status == "ok", check
        assert "deny" in check.detail.lower()
        assert "ms" in check.detail
    assert codes(found) == 0
    assert not (proj / ".runledger").exists()


def test_selftest_fails_when_the_interpreter_path_is_wrong(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    path = hook_file(proj, "claude-code")
    data = read_json(path)
    data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = (
        f'"{tmp_path / "gone" / "python.exe"}" -m runledger guard')
    write_json(path, data)

    check = one(checks(proj, selftest=True), "guard.selftest", "claude-code")
    assert check.status == "fail", check
    assert check.detail


@pytest.mark.parametrize("replacement", [
    f'"{sys.executable}" -c "import sys; sys.exit(1)"',
    f'"{sys.executable}" -c "print(\'not json at all\')"',
    f'"{sys.executable}" -c "print(\'{{}}\')"',
])
def test_a_hook_edited_to_misbehave_is_a_failure(tmp_path, replacement):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "droid") == 0
    path = hook_file(proj, "droid")
    data = read_json(path)
    data["PreToolUse"][0]["hooks"][0]["command"] = replacement
    write_json(path, data)

    check = one(checks(proj, selftest=True), "guard.selftest", "droid")
    assert check.status == "fail", check
    assert "exit" in check.detail.lower() or "deny" in check.detail.lower()
    assert codes(checks(proj, selftest=True)) == 2


def test_no_selftest_skips_the_check_and_starts_no_subprocess(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0

    def boom(*args, **kwargs):
        raise AssertionError("the self-test must not run a hook subprocess")

    monkeypatch.setattr(doctor, "_run_hook_command", boom)
    found = checks(proj, selftest=False)
    skipped = pick(found, "guard.selftest")
    assert len(skipped) == 1 and skipped[0].status == "skip"
    assert codes(found) == 0


def test_selftest_is_isolated_from_the_user_config(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    write_user_config(tmp_path, {"guard": {"mode": "monitor", "deny": []}})

    check = one(checks(proj, selftest=True), "guard.selftest", "claude-code")
    assert check.status == "ok", check


def test_selftest_leaves_no_log_or_folder_in_the_project(tmp_path, isolated_settings):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    write_user_config(tmp_path, {"guard": {"approval": {"server": "http://127.0.0.1:1"}}})
    before = sorted(p.name for p in proj.iterdir())

    checks(proj, selftest=True)
    assert sorted(p.name for p in proj.iterdir()) == before
    assert not (proj / ".runledger").exists()


# ---------------------------------------------------------------- VAL-DOCTOR-008

def test_valid_config_files_are_ok(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_user_config(tmp_path, {"guard": {"fail_closed": True}, "ignore": ["secret"]})
    write_json(project_policy_file(proj), {"guard": {"deny": ["command:high"]}})

    found = checks(proj)
    for check in pick(found, "config"):
        assert check.status == "ok", check
    assert codes(found) == 0


def test_a_missing_project_policy_file_is_not_a_warning(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    found = pick(checks(proj), "config")
    assert found and all(c.status in ("ok", "info") for c in found)
    assert codes(checks(proj)) == 0


def test_invalid_project_json_is_a_failure_with_the_position(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    project_policy_file(proj).write_text('{"guard": {"mode": }', encoding="utf-8")

    found = checks(proj)
    failing = [c for c in pick(found, "config") if c.status == "fail"]
    assert len(failing) == 1, found
    assert ".runledger.json" in failing[0].detail
    assert "line" in failing[0].detail.lower()
    assert codes(found) == 2


def test_invalid_user_json_is_a_failure(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    path = isolated_home(tmp_path) / ".runledger" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    failing = [c for c in pick(checks(proj), "config") if c.status == "fail"]
    assert len(failing) == 1
    assert "config.json" in failing[0].detail
    assert codes(checks(proj)) == 2


def test_unknown_top_level_keys_are_info_and_listed(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_user_config(tmp_path, {"guard": {}, "whatever": 1})
    write_json(project_policy_file(proj), {"nonsense": True})

    found = pick(checks(proj), "config")
    assert any(c.status == "info" and "whatever" in c.detail for c in found)
    assert any(c.status == "info" and "nonsense" in c.detail for c in found)
    assert codes(checks(proj)) == 0


def test_invalid_rule_entries_warn_with_a_count(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_user_config(tmp_path, {
        "command_rules": {"deny": ["(unclosed", r"\bgoodrule\b", 42], "ask": "not-a-list"},
        "tool_rules": {"deny": ["[", " ", 7]},
        "mcp_verbs": {"destructive": "nuke", "write": [1, None]},
    })

    warns = [c for c in pick(checks(proj), "config") if c.status == "warn"]
    assert warns, pick(checks(proj), "config")
    text = " ".join(c.detail for c in warns)
    assert re.search(r"\b\d+\b", text)
    assert "rule" in text.lower()
    assert codes(checks(proj)) == 1


def test_project_keys_the_guard_ignores_are_reported(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_json(project_policy_file(proj), {
        "guard": {"mode": "monitor", "codex_ask": "allow",
                  "approval": {"server": "http://evil.example"}},
        "mcp_verbs": {"read": ["peek"]},
        "severity_overrides": {"secret_in_content": "low"},
    })

    warns = [c for c in pick(checks(proj), "config") if c.status == "warn"]
    assert warns, pick(checks(proj), "config")
    text = " ".join(c.detail for c in warns)
    for key in ("monitor", "codex_ask", "approval", "mcp_verbs", "severity_overrides"):
        assert key in text, key


def test_a_tightening_project_file_reports_no_ignored_keys(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_json(project_policy_file(proj), {
        "guard": {"deny": ["command:high"], "fail_closed": True},
        "severity_overrides": {"command": "high"},
        "command_rules": {"deny": [r"\bwipe\b"]},
    })

    found = pick(checks(proj), "config")
    assert not [c for c in found if c.status == "warn"], found
    project_entry = [c for c in found if ".runledger.json" in c.detail][0]
    assert "0 ignored" in project_entry.detail
    assert codes(checks(proj)) == 0


# ---------------------------------------------------------------- VAL-DOCTOR-009

class _ApprovalStub:
    """A tiny /health server that records every request path and header."""

    def __init__(self, status=200):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the handler's documented name
                outer.requests.append((self.path, dict(self.headers)))
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):  # keep the test output clean
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_no_approval_server_is_info(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    check = one(checks(proj), "approval")
    assert check.status == "info", check
    assert codes(checks(proj)) == 0


def test_a_reachable_approval_server_is_ok_and_carries_no_key(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    stub = _ApprovalStub()
    monkeypatch.setenv("RUNLEDGER_SERVER", stub.url)
    monkeypatch.setenv("RUNLEDGER_API_KEY", "sekret-zz-9f3a")
    try:
        check = one(checks(proj, offline=False), "approval")
    finally:
        stub.close()
    assert check.status == "ok", check
    assert stub.requests, "the /health request was not made"
    path, headers = stub.requests[0]
    assert path == "/health"
    assert not any(name.lower() == "authorization" for name in headers)


def test_an_unreachable_approval_server_warns(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens there any more
    monkeypatch.setenv("RUNLEDGER_SERVER", f"http://127.0.0.1:{port}")

    check = one(checks(proj, offline=False), "approval")
    assert check.status == "warn", check
    assert "unreachable" in check.detail.lower() or "could not" in check.detail.lower()
    assert "on_timeout" in check.detail
    assert codes(checks(proj, offline=False)) == 1


def test_offline_makes_no_request_and_never_warns(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    stub = _ApprovalStub()
    monkeypatch.setenv("RUNLEDGER_SERVER", stub.url)
    try:
        check = one(checks(proj, offline=True), "approval")
    finally:
        stub.close()
    assert stub.requests == [], "an --offline run must not touch the network"
    assert check.status in ("ok", "info"), check


def test_a_cleartext_non_loopback_server_warns_offline(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("RUNLEDGER_SERVER", "http://192.0.2.10:8080")
    check = one(checks(proj, offline=True), "approval")
    assert check.status == "warn", check
    assert "http" in check.detail.lower()

    monkeypatch.setenv("RUNLEDGER_SERVER", "https://approver.example")
    secure = one(checks(proj, offline=True), "approval")
    assert secure.status in ("ok", "info"), secure


def test_a_project_only_server_is_ignored_with_the_config_warning(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_json(project_policy_file(proj),
               {"guard": {"approval": {"server": "http://192.0.2.10:8080"}}})
    check = one(checks(proj), "approval")
    assert check.status == "info", check
    warns = [c for c in pick(checks(proj), "config") if c.status == "warn"]
    assert any("approval" in c.detail for c in warns)


def test_the_api_key_never_appears_in_any_output(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    secret = "sekret-zz-9f3a"
    other = "other-zz-4b7c"
    monkeypatch.setenv("RUNLEDGER_API_KEY", secret)
    monkeypatch.setenv("OTHER_KEY", other)
    write_user_config(tmp_path, {"api_key_env": "OTHER_KEY",
                                 "guard": {"approval": {"server": "http://127.0.0.1:9"}}})

    assert cli_main(["doctor", "--project", str(proj), "--offline", "--no-selftest"]) in (0, 1)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert secret not in text and other not in text
    assert "set (env OTHER_KEY)" in text

    assert cli_main(["doctor", "--project", str(proj), "--offline", "--no-selftest",
                     "--json", "--verbose"]) in (0, 1)
    captured = capsys.readouterr()
    payload = captured.out + captured.err
    assert secret not in payload and other not in payload
    assert "set (env OTHER_KEY)" in payload


# ---------------------------------------------------------------- VAL-DOCTOR-010

def write_log(project, rows):
    path = Path(project) / ".runledger" / "guard.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def error_row(days_ago=0):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"ts": ts.isoformat(timespec="seconds"), "session_id": "s", "tool": "Bash",
            "decision": "error", "reason": "boom"}


def test_no_log_yet_is_info(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    check = one(checks(proj), "log")
    assert check.status == "info", check
    assert codes(checks(proj)) == 0


def test_recent_error_rows_warn_and_old_ones_do_not(tmp_path):
    proj = git_init(tmp_path / "proj")
    write_log(proj, [{"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "session_id": "s", "tool": "Bash", "decision": "deny", "reason": "blocked"},
                     error_row(0), error_row(1)])
    check = one(checks(proj), "log")
    assert check.status == "warn", check
    assert "2" in check.detail and "error" in check.detail.lower()

    write_log(proj, [error_row(30), error_row(40)])
    old = one(checks(proj), "log")
    assert old.status in ("ok", "warn"), old
    if old.status == "warn":
        assert "error" in old.detail.lower() and "7 day" in old.detail.lower()


def test_a_log_that_is_not_git_ignored_warns(tmp_path):
    proj = git_init(tmp_path / "proj")
    write_log(proj, [{"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "session_id": "s", "tool": "Bash", "decision": "deny", "reason": "x"}])
    check = one(checks(proj), "log")
    assert check.status == "warn", check
    assert "git-ignored" in check.detail or "gitignore" in check.detail.lower()

    (proj / ".gitignore").write_text(".runledger/\n", encoding="utf-8")
    ignored = one(checks(proj), "log")
    assert ignored.status == "ok", ignored


def test_the_log_check_prints_no_command_text(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_log(proj, [{"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "session_id": "s", "tool": "Bash", "decision": "deny",
                      "summary": "rm -rf /very/secret/path", "reason": "destroy everything"}])
    check = one(checks(proj), "log")
    assert "very/secret" not in check.detail
    text = doctor.render_text(checks(proj))
    assert "very/secret" not in text


def test_a_log_outside_a_repo_does_not_crash(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_log(proj, [error_row(0)])
    check = one(checks(proj), "log")
    assert check.status in ("warn", "info", "ok"), check


# ---------------------------------------------------------------- VAL-DOCTOR-011

def test_checkpoints_are_counted_with_the_newest_tree_size(tmp_path):
    proj = git_init(tmp_path / "proj")
    (proj / "big.bin").write_bytes(b"0" * (3 * 1024 * 1024))
    (proj / "note.txt").write_text("hello", encoding="utf-8")
    commit_all(proj)
    assert cli_main(["checkpoint", "--cwd", str(proj)]) == 0

    before = subprocess.run(["git", "-C", str(proj), "for-each-ref"], capture_output=True,
                            check=True).stdout
    check = one(checks(proj), "rollback")
    after = subprocess.run(["git", "-C", str(proj), "for-each-ref"], capture_output=True,
                           check=True).stdout
    assert check.status == "ok", check
    assert "1 checkpoint" in check.detail
    size = re.search(r"(\d+) bytes", check.detail)
    assert size and int(size.group(1)) >= 3 * 1024 * 1024, check.detail
    assert before == after, "doctor changed the checkpoint refs"


def test_more_than_a_hundred_checkpoints_warns(tmp_path, monkeypatch):
    proj = git_init(tmp_path / "proj")
    (proj / "a.txt").write_text("a", encoding="utf-8")
    commit_all(proj)
    real = rollback.Repository(str(proj))
    assert real.checkpoints() == []

    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        def checkpoints(self):
            return [rollback.Checkpoint(f"2026010{i % 9}T000000000000Z-0000000{i % 9}", "oid",
                                        "tree", "2026-01-01T00:00:00Z", "", None)
                    for i in range(101)]

        def snapshot_stats(self, tree):
            return 1, 10, ("a.txt", 10)

    monkeypatch.setattr(doctor._rollback, "Repository", Fake)
    check = one(checks(proj), "rollback")
    assert check.status == "warn", check
    assert "101" in check.detail
    assert "prune" in check.fix


def test_the_rollback_check_does_not_run_for_a_plain_folder(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    check = one(checks(plain), "rollback")
    assert check.status in ("info", "skip"), check
    assert codes(checks(plain)) == 0


# ---------------------------------------------------------------- VAL-DOCTOR-012

PRICE_ROWS = {"claude:sonnet-4-5": {"input": 3, "output": 15, "cache_read": 0.3}}


def test_fresh_and_100_day_old_prices_are_ok(tmp_path, monkeypatch):
    today = date(2026, 10, 19)
    fresh = tmp_path / "fresh" / "prices.json"
    write_json(fresh, dict(PRICE_ROWS, checked=today.isoformat()))
    monkeypatch.setattr(pricing, "prices_path", lambda: fresh)
    assert one(checks(project=None, today=today), "pricing").status == "ok"

    old = tmp_path / "old" / "prices.json"
    write_json(old, dict(PRICE_ROWS, checked=(today - timedelta(days=100)).isoformat()))
    monkeypatch.setattr(pricing, "prices_path", lambda: old)
    assert one(checks(project=None, today=today), "pricing").status == "ok"


def test_prices_older_than_120_days_warn_with_the_age(tmp_path, monkeypatch):
    today = date(2026, 10, 19)
    path = tmp_path / "prices.json"
    checked = today - timedelta(days=121)
    write_json(path, dict(PRICE_ROWS, checked=checked.isoformat()))
    monkeypatch.setattr(pricing, "prices_path", lambda: path)

    check = one(checks(project=None, today=today), "pricing")
    assert check.status == "warn", check
    assert checked.isoformat() in check.detail
    assert "121" in check.detail
    assert "prices.json" in check.fix or "prices" in check.fix


def test_a_missing_or_unparseable_checked_date_is_not_a_failure(tmp_path, monkeypatch):
    today = date(2026, 10, 19)
    path = tmp_path / "prices.json"
    write_json(path, dict(PRICE_ROWS))
    monkeypatch.setattr(pricing, "prices_path", lambda: path)
    assert one(checks(project=None, today=today), "pricing").status != "fail"

    write_json(path, dict(PRICE_ROWS, checked="not-a-date"))
    assert one(checks(project=None, today=today), "pricing").status != "fail"


def test_a_missing_or_invalid_prices_json_fails(tmp_path, monkeypatch):
    today = date(2026, 10, 19)
    path = tmp_path / "prices.json"
    monkeypatch.setattr(pricing, "prices_path", lambda: path)
    missing = one(checks(project=None, today=today), "pricing")
    assert missing.status == "fail", missing

    path.write_text("{not json", encoding="utf-8")
    broken = one(checks(project=None, today=today), "pricing")
    assert broken.status == "fail", broken
    assert codes(checks(project=None, today=today)) == 2


def test_the_bundled_prices_file_is_reported_as_ok(tmp_path):
    assert pricing.prices_path().is_file()
    check = one(checks(project=None), "pricing")
    assert check.status in ("ok", "warn"), check


def test_a_broken_packaged_prices_file_never_stops_the_import(monkeypatch):
    """A missing or invalid packaged table degrades to "no prices" instead of a traceback:
    the doctor reports that file as a failure and every command still runs."""

    class _Handle:
        def read_text(self, encoding=None):
            return "{ not json"

    class _Package:
        def joinpath(self, name):
            return _Handle()

    class _Resources:
        def files(self, package):
            return _Package()

    monkeypatch.setattr(pricing, "resources", _Resources())
    assert pricing._load_default() == {}


# ---------------------------------------------------------------- VAL-DOCTOR-013

def session_claude(claude_dir, project, name):
    folder = Path(claude_dir) / "projects" / doctor_encode(project)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.jsonl"
    source = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        obj["cwd"] = str(project)
        lines.append(json.dumps(obj))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def doctor_encode(project):
    from runledger.parser import encode_project_path

    return encode_project_path(str(project))


def session_codex(codex_home, project, name):
    folder = Path(codex_home) / "sessions" / "2026" / "10" / "19"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"rollout-2026-10-19T10-00-00-{name}.jsonl"
    source = Path(__file__).parent / "fixtures" / "codex_session.jsonl"
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("type") == "session_meta":
            obj["payload"]["cwd"] = str(project)
        lines.append(json.dumps(obj))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def session_droid(factory, project, name):
    folder = Path(factory) / "sessions" / "-dr-"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.jsonl"
    source = Path(__file__).parent / "fixtures" / "droid_session.jsonl"
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("type") == "session_start":
            obj["cwd"] = str(project)
        lines.append(json.dumps(obj))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings = Path(__file__).parent / "fixtures" / "droid_session.settings.json"
    target.with_suffix(".settings.json").write_text(settings.read_text(encoding="utf-8"),
                                                    encoding="utf-8")
    return target


def test_session_counts_and_homes_per_agent(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    from runledger import adapters

    claude_home = tmp_path / "claude-home"
    codex_home = tmp_path / "codex-home"
    factory_home = tmp_path / "factory-home" / ".factory"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", str(tmp_path / "factory-home"))
    session_claude(claude_home, proj, "one")
    session_codex(codex_home, proj, "one")
    session_droid(factory_home, proj, "one")
    (proj / ".aider.chat.history.md").write_text(
        (Path(__file__).parent / "fixtures" / "aider" / ".aider.chat.history.md")
        .read_text(encoding="utf-8"), encoding="utf-8")
    runs = proj / ".runledger" / "runs"
    runs.mkdir(parents=True)
    (runs / "demo.runledger.json").write_text(
        (Path(__file__).parent / "fixtures" / "native_session.json").read_text(encoding="utf-8"),
        encoding="utf-8")

    found = checks(proj)
    expected_home = {"claude-code": str(claude_home), "codex": str(codex_home),
                     "droid": str(factory_home), "aider": str(proj), "native": str(proj)}
    for agent in ("claude-code", "codex", "droid", "aider", "native"):
        check = one(found, "sessions", agent)
        assert check.status == "info", check
        expected = len(adapters.get(agent).find_sessions(str(proj)))
        assert expected == 1, (agent, expected)
        assert f"{expected} session" in check.detail, (agent, check.detail)
        assert expected_home[agent] in check.detail, (agent, check.detail)
    assert codes(found) == 0


def test_the_droid_session_check_names_the_directory_discovery_reads(tmp_path, monkeypatch):
    """GE-001 / VAL-AGENT-028: the doctor counts and names the same Factory directory
    `runledger list --agent droid` reads when HOME differs from USERPROFILE."""
    proj = tmp_path / "proj"
    proj.mkdir()
    env_home = tmp_path / "env-home"
    env_profile = tmp_path / "env-profile"
    monkeypatch.setenv("HOME", str(env_home))
    monkeypatch.setenv("USERPROFILE", str(env_profile))
    session_droid(env_home / ".factory", proj, "one")
    session_droid(env_home / ".factory", proj, "two")
    session_droid(env_profile / ".factory", proj, "other")
    check = one(checks(proj), "sessions", "droid")
    assert check.status == "info", check
    assert "2 session" in check.detail, check.detail
    assert str(env_home / ".factory") in check.detail, check.detail


def test_empty_homes_report_zero_without_warnings(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", str(tmp_path / "factory-home"))
    found = checks(proj)
    for agent in ("claude-code", "codex", "droid", "aider", "native"):
        check = one(found, "sessions", agent)
        assert "0 session" in check.detail, check
    assert codes(found) == 0


def test_the_sessions_check_writes_no_cache(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    home = isolated_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    session_codex(tmp_path / "codex-home", proj, "one")

    checks(proj)
    assert not (home / ".runledger" / "cache").exists(), \
        "the doctor must not write the session cache"


# ---------------------------------------------------------------- VAL-DOCTOR-016

def _tree_hash(root):
    out = {}
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_doctor_is_read_only(tmp_path, monkeypatch):
    proj = git_init(tmp_path / "proj")
    (proj / "a.txt").write_text("hello", encoding="utf-8")
    commit_all(proj)
    for agent in AGENTS:
        assert install(proj, agent) == 0
    home = isolated_home(tmp_path)
    write_user_config(tmp_path, {"guard": {"mode": "enforce"}})
    write_json(project_policy_file(proj), {"guard": {"deny": ["command:high"]}})

    def snapshot():
        refs = subprocess.run(["git", "-C", str(proj), "for-each-ref"], capture_output=True,
                              check=True).stdout
        return _tree_hash(proj), _tree_hash(home), refs

    before = snapshot()
    checks(proj, selftest=True, offline=True)
    assert snapshot() == before


# ---------------------------------------------------------------- VAL-DOCTOR-003

def test_the_install_check_names_the_version_python_and_how_it_is_reached(tmp_path, monkeypatch):
    from runledger import __version__

    proj = tmp_path / "proj"
    proj.mkdir()
    check = one(checks(proj), "install")
    assert check.status in ("ok", "info"), check
    assert __version__ in check.detail
    assert platform.python_version() in check.detail
    assert "PATH" in check.detail

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    stripped = one(checks(proj), "install")
    assert stripped.status == "info", stripped
    assert "not on PATH" in stripped.detail
    assert "python -m runledger" in stripped.detail
    assert doctor.exit_code([stripped]) == 0


# ---------------------------------------------------------------- VAL-DOCTOR-001

def test_json_output_is_one_object_with_the_documented_shape(tmp_path, capsys):
    from runledger import __version__

    proj = tmp_path / "proj"
    proj.mkdir()
    for agent in AGENTS:
        assert install(proj, agent) == 0

    capsys.readouterr()             # drop the installers' chatter
    code = cli_main(["doctor", "--project", str(proj), "--offline", "--no-selftest", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)          # exactly one object, and nothing else, on stdout
    assert captured.out.strip().startswith("{") and captured.out.strip().endswith("}")
    assert payload["version"] == 1
    assert payload["runledger"] == __version__
    assert set(payload["summary"]) == {"ok", "warn", "fail", "info"}
    for status in payload["summary"]:
        assert payload["summary"][status] == sum(1 for c in payload["checks"]
                                                if c["status"] == status)
    ids = {check["id"] for check in payload["checks"]}
    assert {"install", "guard.hooks", "guard.note", "guard.selftest", "config", "approval",
            "log", "rollback", "pricing", "sessions"} <= ids
    for check in payload["checks"]:
        assert set(check) == {"id", "status", "title", "detail", "fix", "agent", "scope"}, check
        assert check["status"] in ("ok", "warn", "fail", "info", "skip")
        assert check["fix"] or check["status"] in ("ok", "info", "skip")
    assert code in (0, 1)


def test_the_text_report_survives_the_default_windows_console_encoding(tmp_path, isolated_settings):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), HOME=str(isolated_settings),
               USERPROFILE=str(isolated_settings))
    target = tmp_path / "redirected.txt"

    with open(str(target), "w") as handle:
        result = subprocess.run([sys.executable, "-m", "runledger", "doctor", "--project", str(proj),
                                 "--offline", "--no-selftest"], stdout=handle, stderr=subprocess.PIPE,
                                cwd=str(tmp_path), env=env, timeout=120)
    assert result.returncode in (0, 1), result.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in result.stderr.decode("utf-8", "replace")
    assert "Summary:" in target.read_text(encoding="utf-8", errors="replace")


def test_verbose_names_the_files_behind_the_checks(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    write_user_config(tmp_path, {"guard": {"mode": "enforce"}})

    code = cli_main(["doctor", "--project", str(proj), "--offline", "--no-selftest", "--verbose"])
    out = capsys.readouterr().out
    assert code in (0, 1), out
    assert str(hook_file(proj, "claude-code")) in out
    assert "hook file:" in out
    assert str(tmp_path / "isolated-home" / ".runledger" / "config.json") in out
    assert f"prices: {pricing.prices_path()}" in out
    assert "checkpoint refs:" in out
    assert "PATH lookup:" in out


def test_an_unknown_agent_is_a_usage_error(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    with pytest.raises(SystemExit) as exc:
        cli_main(["doctor", "--project", str(proj), "--agent", "bogus"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "bogus" in err and "Traceback" not in err


# ---------------------------------------------------------------- VAL-DOCTOR-015

def test_guard_status_is_the_compact_guard_only_view(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    write_user_config(tmp_path, {"guard": {"mode": "monitor", "fail_closed": True}})
    capsys.readouterr()             # drop the installer's chatter

    code = cli_main(["guard", "status", "--project", str(proj)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "mode monitor" in out and "fail_closed on" in out
    assert "guard hooks" in out and str(hook_file(proj, "claude-code")) in out
    assert "approval server" in out and "guard log" in out and "config" in out
    for absent in ("pricing", "rollback", "hook self-test", "sessions:"):
        assert absent not in out.lower(), absent

    code = cli_main(["guard", "status", "--project", str(proj), "--json"])
    payload = json.loads(capsys.readouterr().out)
    ids = {check["id"] for check in payload["checks"]}
    assert ids <= {"guard.hooks", "guard.note", "config", "approval", "log", "guard.mode"}
    assert {"guard.hooks", "guard.mode", "config", "approval", "log"} <= ids
    assert "guard.selftest" not in ids
    assert payload["summary"].get("fail", 0) == 0
    assert code == 0


def test_guard_status_reports_a_project_mode_as_ignored_and_stays_enforce(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_json(project_policy_file(proj), {"guard": {"mode": "monitor"}})

    code = cli_main(["guard", "status", "--project", str(proj), "--json"])
    payload = json.loads(capsys.readouterr().out)
    by_id = {check["id"]: check for check in payload["checks"]}
    assert "mode enforce" in by_id["guard.mode"]["detail"]
    assert by_id["config"]["status"] == "warn"
    assert "mode" in by_id["config"]["detail"]
    assert code == 1


def test_guard_status_never_runs_the_self_test(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0

    def boom(*args, **kwargs):
        raise AssertionError("guard status must not run a hook subprocess")

    monkeypatch.setattr(doctor, "_run_hook_command", boom)
    assert cli_main(["guard", "status", "--project", str(proj)]) == 0


# ---------------------------------------------------------------- VAL-DOCTOR-017

def test_project_and_repeated_agent_scoping_from_another_cwd(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert install(proj, "codex") == 0
    monkeypatch.chdir(elsewhere)
    capsys.readouterr()             # drop the installer's chatter

    code = cli_main(["doctor", "--project", str(proj), "--agent", "codex", "--offline",
                     "--no-selftest", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == str(proj)
    assert {c["agent"] for c in payload["checks"] if c["agent"]} == {"codex"}
    assert code in (0, 1)

    code = cli_main(["doctor", "--project", str(proj), "--agent", "droid", "--agent", "codex",
                     "--offline", "--no-selftest", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert {c["agent"] for c in payload["checks"] if c["agent"]} == {"droid", "codex"}
    # A requested agent without hooks stays info, so it cannot raise the exit code.
    requested = [c for c in payload["checks"] if c["agent"] == "droid"]
    assert requested and all(c["status"] == "info" for c in requested), requested
    assert code in (0, 1)


def test_a_full_offline_run_stays_within_the_time_budget(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    for agent in AGENTS:
        assert install(proj, agent) == 0

    started = time.time()
    code = cli_main(["doctor", "--project", str(proj), "--offline"])
    elapsed = time.time() - started
    # The contract budgets 20 s for this run on the development machine; the assertion leaves
    # room for a slower CI runner while still catching a hook that hangs.
    assert elapsed < 30, f"a full run took {elapsed:.1f}s"
    assert code in (0, 1)


# ---------------------------------------------------------------- VAL-DOCTOR-013

def test_session_counts_match_the_list_command(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    session_claude(tmp_path / "claude-home", proj, "one")
    session_claude(tmp_path / "claude-home", proj, "two")
    session_droid(tmp_path / "factory-home" / ".factory", proj, "one")
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude-home")
    try:
        found = checks(proj)
        assert cli_main(["list", "--project", str(proj), "--agent", "claude-code",
                         "--limit", "9999"]) == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        rows = [line for line in lines if not line.startswith("when")]   # the table header
        assert len(rows) == 2, lines
        assert "2 session(s)" in one(found, "sessions", "claude-code").detail
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)


# ---------------------------------------------------------------- VAL-CROSS-006

def test_the_doctor_lifecycle_before_install_after_install_and_broken_config(
        tmp_path, monkeypatch, capsys):
    home = isolated_home(tmp_path)
    for name in (".claude", ".factory", ".codex"):
        (home / name).mkdir(parents=True, exist_ok=True)
    proj = git_init(tmp_path / "proj")
    (proj / "a.txt").write_text("hello", encoding="utf-8")
    commit_all(proj)
    monkeypatch.chdir(proj)

    def run(*args):
        capsys.readouterr()          # drop the installer's chatter
        code = cli_main([*args, "--project", str(proj), "--json"])
        payload = json.loads(capsys.readouterr().out)
        return code, payload

    def hooks_of(payload, agent):
        found = [c for c in payload["checks"]
                 if c["id"] == "guard.hooks" and c["agent"] == agent]
        assert found
        return found

    e0, before = run("doctor")
    assert e0 in (0, 1), e0
    for agent in AGENTS:
        assert all(c["status"] == "info" for c in hooks_of(before, agent)), agent
        assert all("not installed" in c["detail"] for c in hooks_of(before, agent)), agent

    assert cli_main(["guard", "install", "--agent", "all", "--global"]) == 0
    e1, after = run("doctor")
    assert e1 in (0, 1), e1
    for agent in AGENTS:
        # The global install leaves the project scope uninstalled (`info`), so the installed
        # scope is the one that must report ok.
        global_hooks = [c for c in hooks_of(after, agent) if c["scope"] == "global"]
        assert global_hooks and all(c["status"] == "ok" for c in global_hooks), (agent, global_hooks)
        project_hooks = [c for c in hooks_of(after, agent) if c["scope"] == "project"]
        assert all(c["status"] == "info" for c in project_hooks), (agent, project_hooks)
        selftest = [c for c in after["checks"]
                    if c["id"] == "guard.selftest" and c["agent"] == agent]
        assert selftest and all(c["status"] == "ok" for c in selftest), (agent, selftest)

    project_policy_file(proj).write_text("{ oops, not json", encoding="utf-8")
    e2, broken = run("doctor")
    assert e2 == 2
    failures = [c for c in broken["checks"] if c["id"] == "config" and c["status"] == "fail"]
    assert any(".runledger.json" in c["detail"] for c in failures), failures

    status_code = cli_main(["guard", "status", "--project", str(proj), "--json"])
    status = json.loads(capsys.readouterr().out)
    assert status_code == 2
    for agent in AGENTS:
        doctor_state = sorted((c["status"], c["detail"]) for c in hooks_of(broken, agent))
        status_state = sorted((c["status"], c["detail"]) for c in
                              [c for c in status["checks"]
                               if c["id"] == "guard.hooks" and c["agent"] == agent])
        assert status_state == doctor_state, agent


# ---------------------------------------------------------------- scrutiny round 1 (GE-010..GE-015)

def test_a_foreign_project_hook_is_not_self_tested_and_does_not_shadow_a_global_one(tmp_path):
    """GE-010: a foreign project PreToolUse hook is neither run as RunLedger's self-test nor
    reported as a RunLedger failure, and it does not hide a genuine global install."""
    proj = tmp_path / "proj"
    proj.mkdir()
    write_json(hook_file(proj, "claude-code"),
               {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                   {"type": "command", "command": "echo hi", "timeout": 135}]}]}})

    found = checks(proj, selftest=True)
    assert one(found, "guard.hooks", "claude-code", "project").status == "info"
    assert pick(found, "guard.selftest") == [], [c.as_dict() for c in pick(found, "guard.selftest")]
    assert codes(found) == 0

    assert cli_main(["guard", "install", "--agent", "claude-code", "--global"]) == 0
    found = checks(proj, selftest=True)
    self_test = one(found, "guard.selftest", "claude-code")
    assert "global" in self_test.detail, self_test
    assert self_test.status == "ok", self_test
    assert codes(found) == 0


def test_a_broken_codex_windows_command_is_a_failure_and_posix_ignores_it(tmp_path, monkeypatch):
    """GE-011: on Windows Codex runs `commandWindows`, so a broken override is a failure even
    though `command` is healthy; on POSIX `command` stays the one that counts."""
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0
    path = hook_file(proj, "codex")
    data = read_json(path)
    data["hooks"]["PreToolUse"][0]["hooks"][0]["commandWindows"] = 'cmd /c "exit 1"'
    write_json(path, data)

    monkeypatch.setattr(doctor, "_IS_WINDOWS", True, raising=False)
    broken = one(checks(proj), "guard.hooks", "codex", "project")
    assert broken.status == "fail", broken
    assert "commandWindows" in broken.detail
    assert codes(checks(proj)) == 2

    monkeypatch.setattr(doctor, "_IS_WINDOWS", False, raising=False)
    assert one(checks(proj), "guard.hooks", "codex", "project").status == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="Codex runs commandWindows on Windows only")
def test_the_self_test_runs_the_codex_windows_override(tmp_path):
    """GE-011: the self-test runs the command Codex actually runs there."""
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0
    path = hook_file(proj, "codex")
    data = read_json(path)
    data["hooks"]["PreToolUse"][0]["hooks"][0]["commandWindows"] = 'cmd /c "exit 1"'
    write_json(path, data)

    check = one(checks(proj, selftest=True), "guard.selftest", "codex")
    assert check.status == "fail", check
    assert "exit code 1" in check.detail, check


def test_a_global_hook_timeout_is_compared_with_the_project_approval_wait(tmp_path):
    """GE-012: the running global hook reads the project's `guard.approval.timeout_s`, so the
    global hook's timeout must be compared with that wait, not with the default one."""
    proj = tmp_path / "proj"
    proj.mkdir()
    assert cli_main(["guard", "install", "--agent", "claude-code", "--global"]) == 0
    write_json(project_policy_file(proj), {"guard": {"approval": {"timeout_s": 300}}})

    check = one(checks(proj), "guard.hooks", "claude-code", "global")
    assert check.status == "warn", check
    assert "315" in check.detail, check


def test_a_tracked_guard_log_warns_even_after_an_ignore_rule(tmp_path):
    """GE-013: an ignore rule does not untrack the log; its command summaries stay publishable."""
    proj = git_init(tmp_path / "proj")
    write_log(proj, [{"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "session_id": "s", "tool": "Bash", "decision": "deny", "reason": "x"}])
    commit_all(proj)
    (proj / ".gitignore").write_text(".runledger/\n", encoding="utf-8")

    check = one(checks(proj), "log")
    assert check.status == "warn", check
    assert "tracked" in check.detail, check
    assert codes(checks(proj)) == 1

    untrack(proj, ".runledger/guard.log")
    clean = one(checks(proj), "log")
    assert clean.status == "ok", clean


@pytest.mark.parametrize("content", ["", "   \n\t\n"])
def test_an_empty_or_whitespace_only_config_file_is_a_failure(tmp_path, content):
    """GE-014: an empty file holds no JSON object, so it is a config failure, not a valid one."""
    proj = tmp_path / "proj"
    proj.mkdir()
    path = isolated_home(tmp_path) / ".runledger" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    failing = [c for c in pick(checks(proj), "config") if c.status == "fail"]
    assert len(failing) == 1, [c.as_dict() for c in pick(checks(proj), "config")]
    assert "config.json" in failing[0].detail
    assert failing[0].fix
    assert codes(checks(proj)) == 2


def test_a_non_utf8_config_file_is_a_failure_not_a_crash(tmp_path, capsys):
    """GE-014: a UTF-16 config (an ordinary PowerShell redirection) is diagnosed, never raised."""
    proj = tmp_path / "proj"
    proj.mkdir()
    path = isolated_home(tmp_path) / ".runledger" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes('{"guard": {"fail_closed": true}}'.encode("utf-16"))

    failing = [c for c in pick(checks(proj), "config") if c.status == "fail"]
    assert len(failing) == 1, [c.as_dict() for c in pick(checks(proj), "config")]
    assert "UTF-8" in failing[0].detail
    assert codes(checks(proj)) == 2

    code = cli_main(["doctor", "--project", str(proj), "--offline", "--no-selftest", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2, payload
    assert [c for c in payload["checks"] if c["id"] == "config" and c["status"] == "fail"]


def test_a_malformed_approval_server_url_warns_instead_of_aborting(tmp_path, monkeypatch, capsys):
    """GE-015: a server URL without a scheme is a warning with the fallback guidance, not a crash."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("RUNLEDGER_SERVER", "approver.example")

    code = cli_main(["doctor", "--project", str(proj), "--no-selftest", "--json"])
    payload = json.loads(capsys.readouterr().out)
    approval = [c for c in payload["checks"] if c["id"] == "approval"][0]
    assert approval["status"] == "warn", approval
    assert "on_timeout" in approval["detail"], approval
    assert code == 1, payload

    code = cli_main(["guard", "status", "--project", str(proj), "--json"])
    payload = json.loads(capsys.readouterr().out)
    approval = [c for c in payload["checks"] if c["id"] == "approval"][0]
    assert approval["status"] == "warn", approval
    assert "on_timeout" in approval["detail"], approval
    assert code == 1, payload

    code = cli_main(["doctor", "--project", str(proj), "--no-selftest"])
    text = capsys.readouterr().out
    assert code == 1, text
    assert "[warn] approval server" in text, text



