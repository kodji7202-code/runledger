"""The RunLedger format adapter (runledger/adapters/native.py): parsing, validation,
detection and discovery. Tests call the native module directly, so they do not
depend on the other agent adapters being installed."""
import json
import os
from pathlib import Path

import pytest

from runledger import adapters
from runledger.adapters import native
from runledger.pricing import apply_costs

FIX_JSON = Path(__file__).parent / "fixtures" / "native_session.json"
FIX_CLAUDE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"

HEADER = {"runledger_format": 1, "agent": "my-agent", "session_id": "abc123", "cwd": "/work/app",
          "started": "2026-10-08T09:00:00Z", "prompts": ["Do the thing"]}
STEP = {"tool": "Bash", "input": {"command": "echo hi"}, "result_text": "hi", "is_error": False}


def _lines(directory: Path, name: str, *records) -> Path:
    """One record per line. Dicts are dumped as JSON; strings are written as they are."""
    path = directory / name
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")
    return path


def _doc(directory: Path, name: str, doc) -> Path:
    path = directory / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


# ---------------------------------------------------------------- parsing

def test_json_layout_parses_every_field():
    run = native.parse(FIX_JSON)
    assert run.agent == "demo-agent"
    assert run.session_id == "demo-0001-native"
    assert run.cwd == "/home/dev/demo-app"
    assert run.git_branch == "feature/price-cache"
    assert run.duration_seconds == 270
    assert run.prompts == ["Add a small cache to the price lookup and make the tests pass."]
    assert run.final_message.startswith("Added an LRU cache")
    assert run.reported_cost == pytest.approx(0.0871)
    assert run.cost is None  # priced later by pricing.apply_costs, never by the adapter
    assert run.usage.input_tokens == 18400
    assert run.usage.cache_read_tokens == 15000
    assert run.models["claude-sonnet-5-5"].output_tokens == 3900
    assert [s.tool for s in run.steps] == ["Read", "Edit", "Bash", "Bash"]
    assert [s.index for s in run.steps] == [1, 2, 3, 4]
    assert run.steps[1].input["new_string"].startswith("@lru_cache")
    assert run.steps[2].result_text == "12 passed in 0.31s"
    assert run.steps[0].usage.cache_read_tokens == 3000
    assert run.steps[0].model == "claude-sonnet-5-5"


def test_jsonl_layout_with_end_record(tmp_path):
    path = _lines(tmp_path, "run.runledger.jsonl", HEADER, STEP,
                  {"type": "end", "final_message": "done", "ended": "2026-10-08T09:01:00Z",
                   "reported_cost": 0.5})
    run = native.parse(path)
    assert run.agent == "my-agent"
    assert run.session_id == "abc123"
    assert run.prompts == ["Do the thing"]
    assert run.final_message == "done"
    assert run.ended == "2026-10-08T09:01:00Z"
    assert run.reported_cost == 0.5  # the end record repeats the header field and wins
    assert [s.tool for s in run.steps] == ["Bash"]
    assert run.steps[0].is_error is False


def test_jsonl_without_end_record_is_a_readable_partial_run(tmp_path):
    path = _lines(tmp_path, "crashed.runledger.jsonl", HEADER, STEP)
    run = native.parse(path)
    assert len(run.steps) == 1
    assert run.final_message == ""
    assert run.ended is None


def test_jsonl_tolerates_crlf_blank_lines_and_a_bom(tmp_path):
    path = tmp_path / "win.runledger.jsonl"
    body = "\r\n".join([json.dumps(HEADER), "", json.dumps(STEP), "   ", json.dumps({"type": "step", "tool": "Read"})])
    path.write_text(body + "\r\n", encoding="utf-8-sig")
    run = native.parse(path)
    assert [s.tool for s in run.steps] == ["Bash", "Read"]


def test_defaults_when_the_header_is_minimal(tmp_path):
    path = _doc(tmp_path, "run-7.runledger.json", {"runledger_format": 1})
    run = native.parse(path)
    assert run.agent == "native"
    assert run.session_id == "run-7"  # file name without the suffix
    assert run.steps == [] and run.prompts == [] and run.models == {}
    assert run.final_message == ""
    assert run.cost is None and run.reported_cost is None
    assert run.usage.total == 0
    assert run.path == str(path)


