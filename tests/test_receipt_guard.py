"""Local Guard decisions, transcript matching, outcomes and receipt formats."""
import io
import json
from pathlib import Path

import pytest

from runledger.cli import build, main
from runledger.guard import _summary
from runledger.guard_receipt import _MAX_LINE, load_guard_decisions
from runledger.parser import parse_session
from runledger.receipt import redact_run, render

FIXTURES = Path(__file__).parent / "fixtures" / "guard"


@pytest.fixture
def session(tmp_path):
    path = tmp_path / "session.jsonl"
    events = [json.loads(line) for line in
              (FIXTURES / "session.jsonl").read_text(encoding="utf-8").splitlines()]
    events[0]["cwd"] = str(tmp_path)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    log = tmp_path / ".runledger" / "guard.log"
    log.parent.mkdir()
    log.write_bytes((FIXTURES / "guard.log").read_bytes())
    return path, log


def test_fixture_decisions_match_steps_and_outcomes(session):
    path, _ = session
    run = parse_session(path)
    decisions = load_guard_decisions(run)
    assert [(d["step"], d["decision"], d["outcome"]) for d in decisions] == [
        (1, "deny", "Blocked by Guard"),
        (2, "ask", "Approved by user"),
        (3, "ask", "Rejected by user"),
        (4, "allow", "Approved by approval server"),
        (5, "ask", "Approved by user"),
        (6, "ask", "Rejected by user"),
        (7, "ask", "Rejected by user"),
        (8, "deny", "Denied by approval server"),
        (9, "allow", "Would have been blocked (monitor mode)"),
        (10, "allow", "Would have been asked (monitor mode)"),
        (None, "deny", "Blocked by Guard"),
        (None, "ask", "Rejected by user"),
    ]
    assert set(decisions[0]) == {
        "step", "tool", "summary", "decision", "outcome", "reason", "codes", "ts"}
    assert decisions[0]["ts"] == "2026-10-15T09:00:02Z"
    assert decisions[0]["codes"] == ["command"]
    assert run.steps[4].has_result and run.steps[4].result_text == ""
    assert not run.steps[6].has_result


def test_id_match_does_not_require_the_same_summary(session):
    path, log = session
    log.write_text(json.dumps({
        "session_id": "guard-session", "tool_use_id": "approved",
        "tool": "Read", "summary": "a differently truncated summary", "decision": "ask",
    }), encoding="utf-8")
    decision, = load_guard_decisions(parse_session(path))
    assert decision["step"] == 2 and decision["outcome"] == "Approved by user"


def test_legacy_match_uses_guard_summary_and_skips_consumed_steps(session):
    path, log = session
    run = parse_session(path)
    command = "sudo echo token=synthetic-secret-123456 " + "x" * 300
    run.steps[4].input = run.steps[5].input = {"command": command}
    # Same summary on a different tool must not steal the match.
    run.steps[0].tool = "PowerShell"
    run.steps[0].input = {"command": command}
    records = [
        {"tool_use_id": "legacy-1", "summary": "id is authoritative"},
        {"summary": _summary("Bash", {"command": command})},
        {"summary": _summary("Bash", {"command": command})},
    ]
    log.write_text("\n".join(json.dumps(dict(
        r, session_id=run.session_id, tool="Bash", decision="ask")) for r in records),
        encoding="utf-8")
    assert [d["step"] for d in load_guard_decisions(run)] == [5, 6, None]


@pytest.mark.parametrize("message", [
    "The user doesn't want to proceed with this tool use. The tool use was rejected.",
    "The user does not want to proceed with this tool use.",
    "User rejected tool use",
    "Tool call denied by the user",
    "Error: Permission to use Read has been denied.",
    "<tool_use_error>Permission to use Bash was denied.</tool_use_error>",
    "Request interrupted by user",
])
def test_claude_user_rejection_messages(session, message):
    path, _ = session
    run = parse_session(path)
    run.steps[1].result_text = message
    assert load_guard_decisions(run)[1]["outcome"] == "Rejected by user"


