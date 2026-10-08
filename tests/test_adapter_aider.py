"""Tests for the Aider adapter (runledger/adapters/aider.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from runledger import adapters
from runledger.adapters import aider
from runledger.parser import Run
from runledger.pricing import apply_costs
from runledger.risk import assess

HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures" / "aider"
FIXTURE = FIXTURE_DIR / ".aider.chat.history.md"
CLAUDE_FIXTURE = HERE / "fixtures" / "sample_session.jsonl"


def _write(tmp_path: Path, text: str, name: str = ".aider.chat.history.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _tools(run: Run):
    return [s.tool for s in run.steps]


@pytest.fixture
def runs():
    return aider.parse_all(FIXTURE)


@pytest.fixture
def last():
    return aider.parse(FIXTURE)


# --- detection and discovery -------------------------------------------------

def test_detect_true_for_history_file():
    assert aider.detect(FIXTURE) is True


def test_detect_false_for_claude_jsonl():
    assert aider.detect(CLAUDE_FIXTURE) is False


def test_detect_false_for_other_markdown(tmp_path):
    assert aider.detect(tmp_path / "README.md") is False


def test_find_sessions_returns_project_history_file(tmp_path):
    path = _write(tmp_path, "")
    assert aider.find_sessions(str(tmp_path)) == [path]


def test_find_sessions_empty_when_missing_or_no_project(tmp_path):
    assert aider.find_sessions(str(tmp_path)) == []
    assert aider.find_sessions(None) == []


def test_registered_in_adapter_registry():
    assert adapters.get("aider") is aider
    assert adapters.detect(FIXTURE).NAME == "aider"


# --- sessions ----------------------------------------------------------------

def test_parse_all_splits_two_sessions(runs):
    assert len(runs) == 2
    assert [r.started for r in runs] == ["2026-10-01T09:12:44", "2026-10-07T16:41:02"]
    assert all(r.session_id.startswith("aider-") and len(r.session_id) == len("aider-") + 12 for r in runs)
    assert runs[0].session_id != runs[1].session_id


def test_parse_returns_last_session(runs):
    last = aider.parse(FIXTURE)
    assert last.session_id == runs[-1].session_id
    assert last.started == "2026-10-07T16:41:02"


def test_session_id_is_stable_across_parses():
    assert aider.parse(FIXTURE).session_id == aider.parse(FIXTURE).session_id


def test_run_metadata(last):
    assert last.agent == "aider"
    assert Path(last.cwd) == FIXTURE_DIR
    assert last.git_branch is None
    assert last.ended == last.started  # no later timestamps in the session


def test_empty_file_gives_no_sessions(tmp_path):
    path = _write(tmp_path, "")
    assert aider.parse_all(path) == []
    run = aider.parse(path)
    assert run.steps == [] and run.prompts == [] and run.started is None


def test_file_without_header_is_one_session(tmp_path):
    path = _write(tmp_path, "#### hi\n\nHello there.\n")
    runs = aider.parse_all(path)
    assert len(runs) == 1
    assert runs[0].started is None
    assert runs[0].prompts == ["hi"]
    assert runs[0].final_message == "Hello there."


# --- prompts and final message -----------------------------------------------

def test_prompts_skip_housekeeping_and_keep_run_and_test(runs):
    assert runs[0].prompts == ["Make the greeting in app.py say hello to the user by name."]
    assert runs[1].prompts == [
        "Add exponential backoff with jitter to the payment retry loop. Cap the total wait at 30 seconds.",
        "/test pytest tests/test_retry.py -q",
        "Looks good. Summarize the changes for the PR description.",
    ]


def test_final_message_is_last_prose_paragraph(runs):
    assert runs[0].final_message == (
        "I updated `greet()` to take a name. The notes edit did not match the file, "
        "so `docs/notes.md` is unchanged."
    )
    assert runs[1].final_message == "Nothing else in the payments module changed. Run `pytest -q` before merging."
    assert "```" not in runs[1].final_message and "<<<<" not in runs[1].final_message


# --- edits ---------------------------------------------------------------------

def test_edit_kept_only_when_applied_lines_exist(runs):
    # Session 1 has "Applied edit" lines, so the unconfirmed docs/notes.md block is dropped.
    assert [s.input["file_path"] for s in runs[0].steps if s.tool == "Edit"] == ["src/app.py"]


def test_edit_fields_from_search_replace(last):
    edit = next(s for s in last.steps if s.tool == "Edit")
    assert edit.input["file_path"] == "src/payments/retry.py"
    assert "time.sleep(1)" in edit.input["old_string"]
    assert "MAX_TOTAL_WAIT" in edit.input["new_string"]
    assert edit.input["old_string"].endswith("\n")


def test_empty_search_becomes_write(last):
    write = next(s for s in last.steps if s.tool == "Write")
    assert write.input["file_path"] == "tests/test_retry.py"
    assert write.input["content"].startswith("import pytest\n")
    assert "<<<<" not in write.input["content"] and ">>>>" not in write.input["content"]


def test_all_blocks_counted_without_applied_lines(tmp_path):
    path = _write(tmp_path, (
        "# aider chat started at 2026-09-30 10:00:00\n"
        "\n"
        "#### change things\n"
        "\n"
        "a.py\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "x = 1\n"
        "=======\n"
        "x = 2\n"
        ">>>>>>> REPLACE\n"
        "```\n"
        "\n"
        "b.py\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "=======\n"
        "print('new')\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    ))
    run = aider.parse(path)
    assert _tools(run) == ["Edit", "Write"]
    assert run.steps[0].input == {"file_path": "a.py", "old_string": "x = 1\n", "new_string": "x = 2\n"}
    assert run.steps[1].input == {"file_path": "b.py", "content": "print('new')\n"}


def test_malformed_and_truncated_blocks_do_not_crash(tmp_path):
    path = _write(tmp_path, (
        "# aider chat started at 2026-09-30 10:00:00\n"
        "\n"
        "> Main model: anthropic/claude-sonnet-4-5 with diff edit format\n"
        "\n"
        "#### fix it\n"
        "\n"
        "Here is one that is cut off:\n"
        "\n"
        "broken.py\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "x = 1\n"
        "=======\n"
        "x = 2\n"
        "```\n"
        "\n"
        "Here is a block with no file name:\n"
        "\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "a\n"
        "=======\n"
        "b\n"
        ">>>>>>> REPLACE\n"
        "```\n"
        "\n"
        "good.py\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "old\n"
        "=======\n"
        "new\n"
        ">>>>>>> REPLACE\n"
        "```\n"
        "\n"
        "> Tokens: 10 sent, 5 received. Cost: $0.00 message, $0.00 session.\n"
        "> Applied edit to good.py\n"
        "\n"
        "truncated.py\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "never closed\n"
    ))
    runs = aider.parse_all(path)
    assert len(runs) == 1
    assert [s.input["file_path"] for s in runs[0].steps if s.tool == "Edit"] == ["good.py"]


# --- other steps ---------------------------------------------------------------

def test_step_order_and_indices(last):
    assert _tools(last) == ["Read", "Edit", "Write", "Bash", "GitCommit", "Bash"]
    assert [s.index for s in last.steps] == [1, 2, 3, 4, 5, 6]


def test_added_files_become_reads(runs):
    assert [s.input["file_path"] for s in runs[0].steps if s.tool == "Read"] == ["src/app.py"]
    assert [s.input["file_path"] for s in runs[1].steps if s.tool == "Read"] == ["src/payments/retry.py"]


def test_shell_steps_from_running_and_run_test_without_duplicates(last):
    # The "> Running pytest ..." echo of "#### /test ..." is not counted a second time, and the
    # ```bash suggestion in the reply is not a step until aider prints "> Running".
    assert [s.input["command"] for s in last.steps if s.tool == "Bash"] == [
        "rm -rf build/ && pytest -q",
        "pytest tests/test_retry.py -q",
    ]


def test_bash_suggestion_without_running_is_not_a_step(tmp_path):
    path = _write(tmp_path, (
        "# aider chat started at 2026-09-30 10:00:00\n"
        "\n"
        "#### how do I run it\n"
        "\n"
        "Run this:\n"
        "\n"
        "```bash\n"
        "pytest -q\n"
        "```\n"
        "\n"
        "> Tokens: 100 sent, 20 received. Cost: $0.00 message, $0.00 session.\n"
    ))
    assert aider.parse(path).steps == []


def test_commit_step(last):
    commits = [s for s in last.steps if s.tool == "GitCommit"]
    assert len(commits) == 1
    assert commits[0].input == {
        "sha": "7f3c9a2",
        "message": "feat: capped exponential backoff for payment retries",
    }


# --- usage, cost, models -------------------------------------------------------

def test_usage_totals_per_session(runs):
    assert (runs[0].usage.input_tokens, runs[0].usage.output_tokens) == (1200, 340)
    assert (runs[1].usage.input_tokens, runs[1].usage.output_tokens) == (23500, 1700)


def test_reported_cost_is_last_session_value(runs):
    assert runs[0].reported_cost == 0.01
    assert runs[1].reported_cost == 0.11
    assert aider.parse(FIXTURE).reported_cost == 0.11


def test_model_provider_prefix_stripped(runs):
    assert set(runs[0].models) == {"claude-3-5-haiku"}
    assert set(runs[1].models) == {"claude-sonnet-4-5"}
    assert runs[1].models["claude-sonnet-4-5"].input_tokens == 23500


def test_model_prefixes_stripped_when_model_changes(tmp_path):
    path = _write(tmp_path, (
        "# aider chat started at 2026-09-30 10:00:00\n"
        "> Main model: openrouter/anthropic/claude-3-5-haiku with diff edit format\n"
        "#### one\n"
        "\n"
        "Reply one.\n"
        "> Tokens: 1k sent, 100 received. Cost: $0.00 message, $0.00 session.\n"
        "> Main model: gpt-4o with diff edit format\n"
        "#### two\n"
        "\n"
        "Reply two.\n"
        "> Tokens: 2k sent, 200 received. Cost: $0.00 message, $0.00 session.\n"
        "> Main model: anthropic/claude-sonnet-4-5 with diff edit format\n"
        "#### three\n"
        "\n"
        "Reply three.\n"
        "> Tokens: 3k sent, 300 received. Cost: $0.00 message, $0.00 session.\n"
    ))
    models = aider.parse(path).models
    assert {k: (v.input_tokens, v.output_tokens) for k, v in models.items()} == {
        "claude-3-5-haiku": (1000, 100),
        "gpt-4o": (2000, 200),
        "claude-sonnet-4-5": (3000, 300),
    }


def test_token_suffixes_k_and_M(tmp_path):
    path = _write(tmp_path, (
        "# aider chat started at 2026-09-30 10:00:00\n"
        "> Main model: anthropic/claude-sonnet-4-5 with diff edit format\n"
        "#### first\n"
        "\n"
        "Answer one.\n"
        "> Tokens: 1.2M sent, 850 received. Cost: $0.05 message, $0.31 session.\n"
        "#### second\n"
        "\n"
        "Answer two.\n"
        "> Tokens: 1,234 sent, 2.5k received. Cost: $0.02 message, $0.33 session.\n"
    ))
    run = aider.parse(path)
    assert run.usage.input_tokens == 1_200_000 + 1_234
    assert run.usage.output_tokens == 850 + 2_500
    assert run.reported_cost == 0.33


def test_usage_split_across_reply_edits_only(last):
    edit, write = [s for s in last.steps if s.tool in ("Edit", "Write")]
    assert (edit.usage.input_tokens, edit.usage.output_tokens) == (5500, 700)
    assert (write.usage.input_tokens, write.usage.output_tokens) == (5500, 700)
    assert all(s.usage.total == 0 for s in last.steps if s.tool not in ("Edit", "Write"))


# --- risk and pricing on a parsed run ----------------------------------------

def test_risk_flags_rm_rf_and_costs_apply(last):
    score, _level, risks = assess(last)
    rm = [r for r in risks if r.code == "command" and "rm -rf" in r.reason]
    assert rm and rm[0].severity == "high"
    assert score > 0

    apply_costs(last)
    # claude-sonnet-4-5 at $3 in / $15 out per million tokens
    assert last.cost == pytest.approx(23500 * 3 / 1e6 + 1700 * 15 / 1e6)
    assert last.steps[1].cost == pytest.approx(5500 * 3 / 1e6 + 700 * 15 / 1e6)
