"""Quality score (runledger/quality.py): test-output parsing, every signal, clamping,
grades, missing data and the sample fixture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from runledger import adapters
from runledger.parser import Run, Step, Usage
from runledger.pricing import apply_costs
from runledger.quality import (
    _is_test_command,
    analyze,
    grade_for,
    parse_test_output,
    score_run,
)
from runledger.risk import Risk, assess
from runledger.summarize import apply_templates

FIXTURES = Path(__file__).parent / "fixtures"
SONNET = "claude-sonnet-5-5"
ROOT = "/work/app"


def _step(index, tool, inp=None, result="", is_error=False, model=SONNET, usage=None):
    return Step(index=index, tool=tool, input=dict(inp or {}), tool_use_id=f"t{index}", model=model,
                timestamp=None, usage=usage or Usage(), result_text=result, is_error=is_error)


def _bash(index, command, result="", is_error=False, **kw):
    return _step(index, "Bash", {"command": command}, result, is_error, **kw)


def _write(index, path, lines=1):
    return _step(index, "Write", {"file_path": f"{ROOT}/{path}", "content": "x = 1\n" * lines})


def _run(steps=(), prompts=("Fix the retry logic in src/payments/retry.ts.",), final="Done."):
    steps = list(steps)
    models = {}
    for s in steps:
        models.setdefault(s.model or "unknown", Usage()).add(s.usage)
    total = Usage()
    for usage in models.values():
        total.add(usage)
    return Run(session_id="s1", path="x.jsonl", cwd=ROOT, git_branch=None, started=None, ended=None,
               prompts=list(prompts), steps=steps, final_message=final, usage=total, models=models)


def _sig(quality, name):
    found = [s for s in quality["signals"] if s["name"] == name]
    return found[0] if found else None


# ---------------------------------------------------------------- test output parsing

def test_pytest_summary_counts_passed_and_failed():
    assert parse_test_output("============ 12 passed in 0.31s ============", "pytest") == (12, 0)
    assert parse_test_output("=== 1 failed, 11 passed, 2 warnings in 0.50s ===", "pytest") == (11, 1)


def test_pytest_errors_count_as_failures():
    assert parse_test_output("== 3 passed, 1 error in 0.9s ==", "pytest") == (3, 1)


def test_jest_counts_the_tests_line_not_the_suites_line():
    text = "Test Suites: 3 passed, 3 total\nTests:       1 failed, 141 passed, 142 total"
    assert parse_test_output(text, "jest") == (141, 1)


def test_vitest_tests_line_is_read():
    assert parse_test_output("      Tests  1 failed | 141 passed (142)", "vitest") == (141, 1)


def test_mocha_passing_and_failing_lines():
    assert parse_test_output("  10 passing (1s)\n  2 failing", "mocha") == (10, 2)


def test_go_test_counts_packages_including_build_failures():
    text = "ok  \tacme/payments\t0.210s\nFAIL\tacme/ledger\t0.301s\nFAIL\tacme/api [build failed]\n"
    assert parse_test_output(text, "go test") == (1, 2)


def test_cargo_test_sums_every_test_binary():
    text = ("running 5 tests\ntest result: ok. 5 passed; 0 failed; 0 ignored\n\n"
            "running 3 tests\ntest result: FAILED. 2 passed; 1 failed; 0 ignored\n")
    assert parse_test_output(text, "cargo test") == (7, 1)


def test_unittest_failures_and_success():
    assert parse_test_output("Ran 5 tests in 0.002s\n\nFAILED (failures=2)", "unittest") == (3, 2)
    assert parse_test_output("Ran 4 tests in 0.001s\n\nOK", "unittest") == (4, 0)


def test_output_without_a_summary_is_none():
    assert parse_test_output("collecting ... done", "pytest") is None
    assert parse_test_output("", "pytest") is None


@pytest.mark.parametrize("command", [
    "npm test", "pnpm run test:unit", "yarn test", "go test ./...", "cargo test --all",
    "python -m pytest -q", "pytest tests/test_x.py", "cat ~/.npmrc && npm test",
])
def test_test_commands_are_recognised(command):
    assert _is_test_command(command)


@pytest.mark.parametrize("command", [
    "grep pytest README.md", "git log --grep=test", "npm install p-retry", "echo npm test", "ls tests/",
])
def test_other_commands_are_not_test_runs(command):
    assert not _is_test_command(command)


# ---------------------------------------------------------------- test signals

def test_passing_suite_adds_points_and_reports_counts():
    q = score_run(_run([_bash(1, "npm test", result="Tests: 142 passed, 142 total")]), [])
    assert _sig(q, "tests_run")["impact"] == 10
    assert _sig(q, "test_outcome") == {
        "name": "test_outcome", "value": "pass", "impact": 15,
        "note": "The last test run passed (142 passed, 0 failed).",
    }
    assert q["score"] == 100 and q["grade"] == "A"


def test_failed_command_without_a_summary_counts_as_failing():
    q = score_run(_run([_bash(1, "npm test", result="sh: jest: not found", is_error=True)]), [])
    assert _sig(q, "test_outcome")["value"] == "fail"
    assert _sig(q, "test_outcome")["impact"] == -15


def test_exit_zero_with_output_but_no_summary_counts_as_passing():
    q = score_run(_run([_bash(1, "pytest -q", result="all good")]), [])
    assert _sig(q, "test_outcome")["value"] == "pass"
    assert "exit code 0" in _sig(q, "test_outcome")["note"]


def test_test_run_without_any_result_is_not_measured():
    q = score_run(_run([_bash(1, "npm test", result="")]), [])
    assert _sig(q, "tests_run")["impact"] == 10
    assert "no readable result" in _sig(q, "tests_run")["note"]
    assert _sig(q, "test_outcome") is None


def test_fail_then_pass_is_a_recovery():
    steps = [
        _bash(1, "npm test", result="Tests: 1 failed, 141 passed, 142 total", is_error=True),
        _step(2, "Edit", {"file_path": f"{ROOT}/src/a.ts", "old_string": "x", "new_string": "y"}),
        _bash(3, "npm test", result="Tests: 142 passed, 142 total"),
    ]
    q = score_run(_run(steps), [])
    assert _sig(q, "test_outcome")["impact"] == 15
    assert _sig(q, "failing_then_passing")["value"] == 1
    assert _sig(q, "failing_then_passing")["impact"] == 5
    assert _sig(q, "failing_streak")["impact"] == 0


def test_three_failures_in_a_row_are_penalised():
    steps = [_bash(i, "pytest -q", result="1 failed, 3 passed in 0.1s", is_error=True) for i in (1, 2, 3)]
    q = score_run(_run(steps), [])
    assert _sig(q, "failing_streak") == {
        "name": "failing_streak", "value": 3, "impact": -10,
        "note": "Longest run of consecutive failing test runs: 3.",
    }
    assert _sig(q, "test_outcome")["impact"] == -15


def test_pass_then_fail_is_not_a_recovery():
    steps = [
        _bash(1, "npm test", result="1 failed, 9 passed", is_error=True),
        _bash(2, "npm test", result="10 passed"),
        _bash(3, "npm test", result="1 failed, 9 passed", is_error=True),
    ]
    sig = _sig(score_run(_run(steps), []), "failing_then_passing")
    assert sig["value"] == 1 and sig["impact"] == 0
    assert "last run failed" in sig["note"]


def test_changes_without_any_test_run_are_penalised():
    q = score_run(_run([_write(1, "src/a.py")]), [])
    assert _sig(q, "tests_run") == {
        "name": "tests_run", "value": 0, "impact": -10,
        "note": "1 file(s) changed and no test command ran.",
    }


def test_no_changes_and_no_tests_leaves_out_the_test_run_signal():
    q = score_run(_run([_step(1, "Read", {"file_path": f"{ROOT}/src/a.py"})]), [])
    assert _sig(q, "tests_run") is None
    assert _sig(q, "test_outcome") is None


def test_test_tampering_points_come_from_risk_codes():
    risks = [
        Risk("high", "test_deleted", "Deleted a test file (a.test.ts)", 1),
        Risk("high", "test_skipped", "Disabled tests in b.test.ts", 2),
        Risk("medium", "test_weakened", "Removed assertions from c.test.ts", 3),
    ]
    run = _run([_step(i, "Read", {"file_path": f"{ROOT}/x{i}.py"}) for i in (1, 2, 3)])
    sig = _sig(score_run(run, risks), "test_tampering")
    assert sig["value"] == 3
    assert sig["impact"] == -25  # 10 + 10 + 5, at the cap


def test_test_tampering_is_capped_and_ignores_low_fixture_deletions():
    skipped = [Risk("high", "test_skipped", "Disabled tests", i) for i in range(1, 5)]
    run = _run([_step(i, "Read", {"file_path": f"{ROOT}/x{i}.py"}) for i in range(1, 5)])
    assert _sig(score_run(run, skipped), "test_tampering")["impact"] == -25

    fixture = [Risk("low", "test_deleted", "Deleted a test fixture (a.json)", 1)]
    sig = _sig(score_run(run, fixture), "test_tampering")
    assert sig["value"] == 0 and sig["impact"] == 0


# ---------------------------------------------------------------- errors and loops

@pytest.mark.parametrize("errors, total, impact", [
    (0, 10, 5), (1, 10, -1), (2, 10, -7), (5, 10, -15), (10, 10, -15),
])
def test_error_rate_points(errors, total, impact):
    steps = [_step(i, "Read", {"file_path": f"{ROOT}/f{i}.py"}, is_error=i <= errors)
             for i in range(1, total + 1)]
    sig = _sig(score_run(_run(steps), []), "error_rate")
    assert sig["impact"] == impact
    assert sig["value"] == pytest.approx(errors / total, abs=1e-3)


def test_same_command_three_times_is_a_retry_loop():
    steps = [_bash(i, "make build", result="ok") for i in (1, 2, 3)]
    sig = _sig(score_run(_run(steps), []), "retry_loops")
    assert sig["value"] == 1 and sig["impact"] == -5


def test_two_repeats_are_not_a_loop():
    steps = [_bash(i, "make build", result="ok") for i in (1, 2)]
    sig = _sig(score_run(_run(steps), []), "retry_loops")
    assert sig["value"] == 0 and sig["impact"] == 0


def test_repeated_test_runs_are_not_retry_loops():
    steps = [_bash(i, "npm test", result="Tests: 1 passed, 1 total") for i in (1, 2, 3, 4)]
    assert _sig(score_run(_run(steps), []), "retry_loops")["value"] == 0


def test_file_edited_five_times_is_a_retry_loop():
    steps = [_step(i, "Edit", {"file_path": f"{ROOT}/src/a.py", "old_string": "a", "new_string": f"b{i}"})
             for i in range(1, 6)]
    sig = _sig(score_run(_run(steps), []), "retry_loops")
    assert sig["value"] == 1 and sig["impact"] == -5


def test_retry_loop_penalty_is_capped():
    steps = []
    for n, command in enumerate(["make a", "make b", "make c", "make d"]):
        steps += [_bash(4 * n + k, command, result="ok") for k in (1, 2, 3)]
    sig = _sig(score_run(_run(steps), []), "retry_loops")
    assert sig["value"] == 4 and sig["impact"] == -15


# ---------------------------------------------------------------- scope and final message

def test_unrelated_churn_is_a_mild_penalty():
    names = ["billing/tax.py", "reports/export.py", "ui/theme.css",
             "auth/session.py", "db/schema.sql", "docs/setup.md"]
    steps = [_write(i, n) for i, n in enumerate(names, 1)]
    sig = _sig(score_run(_run(steps, prompts=["Fix the invoice rounding bug."]), []), "scope")
    assert sig["value"] == 6
    assert sig["impact"] == -2  # 6 unrelated files: min(10, 6 - 4)


def test_related_files_have_no_scope_penalty():
    steps = [_write(1, "billing/invoice.py"), _write(2, "tests/test_invoice.py")]
    sig = _sig(score_run(_run(steps, prompts=["Fix the invoice rounding."]), []), "scope")
    assert sig["value"] == 0 and sig["impact"] == 0


def test_plural_words_match_the_request():
    sig = _sig(score_run(_run([_write(1, "src/prices.py")], prompts=["Fix the price lookup."]), []), "scope")
    assert sig["value"] == 0


def test_scope_needs_prompts():
    q = score_run(_run([_write(1, "src/a.py")], prompts=()), [])
    assert _sig(q, "scope") is None


@pytest.mark.parametrize("message", [
    "I couldn't finish the migration.", "Unable to reach the API.", "TODO: handle retries",
    "Not implemented for PostgreSQL.", "The change is incomplete.",
])
def test_unfinished_markers_cost_points(message):
    sig = _sig(score_run(_run(final=message), []), "unfinished")
    assert sig["impact"] == -10


def test_clean_final_message_has_no_penalty():
    sig = _sig(score_run(_run(final="All 12 tests pass."), []), "unfinished")
    assert sig["impact"] == 0 and sig["value"] == 0


def test_lowercase_todo_in_prose_is_not_a_marker():
    sig = _sig(score_run(_run(final="Added a todo list to the page."), []), "unfinished")
    assert sig["impact"] == 0


def test_missing_final_message_leaves_out_the_signal():
    assert _sig(score_run(_run(final=""), []), "unfinished") is None


# ---------------------------------------------------------------- cost and risk

@pytest.mark.parametrize("input_tokens, impact", [
    (50_000, 5),       # $0.10 over 10 lines: $0.01 per line
    (500_000, 0),      # $1.00: $0.10 per line
    (1_000_000, -5),   # $2.00: $0.20 per line
    (2_500_000, -10),  # $5.00: $0.50 per line
])
def test_cost_per_changed_line_points(input_tokens, impact):
    step = _write(1, "src/a.py", lines=10)
    step.usage = Usage(input_tokens=input_tokens)
    sig = _sig(score_run(_run([step]), []), "cost_efficiency")
    assert sig["impact"] == impact
    assert sig["value"] == pytest.approx(input_tokens * 2.0 / 1_000_000 / 10, abs=1e-4)


def test_cost_signal_needs_a_known_price():
    step = _write(1, "src/a.py", lines=10)
    step.model = "claude-mystery-9"
    step.usage = Usage(input_tokens=1_000_000)
    assert _sig(score_run(_run([step]), []), "cost_efficiency") is None


@pytest.mark.parametrize("risks, level, impact", [
    ([], "Low", 0),
    ([Risk("medium", "git_internals", "Wrote inside .git", 1)], "Low", 0),          # 15
    ([Risk("high", "secret_file", "Read .env", 1)], "Medium", -8),                  # 30
    ([Risk("high", "secret_file", "Read .env", 1), Risk("high", "write_outside", "x", 2)],
     "High", -20),                                                                  # 60
])
def test_risk_level_penalty(risks, level, impact):
    sig = _sig(score_run(_run(), risks), "risk_level")
    assert sig["value"] == level
    assert sig["impact"] == impact


# ---------------------------------------------------------------- score, grades, edge cases

def test_score_is_clamped_to_zero():
    steps = [_bash(i, "npm test", result="Tests: 5 failed, 0 passed", is_error=True) for i in range(1, 5)]
    risks = [Risk("high", "secret_file", "x", 1), Risk("high", "write_outside", "y", 2),
             Risk("high", "test_skipped", "z", 3)]
    q = score_run(_run(steps, final="Could not finish."), risks)
    assert q["score"] == 0 and q["grade"] == "F"


def test_score_is_clamped_to_one_hundred():
    steps = [_write(1, "src/a.py", lines=10), _bash(2, "npm test", result="Tests: 9 passed, 9 total")]
    steps[0].usage = Usage(input_tokens=50_000)
    q = score_run(_run(steps), [])
    assert q["score"] == 100 and q["grade"] == "A"


@pytest.mark.parametrize("score, grade", [
    (100, "A"), (85, "A"), (84, "B"), (70, "B"), (69, "C"), (55, "C"), (54, "D"), (40, "D"), (39, "F"), (0, "F"),
])
def test_grade_boundaries(score, grade):
    assert grade_for(score) == grade


def test_empty_run_is_neutral_and_does_not_crash():
    run = _run(steps=[], prompts=(), final="")
    q = score_run(run, [])
    assert q["score"] == 70 and q["grade"] == "B"
    assert [s["name"] for s in q["signals"]] == ["risk_level"]
    analyze(run, [])
    assert run.quality["score"] == 70
    assert run.recommendations == []


def test_missing_fields_never_crash():
    steps = [
        Step(index=1, tool="Bash", input={}, tool_use_id="", model=None, timestamp=None,
             result_text=None, is_error=False),
        Step(index=2, tool="Edit", input={"file_path": None}, tool_use_id="", model=None, timestamp=None),
        Step(index=3, tool="Write", input={"content": None}, tool_use_id="", model=None, timestamp=None),
        Step(index=4, tool="Bash", input={"command": None}, tool_use_id="", model=None, timestamp=None,
             result_text=None, is_error=True),
    ]
    run = _run(steps, prompts=[""], final="")
    run.final_message = None  # type: ignore[assignment]
    analyze(run)
    assert 0 <= run.quality["score"] <= 100
    assert run.quality["grade"] in "ABCDF"


def test_signal_schema_is_exact_and_serialisable():
    steps = [_write(1, "src/a.py", lines=3), _bash(2, "npm test", result="Tests: 3 passed, 3 total")]
    q = score_run(_run(steps, final="Done."), [])
    assert set(q) == {"score", "grade", "signals"}
    assert isinstance(q["score"], int) and q["grade"] in ("A", "B", "C", "D", "F")
    for sig in q["signals"]:
        assert set(sig) == {"name", "value", "impact", "note"}
        assert isinstance(sig["name"], str) and isinstance(sig["note"], str)
        assert isinstance(sig["impact"], int) and not isinstance(sig["impact"], bool)
        assert sig["value"] is None or (isinstance(sig["value"], (int, float, str))
                                        and not isinstance(sig["value"], bool))
    json.dumps(q)


def test_only_measurable_signals_are_listed():
    q = score_run(_run([_step(1, "Read", {"file_path": f"{ROOT}/src/a.py"})], prompts=(), final=""), [])
    names = [s["name"] for s in q["signals"]]
    assert "test_outcome" not in names and "scope" not in names and "unfinished" not in names
    assert "error_rate" in names and "risk_level" in names


def test_analyze_uses_the_steps_own_risks_when_none_are_given():
    step = _step(1, "Read", {"file_path": f"{ROOT}/src/a.py"})
    step.risks = [Risk("high", "secret_file", "Read .env", 1)]
    run = _run([step])
    analyze(run)
    assert _sig(run.quality, "risk_level")["value"] == "Medium"
    assert isinstance(run.recommendations, list)


# ---------------------------------------------------------------- sample fixture

def _analysed(path):
    run = adapters.detect(path).parse(path)
    apply_costs(run)
    apply_templates(run)
    _, _, risks = assess(run)
    analyze(run, risks)
    return run


def test_sample_session_quality_is_pinned():
    run = _analysed(FIXTURES / "sample_session.jsonl")
    assert run.quality["score"] == 74 and run.quality["grade"] == "B"
    impacts = {s["name"]: s["impact"] for s in run.quality["signals"]}
    assert impacts == {
        "tests_run": 10, "test_outcome": 15, "failing_then_passing": 5, "failing_streak": 0,
        "test_tampering": -10, "error_rate": -1, "retry_loops": 0, "scope": 0, "unfinished": 0,
        "cost_efficiency": 5, "risk_level": -20,
    }


def test_every_fixture_analyses_without_errors():
    for name in ["sample_session.jsonl", "codex_session.jsonl", "native_session.json",
                 "aider/.aider.chat.history.md"]:
        run = _analysed(FIXTURES / name)
        assert 0 <= run.quality["score"] <= 100, name
        assert run.quality["grade"] in ("A", "B", "C", "D", "F"), name
        assert isinstance(run.recommendations, list), name