@pytest.mark.parametrize("message,is_error", [
    ("", False), ("Permission denied", True), ("command exited with code 1", True),
    ("The test output mentions User rejected tool use", False),
])
def test_normal_results_including_execution_errors_mean_user_approved(session, message, is_error):
    path, _ = session
    run = parse_session(path)
    run.steps[1].result_text = message
    run.steps[1].is_error = is_error
    assert load_guard_decisions(run)[1]["outcome"] == "Approved by user"


@pytest.mark.parametrize("fmt", ["html", "md", "json"])
def test_build_and_receipt_formats_include_guard_decisions(session, fmt):
    path, _ = session
    run, score, level, risks, note = build(str(path))
    assert note is None
    output = render(run, score, level, risks, fmt)
    if fmt == "json":
        data = json.loads(output)
        assert data["guard"] == run.guard
        assert data["totals"]["guard"] == {
            "blocked": 2, "asked": 8, "approved": 3, "rejected": 5,
            "monitor_blocked": 1, "monitor_asked": 1}
    else:
        assert "Guard decisions" in output
        assert "2 blocked, 8 asked (3 approved, 5 rejected)" in output
        assert "unmatched" in output
        for decision in run.guard:
            assert decision["outcome"] in output
        if fmt == "html":
            assert 'class="guard-decisions"><div class="card">' in output


def test_receipt_cli_reads_run_cwd_instead_of_current_folder(session, tmp_path, monkeypatch, capsys):
    path, _ = session
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert main(["receipt", str(path), "--format", "json", "-o", "-"]) == 0
    assert len(json.loads(capsys.readouterr().out)["guard"]) == 12


@pytest.mark.parametrize("log_state", ["missing", "corrupt", "other-session", "plain-allow"])
def test_no_records_omits_guard_section_and_counts(session, log_state):
    path, log = session
    if log_state == "missing":
        log.unlink()
    elif log_state == "corrupt":
        log.write_text('invalid\nnull\n[]\n{"session_id":', encoding="utf-8")
    else:
        log.write_text(json.dumps({
            "session_id": "other" if log_state == "other-session" else "guard-session",
            "tool": "Bash", "summary": "ls",
            "decision": "deny" if log_state == "other-session" else "allow",
        }), encoding="utf-8")
    run, score, level, risks, _ = build(str(path))
    assert run.guard == []
    data = json.loads(render(run, score, level, risks, "json"))
    assert "guard" not in data and "guard" not in data["totals"]
    for fmt in ("html", "md"):
        output = render(run, score, level, risks, fmt)
        assert "Guard decisions" not in output and "Guard:" not in output


def test_missing_cwd_and_unreadable_log_are_safe(session, monkeypatch):
    path, log = session
    run = parse_session(path)
    original_open = Path.open

    def unreadable(self, *args, **kwargs):
        if self == log:
            raise PermissionError("unreadable")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    assert load_guard_decisions(run) == []
    run.cwd = None
    assert load_guard_decisions(run) == []


def test_streams_log_and_skips_corrupt_and_oversized_lines(session, monkeypatch):
    path, log = session
    run = parse_session(path)
    valid = log.read_text(encoding="utf-8")
    content = ('bad\nnull\n[]\n' + '{"session_id":"guard-session","decision":[]}\n'
               + "[" * 2000 + "\n" + "x" * (_MAX_LINE * 3) + "\n" + valid)

    class StreamingOnly(io.StringIO):
        def read(self, *args):
            raise AssertionError("must not read the whole log")

        def readlines(self, *args):
            raise AssertionError("must not collect the whole log")

        def readline(self, size=-1):
            assert 0 < size <= _MAX_LINE + 1
            return super().readline(size)

    original_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **kw:
                        StreamingOnly(content) if self == log else original_open(self, *a, **kw))
    assert len(load_guard_decisions(run)) == 12


def test_invalid_utf8_and_partial_last_line_do_not_hide_valid_records(session):
    path, log = session
    with log.open("ab") as fh:
        fh.write(b'\xffbad\n{"session_id":')
    assert len(load_guard_decisions(parse_session(path))) == 12


