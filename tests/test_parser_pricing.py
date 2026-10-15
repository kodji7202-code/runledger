import json
import os
import re
from pathlib import Path

import pytest

from runledger import pricing
from runledger.parser import Run, Usage, encode_project_path, find_sessions, parse_session
from runledger.pricing import cost_of, load_prices, model_key, price_for, unknown_models
from runledger.receipt import render


@pytest.fixture(autouse=True)
def isolated_user_prices(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv(pricing.PRICES_ENV, raising=False)


# --- encode_project_path -----------------------------------------------------

def test_encode_posix_absolute_is_not_resolved():
    assert encode_project_path("/home/dev/my.app") == "-home-dev-my-app"


def test_encode_windows_absolute_backslash():
    assert encode_project_path("D:\\runledger") == "D--runledger"


def test_encode_windows_absolute_forward_slash():
    assert encode_project_path("C:/Users/me/my app") == "C--Users-me-my-app"


def test_encode_relative_path_is_resolved_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = re.sub(r"[^A-Za-z0-9]", "-", os.path.join(os.getcwd(), "my.app"))
    assert encode_project_path("my.app") == expected


def test_find_sessions_uses_encoded_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    projects = tmp_path / "projects"
    (projects / "D--runledger").mkdir(parents=True)
    session = projects / "D--runledger" / "abc.jsonl"
    session.write_text("", encoding="utf-8")
    (projects / "C--other").mkdir()
    (projects / "C--other" / "xyz.jsonl").write_text("", encoding="utf-8")

    assert find_sessions("D:\\runledger") == [session]
    assert find_sessions("D:/runledger") == [session]
    assert find_sessions("D:\\missing") == []


def test_multi_tool_message_usage_split_keeps_integer_remainders(tmp_path):
    path = tmp_path / "split.jsonl"
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {
            "id": "m1", "model": "claude-haiku-5-5",
            "usage": {"input_tokens": 5, "output_tokens": 3,
                      "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1},
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}},
                {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "b.py"}},
            ],
        },
    }) + "\n", encoding="utf-8")

    run = parse_session(path)
    summed = Usage()
    for step in run.steps:
        summed.add(step.usage)

    assert summed == run.usage == Usage(5, 3, 1, 1)
    assert run.steps[0].usage is not run.steps[1].usage


# --- model_key ---------------------------------------------------------------

@pytest.mark.parametrize("model, expected", [
    ("claude-opus-5-5", ("opus", "5.5")),
    ("claude-haiku-5-5", ("haiku", "5.5")),
    ("claude-sonnet-4-5-20250929", ("sonnet", "4.5")),
    ("claude-3-5-haiku-x", ("haiku", "3.5")),
    ("claude-opus-4-20250514", ("opus", "4")),
    ("claude-3-5-haiku-20241022", ("haiku", "3.5")),
    ("claude-3-haiku-20240307", ("haiku", "3")),
    (None, None),
    ("gpt-4o", ("gpt", "4o")),
    (" OpenAI/GPT-6.1-SOL-LATEST ", ("gpt", "6.1-sol")),
    ("openai/gpt-5-codex-2026-10-01", ("gpt", "5-codex")),
    ("o3", ("o", "3")),
    ("anthropic/CLAUDE-SONNET-4.5-20250929", ("sonnet", "4.5")),
    ("anthropic/claude-3.5-haiku-latest", ("haiku", "3.5")),
])
def test_model_key(model, expected):
    assert model_key(model) == expected


# --- cost_of -----------------------------------------------------------------

def test_cost_of_counts_cache_write_and_read_tokens():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000,
                  cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)
    # opus 5.5: $4 input, $20 output, cache write 1.25x input, cache read $0.20 per MTok
    assert cost_of(usage, "claude-opus-5-5") == pytest.approx(4 + 20 + 5 + 0.2)


def test_cost_of_cache_read_only():
    usage = Usage(cache_read_tokens=2_000_000)
    assert cost_of(usage, "claude-haiku-5-5") == pytest.approx(2 * 0.01)


def test_cost_of_unknown_model_is_none():
    assert cost_of(Usage(input_tokens=10), "claude-mystery-9") is None