def test_single_model_prices_steps_that_name_no_model(tmp_path):
    doc = {"runledger_format": 1, "models": {"claude-haiku-5-5": {"input_tokens": 10}},
           "steps": [{"tool": "Read", "input": {"file_path": "a.py"}}]}
    run = native.parse(_doc(tmp_path, "one.runledger.json", doc))
    assert run.steps[0].model == "claude-haiku-5-5"


def test_run_totals_come_from_steps_when_models_is_absent(tmp_path):
    doc = {"runledger_format": 1, "steps": [
        {"tool": "Read", "usage": {"input_tokens": 10, "output_tokens": 1}},
        {"tool": "Read", "usage": {"input_tokens": 5}},
    ]}
    run = native.parse(_doc(tmp_path, "sum.runledger.json", doc))
    assert run.usage.input_tokens == 15
    assert run.usage.output_tokens == 1


def test_models_and_run_cost_are_derived_from_priced_steps_when_header_models_are_absent(tmp_path):
    doc = {"runledger_format": 1, "steps": [
        {"tool": "Read", "model": "claude-haiku-5-5", "usage": {"input_tokens": 1_000_000}},
        {"tool": "Read", "model": "claude-haiku-5-5", "usage": {"output_tokens": 1_000_000}},
    ]}
    run = native.parse(_doc(tmp_path, "priced.runledger.json", doc))
    apply_costs(run)

    assert run.models == {"claude-haiku-5-5": run.usage}
    assert run.cost == pytest.approx(0.6)
    assert sum(step.cost or 0 for step in run.steps) == pytest.approx(run.cost)


def test_result_text_is_capped_like_the_claude_adapter(tmp_path):
    doc = {"runledger_format": 1, "steps": [{"tool": "Bash", "result_text": "x" * 5000}]}
    run = native.parse(_doc(tmp_path, "big.runledger.json", doc))
    assert len(run.steps[0].result_text) == native.RESULT_LIMIT == 4000


# ---------------------------------------------------------------- validation

BAD_JSONL = [
    ("invalid JSON on line 2", [HEADER, "{not json"], "line 2: invalid JSON"),
    ("first line is not a header", [{"tool": "Bash", "input": {}}], "line 1: the first line must be the header"),
    ("header without runledger_format", [{"agent": "x"}], "line 1: the first line must be the header"),
    ("unsupported format version", [{"runledger_format": 2}], "unsupported runledger_format 2"),
    ("boolean format version", [{"runledger_format": True}], "unsupported runledger_format True"),
    ("step without a tool", [HEADER, {"input": {}}], "line 2: 'tool' must be a non-empty string"),
    ("blank tool name", [HEADER, {"tool": "  "}], "line 2: 'tool' must be a non-empty string"),
    ("input is a list", [HEADER, {"tool": "Read", "input": []}], "line 2: 'input' must be an object"),
    ("negative tokens", [HEADER, {"tool": "Read", "usage": {"input_tokens": -1}}],
     "line 2: 'usage.input_tokens' must be a non-negative whole number"),
    ("fractional tokens", [HEADER, {"tool": "Read", "usage": {"output_tokens": 1.5}}],
     "line 2: 'usage.output_tokens' must be a non-negative whole number"),
    ("is_error is text", [HEADER, {"tool": "Read", "is_error": "yes"}],
     "line 2: 'is_error' must be true or false"),
    ("reported_cost is text", [dict(HEADER, reported_cost="1.20")],
     "line 1: 'reported_cost' must be a non-negative number or null"),
    ("negative reported_cost in end record", [HEADER, {"type": "end", "reported_cost": -2}],
     "line 2: 'reported_cost' must be a non-negative number or null"),
    ("model usage is not an object", [dict(HEADER, models={"m": 5})],
     "line 1: 'models.m' must be an object"),
    ("unknown record type", [HEADER, {"type": "note"}], "line 2: unknown record type 'note'"),
    ("a line after the end record", [HEADER, {"type": "end"}, STEP],
     "line 3: nothing may follow the end record"),
    ("prompts is not a list of strings", [dict(HEADER, prompts="fix it")],
     "line 1: 'prompts' must be an array of strings"),
    ("a JSON array instead of an object", [HEADER, [1, 2]], "line 2: each line must be a JSON object"),
]


@pytest.mark.parametrize("records, expected", [(r, e) for _, r, e in BAD_JSONL],
                         ids=[name for name, _, _ in BAD_JSONL])