@pytest.mark.parametrize("decision,approval,outcome", [
    ("ask", "approved", "Approved by approval server"),
    ("ask", "denied", "Denied by approval server"),
    ("deny", "expired", "Blocked by Guard"),
    ("deny", "timeout", "Blocked by Guard"),
    ("ask", "timeout", "Approved by user"),
    ("ask", "unavailable", "Approved by user"),
])
def test_server_status_and_local_fallback_outcomes(session, decision, approval, outcome):
    path, log = session
    log.write_text(json.dumps({
        "session_id": "guard-session", "tool_use_id": "approved", "tool": "Read",
        "summary": ".env", "decision": decision, "approval": approval,
    }), encoding="utf-8")
    record, = load_guard_decisions(parse_session(path))
    assert record["outcome"] == outcome


def test_guard_text_is_redacted_and_escaped_in_receipts(session):
    path, log = session
    secret = "sk-ant-api03-" + "x" * 30  # synthetic
    hostile = '<script>alert("guard")</script>'
    log.write_text(json.dumps({
        "session_id": "guard-session", "tool": "Bash", "decision": "deny",
        "summary": f"{hostile} | echo {secret}",
        "reason": f"{hostile} | token={secret}",
        "codes": ["command"],
    }), encoding="utf-8")
    run = parse_session(path)
    run.guard = load_guard_decisions(run)
    assert secret not in json.dumps(run.guard)
    for fmt in ("html", "md", "json"):
        output = render(run, 0, "Low", [], fmt)
        assert secret not in output
        assert "[REDACTED]" in output
        if fmt != "json":
            assert "<script>" not in output
            assert "&lt;script&gt;" in output
    # The general receipt redaction boundary also covers records supplied in memory.
    run.guard[0]["reason"] = secret
    redact_run(run, [])
    assert secret not in run.guard[0]["reason"]


# ---------------------------------------------------------------- native agents

DROID_SESSION_ID = "a96d6f95-c82e-463d-947c-e0cf04eb7fac"
CODEX_SESSION_ID = "01a148d0-fad4-7553-891d-c68363b89cf2"


def _write_log(project, rows):
    log = Path(project) / ".runledger" / "guard.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return log


def _droid_run(tmp_path, command="Remove-Item -Recurse -Force .\\victim"):
    """A Droid transcript: the call is `Execute`, the step the adapter builds is `Bash`."""
    from runledger.adapters import droid as droid_adapter

    project = tmp_path / "droid-proj"
    project.mkdir()
    lines = [
        {"type": "session_start", "id": DROID_SESSION_ID, "title": "Delete the victim folder",
         "cwd": str(project), "hostId": "host-example", "version": 1},
        {"type": "message", "id": "request", "timestamp": "2026-10-19T10:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "Delete the victim folder."}]}},
        {"type": "message", "id": "call", "timestamp": "2026-10-19T10:00:01Z",
         "message": {"role": "assistant", "modelId": "claude-haiku-4-5", "content": [
             {"type": "tool_use", "id": "call-droid-1", "name": "Execute",
              "input": {"command": command, "summary": "Delete the victim folder", "riskLevel": "high"}}]}},
        {"type": "message", "id": "result", "timestamp": "2026-10-19T10:00:02Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call-droid-1", "is_error": True,
              "content": "Blocked by RunLedger guard: Recursive force delete (Remove-Item -Recurse -Force)"}]}},
        {"type": "message", "id": "answer", "timestamp": "2026-10-19T10:00:03Z",
         "message": {"role": "assistant", "openaiPhase": "final_answer",
                     "content": [{"type": "text", "text": "Blocked by the guard."}]}},
    ]
    session = project / (DROID_SESSION_ID + ".jsonl")
    session.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return droid_adapter.parse(session), project


def _codex_run(tmp_path, command="Remove-Item -Recurse -Force victim", call_id="exec-1"):
    """A Codex rollout: on Windows the shell call is a PowerShell argv, and the hook sees
    the tool name `Bash`."""
    from runledger.adapters import codex as codex_adapter

    project = tmp_path / "codex-proj"
    project.mkdir()
    lines = [
        {"timestamp": "2026-10-19T11:00:00.000Z", "type": "session_meta",
         "payload": {"id": CODEX_SESSION_ID, "cwd": str(project), "model": "gpt-6-luna"}},
        {"timestamp": "2026-10-19T11:00:01.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": call_id,
                     "arguments": json.dumps({"command": ["powershell.exe", "-Command", command]})}},
        {"timestamp": "2026-10-19T11:00:02.000Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": call_id,
                     "output": json.dumps({"output": "blocked by RunLedger guard", "metadata": {"exit_code": 1}})}},
    ]
    session = project / ("rollout-2026-10-19T11-00-00-" + CODEX_SESSION_ID + ".jsonl")
    session.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return codex_adapter.parse(session), project