@pytest.mark.parametrize("model", [
    "ANTHROPIC/CLAUDE-SONNET-4-5",
    "anthropic/claude-sonnet-4.5-latest",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5-2025-09-29",
    "claude-sonnet-4-5@20250929",
    "claude-sonnet-4-5-20250929-v1:0",
    "claude-sonnet-4-5-v2.0-latest",
    "claude-4.5-sonnet-latest",
])
def test_price_lookup_normalizes_provider_case_dates_and_versions(model):
    assert price_for(model) == price_for("claude-sonnet-4-5")


def test_normalization_does_not_guess_model_versions_or_variants():
    assert price_for("openai/gpt-6.1-sol") is None
    assert price_for("anthropic/claude-sonnet-99-latest") is None
    assert model_key("gpt-5-codex") != model_key("gpt-5")
    assert model_key("gpt-4o-mini") != model_key("gpt-4o")
    assert model_key("gpt-6.1-sol") != model_key("gpt-6.1")


# --- price table and env override --------------------------------------------

def test_defaults_load_from_prices_json(monkeypatch):
    monkeypatch.delenv(pricing.PRICES_ENV, raising=False)
    raw = json.loads(Path(pricing.__file__).with_name("prices.json").read_text(encoding="utf-8"))
    table = load_prices()
    assert len(table) == len(raw) - 1  # minus the "checked" date
    assert table[("opus", "5.5")] == (4.0, 20.0, 0.2)
    assert table == pricing.PRICES


def test_env_override_merges_over_defaults(tmp_path, monkeypatch):
    override = tmp_path / "my-prices.json"
    override.write_text(json.dumps({
        "checked": "2026-10-09",
        "opus:5.5": {"input": 1.0, "output": 2.0, "cache_read": 0.1},
        "newfam:1": {"input": 3.0, "output": 6.0, "cache_read": 0.3},
    }), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_PRICES", str(override))

    table = load_prices()
    assert table[("opus", "5.5")] == (1.0, 2.0, 0.1)      # replaced
    assert table[("newfam", "1")] == (3.0, 6.0, 0.3)      # added
    assert table[("haiku", "5.5")] == (0.1, 0.5, 0.01)    # default kept

    monkeypatch.setattr(pricing, "PRICES", table)
    assert cost_of(Usage(input_tokens=1_000_000), "claude-opus-5-5") == pytest.approx(1.0)


def test_override_warns_and_ignores_entire_malformed_table(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "opus:5.5": {"input": 99, "output": 99, "cache_read": 99},
        "gpt:6.1-sol": {"input": 1.0},
    }), encoding="utf-8")
    assert load_prices(bad)[("opus", "5.5")] == pricing.PRICES[("opus", "5.5")]
    warning = capsys.readouterr().err
    assert "warning: ignoring prices file" in warning
    assert str(bad) in warning and "cache_read" in warning


@pytest.mark.parametrize("content", [
    "{bad", "[]", '{"bad-key": {}}',
    '{"gpt:6.1-sol": {"input": -1, "output": 1, "cache_read": 0}}',
    '{"gpt:6.1-sol": {"input": true, "output": 1, "cache_read": 0}}',
    '{"gpt:6.1-sol": {"input": NaN, "output": 1, "cache_read": 0}}',
    '{"gpt:6.1-sol": {"input": Infinity, "output": 1, "cache_read": 0}}',
    '{"gpt:6.1-sol": {"input": 1e999, "output": 1, "cache_read": 0}}',
    b"\xff",
])
def test_invalid_user_prices_warn_and_keep_bundled_prices(tmp_path, capsys, content):
    path = tmp_path / ".runledger" / "prices.json"
    path.parent.mkdir()
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    assert load_prices() == pricing.PRICES
    assert "warning: ignoring prices file" in capsys.readouterr().err


