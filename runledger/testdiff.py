"""What a run did to its test files, read from their Git diffs against HEAD.

The diff shows the net result, however the file was written (edit tools, heredocs,
Python scripts), so it is a firmer reading than any single step. Each changed test
file gets one verdict:

  skipped   a skip or only marker was added
  replaced  tests were removed and new tests that cover the same behaviour were added
            (a shared word in the test names, or the new test names words from the
            request) and the file has at least as many assertions as before
  weakened  assertions were removed overall, or a test was removed with no replacement
  changed   assertions were edited with no net loss

A file whose diff only adds lines gets no verdict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Set, Tuple

SKIP_RE = re.compile(r"\.(skip|only)\(|@pytest\.mark\.skip|xit\(|xdescribe\(|@Ignore|@Disabled")
_ASSERT_RE = re.compile(r"\bassert\w*\b|\bexpect\s*\(|\bpytest\.raises\b|\bt\.(?:Error|Errorf|Fatal|Fatalf)\b")
_TEST_DEF_RES = [
    re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\("),
    re.compile(r"^\s*class\s+(Test\w*)"),
    re.compile(r"^func\s+(Test\w+)\s*\("),
    re.compile(r"\b(?:it|test)(?:\.(?:concurrent|only|skip|todo))?\s*\(\s*(['\"`])(.+?)\1"),
]
_STOP_WORDS = {"test", "tests", "the", "and", "for", "with", "when", "should", "not", "its", "can",
               "does", "that", "are", "was", "has", "into", "from"}


@dataclass
class TestChange:
    __test__ = False  # not a pytest test class

    path: str
    verdict: str  # skipped | replaced | weakened | changed
    removed_tests: List[str] = field(default_factory=list)
    added_tests: List[str] = field(default_factory=list)
    asserts_removed: int = 0
    asserts_added: int = 0
    note: str = ""


def _hunk_lines(diff: str) -> Tuple[List[str], List[str]]:
    removed: List[str] = []
    added: List[str] = []
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("-"):
            removed.append(line[1:])
        elif in_hunk and line.startswith("+"):
            added.append(line[1:])
    return removed, added


def _test_names(lines: Iterable[str]) -> List[str]:
    names: List[str] = []
    for line in lines:
        for rx in _TEST_DEF_RES:
            m = rx.search(line)
            if m:
                names.append(m.group(m.lastindex or 1))
                break
    return names


def _words(text: str) -> Set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in _STOP_WORDS}


def _count(rx: "re.Pattern", lines: Iterable[str]) -> int:
    return sum(len(rx.findall(line)) for line in lines)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def classify(path: str, diff: str, prompts: Iterable[str] = ()) -> "TestChange | None":
    removed, added = _hunk_lines(diff)
    before, after = _test_names(removed), _test_names(added)
    gone = [n for n in before if n not in after]
    new = [n for n in after if n not in before]
    a_removed, a_added = _count(_ASSERT_RE, removed), _count(_ASSERT_RE, added)
    counts = f"{_plural(a_removed, 'assertion')} removed, {a_added} added"
    change = TestChange(path, "", gone, new, a_removed, a_added)

    if _count(SKIP_RE, added) > _count(SKIP_RE, removed):
        change.verdict, change.note = "skipped", f"{path}: tests were disabled or skipped."
        return change
    if gone:
        request = _words(" ".join(prompts))
        new_words = [_words(n) for n in new]
        by_name = all(any(_words(old) & w for w in new_words) for old in gone)
        by_request = any(w & request for w in new_words)
        covered = bool(new) and (by_name or by_request)
        if covered and a_added >= a_removed:
            change.verdict = "replaced"
            change.note = f"{path}: {', '.join(gone)} replaced by {', '.join(new)} ({counts})."
        else:
            change.verdict = "weakened"
            change.note = f"{path}: removed {', '.join(gone)} with no replacement covering it ({counts})."
        return change
    if a_removed > a_added:
        change.verdict, change.note = "weakened", f"{path}: {counts}."
        return change
    if a_removed:
        change.verdict, change.note = "changed", f"{path}: assertions changed with no net loss ({counts})."
        return change
    return None


def analyze(diffs: Dict[str, str], prompts: Iterable[str] = ()) -> List[TestChange]:
    """One TestChange per test file whose diff removes or changes tests, sorted by path."""
    prompts = list(prompts)
    out = []
    for path in sorted(diffs):
        change = classify(path, diffs[path], prompts)
        if change is not None:
            out.append(change)
    return out
