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
