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
    assert run.models == {"unattributed": Usage(100)}
    apply_costs(run)
    assert run.cost is None
    assert unknown_models(run) == []  # adding a price cannot fix unattributed tokens


def test_sub_agent_usage_is_credited_to_the_child_model(tmp_path):
    (tmp_path / "child-1.settings.json").write_text(json.dumps({"model": "claude-haiku-4-5"}), encoding="utf-8")
    run = droid.parse(_session(tmp_path, [
        _message([_call("Read", {"file_path": "a.py"})], modelId=MODEL, usage={"inputTokens": 100}),
    ], settings={"model": MODEL, "tokenUsage": {"inputTokens": 100},
                 "inclusiveTokenUsage": {"inputTokens": 400, "outputTokens": 50},
                 "childInclusiveTokenUsageBySessionId": {
                     "child-1": {"inputTokens": 250, "outputTokens": 50},
                     "child-gone": {"inputTokens": 50}}}))
    assert run.usage == Usage(400, 50)
    assert run.models == {MODEL: Usage(100), "claude-haiku-4-5": Usage(250, 50), "unattributed": Usage(50)}
    assert run.steps[0].usage == Usage(100)  # sub-agent tokens are not charged to parent steps
    apply_costs(run)
    assert run.cost_partial
    assert run.cost == pytest.approx(cost_of(Usage(100), MODEL) + cost_of(Usage(250, 50), "claude-haiku-4-5"))


def test_inclusive_usage_without_children_is_unattributed(tmp_path):
    run = droid.parse(_session(tmp_path, [
        _message([_call("Read", {"file_path": "a.py"})], modelId=MODEL),
    ], settings={"model": MODEL, "tokenUsage": {"inputTokens": 100},
                 "inclusiveTokenUsage": {"inputTokens": 160}}))
    assert run.usage == Usage(160)
    assert run.models == {MODEL: Usage(100), "unattributed": Usage(60)}
    assert run.usage_source == "settings"


@pytest.mark.parametrize("settings_text", [None, "{partial", "[]", '{"model":"claude-sonnet-4-5"}'])
def test_missing_usage_or_unreadable_settings_never_fabricate_run_cost(tmp_path, monkeypatch, settings_text):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    path = _session(tmp_path, [_message([_call("Read", {"file_path": "a.py"})])])
    if settings_text is not None:
        path.with_suffix(".settings.json").write_text(settings_text, encoding="utf-8")
    run = droid.parse(path)
    apply_costs(run)
    assert run.usage == Usage()
    assert run.cost is None


SHELL_FIX = FIXTURES / "droid_shell_edits.jsonl"
SHELL_ID = "4f081969-21df-49b2-9272-75a305796c06"
SHELL_MODEL = "claude-sonnet-5-5"


def _receipt(capsys, path, fmt):
    assert main(["receipt", str(path), "--format", fmt, "-o", "-"]) == 0
    out = capsys.readouterr().out
    return json.loads(out) if fmt == "json" else out


