"""Deterministic adversarial inputs for parsers and the guard.

These are regression smoke properties, not a substitute for a coverage-guided fuzzer: hostile
input must never crash the synchronous hook path or turn parser edge cases into unbounded work.
"""
import random
import string

from runledger.guard import decide
from runledger.ps_writes import ps_deletes, ps_writes
from runledger.shell_writes import database_writes, shell_deletes, shell_writes


def _mutations(seed: int, count: int, maximum: int = 240):
    rng = random.Random(seed)
    alphabet = string.printable + "…é\x00\x1b" + chr(92)
    seeds = [
        "cat <<EOF > f\nEOF", "powershell -Command Set-Content x y", "rm -rf -- one two",
        "$'a\\'b' # comment\nsed -i s/a/b/ f", "python - <<'PY'\nopen('x','w')\nPY",
        "curl 'http://x/?a=b' > out && tee -a log", "@" + '"' + "\nbody\n" + '"' + "@; Remove-Item x",
    ]
    for _ in range(count):
        base = rng.choice(seeds)
        chars = list(base)
        for _ in range(rng.randrange(1, 7)):
            op = rng.randrange(3)
            pos = rng.randrange(len(chars) + 1)
            if op == 0:
                chars.insert(pos, rng.choice(alphabet))
            elif op == 1 and chars:
                chars.pop(min(pos, len(chars) - 1))
            elif chars:
                chars[min(pos, len(chars) - 1)] = rng.choice(alphabet)
        suffixes = ("'" * rng.randrange(0, 8), '"' * rng.randrange(0, 8), "(" * rng.randrange(0, 12),
                    "<" * rng.randrange(0, 12), "x" * rng.randrange(0, maximum))
        yield "".join(chars) + rng.choice(suffixes)
    yield "cat <<'EOF' > f\n" + "A" * 20_000 + "\nEOF"
    yield "echo " + "(".join("abcdef" for _ in range(800))
    yield "Remove-Item " + "\\" * 4_000 + "x"


def test_shell_parser_fuzz_corpus_never_crashes():
    for command in _mutations(20261125, 2_000):
        shell_writes(command)
        shell_deletes(command)
        database_writes(command)


def test_powershell_parser_fuzz_corpus_never_crashes():
    for command in _mutations(20261126, 1_000):
        ps_writes(command)
        ps_deletes(command)


def test_guard_decide_fuzz_corpus_keeps_the_decision_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("RUNLEDGER_SERVER", raising=False)
    rng = random.Random(20261127)
    tools = ["Bash", "PowerShell", "Write", "Edit", "UnknownTool", "mcp__db__write", "", None, 7]
    values = [None, True, 0, -1, 3.14, "", "\x00", [], {}, ["x"], {"x": object()}, "C:\\temp\\x", "../x"]

    for index in range(600):
        tool_input = {}
        for _ in range(rng.randrange(1, 6)):
            key = rng.choice(["command", "file_path", "path", "content", "old_string", "new_string", "url"])
            tool_input[key] = rng.choice(values)
        event = {
            "tool_name": rng.choice(tools),
            "tool_input": tool_input if rng.randrange(5) else rng.choice(values),
            "cwd": str(tmp_path),
            "session_id": f"fuzz-{index}",
        }
        result = decide(event, policy={}, approver=lambda *_: "denied")
        assert result["decision"] in {"allow", "ask", "deny"}
        assert result["mode"] in {"enforce", "monitor"}
        assert isinstance(result["codes"], list)
        assert isinstance(result["warnings"], list)
        assert isinstance(result["summary"], str)
