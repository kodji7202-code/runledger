"""Synthetic Factory Droid sessions, discovery, canonical tools and CLI receipts."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from runledger import adapters
from runledger.adapters import droid
from runledger.cli import main
from runledger.parser import Usage
from runledger.pricing import apply_costs, cost_of, unknown_models
from runledger.risk import assess

FIXTURES = Path(__file__).parent / "fixtures"
FIX = FIXTURES / "droid_session.jsonl"
MODEL = "claude-sonnet-4-5"


def _message(content, role="assistant", mid="message", **extra):
    return {"type": "message", "id": mid, "timestamp": "2026-01-02T10:00:00Z",
            "message": {"role": role, "content": content, **extra}}


def _call(name, inp, call_id="call"):
    return {"type": "tool_use", "id": call_id, "name": name, "input": inp}


def _session(tmp_path, rows, settings=None, name="example.jsonl"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "session_start", "id": "synthetic-session", "cwd": "/demo/widget", "version": 1}
    data = [header] + rows
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in data) + "\n",
                    encoding="utf-8")
    if settings is not None:
        path.with_suffix(".settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return path


def _discovered(root, cwd, name, mtime):
    folder = root / "sessions" / cwd.replace("\\", "-").replace("/", "-")
    path = _session(folder, [], name=name)
    path.write_text(json.dumps({"type": "session_start", "id": name, "cwd": cwd}) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_fixture_metadata_prompts_and_results():
    run = droid.parse(FIX)
    assert (run.agent, run.session_id, run.cwd) == ("droid", "synthetic-droid-session", "/demo/widget")
    assert run.started == "2026-01-02T10:00:00Z"
    assert run.ended == "2026-01-02T10:00:06Z"
    assert run.duration_seconds == 6
    assert run.prompts == ["Add a greeting and run the tests."]
    assert run.final_message == "Added the greeting. One test passed."
    assert [s.tool for s in run.steps] == ["Read", "Search", "Edit", "Write", "Bash"]
    assert [s.index for s in run.steps] == [1, 2, 3, 4, 5]
    assert all(s.model == MODEL for s in run.steps)
    assert run.steps[0].tool_use_id == "read-example"
    assert run.steps[0].timestamp == "2026-01-02T10:00:02Z"
    assert run.steps[-1].result_text == "1 passed\n[Process exited with code 0]"
    assert not any(s.is_error for s in run.steps)


def test_cumulative_usage_is_not_counted_again_as_inclusive_last_call_or_thinking():
    run = droid.parse(FIX)
    assert run.usage == Usage(1003, 203, 103, 503)
    assert run.models == {MODEL: run.usage}
    total = Usage()
    for step in run.steps:
        total.add(step.usage)
    assert total == run.usage
    assert run.steps[0].usage == Usage(201, 41, 21, 101)
    apply_costs(run)
    assert run.cost == pytest.approx(cost_of(run.usage, MODEL))
    assert sum(s.cost for s in run.steps) == pytest.approx(run.cost)
    assert run.reported_cost is None


@pytest.mark.parametrize("name, inp, tool, expected", [
    ("Execute", {"command": "pwd", "timeout": 30}, "Bash", {"command": "pwd"}),
    ("Create", {"file_path": "a.py", "content": "x = 1\n"}, "Write",
     {"file_path": "a.py", "content": "x = 1\n"}),
    ("Edit", {"file_path": "a.py", "old_str": "1", "new_str": "2"}, "Edit",
     {"file_path": "a.py", "old_string": "1", "new_string": "2"}),
    ("Read", {"file_path": "a.py", "offset": 3}, "Read", {"file_path": "a.py", "offset": 3}),
    ("Grep", {"pattern": "hello", "path": "src"}, "Search", {"pattern": "hello", "path": "src"}),
    ("Glob", {"patterns": ["*.py", "*.md"], "folder": "src"}, "Search",
     {"pattern": "*.py | *.md", "path": "src"}),
    ("LS", {"directory_path": "src"}, "Search", {"pattern": "*", "path": "src"}),
    ("FetchUrl", {"url": "https://example.test/docs"}, "WebFetch", {"url": "https://example.test/docs"}),
    ("Delete", {"file_path": "old.py"}, "Delete", {"file_path": "old.py"}),
    ("functions.Execute", {"command": "pwd"}, "Bash", {"command": "pwd"}),
    ("docs__search", {"q": "example"}, "mcp__docs__search", {"q": "example"}),
    ("mcp__docs__search", {"q": "example"}, "mcp__docs__search", {"q": "example"}),
    ("TodoWrite", {"todos": []}, "TodoWrite", {"todos": []}),
])
def test_tool_mapping(tmp_path, name, inp, tool, expected):
    run = droid.parse(_session(tmp_path, [_message([_call(name, inp)])]))
    assert [(s.tool, s.input) for s in run.steps] == [(tool, expected)]


def test_patch_and_parallel_calls_expand_without_losing_results(tmp_path):
    patch = "*** Begin Patch\n*** Add File: new.py\n+x = 1\n*** Delete File: old.py\n*** End Patch"
    run = droid.parse(_session(tmp_path, [
        _message([_call("ApplyPatch", {"input": patch}, "patch")]),
        _message([{"type": "tool_result", "tool_use_id": "patch",
                   "content": [{"type": "text", "text": "Done"}], "is_error": False}], role="user", mid="result"),
        _message([_call("multi_tool_use.parallel", {"tool_uses": [
            {"recipient_name": "functions.Read", "parameters": {"file_path": "new.py"}},
            {"recipient_name": "functions.Execute", "parameters": {"command": "pwd"}},
        ]}, "parallel")], mid="parallel"),
    ]))
    assert [s.tool for s in run.steps] == ["Write", "Delete", "Read", "Bash"]
    assert run.steps[0].input == {"file_path": "new.py", "content": "x = 1\n"}
    assert [s.result_text for s in run.steps[:2]] == ["Done", "Done"]


@pytest.mark.parametrize("is_error, text", [
    (True, "Failed"), (False, "tests failed\n[Process exited with code 2]"),
])
def test_errors_from_flags_and_execute_exit_status(tmp_path, is_error, text):
    run = droid.parse(_session(tmp_path, [
        _message([_call("Execute", {"command": "pytest"})]),
        _message([{"type": "tool_result", "tool_use_id": "call", "is_error": is_error,
                   "content": text}], role="user", mid="result"),
    ]))
    assert run.steps[0].is_error is True


def test_message_usage_is_deduplicated_and_not_added_to_snapshot_twice(tmp_path):
    msg = _message([_call("Read", {"file_path": "a.py"})], modelId=MODEL,
                   usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2})
    run = droid.parse(_session(tmp_path, [msg, msg], settings={
        "model": MODEL, "tokenUsage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 2},
    }))
    assert len(run.steps) == 1
    assert run.usage == Usage(10, 5, 0, 2)
    assert run.models == {MODEL: run.usage}
    assert run.steps[0].usage == run.usage


def test_snapshot_remainder_counts_only_unattributed_usage(tmp_path):
    run = droid.parse(_session(tmp_path, [
        _message([_call("Read", {"file_path": "a.py"}, "one")], modelId=MODEL,
                 usage={"inputTokens": 10, "outputTokens": 5}),
        _message([_call("Read", {"file_path": "b.py"}, "two")], mid="two", modelId=MODEL),
    ], settings={"model": MODEL, "tokenUsage": {"inputTokens": 30, "outputTokens": 15}}))
    assert run.usage == Usage(30, 15)
    assert [s.usage for s in run.steps] == [Usage(10, 5), Usage(20, 10)]


def test_unknown_model_does_not_get_an_invented_cost(tmp_path):
    run = droid.parse(_session(tmp_path, [
        _message([_call("Read", {"file_path": "a.py"})], modelId="example-unpriced-model"),
    ], settings={"model": MODEL, "tokenUsage": {"inputTokens": 100, "outputTokens": 20}}))
    assert run.steps[0].model == "example-unpriced-model"  # message wins over settings
    apply_costs(run)
    assert unknown_models(run) == ["example-unpriced-model"]
    assert run.cost is None and run.steps[0].cost is None


def test_mixed_model_snapshot_cannot_be_priced_as_the_last_model(tmp_path):
    run = droid.parse(_session(tmp_path, [
        _message([_call("Read", {"file_path": "a.py"}, "one")], modelId=MODEL),
        _message([_call("Read", {"file_path": "b.py"}, "two")], mid="two", modelId="claude-haiku-4-5"),
    ], settings={"model": "claude-haiku-4-5", "tokenUsage": {"inputTokens": 100}}))
    assert run.models == {"unknown": Usage(100)}
    apply_costs(run)
    assert run.cost is None


@pytest.mark.parametrize("settings_text", [None, "{partial", "[]", '{"model":"claude-sonnet-4-5"}'])
def test_missing_usage_or_unreadable_settings_never_fabricate_run_cost(tmp_path, settings_text):
    path = _session(tmp_path, [_message([_call("Read", {"file_path": "a.py"})])])
    if settings_text is not None:
        path.with_suffix(".settings.json").write_text(settings_text, encoding="utf-8")
    run = droid.parse(path)
    apply_costs(run)
    assert run.usage == Usage()
    assert run.cost is None


def test_partial_lines_bad_shapes_and_context_are_ignored(tmp_path):
    path = _session(tmp_path, [
        "not json", '{"partial":', "[]", "null", {"type": "message", "message": None},
        _message([None, {"type": "thinking", "thinking": "ignore"}]),
        _message([{"type": "text", "text": "<system-reminder>Context.</system-reminder>\nFix the example."}],
                 role="user", mid="prompt"),
        _message([_call("Read", {"file_path": "a.py"})], mid="read"),
    ])
    run = droid.parse(path)
    assert run.prompts == ["Fix the example."]
    assert [s.tool for s in run.steps] == ["Read"]


def test_detect_and_registry_do_not_steal_other_formats(tmp_path):
    assert droid.detect(FIX)
    assert adapters.detect(FIX) is droid
    assert adapters.get("droid") is droid
    assert adapters.label("droid") == "Factory Droid"
    for name in ("sample_session.jsonl", "codex_session.jsonl", "native_session.json"):
        assert not droid.detect(FIXTURES / name)
    for text in ('{"type":"message","message":{"role":"assistant"}}', '{"type":"session_start"}', "not json"):
        path = tmp_path / "other.jsonl"
        path.write_text(text, encoding="utf-8")
        assert not droid.detect(path)
    assert not droid.detect(FIX.with_suffix(".settings.json"))
    assert not droid.detect(tmp_path / "missing.jsonl")


def test_detect_reads_only_the_header_and_tolerates_bom(tmp_path):
    path = tmp_path / "bounded.jsonl"
    path.write_text('\ufeff\n{"type":"session_start","id":"synthetic","cwd":"/demo/widget"}\n'
                    + "x" * 100000, encoding="utf-8")
    assert droid.detect(path)


def test_find_sessions_honors_factory_home_and_sorts_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path))
    a = _discovered(tmp_path, "/demo/widget", "a.jsonl", 100)
    b = _discovered(tmp_path, "/demo/other", "b.jsonl", 200)
    c = _discovered(tmp_path, "/demo/widget", "c.jsonl", 300)
    c.with_suffix(".settings.json").write_text("{}", encoding="utf-8")
    (c.parent / "notes.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    assert droid.find_sessions() == [c, b, a]
    assert droid.find_sessions("/demo/widget/") == [c, a]
    assert adapters.find_sessions(agent="droid") == [c, b, a]


def test_discovery_checks_cwd_even_when_folder_encoding_collides(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path))
    a = _discovered(tmp_path, "/demo/a-b", "one.jsonl", 100)
    b = _discovered(tmp_path, "/demo/a/b", "two.jsonl", 200)
    assert droid.find_sessions("/demo/a-b") == [a]
    assert droid.find_sessions("/demo/a/b") == [b]


def test_factory_home_default_expansion_and_missing_root(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_HOME", raising=False)
    assert droid.factory_home() == Path.home() / ".factory"
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "missing"))
    assert droid.find_sessions() == []
    monkeypatch.setenv("FACTORY_HOME", "~/example-factory")
    assert droid.factory_home() == Path.home() / "example-factory"


def test_relative_project_discovery(tmp_path, monkeypatch):
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    path = _discovered(tmp_path / "factory", str(project), "example.jsonl", 100)
    monkeypatch.chdir(tmp_path)
    assert droid.find_sessions("app") == [path]


def test_windows_project_matching_uses_header_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path))
    # A copied session may retain Windows cwd metadata on any operating system.
    path = _discovered(tmp_path, "/demo/copied", "windows.jsonl", 100)
    path.write_text(json.dumps({"type": "session_start", "id": "synthetic", "cwd": "D:\\demo\\app"})
                    + "\n", encoding="utf-8")
    assert droid.find_sessions("d:/DEMO/app/") == [path]


def test_risk_uses_canonical_tools(tmp_path):
    run = droid.parse(_session(tmp_path, [
        _message([_call("Read", {"file_path": ".env"})]),
        _message([_call("Execute", {"command": "rm -rf /"}, "delete")], mid="delete"),
    ]))
    _, _, risks = assess(run)
    assert any(r.code == "secret_file" for r in risks)
    assert any(r.step == 2 and r.severity == "high" for r in risks)


def test_receipt_agent_droid_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path))
    path = _discovered(tmp_path, "/demo/widget", "synthetic.jsonl", 100)
    shutil.copyfile(FIX, path)
    shutil.copyfile(FIX.with_suffix(".settings.json"), path.with_suffix(".settings.json"))
    assert main(["receipt", "--agent", "droid", "--project", "/demo/widget",
                 "--format", "json", "-o", "-"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["agent_id"], data["agent"]) == ("droid", "Factory Droid")
    assert data["totals"]["tokens"] == 1812
    assert data["totals"]["cost_usd"] == pytest.approx(cost_of(Usage(1003, 203, 103, 503), MODEL))
    assert main(["receipt", str(FIX), "--format", "md", "-o", "-"]) == 0
    assert "**Agent:** Factory Droid" in capsys.readouterr().out
    assert main(["list", "--agent", "droid", "--all"]) == 0
    assert "droid" in capsys.readouterr().out


@pytest.mark.parametrize("source", ["default", "env", "invalid"])
def test_cli_loads_user_prices_and_handles_invalid_files(tmp_path, source):
    path = _session(tmp_path, [_message([_call("Read", {"file_path": "example.py"})])],
                    settings={"model": "openai/GPT-6.1-SOL-latest",
                              "tokenUsage": {"inputTokens": 1000, "outputTokens": 200}})
    prices = tmp_path / ".runledger" / "prices.json"
    prices.parent.mkdir()
    # Synthetic rates only, not a claim about this model's real price.
    prices.write_text("{bad" if source == "invalid" else json.dumps({
        "gpt:6.1-sol": {"input": 1, "output": 2, "cache_read": 0},
    }), encoding="utf-8")
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    env.pop("RUNLEDGER_PRICES", None)
    if source == "env":
        explicit = tmp_path / "custom-prices.json"
        prices.rename(explicit)
        env["RUNLEDGER_PRICES"] = str(explicit)
    result = subprocess.run(
        [sys.executable, "-m", "runledger", "receipt", str(path), "--format", "json", "-o", "-"],
        cwd=str(Path(__file__).resolve().parents[1]), env=env, capture_output=True, text=True,
        timeout=30)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["totals"]["tokens"] == 1200
    if source == "invalid":
        assert "warning: ignoring prices file" in result.stderr
        assert receipt["totals"]["cost_usd"] is None
        assert "~/.runledger/prices.json" in receipt["pricing_note"]
    else:
        assert result.stderr == ""
        assert receipt["totals"]["cost_usd"] == pytest.approx(0.0014)
        assert receipt["steps"][0]["cost_usd"] == pytest.approx(0.0014)
        assert receipt["pricing_note"] is None