def test_settings_total_is_split_evenly_and_marked_estimated(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    run = droid.parse(SHELL_FIX)
    assert run.usage == Usage(12, 3451, 13726, 92818)
    assert run.usage.total == 110007
    assert run.models == {SHELL_MODEL: run.usage}
    assert [s.usage.total for s in run.steps] == [22004, 22002, 22001, 22000, 22000]
    assert all(s.usage_estimated for s in run.steps)
    assert (run.usage_source, run.usage_file) == ("settings", "droid_shell_edits.settings.json")
    apply_costs(run)
    assert run.cost == pytest.approx(cost_of(run.usage, SHELL_MODEL))
    assert run.cost == pytest.approx(0.0781, abs=1e-4)

    data = _receipt(capsys, SHELL_FIX, "json")
    assert data["totals"]["tokens"] == 110007
    assert data["totals"]["usage_source"] == "settings"
    assert all(s["tokens_estimated"] for s in data["steps"])
    note = ("Per-step tokens are estimated: an even split of the session total "
            "in droid_shell_edits.settings.json.")
    assert note in _receipt(capsys, SHELL_FIX, "md")
    assert note in _receipt(capsys, SHELL_FIX, "html")


def test_settings_are_found_by_session_id_when_the_transcript_was_copied(tmp_path, monkeypatch):
    home = tmp_path / "factory"
    monkeypatch.setenv("FACTORY_HOME", str(home))
    copy = tmp_path / "copies" / "renamed.jsonl"
    copy.parent.mkdir()
    shutil.copyfile(SHELL_FIX, copy)
    cwd = droid.parse(SHELL_FIX).cwd
    other = home / "sessions" / "-somewhere-else" / f"{SHELL_ID}.settings.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"tokenUsage": {"inputTokens": 7}}), encoding="utf-8")

    run = droid.parse(copy)
    assert (run.usage, run.usage_source) == (Usage(7), "settings")

    own = home / "sessions" / cwd.replace("/", "-") / f"{SHELL_ID}.settings.json"
    own.parent.mkdir(parents=True)
    shutil.copyfile(SHELL_FIX.with_suffix(".settings.json"), own)
    run = droid.parse(copy)
    assert run.usage.total == 110007  # the folder for the session's cwd wins
    assert run.usage_file == own.name


@pytest.mark.parametrize("sid", ["../escape", "*", "a/b", ""])
def test_unsafe_session_ids_are_never_used_to_search(tmp_path, monkeypatch, sid):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path))
    (tmp_path / "sessions" / "x").mkdir(parents=True)
    (tmp_path / "sessions" / "x" / "anything.settings.json").write_text(
        json.dumps({"tokenUsage": {"inputTokens": 7}}), encoding="utf-8")
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"type": "session_start", "id": sid, "cwd": "/demo"}) + "\n",
                    encoding="utf-8")
    assert droid._settings_path(path, droid._header(path)) is None