def test_droid_execute_row_links_the_bash_step(tmp_path, capsys):
    run, project = _droid_run(tmp_path)
    _write_log(project, [{
        "ts": "2026-10-19T10:00:01Z", "session_id": DROID_SESSION_ID, "tool_use_id": "",
        "tool": "Execute", "agent": "droid", "canonical_tool": "Bash",
        "decision": "deny", "policy_decision": "deny", "codes": ["command"],
        "summary": "Remove-Item -Recurse -Force .\\victim",
        "reason": "Blocked by RunLedger guard: Recursive force delete (Remove-Item -Recurse -Force)",
    }])
    assert [s.tool for s in run.steps] == ["Bash"]
    decision, = load_guard_decisions(run)
    assert decision["step"] == 1 and decision["decision"] == "deny"
    assert decision["outcome"] == "Blocked by Guard"

    run.guard = load_guard_decisions(run)
    data = json.loads(render(run, 0, "Low", [], "json"))
    assert data["guard"][0]["step"] == 1
    assert data["totals"]["guard"]["blocked"] == 1
    text = render(run, 0, "Low", [], "md")
    assert "Guard decisions" in text and "Blocked by Guard" in text


def test_codex_bash_row_links_the_powershell_step(tmp_path):
    run, project = _codex_run(tmp_path)
    _write_log(project, [{
        "ts": "2026-10-19T11:00:01Z", "session_id": CODEX_SESSION_ID,
        "tool_use_id": "exec-2f4c", "tool": "Bash", "agent": "codex", "canonical_tool": "Bash",
        "decision": "deny", "policy_decision": "deny", "codes": ["command"],
        "summary": "Remove-Item -Recurse -Force victim",
        "reason": "Blocked by RunLedger guard: Recursive force delete (Remove-Item -Recurse -Force)",
    }])
    assert [s.tool for s in run.steps] == ["PowerShell"]
    decision, = load_guard_decisions(run)
    assert decision["step"] == 1 and decision["outcome"] == "Blocked by Guard"


def test_codex_row_links_by_the_recorded_tool_use_id(tmp_path):
    run, project = _codex_run(tmp_path, call_id="exec-same-id")
    _write_log(project, [{
        "ts": "2026-10-19T11:00:01Z", "session_id": CODEX_SESSION_ID,
        "tool_use_id": "exec-same-id", "tool": "Bash", "agent": "codex", "canonical_tool": "Bash",
        "decision": "allow", "policy_decision": "ask", "approval": "approved",
        "summary": "a differently truncated summary",
    }])
    decision, = load_guard_decisions(run)
    assert decision["step"] == 1 and decision["outcome"] == "Approved by approval server"


def _codex_code_mode_run(tmp_path, command="rm -rf victim", call_id="ctc-1", script=None):
    """A Codex code-mode `exec` call whose script scans a regular expression holding a quote
    before it really calls `tools.exec_command`."""
    from runledger.adapters import codex as codex_adapter

    project = tmp_path / "codex-code-proj"
    project.mkdir()
    if script is None:
        script = ('const stripQuotes = /\\"/g;\n'
                  'const r = await tools.exec_command({cmd:"' + command + '",shell:"bash"});\n'
                  'text(r.output);')
    lines = [
        {"timestamp": "2026-10-19T13:00:00.000Z", "type": "session_meta",
         "payload": {"id": CODEX_SESSION_ID, "cwd": str(project), "model": "gpt-6-luna"}},
        {"timestamp": "2026-10-19T13:00:01.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec", "call_id": call_id,
                     "id": "ctc_" + call_id, "status": "completed", "input": script}},
        {"timestamp": "2026-10-19T13:00:02.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                     "output": [{"type": "input_text", "text": "blocked by RunLedger guard"}]}},
    ]
    session = project / ("rollout-2026-10-19T13-00-00-" + CODEX_SESSION_ID + ".jsonl")
    session.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return codex_adapter.parse(session), project


