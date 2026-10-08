"""Deterministic session quality score (0-100), with the signals that explain it.

The score starts at 70 (neutral). Each signal that can be measured adds or subtracts
points, and the total is clamped to 0-100. Grades: A from 85, B from 70, C from 55,
D from 40, F below 40. No model is called: every point comes from the steps, their
results, the risk findings, the prompts and the final message, and each signal's
note says what it measured.

A signal is listed only when its input exists. A run with no test command has no
test_outcome signal; a run with no prompts has no scope signal; a run with no steps
has no tool-based signals. A run with nothing measurable still gets a score (70, B).

Signals and their points:
  tests_run             +10 when a test command ran; -10 when files changed and none did
  test_outcome          +15 the last readable test run passed; -15 it failed
  failing_then_passing  +5 a failing run followed by a passing run, ending green
  failing_streak        -5 per failing test run beyond the first in a row, at most -15
  test_tampering        -10 per deleted or skipped test file, -5 per weakened one, at most -25
  error_rate            +5 with no errored steps, down 60 points per 100%, at least -15
  retry_loops           -5 per non-test command repeated 3+ times or file edited 5+ times, at most -15
  scope                 -1 per unrelated changed file past the fourth, at most -10 (mild)
  unfinished            -10 the final message says work is unfinished
  cost_efficiency       +5 / 0 / -5 / -10 at up to $0.02 / $0.10 / $0.25 / more per changed line
  risk_level            -20 High, -8 Medium, 0 Low (the level from risk.py)

analyze(run, risks) sets run.quality and, through advisor.recommend, run.recommendations.
run.quality has this shape (see docs/analysis.md):
  {"score": int, "grade": str,
   "signals": [{"name": str, "value": number | str | null, "impact": int, "note": str}]}
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .advisor import estimated_cost, recommend
from .parser import Run, Step
from .risk import Risk, _score as _risk_score
from .summarize import file_changes

BASE_SCORE = 70
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
TEST_RISK_CODES = ("test_deleted", "test_skipped", "test_weakened")
LOOP_COMMAND_REPEATS = 3         # the same non-test command this many times is a loop
LOOP_FILE_EDITS = 5              # the same file edited this many times is a loop

_TEST_COMMAND = re.compile(
    r"\b(pytest|py\.test|unittest|jest|vitest|mocha|go\s+test|cargo\s+test"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test)\b", re.I)
# commands that print or search text and never run a test suite
_NOT_A_RUN = {
    "grep", "rg", "egrep", "cat", "echo", "printf", "head", "tail", "less", "more", "sed", "awk",
    "git", "type", "ls", "dir", "which", "where", "select-string", "write-output", "write-host",
    "findstr", "code", "notepad",
}
_RUNNERS = [
    ("go test", r"\bgo\s+test\b"),
    ("cargo test", r"\bcargo\s+test\b"),
    ("pytest", r"\bpy\.?test\b|pytest"),
    ("unittest", r"\bunittest\b"),
    ("vitest", r"\bvitest\b"),
    ("jest", r"\bjest\b"),
    ("mocha", r"\bmocha\b"),
]
_TESTS_LINE = re.compile(r"^\s*Tests:?\s+(.+)$", re.M)  # jest and vitest summary line
_UNFINISHED = [
    ("couldn't or could not finish", re.compile(r"couldn['’]t|could not", re.I)),
    ("unable to do something", re.compile(r"\bunable\b", re.I)),
    ("a TODO", re.compile(r"\bTODO\b")),
    ("not implemented", re.compile(r"not implemented", re.I)),
    ("incomplete or unfinished work", re.compile(r"\bincomplete\b|\bunfinished\b", re.I)),
]
# words too common to show a file belongs to the request
_GENERIC_PATH_WORDS = {
    "src", "lib", "app", "apps", "test", "tests", "spec", "specs", "index", "main", "utils", "util",
    "pkg", "internal", "components", "models", "types", "data", "docs", "doc", "readme", "file",
    "files", "code", "json", "yaml", "yml", "toml", "txt", "html", "css", "tsx", "jsx", "dist",
    "build", "new", "old", "tmp",
}


def grade_for(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _risk_level(score: int) -> str:
    """Same thresholds as risk.assess: Low below 25, Medium below 60, High otherwise."""
    return "Low" if score < 25 else "Medium" if score < 60 else "High"


def parse_test_output(text: str, runner: str = "") -> Optional[Tuple[int, int]]:
    """(passed, failed) read from a test command's output, or None when no summary is found.
    For go test the counts are packages, not individual tests."""
    if not text:
        return None
    if runner == "cargo test":
        results = re.findall(r"test result: \w+\. (\d+) passed; (\d+) failed", text)
        if results:
            return sum(int(p) for p, _ in results), sum(int(f) for _, f in results)
    elif runner == "go test":
        ok = len(re.findall(r"(?m)^ok\s+\S+", text))
        bad = len(re.findall(r"(?m)^FAIL\s+\S+", text))
        if ok or bad:
            return ok, bad
    elif runner == "unittest":
        ran = re.search(r"(?m)^Ran (\d+) tests?\b", text)
        if ran:
            total = int(ran.group(1))
            broken = re.search(r"(?m)^FAILED \(([^)]*)\)", text)
            if not broken:
                return total, 0
            bad = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", broken.group(1)))
            return max(0, total - bad), bad
    line = _TESTS_LINE.search(text)  # jest/vitest: count the "Tests:" line, not "Test Suites:"
    return _count(line.group(1) if line else text)


def _count(segment: str) -> Optional[Tuple[int, int]]:
    passed = re.findall(r"(\d+) (?:passed|passing)\b", segment)
    failed = re.findall(r"(\d+) (?:failed|failing|errors?)\b", segment)
    if not passed and not failed:
        return None
    return sum(int(n) for n in passed), sum(int(n) for n in failed)


def _runner(command: str) -> str:
    for label, pattern in _RUNNERS:
        if re.search(pattern, command, re.I):
            return label
    return "npm test" if re.search(r"\b(npm|pnpm|yarn|bun)\b", command, re.I) else "test command"


def _is_test_command(command: str) -> bool:
    for segment in re.split(r"&&|\|\||;|\||\n", command):
        words = segment.strip().split()
        if not words:
            continue
        head = words[0].lower().replace("\\", "/").rsplit("/", 1)[-1]
        if head in _NOT_A_RUN:
            continue
        if _TEST_COMMAND.search(segment):
            return True
    return False


@dataclass
class _TestRun:
    step: Step
    runner: str
    outcome: Optional[str]          # "pass", "fail", or None when the result cannot be read
    counts: Optional[Tuple[int, int]]

    @property
    def description(self) -> str:
        if self.counts is not None:
            passed, failed = self.counts
            unit = " packages" if self.runner == "go test" else ""
            return f"{passed} passed, {failed} failed{unit}"
        if self.step.is_error:
            return "the command failed with no summary line"
        return "exit code 0 with no summary line"


def _outcome(counts: Optional[Tuple[int, int]], step: Step) -> Optional[str]:
    if step.is_error or (counts is not None and counts[1] > 0):
        return "fail"
    if counts is not None and counts[0] > 0:
        return "pass"
    if counts is None and (step.result_text or "").strip():
        return "pass"  # exit code 0 and some output, but no summary line
    return None


def _test_runs(run: Run) -> List[_TestRun]:
    found: List[_TestRun] = []
    for s in run.steps:
        if s.tool not in SHELL_TOOLS:
            continue
        command = str(s.input.get("command") or "")
        if not _is_test_command(command):
            continue
        runner = _runner(command)
        counts = parse_test_output(s.result_text or "", runner)
        found.append(_TestRun(s, runner, _outcome(counts, s), counts))
    return found


def _shell_commands(run: Run) -> List[str]:
    out = []
    for s in run.steps:
        if s.tool in SHELL_TOOLS:
            text = " ".join(str(s.input.get("command") or "").split())
            if text:
                out.append(text)
    return out


def _stem(word: str) -> str:
    """Drop a plural 's' so 'prices' matches 'price' and 'tests' matches 'test'."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _unrelated_files(run: Run, paths: Sequence[str]) -> List[str]:
    """Changed files that share no word with the request. A file with no usable
    word in its path counts as related, since there is nothing to compare."""
    prompt_text = " ".join(run.prompts).lower()
    prompt_words = {_stem(w) for w in re.findall(r"[a-z0-9]+", prompt_text)}
    unrelated = []
    for path in paths:
        tokens = [_stem(t) for t in re.findall(r"[a-z0-9]+", path.lower())
                  if len(t) >= 3 and t not in _GENERIC_PATH_WORDS]
        if not tokens or path.lower() in prompt_text or any(t in prompt_words for t in tokens):
            continue
        unrelated.append(path)
    return unrelated