def test_jsonl_errors_name_the_file_and_line(tmp_path, records, expected):
    path = _lines(tmp_path, "bad.runledger.jsonl", *records)
    with pytest.raises(ValueError) as exc:
        native.parse(path)
    assert expected in str(exc.value)
    assert str(path) in str(exc.value)


def test_empty_jsonl_file_is_rejected(tmp_path):
    path = tmp_path / "empty.runledger.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty file"):
        native.parse(path)


BAD_JSON = [
    ("top level is an array", "[1, 2]", "the top level must be a JSON object"),
    ("steps is an object", {"runledger_format": 1, "steps": {}}, "'steps' must be an array"),
    ("second step has no tool", {"runledger_format": 1, "steps": [{"tool": "Read"}, {"input": {}}]},
     "step 2: 'tool' must be a non-empty string"),
    ("models is a list", {"runledger_format": 1, "models": []},
     "'models' must be an object keyed by model id"),
    ("negative cache tokens", {"runledger_format": 1, "models": {"m": {"cache_read_tokens": -5}}},
     "'models.m.cache_read_tokens' must be a non-negative whole number"),
    ("agent is a number", {"runledger_format": 1, "agent": 7}, "'agent' must be a string or null"),
    ("ended is a number", {"runledger_format": 1, "ended": 7}, "'ended' must be a string or null"),
    ("broken JSON", '{"runledger_format": 1,\n "steps": [}', "line 2, column"),
    ("JSONL content in a .json name", '{"runledger_format": 1}\n{"tool": "Read"}\n',
     "needs the .runledger.jsonl name"),
]


@pytest.mark.parametrize("doc, expected", [(d, e) for _, d, e in BAD_JSON], ids=[n for n, _, _ in BAD_JSON])
def test_json_errors_name_the_file_and_step(tmp_path, doc, expected):
    path = _doc(tmp_path, "bad.runledger.json", doc)
    with pytest.raises(ValueError) as exc:
        native.parse(path)
    assert expected in str(exc.value)
    assert str(path) in str(exc.value)


def test_json_layout_is_the_default_when_the_name_says_nothing(tmp_path):
    path = _doc(tmp_path, "plain.json", {"runledger_format": 1, "steps": [{"tool": "Read"}]})
    assert [s.tool for s in native.parse(path).steps] == ["Read"]


# ---------------------------------------------------------------- detection

def test_detect_by_file_name(tmp_path):
    assert native.detect(tmp_path / "a.runledger.json")
    assert native.detect(tmp_path / "a.runledger.jsonl")
    assert not native.detect(tmp_path / "a.txt")


def test_detect_json_by_content_of_the_first_object(tmp_path):
    assert native.detect(FIX_JSON)  # native_session.json has no runledger suffix
    jsonl = _lines(tmp_path, "header.jsonl", HEADER, STEP)
    assert native.detect(jsonl)


def test_detect_rejects_other_json_and_sessions(tmp_path):
    assert not native.detect(FIX_CLAUDE)  # a Claude Code transcript
    assert not native.detect(_doc(tmp_path, "other.json", {"name": "package"}))
    assert not native.detect(_doc(tmp_path, "broken.json", "{not json"))
    assert not native.detect(tmp_path / "missing.json")


# ---------------------------------------------------------------- discovery

def test_find_sessions_reads_only_the_runs_folder_newest_first(tmp_path):
    runs = tmp_path / ".runledger" / "runs"
    (runs / "nested").mkdir(parents=True)
    old = runs / "old.runledger.json"
    new = runs / "new.runledger.jsonl"
    for f in (old, new, runs / "notes.json", runs / "readme.runledger.txt", runs / "nested" / "deep.runledger.json"):
        f.write_text("{}", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert native.find_sessions(str(tmp_path)) == [new, old]


def test_find_sessions_without_a_project_or_runs_folder(tmp_path):
    assert native.find_sessions(None) == []
    assert native.find_sessions("") == []
    assert native.find_sessions(str(tmp_path)) == []


def test_registry_resolves_native_and_labels_agents():
    assert adapters.get("native").NAME == "native"
    assert adapters.label("native") == "RunLedger format"
    assert adapters.label("claude-code") == "Claude Code"
    assert adapters.label("my-agent") == "my-agent"  # no adapter of that name: shown as given