def test_codex_code_mode_row_links_the_step_behind_a_regex_literal(tmp_path):
    """GE-018: a quote inside a regex literal before the real call cannot hide the shell step,
    so the guard decision that blocked it still links to that step."""
    run, project = _codex_code_mode_run(tmp_path)
    assert [(s.tool, s.input) for s in run.steps] == [("Bash", {"command": "rm -rf victim"})]
    _write_log(project, [{
        "ts": "2026-10-19T13:00:01Z", "session_id": CODEX_SESSION_ID,
        "tool_use_id": "ctc-1", "tool": "Bash", "agent": "codex", "canonical_tool": "Bash",
        "decision": "deny", "policy_decision": "deny", "codes": ["command"],
        "summary": "rm -rf victim",
        "reason": "Blocked by RunLedger guard: Recursive force delete (rm -rf)",
    }])
    decision, = load_guard_decisions(run)
    assert decision["step"] == 1 and decision["outcome"] == "Blocked by Guard"


def test_codex_code_mode_row_links_the_step_behind_a_postfix_increment(tmp_path):
    """GE-018 round-3: a division after `n++` used to be read as a regex literal, which swallowed
    the shell call and left the blocked row with no step. The uncertain scan is searched as raw
    text now, so the deletion keeps its step and the guard row keeps linking to it."""
    run, project = _codex_code_mode_run(tmp_path, script=(
        'let n = 1;\n'
        'n++ / (await tools.exec_command({cmd:"rm -rf victim",shell:"bash"})).exit_code / 2;\n'))
    assert [(s.tool, s.input) for s in run.steps] == [("Bash", {"command": "rm -rf victim"})]
    _write_log(project, [{
        "ts": "2026-10-19T13:00:01Z", "session_id": CODEX_SESSION_ID,
        "tool_use_id": "ctc-1", "tool": "Bash", "agent": "codex", "canonical_tool": "Bash",
        "decision": "deny", "policy_decision": "deny", "codes": ["command"],
        "summary": "rm -rf victim",
        "reason": "Blocked by RunLedger guard: Recursive force delete (rm -rf)",
    }])
    decision, = load_guard_decisions(run)
    assert decision["step"] == 1 and decision["outcome"] == "Blocked by Guard"


def _codex_shell_switch_run(tmp_path, command="Get-Content .env"):
    """A Codex rollout where the same command ran twice, first through PowerShell, then Bash:
    a code-mode retry after a block. The hook sees `Bash` for both calls."""
    from runledger.adapters import codex as codex_adapter

    project = tmp_path / "codex-switch-proj"
    project.mkdir()
    lines = [
        {"timestamp": "2026-10-19T12:00:00.000Z", "type": "session_meta",
         "payload": {"id": CODEX_SESSION_ID, "cwd": str(project), "model": "gpt-6-luna"}},
        {"timestamp": "2026-10-19T12:00:01.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "call-1",
                     "arguments": json.dumps({"command": ["powershell.exe", "-Command", command]})}},
        {"timestamp": "2026-10-19T12:00:02.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "call-2",
                     "arguments": json.dumps({"command": command})}},
    ]
    session = project / ("rollout-2026-10-19T12-00-00-" + CODEX_SESSION_ID + ".jsonl")
    session.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return codex_adapter.parse(session), project


def test_repeated_command_across_shells_keeps_block_and_approval_in_order(tmp_path):
    """The cross-shell fallback takes the oldest matching step, not the first nonempty shell
    queue: the blocked first call keeps step 1 (PowerShell) and its later retry step 2 (Bash),
    even though both hook rows name Bash (GE-007)."""
    run, project = _codex_shell_switch_run(tmp_path)
    assert [(s.index, s.tool) for s in run.steps] == [(1, "PowerShell"), (2, "Bash")]
    _write_log(project, [
        {"ts": "2026-10-19T12:00:01Z", "session_id": CODEX_SESSION_ID,
         "tool_use_id": "exec-hook-1", "tool": "Bash", "agent": "codex", "canonical_tool": "Bash",
         "decision": "deny", "policy_decision": "deny", "codes": ["command"],
         "summary": "Get-Content .env", "reason": "Blocked by RunLedger guard: secret file (.env)"},
        {"ts": "2026-10-19T12:00:02Z", "session_id": CODEX_SESSION_ID,
         "tool_use_id": "exec-hook-2", "tool": "Bash", "agent": "codex", "canonical_tool": "Bash",
         "decision": "allow", "policy_decision": "ask", "approval": "approved",
         "summary": "Get-Content .env", "reason": "Approved for this once"},
    ])
    assert [(d["step"], d["outcome"]) for d in load_guard_decisions(run)] == [
        (1, "Blocked by Guard"),
        (2, "Approved by approval server"),
    ]