def test_missing_default_user_file_is_silent(capsys):
    assert load_prices() == pricing.PRICES
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_unreadable_explicit_override_warns(tmp_path, monkeypatch, capsys, kind):
    path = tmp_path / "override.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        real_read = Path.read_text

        def read(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("Permission denied")
            return real_read(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setenv("RUNLEDGER_PRICES", str(path))
    assert load_prices() == pricing.PRICES
    assert "warning: ignoring prices file" in capsys.readouterr().err


def test_unexpandable_user_path_warns(monkeypatch, capsys):
    real_expand = Path.expanduser

    def expand(path):
        if str(path).startswith("~unavailable-user/"):
            raise RuntimeError("Could not determine home directory")
        return real_expand(path)

    monkeypatch.setattr(Path, "expanduser", expand)
    monkeypatch.setenv("RUNLEDGER_PRICES", "~unavailable-user/prices.json")
    assert load_prices() == pricing.PRICES
    assert "warning: ignoring prices file" in capsys.readouterr().err


def test_default_user_file_and_env_precedence(tmp_path, monkeypatch):
    user = tmp_path / ".runledger" / "prices.json"
    user.parent.mkdir()
    user.write_text(json.dumps({
        "opus:5.5": {"input": 1, "output": 2, "cache_read": 0},
        "gpt:6.1-sol": {"input": 3, "output": 4, "cache_read": 0},
    }), encoding="utf-8")
    table = load_prices()
    assert table[("opus", "5.5")] == (1, 2, 0)
    assert table[("gpt", "6.1-sol")] == (3, 4, 0)
    assert table[("haiku", "5.5")] == pricing.PRICES[("haiku", "5.5")]
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({
        "gpt:6.1-sol": {"input": 5, "output": 6, "cache_read": 0},
    }), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_PRICES", str(explicit))
    table = load_prices()
    assert table[("gpt", "6.1-sol")] == (5, 6, 0)
    assert table[("opus", "5.5")] == pricing.PRICES[("opus", "5.5")]
    # An explicit argument also takes precedence over the environment variable.
    assert load_prices(user)[("gpt", "6.1-sol")] == (3, 4, 0)
    monkeypatch.setenv("RUNLEDGER_PRICES", "~/.runledger/prices.json")
    assert load_prices()[("gpt", "6.1-sol")] == (3, 4, 0)


def test_custom_non_claude_prices_normalize_and_apply(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    # Synthetic test rates, not published model prices.
    path.write_text(json.dumps({
        "GPT:6.1-SOL": {"input": 1, "output": 2, "cache_read": 0.5},
        "gpt:5-codex": {"input": 3, "output": 4, "cache_read": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(pricing, "PRICES", load_prices(path))
    usage = Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert cost_of(usage, "openai/GPT-6.1-SOL-2026-10-01-v1-latest") == 4.75
    assert cost_of(Usage(input_tokens=1_000_000), "openai/gpt-5-codex-latest") == 3
    assert price_for("gpt-6.1") is None
    assert price_for("gpt-5") is None


# --- unknown_models ----------------------------------------------------------

def _run(models):
    return Run(session_id="s", path="s.jsonl", cwd=None, git_branch=None,
               started=None, ended=None, prompts=[], steps=[], final_message="",
               usage=Usage(), models=models)


def test_unknown_models_lists_only_unpriced_ids():
    run = _run({
        "claude-opus-5-5": Usage(input_tokens=1),
        "claude-mystery-9": Usage(input_tokens=1),
        "unknown": Usage(input_tokens=1),
    })
    assert unknown_models(run) == ["claude-mystery-9", "unknown"]


def test_unknown_models_respects_env_override(tmp_path, monkeypatch):
    override = tmp_path / "prices.json"
    override.write_text(json.dumps({"mystery:9": {"input": 1, "output": 1, "cache_read": 0}}),
                        encoding="utf-8")
    monkeypatch.setattr(pricing, "PRICES", load_prices(override))
    run = _run({"claude-mystery-9": Usage(input_tokens=1)})
    assert unknown_models(run) == []


@pytest.mark.parametrize("fmt", ["md", "html", "json"])
@pytest.mark.parametrize("mixed", [False, True])
def test_receipts_explain_how_to_add_unknown_prices(fmt, mixed):
    models = {"gpt-6.1-sol": Usage(input_tokens=10)}
    if mixed:
        models["claude-opus-5-5"] = Usage(input_tokens=10)
    run = _run(models)
    pricing.apply_costs(run)
    result = render(run, 0, "Low", [], fmt)
    assert "~/.runledger/prices.json" in result
    assert "RUNLEDGER_PRICES" in result
    assert "docs/agents.md#costs-across-agents" in result
    if fmt == "json":
        payload = json.loads(result)
        assert payload["pricing_note"] == pricing.PRICING_HELP
        assert (payload["totals"]["cost_usd"] is None) == (not mixed)


@pytest.mark.parametrize("fmt", ["md", "html", "json"])
def test_receipts_with_known_prices_do_not_need_pricing_help(fmt):
    run = _run({"claude-opus-5-5": Usage(input_tokens=10)})
    pricing.apply_costs(run)
    assert pricing.PRICING_HELP not in render(run, 0, "Low", [], fmt)