def score_run(run: Run, risks: Optional[Sequence[Risk]] = None) -> Dict[str, Any]:
    """The quality score and its signals. Never raises on missing data."""
    if risks is None:
        risks = [r for s in run.steps for r in s.risks]
    changes = file_changes(run)
    signals: List[Dict[str, Any]] = []

    def add(name: str, value: Any, impact: int, note: str) -> None:
        signals.append({"name": name, "value": value, "impact": int(impact), "note": note})

    # --- tests
    tests = _test_runs(run)
    measured = [t for t in tests if t.outcome is not None]
    if tests:
        unreadable = len(tests) - len(measured)
        note = f"{len(tests)} test command(s) ran in the session."
        if unreadable:
            note += f" {unreadable} of them had no readable result."
        add("tests_run", len(tests), 10, note)
    elif changes:
        add("tests_run", 0, -10, f"{len(changes)} file(s) changed and no test command ran.")

    if measured:
        last = measured[-1]
        if last.outcome == "pass":
            add("test_outcome", "pass", 15, f"The last test run passed ({last.description}).")
        else:
            add("test_outcome", "fail", -15, f"The last test run failed ({last.description}).")

        cycles = 0
        waiting = False
        streak = best = 0
        for t in measured:
            if t.outcome == "fail":
                waiting = True
                streak += 1
                best = max(best, streak)
            else:
                streak = 0
                if waiting:
                    cycles += 1
                    waiting = False
        if cycles:
            ends_green = measured[-1].outcome == "pass"
            add("failing_then_passing", cycles, 5 if ends_green else 0,
                f"Tests failed, the agent kept working, and a later run passed ({cycles} time(s))."
                if ends_green else
                f"Tests failed, then passed {cycles} time(s), but the last run failed.")
        if best:
            add("failing_streak", best, -min(15, 5 * (best - 1)),
                f"Longest run of consecutive failing test runs: {best}.")

    if run.steps:
        counted = 0
        points = 0
        for r in risks:
            if r.code not in TEST_RISK_CODES:
                continue
            if r.code == "test_weakened":
                points += 5
                counted += 1
            elif r.severity == "high":  # test_deleted or test_skipped
                points += 10
                counted += 1
        note = ("No test file was deleted, skipped or weakened." if not counted else
                f"{counted} test change(s) flagged: deleted, skipped or weakened.")
        add("test_tampering", counted, -min(25, points), note)

    # --- errors and loops
    if run.steps:
        errors = sum(1 for s in run.steps if s.is_error)
        rate = errors / len(run.steps)
        add("error_rate", round(rate, 3), max(-15, min(5, round(5 - 60 * rate))),
            f"{errors} of {len(run.steps)} steps returned an error.")

        command_counts = Counter(c for c in _shell_commands(run) if not _is_test_command(c))
        repeated = [c for c, n in command_counts.items() if n >= LOOP_COMMAND_REPEATS]
        edits: Dict[str, int] = {}
        for s in run.steps:
            if s.tool in WRITE_TOOLS and s.input.get("file_path"):
                edits[str(s.input["file_path"])] = edits.get(str(s.input["file_path"]), 0) + 1
        looping_files = [p for p, n in edits.items() if n >= LOOP_FILE_EDITS]
        loops = len(repeated) + len(looping_files)
        if loops:
            note = (f"{len(repeated)} command(s) repeated {LOOP_COMMAND_REPEATS}+ times and "
                    f"{len(looping_files)} file(s) edited {LOOP_FILE_EDITS}+ times.")
        else:
            note = (f"No command repeated {LOOP_COMMAND_REPEATS}+ times and no file was edited "
                    f"{LOOP_FILE_EDITS}+ times.")
        add("retry_loops", loops, -min(15, 5 * loops), note)

    # --- scope
    if run.prompts and changes:
        unrelated = _unrelated_files(run, list(changes))
        mild = len(unrelated) >= 5 and len(unrelated) > len(changes) / 2
        note = (f"{len(unrelated)} of {len(changes)} changed files share no word with the request."
                if unrelated else f"All {len(changes)} changed file(s) relate to the request.")
        add("scope", len(unrelated), -min(10, len(unrelated) - 4) if mild else 0, note)

    # --- final message
    final = (run.final_message or "").strip()
    if final:
        hits = [label for label, pattern in _UNFINISHED if pattern.search(final)]
        if hits:
            add("unfinished", len(hits), -10,
                f"The final message says work is unfinished: {'; '.join(hits)}.")
        else:
            add("unfinished", 0, 0, "The final message reports no unfinished work.")

    # --- cost per changed line
    cost = estimated_cost(run)
    lines = sum(c.added + c.removed for c in changes.values())
    if cost and cost > 0 and lines > 0:
        per_line = cost / lines
        impact = 5 if per_line <= 0.02 else 0 if per_line <= 0.10 else -5 if per_line <= 0.25 else -10
        add("cost_efficiency", round(per_line, 4), impact,
            f"${cost:.2f} estimated for {lines} changed line(s), ${per_line:.4f} per line.")

    # --- rule-based risk level
    risk_score = _risk_score(list(risks))
    level = _risk_level(risk_score)
    add("risk_level", level, {"High": -20, "Medium": -8, "Low": 0}[level],
        f"Rule-based risk {risk_score}/100 ({level}).")

    score = max(0, min(100, BASE_SCORE + sum(s["impact"] for s in signals)))
    return {"score": score, "grade": grade_for(score), "signals": signals}


def analyze(run: Run, risks: Optional[Sequence[Risk]] = None) -> None:
    """Set run.quality and run.recommendations. Call after the risk assessment, so the
    risk level and findings are known. `risks` defaults to the steps' own findings."""
    run.quality = score_run(run, risks)
    run.recommendations = recommend(run)
