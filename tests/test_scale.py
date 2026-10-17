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


def test_parse_session_is_single_pass_with_bounded_memory(tmp_path):
    import inspect
    import tracemalloc
    from runledger import parser

    src = inspect.getsource(parser.parse_session)
    assert "events: List" not in src and "events = [" not in src

    # Write a ~10 MB JSONL session with 20 tool calls each returning a 500 KB tool_result
    big_chunk = "A" * 500_000
    session = tmp_path / "big.jsonl"
    with session.open("w", encoding="utf-8") as f:
        for i in range(20):
            f.write(json.dumps({
                "type": "assistant",
                "message": {
                    "id": f"m{i}",
                    "model": "claude-opus-4-6",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "content": [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": f"f{i}.txt"}}],
                },
            }) + "\n")
            f.write(json.dumps({
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": big_chunk}],
                },
            }) + "\n")

    tracemalloc.start()
    try:
        run = parser.parse_session(session)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(run.steps) == 20
    # Peak allocation stays under 3x the largest single event (~1.8 MB << 10 MB file)
    assert peak < 3 * 650_000


def test_list_is_fast_on_large_sessions(tmp_path, monkeypatch, capsys):
    from runledger.parser import encode_project_path

    cfg = tmp_path / "claude_cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    prj = tmp_path / "proj"
    prj.mkdir()
    proj_dir = cfg / "projects" / encode_project_path(str(prj))
    proj_dir.mkdir(parents=True)

    fake_tok = "sk-" + "ant-api03-" + ("a" * 24)
    big_chunk = "B" * 200_000
    for s in range(6):
        sid = f"large-{s:02d}"
        sf = proj_dir / f"{sid}.jsonl"
        with sf.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user",
                "sessionId": sid,
                "cwd": str(prj),
                "message": {"content": f"fix task {s} with token {fake_tok}"},
            }) + "\n")
            for i in range(10):
                f.write(json.dumps({
                    "type": "assistant",
                    "sessionId": sid,
                    "message": {
                        "id": f"m-{s}-{i}",
                        "model": "claude-sonnet-4-5-20250929",
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                        "content": [{"type": "tool_use", "id": f"t-{s}-{i}", "name": "Read",
                                     "input": {"file_path": f"f{i}.txt"}}],
                    },
                }) + "\n")
                f.write(json.dumps({
                    "type": "user",
                    "sessionId": sid,
                    "message": {
                        "content": [{"type": "tool_result", "tool_use_id": f"t-{s}-{i}", "content": big_chunk}],
                    },
                }) + "\n")

    assert main(["list", "--project", str(prj), "--limit", "6"]) == 0
    out1 = capsys.readouterr().out
    assert "10 steps" in out1
    assert "sk-ant-" not in out1
    assert "[REDACTED" in out1

    # Warm run uses the session cache without re-opening the multi-megabyte session files
    from pathlib import Path
    opened = []
    real_open = Path.open

    def spy_open(self, *args, **kwargs):
        if self.suffix == ".jsonl":
            opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)
    start = time.perf_counter()
    assert main(["list", "--project", str(prj), "--limit", "6"]) == 0
    elapsed = time.perf_counter() - start
    out2 = capsys.readouterr().out
    assert out2 == out1
    assert elapsed < 1.0
    assert opened == []


