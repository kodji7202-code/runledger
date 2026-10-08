"""Tests for the OpenAI Codex CLI adapter (runledger/adapters/codex.py)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runledger import adapters
from runledger.adapters import codex
from runledger.parser import Usage
from runledger.pricing import apply_costs, cost_of, unknown_models
from runledger.receipt import render
from runledger.risk import assess
from runledger.summarize import apply_templates

FIXTURES = Path(__file__).parent / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex_session.jsonl"
CLAUDE_FIXTURE = FIXTURES / "sample_session.jsonl"
CWD = "D:\\work\\payments-service"
PROMPT = ("The retry logic in src/payments/retry.ts gives up after one failure. "
          "Add exponential backoff with 3 attempts, add tests/retry.spec.ts, "
          "and remove the stale tests/old.spec.ts.")
FINAL = ("Added exponential backoff to withRetry (3 attempts, 200 ms base) and a new "
         "tests/retry.spec.ts. Removed the stale tests/old.spec.ts. I did not run the "
         "test suite, so please run it before merging.")


# ---------------------------------------------------------------- helpers

def _line(kind, payload, sec=0):
    return {"timestamp": f"2026-10-08T09:00:{sec:02d}.000Z", "type": kind, "payload": payload}


def _meta_line(cwd=CWD, sid="sess-1", branch=None):
    payload = {"id": sid, "cwd": cwd}
    if branch:
        payload["git"] = {"branch": branch}
    return _line("session_meta", payload)


def _call(name, arguments, call_id="c1"):
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return _line("response_item", {"type": "function_call", "name": name,
                                   "arguments": arguments, "call_id": call_id})


def _shell(command, call_id="c1"):
    return _call("shell", {"command": command}, call_id)


def _output(call_id, output):
    if not isinstance(output, str):
        output = json.dumps(output)
    return _line("response_item", {"type": "function_call_output", "call_id": call_id,
                                   "output": output})


def _rollout(tmp_path, items, name="rollout-2026-10-08T09-00-00-test.jsonl"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [item if isinstance(item, str) else json.dumps(item) for item in items]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _parse_items(tmp_path, items):
    return codex.parse(_rollout(tmp_path, items))


@pytest.fixture
def fixture_run():
    """A fresh parse of the fixture for each test (apply_costs and assess mutate the Run)."""
    return codex.parse(CODEX_FIXTURE)


# ---------------------------------------------------------------- parsing the fixture

def test_session_header_fields(fixture_run):
    run = fixture_run
    assert run.agent == "codex"
    assert run.session_id == "0198c3f2-5b7e-7a41-9d3e-6c2f1a8b4e10"
    assert run.cwd == CWD
    assert run.git_branch == "fix/retry-backoff"
    assert run.started == "2026-10-08T09:12:04.102Z"
    assert run.ended == "2026-10-08T09:12:10.050Z"


def test_environment_context_is_not_a_prompt(fixture_run):
    assert fixture_run.prompts == [PROMPT]


def test_final_message_is_last_assistant_message(fixture_run):
    assert fixture_run.final_message == FINAL


def test_steps_use_canonical_tool_names_in_order(fixture_run):
    assert [s.tool for s in fixture_run.steps] == [
        "Bash", "update_plan", "Edit", "Write", "Delete", "Bash", "mcp__github__search_issues"]
    assert [s.index for s in fixture_run.steps] == list(range(1, 8))


def test_shell_command_is_unwrapped_from_bash_lc(fixture_run):
    step = fixture_run.steps[0]
    assert step.input == {"command": "rg -n retry src"}
    assert step.tool_use_id == "call_rg_01"
    assert step.model == "gpt-5-codex"


def test_result_text_is_unwrapped_from_output_json(fixture_run):
    rg = fixture_run.steps[0]
    assert rg.result_text.startswith("src/payments/retry.ts:14:export async function withRetry")
    assert rg.is_error is False


def test_plain_text_output_is_kept_as_is(fixture_run):
    mcp = fixture_run.steps[6]
    assert mcp.input == {"query": "repo:acme/payments-service retry backoff", "limit": 5}
    assert mcp.result_text == '[{"number": 214, "title": "Retry gives up after first failure"}]'


def test_patch_fixture_maps_update_add_delete(fixture_run):
    edit, write, delete = fixture_run.steps[2:5]
    assert edit.input == {
        "file_path": "src/payments/retry.ts",
        "old_string": "const MAX_ATTEMPTS = 1;\nconst sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));",
        "new_string": "const MAX_ATTEMPTS = 3;\nconst BASE_DELAY_MS = 200;\nconst sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));",
    }
    assert write.input["file_path"] == "tests/retry.spec.ts"
    assert write.input["content"].startswith('import { withRetry } from "../src/payments/retry";\n\ntest(')
    assert write.input["content"].endswith("  expect(calls).toBe(3);\n});\n")
    assert delete.input == {"file_path": "tests/old.spec.ts"}


def test_env_read_is_a_shell_step(fixture_run):
    env_step = fixture_run.steps[5]
    assert env_step.input == {"command": "cat .env"}
    assert env_step.result_text.startswith("STRIPE_API_KEY=")


# ---------------------------------------------------------------- tool mapping

def test_powershell_argv_maps_to_powershell_tool(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _shell(["powershell.exe", "-NoProfile", "-Command", "Get-ChildItem src"], "ps1"),
    ])
    assert run.steps[0].tool == "PowerShell"
    assert run.steps[0].input == {"command": "Get-ChildItem src"}


def test_plain_argv_is_shell_quoted(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _shell(["rg", "-n", "hello world", "src"], "argv"),
        _shell("git status", "str"),
    ])
    assert run.steps[0].tool == "Bash"
    assert run.steps[0].input == {"command": "rg -n 'hello world' src"}
    assert run.steps[1].input == {"command": "git status"}


def test_local_shell_call_maps_to_shell_tool(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _line("response_item", {"type": "local_shell_call", "call_id": "ls1",
                                "action": {"type": "exec", "command": ["pwsh", "-Command", "git log -1"]}}),
        _output("ls1", "commit abc123"),
    ])
    step = run.steps[0]
    assert step.tool == "PowerShell"
    assert step.input == {"command": "git log -1"}
    assert step.result_text == "commit abc123"


def test_mcp_and_unknown_tool_names(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _call("docs__search", {"q": "retry"}, "d1"),
        _call("mcp__docs__search", {"q": "x"}, "d2"),
        _call("update_plan", {"plan": []}, "u1"),
    ])
    assert [s.tool for s in run.steps] == ["mcp__docs__search", "mcp__docs__search", "update_plan"]
    assert run.steps[0].input == {"q": "retry"}


def test_nonzero_exit_code_sets_is_error(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _shell("pytest", "t1"),
        _output("t1", {"output": "boom", "metadata": {"exit_code": 1}}),
        _shell("ls", "t2"),
        _output("t2", {"output": "ok", "metadata": {"exit_code": 0}}),
        _shell("pwd", "t3"),
        _output("t3", "/plain/text"),
    ])
    assert (run.steps[0].is_error, run.steps[0].result_text) == (True, "boom")
    assert run.steps[1].is_error is False
    assert run.steps[2].is_error is False and run.steps[2].result_text == "/plain/text"


def test_command_end_event_supplies_result_when_output_missing(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _shell(["bash", "-lc", "make test"], "m1"),
        _line("event_msg", {"type": "exec_command_end", "call_id": "m1", "exit_code": 2,
                            "aggregated_output": "FAILED tests/test_x.py"}),
    ])
    step = run.steps[0]
    assert step.input == {"command": "make test"}
    assert step.result_text == "FAILED tests/test_x.py"
    assert step.is_error is True


def test_shell_apply_patch_is_treated_as_patch(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _shell(["apply_patch", "*** Begin Patch\n*** Delete File: old.txt\n*** End Patch\n"], "a1"),
        _shell(["bash", "-lc", "apply_patch <<'EOF'\n*** Begin Patch\n*** Delete File: b.txt\n"
                               "*** End Patch\nEOF"], "a2"),
    ])
    assert [(s.tool, s.input) for s in run.steps] == [
        ("Delete", {"file_path": "old.txt"}),
        ("Delete", {"file_path": "b.txt"}),
    ]


def test_risk_flags_env_read_in_fixture(fixture_run):
    apply_costs(fixture_run)
    _, _, risks = assess(fixture_run)
    secret = [r for r in risks if r.code == "secret_file"]
    assert secret and secret[0].step == 6
    assert "(.env)" in secret[0].reason


def test_risk_flags_deleted_test_file(fixture_run):
    _, _, risks = assess(fixture_run)
    assert any(r.code == "test_deleted" and r.step == 5 for r in risks)


# ---------------------------------------------------------------- patch parsing

def test_patch_move_edits_destination_and_deletes_source():
    patch = "\n".join(["*** Begin Patch", "*** Update File: src/old_name.py",
                       "*** Move to: src/new_name.py", "@@", "-x = 1", "+x = 2", "*** End Patch"])
    assert codex.patch_to_steps(patch) == [
        ("Edit", {"file_path": "src/new_name.py", "old_string": "x = 1", "new_string": "x = 2"}),
        ("Delete", {"file_path": "src/old_name.py"}),
    ]


def test_patch_pure_rename_keeps_destination_visible():
    patch = "*** Begin Patch\n*** Update File: notes.txt\n*** Move to: .env\n*** End Patch"
    assert codex.patch_to_steps(patch) == [
        ("Edit", {"file_path": ".env", "old_string": "", "new_string": ""}),
        ("Delete", {"file_path": "notes.txt"}),
    ]


def test_patch_multiple_hunks_become_multiedit():
    patch = "\n".join(["*** Begin Patch", "*** Update File: app/calc.py",
                       "@@ def add(a, b):", "-    return a - b", "+    return a + b",
                       "@@ def mul(a, b):", "-    return a * 2", "+    return a * b",
                       "*** End Patch"])
    assert codex.patch_to_steps(patch) == [("MultiEdit", {
        "file_path": "app/calc.py",
        "edits": [
            {"old_string": "    return a - b", "new_string": "    return a + b"},
            {"old_string": "    return a * 2", "new_string": "    return a * b"},
        ],
    })]


def test_patch_add_file_content_and_empty_file():
    patch = "*** Begin Patch\n*** Add File: a.txt\n+a\n+\n+b\n*** Add File: empty.txt\n*** End Patch"
    assert codex.patch_to_steps(patch) == [
        ("Write", {"file_path": "a.txt", "content": "a\n\nb\n"}),
        ("Write", {"file_path": "empty.txt", "content": ""}),
    ]


def test_patch_ignores_text_outside_the_envelope():
    patch = "preamble noise\n*** Begin Patch\n*** Delete File: gone.txt\n*** End Patch\ntrailing"
    assert codex.patch_to_steps(patch) == [("Delete", {"file_path": "gone.txt"})]


# ---------------------------------------------------------------- token math

def test_usage_moves_cached_tokens_and_keeps_reasoning_inside_output():
    u = codex._usage({"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 250,
                      "reasoning_output_tokens": 100, "total_tokens": 1250})
    assert u == Usage(input_tokens=600, output_tokens=250, cache_write_tokens=0, cache_read_tokens=400)
    assert u.total == 1250


def test_run_usage_is_last_cumulative_total(fixture_run):
    assert fixture_run.usage == Usage(input_tokens=5490, output_tokens=1023,
                                      cache_write_tokens=0, cache_read_tokens=10240)
    assert fixture_run.usage.total == 16753  # equals the last total_tokens in the fixture
    assert fixture_run.models == {"gpt-5-codex": fixture_run.usage}


def test_step_usage_is_split_per_call_and_sums_to_attributed(fixture_run):
    # Call 1 (rg, update_plan): 4072 input, 2048 cached, 312 output -> two equal shares.
    assert fixture_run.steps[0].usage == Usage(2036, 156, 0, 1024)
    assert fixture_run.steps[1].usage == Usage(2036, 156, 0, 1024)
    # Call 2 (five steps): 922 input, 7168 cached, 593 output, shares differ by at most one.
    shares = [s.usage for s in fixture_run.steps[2:]]
    assert sum(u.input_tokens for u in shares) == 922
    assert sum(u.output_tokens for u in shares) == 593
    assert sum(u.cache_read_tokens for u in shares) == 7168
    # Call 3 issued no steps, so it counts toward the run only.
    total_steps = Usage()
    for s in fixture_run.steps:
        total_steps.add(s.usage)
    assert total_steps == Usage(4994, 905, 0, 9216)


def test_usage_without_last_counts_is_split_evenly(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _shell("ls", "e1"),
        _shell("pwd", "e2"),
        _line("event_msg", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                  "output_tokens": 250, "reasoning_output_tokens": 100},
            "last_token_usage": None}}),
    ])
    assert run.usage == Usage(600, 250, 0, 400)
    assert run.steps[0].usage == Usage(300, 125, 0, 200)
    assert run.steps[1].usage == Usage(300, 125, 0, 200)


def test_split_parts_add_back_exactly():
    parts = codex._split(Usage(10, 7, 3, 4), 3)
    assert parts == [Usage(4, 3, 1, 2), Usage(3, 2, 1, 1), Usage(3, 2, 1, 1)]


def test_null_token_info_is_ignored(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _line("event_msg", {"type": "token_count", "info": None, "rate_limits": {}}),
    ])
    assert run.usage == Usage()
    assert run.models == {"unknown": Usage()}  # no turn_context in this rollout


# ---------------------------------------------------------------- detect

def test_detect_codex_fixture_is_true():
    assert codex.detect(CODEX_FIXTURE) is True


def test_detect_claude_fixture_is_false():
    assert codex.detect(CLAUDE_FIXTURE) is False


def test_detect_rollout_under_sessions_folder_without_header(tmp_path):
    path = tmp_path / "sessions" / "2026" / "10" / "08" / "rollout-2026-10-08T09-00-00-x.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not json\n", encoding="utf-8")
    assert codex.detect(path) is True


def test_detect_old_header_without_type(tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps({"id": "abc", "timestamp": "2025-01-01T00:00:00Z",
                                "instructions": "be brief"}) + "\n", encoding="utf-8")
    assert codex.detect(path) is True


def test_detect_requires_jsonl_suffix(tmp_path):
    path = tmp_path / "sessions" / "rollout-x.txt"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_meta_line()) + "\n", encoding="utf-8")
    assert codex.detect(path) is False


def test_detect_other_jsonl_is_false(tmp_path):
    path = tmp_path / "other.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n", encoding="utf-8")
    assert codex.detect(path) is False


def test_adapter_registry_routes_by_content():
    assert adapters.detect(CODEX_FIXTURE).NAME == "codex"
    assert adapters.detect(CLAUDE_FIXTURE).NAME == "claude-code"
    assert adapters.get("codex") is codex


# ---------------------------------------------------------------- find_sessions

def _session_file(root, day, name, cwd):
    folder = root / "sessions" / "2026" / "10" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(_meta_line(cwd=cwd)) + "\n", encoding="utf-8")
    return path


def test_find_sessions_newest_first_with_project_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    a = _session_file(tmp_path, "07", "rollout-2026-10-07T08-00-00-a.jsonl", "D:\\work\\app")
    b = _session_file(tmp_path, "08", "rollout-2026-10-08T08-00-00-b.jsonl", "D:\\work\\other")
    c = _session_file(tmp_path, "08", "rollout-2026-10-08T09-00-00-c.jsonl", "d:/WORK/app/")
    note = _session_file(tmp_path, "08", "notes.jsonl", "D:\\work\\app")
    for path, mtime in ((a, 1_700_000_000), (b, 1_700_000_100), (c, 1_700_000_200), (note, 1_700_000_300)):
        os.utime(path, (mtime, mtime))

    assert codex.find_sessions() == [c, b, a]
    assert codex.find_sessions("D:\\work\\app") == [c, a]
    assert codex.find_sessions("d:/work/app") == [c, a]  # slashes and case ignored on Windows paths
    assert adapters.find_sessions(agent="codex") == [c, b, a]


def test_find_sessions_missing_root_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
    assert codex.find_sessions() == []


def test_codex_home_defaults_to_user_profile(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert codex.codex_home() == Path.home() / ".codex"


# ---------------------------------------------------------------- robustness and old format

def test_malformed_lines_are_skipped(tmp_path):
    run = _parse_items(tmp_path, [
        "not json at all",
        '{"type": "response_item", "payload": ',
        "[1, 2, 3]",
        "42",
        "",
        _line("mystery_kind", {"x": 1}),
        _line("response_item", "not-a-dict"),
        _meta_line(),
        _line("event_msg", {"type": "token_count", "info": None}),
        _line("response_item", {"type": "message", "role": "user", "content": "plain string prompt"}),
        _shell("rg -n retry src", "ok1"),
        "{broken json",
        _output("ok1", "1 match"),
    ])
    assert run.prompts == ["plain string prompt"]
    assert [s.tool for s in run.steps] == ["Bash"]
    assert run.steps[0].result_text == "1 match"


def test_old_format_without_envelope(tmp_path):
    run = _parse_items(tmp_path, [
        {"id": "legacy-1", "timestamp": "2025-06-01T10:00:00Z", "instructions": "You are Codex."},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list the files"}]},
        {"type": "local_shell_call", "call_id": "l1",
         "action": {"type": "exec", "command": ["bash", "-lc", "ls -la"]}},
        {"type": "function_call_output", "call_id": "l1", "output": "total 0"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Here are the files."}]},
    ])
    assert run.session_id == "legacy-1"
    assert run.prompts == ["list the files"]
    assert [(s.tool, s.input, s.result_text) for s in run.steps] == [
        ("Bash", {"command": "ls -la"}, "total 0")]
    assert run.final_message == "Here are the files."
    assert run.started == run.ended == "2025-06-01T10:00:00Z"
    assert run.cwd is None


def test_utf8_bom_on_header_line_is_tolerated(tmp_path):
    path = tmp_path / "rollout-bom.jsonl"
    path.write_text("﻿" + json.dumps(_meta_line(sid="bom-1")) + "\n", encoding="utf-8")
    assert codex.parse(path).session_id == "bom-1"
    assert codex.detect(path) is True


def test_user_instructions_block_is_not_a_prompt(tmp_path):
    run = _parse_items(tmp_path, [
        _meta_line(),
        _line("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<user_instructions>\nBe terse.\n</user_instructions>"}]}),
        _line("event_msg", {"type": "user_message", "message": "fix the typo"}),
    ])
    assert run.prompts == ["fix the typo"]


# ---------------------------------------------------------------- cli-equivalent flow

def test_build_flow_risk_costs_and_receipt(fixture_run):
    # Same steps as runledger.cli.build: parse, costs, templates, risk, receipt.
    run = fixture_run
    apply_costs(run)
    apply_templates(run)
    score, level, risks = assess(run)

    assert run.cost is None
    assert all(s.cost is None for s in run.steps)
    assert unknown_models(run) == ["gpt-5-codex"]
    assert cost_of(Usage(100, 10, 0, 5), "gpt-5-codex") is None
    assert score >= 30 and level in ("Medium", "High")
    assert any(r.code == "secret_file" for r in risks)
    receipt = render(run, score, level, risks, "md")
    assert "Codex CLI" in receipt
    assert "Cost:** n/a" in receipt  # unpriced model shows no cost, no crash
    assert "Touched a secrets file from the shell (.env)" in receipt