def test_rows_without_an_agent_keep_the_strict_tool_match(session):
    """v0.6.0 rows keep matching exactly; only rows that name their agent may cross the
    shell tool names the adapters use."""
    path, log = session
    run = parse_session(path)
    command = "Remove-Item -Recurse -Force victim"
    run.steps[0].tool = "PowerShell"
    run.steps[0].input = {"command": command}
    log.write_text("\n".join(json.dumps(r) for r in [
        dict({"tool": "Bash", "summary": command, "decision": "deny"}, session_id=run.session_id),
        dict({"tool": "Bash", "summary": command, "decision": "deny", "agent": "codex"},
             session_id=run.session_id),
    ]) + "\n", encoding="utf-8")
    assert [d["step"] for d in load_guard_decisions(run)] == [None, 1]


def test_receipt_reads_root_and_legacy_subfolder_guard_logs_with_dedup(tmp_path, capsys):
    from runledger import guard

    r = tmp_path / "R"
    pkg = r / "pkg"
    pkg.mkdir(parents=True)
    (r / ".git").mkdir()

    # Run hook from cwd=R/pkg with session_id="s-guard1" -> logs to R/.runledger/guard.log
    ev1 = {
        "session_id": "s-guard1", "cwd": str(pkg), "hook_event_name": "PreToolUse",
        "tool_name": "Bash", "tool_use_id": "t-deny-1", "tool_input": {"command": "rm -rf /"},
    }
    guard.handle(json.dumps(ev1))
    root_log = r / ".runledger" / "guard.log"
    assert root_log.exists()
    assert not (pkg / ".runledger" / "guard.log").exists()
    root_line = root_log.read_text(encoding="utf-8").splitlines()[0]

    session_file = tmp_path / "s-guard1.jsonl"
    session_file.write_text(json.dumps({
        "type": "assistant", "sessionId": "s-guard1", "cwd": str(pkg),
        "message": {"id": "m1", "content": [{
            "type": "tool_use", "id": "t-deny-1", "name": "Bash",
            "input": {"command": "rm -rf /"},
        }]},
    }) + "\n", encoding="utf-8")

    assert main(["receipt", str(session_file), "--format", "json", "-o", "-"]) == 0
    receipt1 = json.loads(capsys.readouterr().out)
    assert len(receipt1["guard"]) == 1
    assert receipt1["guard"][0]["decision"] == "deny"
    assert receipt1["guard"][0]["outcome"] == "Blocked by Guard"
    assert receipt1["totals"]["guard"]["blocked"] == 1

    # Add a legacy log at R/pkg/.runledger/guard.log with a second decision + exact copy of root_line
    legacy_log = pkg / ".runledger" / "guard.log"
    legacy_log.parent.mkdir(parents=True)
    second_rec = json.dumps({
        "ts": "2026-04-20T12:00:05+00:00",
        "session_id": "s-guard1",
        "tool_use_id": "t-deny-2",
        "tool": "Bash",
        "summary": "rm -rf /var/data",
        "mode": "enforce",
        "decision": "deny",
        "policy_decision": "deny",
        "approval": None,
        "codes": ["command"],
        "reason": "Recursive force delete (rm -rf /)",
    })
    legacy_log.write_text(root_line + "\n" + second_rec + "\n", encoding="utf-8")

    assert main(["receipt", str(session_file), "--format", "json", "-o", "-"]) == 0
    receipt2 = json.loads(capsys.readouterr().out)
    assert len(receipt2["guard"]) == 2
    assert [g["summary"] for g in receipt2["guard"]] == ["rm -rf /", "rm -rf /var/data"]
    assert receipt2["totals"]["guard"]["blocked"] == 2

