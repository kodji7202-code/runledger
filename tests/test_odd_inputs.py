"""Step inputs with the wrong types still give a receipt in every format."""
import json

import pytest

from runledger.cli import main

TOOLS = ["Read", "Write", "Edit", "MultiEdit", "Delete", "NotebookEdit", "Bash", "Grep", "Glob",
         "LS", "Search", "WebFetch", "WebSearch", "TodoWrite", "Task", "Agent", "mcp__srv__call"]
KEYS = ["file_path", "notebook_path", "path", "content", "old_string", "new_string", "edits",
        "new_source", "command", "description", "pattern", "url", "query", "workdir", "cwd"]
ODD = {"int": 5, "float": 1.5, "bool": True, "null": None, "list": ["a", 1], "dict": {"a": 1},
       "nested_edits": [1, None, {"old_string": 3, "new_string": ["x"]}]}


def _session(tmp_path, tool, value):
    steps = [{"tool": tool, "input": {k: value for k in KEYS}, "result_text": "ok"},
             {"tool": tool, "input": {"file_path": value, "edits": value, "command": value}},
             {"tool": "Bash", "input": {"command": "rm -rf build && echo ok"}}]
    path = tmp_path / "odd.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "agent": "odd", "session_id": "odd-1",
                                "cwd": str(tmp_path), "prompts": ["try odd inputs"],
                                "steps": steps}), encoding="utf-8")
    return path


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("kind", sorted(ODD))
def test_odd_inputs_render_in_every_format(tmp_path, capsys, tool, kind):
    path = _session(tmp_path, tool, ODD[kind])
    for fmt in ("html", "md", "json"):
        assert main(["receipt", str(path), "--format", fmt, "-o", "-"]) == 0, fmt
        out = capsys.readouterr().out
        if fmt == "json":
            data = json.loads(out)
            assert data["totals"]["steps"] == 3


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit"])
@pytest.mark.parametrize("kind", sorted(ODD))
def test_odd_edits_on_test_files_are_assessed_and_reviewed(tmp_path, tool, kind):
    from runledger.parser import Step
    from runledger.review import _compact_step
    from runledger.risk import assess_step
    step = Step(1, tool, {"file_path": str(tmp_path / "tests" / "test_x.py"), "edits": ODD[kind],
                          "old_string": ODD[kind], "new_string": ODD[kind]}, "t", None, None)
    assert isinstance(assess_step(step, str(tmp_path)), list)
    assert isinstance(_compact_step(step, str(tmp_path)), dict)