def test_missing_settings_show_na_and_a_note_and_no_cost(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    path = tmp_path / f"{SHELL_ID}.jsonl"
    shutil.copyfile(SHELL_FIX, path)
    run = droid.parse(path)
    apply_costs(run)
    assert (run.usage, run.usage_source, run.cost) == (Usage(), "missing", None)
    assert not any(s.usage_estimated for s in run.steps)

    data = _receipt(capsys, path, "json")
    assert data["totals"]["usage_source"] == "missing"
    assert data["totals"]["cost_usd"] is None
    note = (f"Token usage not found: Droid keeps it in {SHELL_ID}.settings.json "
            "next to the session file.")
    md = _receipt(capsys, path, "md")
    assert note in md and "**Tokens:** n/a" in md and ", n/a tok," in md
    page = _receipt(capsys, path, "html")
    assert note in page and "<span>Tokens</span><b>n/a</b>" in page


def test_missing_settings_steps_show_na_cost_and_null_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    path = tmp_path / f"{SHELL_ID}.jsonl"
    shutil.copyfile(SHELL_FIX, path)
    run = droid.parse(path)
    apply_costs(run)
    assert not any(s.usage_known for s in run.steps)
    assert all(s.cost is None for s in run.steps)

    md = _receipt(capsys, path, "md")
    steps = [line for line in md.splitlines() if " tok, " in line]
    assert steps and all(line.endswith("n/a tok, n/a") for line in steps)
    assert "$0.0000" not in md
    data = _receipt(capsys, path, "json")
    assert all(s["cost_usd"] is None and s["tokens_known"] is False for s in data["steps"])
    assert "$0.0000" not in _receipt(capsys, path, "html")


def test_steps_without_attributed_usage_are_unknown_not_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    usage = {"input_tokens": 100, "output_tokens": 20}
    rows = [
        _message([_call("Read", {"file_path": "a.py"}, "one")], modelId=MODEL, usage=usage),
        _message([_call("Read", {"file_path": "b.py"}, "two")], mid="two", modelId=MODEL),
    ]
    # The snapshot equals the per-message usage, so nothing is left to give the second step.
    path = _session(tmp_path, rows, settings={"model": MODEL, "tokenUsage": {"inputTokens": 100, "outputTokens": 20}})
    run = droid.parse(path)
    apply_costs(run)
    assert [s.usage_known for s in run.steps] == [True, False]
    assert run.steps[0].cost == pytest.approx(cost_of(Usage(100, 20), MODEL))
    assert run.steps[1].cost is None
    assert run.usage_source == "messages"
    data = _receipt(capsys, path, "json")
    assert [(s["tokens_known"], s["cost_usd"] is None) for s in data["steps"]] == [(True, False), (False, True)]
    assert _receipt(capsys, path, "md").splitlines().count("2. Read b.py — Sonnet 4.5, n/a tok, n/a") == 1


def test_mixed_model_remainder_leaves_unattributed_steps_unknown(tmp_path):
    run = droid.parse(_session(tmp_path, [
        _message([_call("Read", {"file_path": "a.py"}, "one")], modelId=MODEL),
        _message([_call("Read", {"file_path": "b.py"}, "two")], mid="two", modelId="claude-haiku-4-5"),
    ], settings={"model": "claude-haiku-4-5", "tokenUsage": {"inputTokens": 100}}))
    apply_costs(run)
    assert [(s.usage_known, s.cost) for s in run.steps] == [(False, None), (False, None)]


def test_settings_split_marks_steps_known(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    run = droid.parse(SHELL_FIX)
    apply_costs(run)
    assert all(s.usage_known and s.cost is not None for s in run.steps)
    assert sum(s.usage.total for s in run.steps) == run.usage.total


def test_per_message_usage_is_exact_and_has_no_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    path = _session(tmp_path, [_message([_call("Read", {"file_path": "a.py"})], modelId=MODEL,
                                        usage={"input_tokens": 10, "output_tokens": 5})])
    run = droid.parse(path)
    assert (run.usage_source, run.usage_file) == ("messages", None)
    assert not run.steps[0].usage_estimated
    md = _receipt(capsys, path, "md")
    assert "estimated" not in md and "Token usage not found" not in md
    assert _receipt(capsys, path, "json")["steps"][0]["tokens_estimated"] is False


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
                    settings={"model": "openai/GPT-5-codex-latest",
                              "tokenUsage": {"inputTokens": 1000, "outputTokens": 200}})
    prices = tmp_path / ".runledger" / "prices.json"
    prices.parent.mkdir()
    # Synthetic rates only, not a claim about this model's real price.
    prices.write_text("{bad" if source == "invalid" else json.dumps({
        "gpt:5-codex": {"input": 1, "output": 2, "cache_read": 0},
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


# ---------------------------------------------------------------- relative cwd recovery

def _relative_session(folder, rows=(), cwd=".", settings=None, name="rel.jsonl"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    header = {"type": "session_start", "id": "rel-session", "cwd": cwd, "version": 2}
    path.write_text("\n".join(json.dumps(r) for r in [header, *rows]) + "\n", encoding="utf-8")
    if settings is not None:
        path.with_suffix(".settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return path


def _context(pwd):
    reminder = (f"<system-reminder>The `% pwd` output below is the session working directory.\n\n"
                f"% pwd\n{pwd}\n\n</system-reminder>Fix the bug.")
    return _message([{"type": "text", "text": reminder}], role="user", mid="u1")


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "work", str(path)], check=True)
    return path


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def test_absolute_header_cwd_is_kept(tmp_path):
    run = droid.parse(_relative_session(tmp_path / "s", cwd="/demo/widget"))
    assert (run.cwd, run.cwd_source) == ("/demo/widget", "header")


def test_pwd_in_the_session_context_wins(tmp_path):
    project = tmp_path / "proj"
    run = droid.parse(_relative_session(tmp_path / "s", [_context(str(project))],
                                        settings={"cwd": "/somewhere/else"}))
    assert (run.cwd, run.cwd_source) == (str(project), "session")
    assert run.prompts == ["Fix the bug."]


def test_settings_field_is_the_next_source(tmp_path):
    run = droid.parse(_relative_session(tmp_path / "s", settings={"workingDirectory": "/srv/app"}))
    assert (run.cwd, run.cwd_source) == ("/srv/app", "settings")


def test_a_relative_cwd_is_resolved_against_the_recovered_base(tmp_path):
    run = droid.parse(_relative_session(tmp_path / "s", [_context("/srv/app")], cwd="pkg"))
    assert run.cwd == "/srv/app/pkg"
    run = droid.parse(_relative_session(tmp_path / "t", [_context("/srv/app/pkg")], cwd="pkg"))
    assert run.cwd == "/srv/app/pkg"


@needs_git
def test_tool_paths_in_one_git_work_tree_give_the_project(tmp_path):
    repo = _git_repo(tmp_path / "my-repo")
    (repo / "src").mkdir()
    rows = [_message([_call("Read", {"file_path": str(repo / "src" / "a.py")}, "one"),
                      _call("Create", {"file_path": str(repo / "src" / "new" / "b.py"), "content": "x"}, "two"),
                      _call("Read", {"file_path": "/etc/hosts"}, "three")])]
    run = droid.parse(_relative_session(tmp_path / "s", rows))
    assert (run.cwd, run.cwd_source) == (str(repo), "session")


@needs_git
def test_tool_paths_without_a_majority_work_tree_are_not_trusted(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    rows = [_message([_call("Read", {"file_path": str(repo / "a.py")}, "one"),
                      _call("Read", {"file_path": "/etc/hosts"}, "two"),
                      _call("Read", {"file_path": "/etc/passwd"}, "three")])]
    run = droid.parse(_relative_session(tmp_path / "s", rows))
    assert (run.cwd, run.cwd_source) == (".", "unknown")


def test_encoded_session_folder_is_decoded_against_the_filesystem(tmp_path):
    target = tmp_path / "work" / "my-app" / "sub-dir"
    target.mkdir(parents=True)
    (tmp_path / "work" / "my").mkdir()  # a decoy: "my" exists, "my/app" does not
    (tmp_path / "work" / "my-app-sub").mkdir()  # a longer decoy with no "dir" below it
    folder = tmp_path / "sessions" / droid._encode(str(target))
    run = droid.parse(_relative_session(folder))
    assert (run.cwd, run.cwd_source) == (str(target), "session_dir")


def test_a_decoded_folder_that_does_not_exist_stays_unknown(tmp_path):
    folder = tmp_path / "sessions" / droid._encode(str(tmp_path / "gone" / "with-hyphens"))
    run = droid.parse(_relative_session(folder))
    assert (run.cwd, run.cwd_source) == (".", "unknown")
    assert droid._decode_folder("not-encoded") is None


def test_unknown_cwd_never_uses_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = droid.parse(_relative_session(tmp_path / "plain"))
    assert (run.cwd, run.cwd_source) == (".", "unknown")


def test_find_sessions_matches_relative_cwd_by_folder_name(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    project = tmp_path / "my-proj"
    project.mkdir()
    folder = tmp_path / "factory" / "sessions" / droid._encode(str(project))
    path = _relative_session(folder)
    _relative_session(tmp_path / "factory" / "sessions" / "-other", name="other.jsonl")
    assert droid.find_sessions(str(project)) == [path]


@needs_git
def test_receipt_for_a_recovered_cwd_has_branch_and_relative_files(tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")
    rows = [_context(str(repo)),
            _message([_call("Create", {"file_path": str(repo / "a.py"), "content": "x\ny\n"}, "one"),
                      _call("Read", {"file_path": str(repo / "a.py")}, "two")])]
    path = _relative_session(tmp_path / "s", rows)
    assert main(["receipt", str(path), "--format", "json", "-o", "-", "--no-safe"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["cwd"], data["cwd_source"], data["git_branch"]) == (str(repo), "session", "work")
    assert [f["path"] for f in data["files"]] == ["a.py"]
    assert not [r for r in data["risk"]["reasons"] if r["code"] == "read_outside"]


# ---------------------------------------------------------------- Windows sessions on any host

def test_a_relative_cwd_is_resolved_against_a_windows_base(tmp_path):
    run = droid.parse(_relative_session(tmp_path / "s", [_context("C:\\srv\\app")], cwd="pkg"))
    assert (run.cwd, run.cwd_source) == ("C:\\srv\\app\\pkg", "session")
    run = droid.parse(_relative_session(tmp_path / "t", [_context("C:\\srv\\app\\pkg")], cwd="pkg"))
    assert run.cwd == "C:\\srv\\app\\pkg"
    run = droid.parse(_relative_session(tmp_path / "u", settings={"cwd": "D:/work/app"}, cwd="sub\\pkg"))
    assert (run.cwd, run.cwd_source) == ("D:\\work\\app\\sub\\pkg", "settings")


def test_windows_folders_are_encoded_without_a_colon():
    assert droid._encode("C:\\Users\\dev\\my-app") == "C--Users-dev-my-app"
    assert droid._encode("C:/Users/dev") == "C--Users-dev"
    assert droid._encode("/home/dev/my-app") == "-home-dev-my-app"


@pytest.mark.parametrize("name", ["C--Users-dev-my-app-sub-dir", "C:-Users-dev-my-app-sub-dir"])
def test_windows_session_folder_is_decoded_with_its_drive(monkeypatch, name):
    existing = {"C:\\Users", "C:\\Users\\dev", "C:\\Users\\dev\\my", "C:\\Users\\dev\\my-app",
                "C:\\Users\\dev\\my-app\\sub-dir"}
    monkeypatch.setattr(droid.os.path, "isdir", lambda p: p in existing)
    assert droid._decode_folder(name) == "C:\\Users\\dev\\my-app\\sub-dir"


def test_find_sessions_matches_a_windows_project_by_folder_name(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "factory"))
    sessions = tmp_path / "factory" / "sessions"
    names = ["C--Users-dev-My-Proj", "-Users-dev-My-Proj"]
    if os.name != "nt":
        names.append("C:-Users-dev-My-Proj")  # a colon is not valid in a Windows file name
    expected = {_relative_session(sessions / n, name=f"{i}.jsonl") for i, n in enumerate(names)}
    _relative_session(sessions / "C--Users-dev-other", name="other.jsonl")
    assert set(droid.find_sessions("C:\\Users\\dev\\My-Proj")) == expected
    assert set(droid.find_sessions("c:/users/dev/my-proj")) == expected


def test_droid_settings_only_usage_with_long_context_model_uses_base_tier_and_notes(tmp_path, capsys):
    path = tmp_path / "lc-droid.jsonl"
    rows = [
        {"type": "session_start", "id": "lc-droid", "cwd": str(tmp_path)},
        _message([_call("Read", {"file_path": "a.py"}, "c1")]),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    settings = tmp_path / "lc-droid.settings.json"
    settings.write_text(json.dumps({
        "model": "gemini-2.5-pro",
        "tokenUsage": {"inputTokens": 500_000, "outputTokens": 0},
    }), encoding="utf-8")

    run = droid.parse(path)
    apply_costs(run)
    assert run.usage_source == "settings"
    # Base tier for gemini:2.5-pro is $1.25/MTok ($0.625 for 500k), not long-context $2.50/MTok ($1.25)
    assert run.cost == pytest.approx(0.625)
    assert any("long-context tier not applied" in n for n in run.notes)
    md = _receipt(capsys, path, "md")
    assert "long-context tier not applied" in md


def test_find_sessions_uses_header_cache(tmp_path, monkeypatch):
    fhome = tmp_path / "factory"
    monkeypatch.setenv("FACTORY_HOME", str(fhome))
    target = "/demo/target-proj"
    expected = []
    for i in range(4):
        expected.append(_discovered(fhome, target, f"t{i}.jsonl", 100 + i))
    for p in range(5):
        for i in range(4):
            _discovered(fhome, f"/demo/other-{p}", f"o{i}.jsonl", 50 + i)

    first = droid.find_sessions(target)
    assert set(first) == set(expected)
    cache_file = Path(os.environ["HOME"]) / ".runledger" / "cache" / "sessions.json"
    assert cache_file.is_file()

    opened = []
    real_open = Path.open

    def spy_open(self, *args, **kwargs):
        if self.suffix == ".jsonl":
            opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)
    second = droid.find_sessions(target)
    assert second == first
    assert opened == []


