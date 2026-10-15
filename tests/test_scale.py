"""Large sessions stay fast: 5,000 steps in every format, and a 1 MB shell command."""
import json
import time

from runledger.cli import main
from runledger.parser import Step
from runledger.risk import assess_step
from runledger.shell_writes import shell_deletes, shell_writes


def _steps(root, n):
    steps = []
    for i in range(n):
        kind = i % 5
        if kind == 0:
            command = (f"cd src && cat > f{i}.py <<'EOF'\nx = {i}\nEOF\n"
                       f"sed -i 's/a/b/' g{i % 50}.py && rm -f old{i}.txt")
            steps.append({"tool": "Bash", "input": {"command": command}, "result_text": "ok"})
        elif kind == 1:
            steps.append({"tool": "Edit", "input": {"file_path": str(root / f"src/e{i % 100}.py"),
                                                    "old_string": "a", "new_string": "b\nc"}})
        elif kind == 2:
            steps.append({"tool": "Read", "input": {"file_path": str(root / f"src/e{i % 100}.py")},
                          "result_text": "x" * 500})
        elif kind == 3:
            steps.append({"tool": "Bash", "input": {"command": f"pytest tests/test_{i % 30}.py -q"},
                          "result_text": "1 passed", "is_error": i % 7 == 0})
        else:
            steps.append({"tool": "Write", "input": {"file_path": str(root / f"src/w{i}.py"),
                                                     "content": "y\n" * 5}})
    return steps


def test_five_thousand_steps_render_in_every_format(tmp_path, capsys):
    path = tmp_path / "huge.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "agent": "scale", "session_id": "huge-1",
                                "cwd": str(tmp_path), "prompts": ["scale"],
                                "steps": _steps(tmp_path, 5000)}), encoding="utf-8")
    start = time.perf_counter()
    for fmt in ("html", "md", "json"):
        assert main(["receipt", str(path), "--format", fmt, "-o", "-"]) == 0, fmt
        out = capsys.readouterr().out
        if fmt == "json":
            assert json.loads(out)["totals"]["steps"] == 5000
    assert time.perf_counter() - start < 20


def _megabyte_command():
    heredocs = "\n".join(f"cat > out{i}.txt <<'EOF'\n" + "line\n" * 20 + "EOF" for i in range(2500))
    command = "echo start\n" + heredocs + "\n"
    return command + " # " + "z" * max(0, 1_000_000 - len(command))


def test_one_megabyte_command_is_assessed_quickly(tmp_path):
    command = _megabyte_command()
    step = Step(1, "Bash", {"command": command}, "t1", None, None)
    start = time.perf_counter()
    assess_step(step, str(tmp_path))
    writes = shell_writes(command)
    assert time.perf_counter() - start < 3
    assert len(writes) == 2500
    assert writes[0].path == "out0.txt" and writes[0].added == 20


def test_cached_results_cannot_be_changed_by_callers():
    command = "cd pkg && echo x > a.txt && rm b.txt"
    first = shell_writes(command)
    first.clear()
    assert [w.path for w in shell_writes(command)] == ["pkg/a.txt"]
    assert shell_deletes(command) == ["pkg/b.txt"]
    assert shell_deletes(command) == ["pkg/b.txt"]
