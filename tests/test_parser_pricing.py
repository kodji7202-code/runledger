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
    if hasattr(pricing, "_WARNED"):
        monkeypatch.setattr(pricing, "_WARNED", set())


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
    # A 2M-token prompt counts cache reads, so it bills at the haiku 5.5 tier-2 cache-read rate
    assert cost_of(usage, "claude-haiku-5-5") == pytest.approx(2 * 0.05)


def test_cost_of_unknown_model_is_none():
    assert cost_of(Usage(input_tokens=10), "claude-mystery-9") is None


def test_apply_costs_leaves_steps_with_unknown_usage_unpriced():
    from runledger.parser import Step
    known = Step(1, "Read", {}, "a", "claude-opus-5-5", None, usage=Usage(input_tokens=1000))
    unknown = Step(2, "Read", {}, "b", "claude-opus-5-5", None, usage_known=False)
    run = Run("s", "/x.jsonl", None, None, None, None, [], [known, unknown], "",
              Usage(input_tokens=1000), {"claude-opus-5-5": Usage(input_tokens=1000)})
    pricing.apply_costs(run)
    assert known.cost == pytest.approx(cost_of(Usage(input_tokens=1000), "claude-opus-5-5"))
    assert unknown.cost is None
    md = render(run, 0, "Low", [], "md")
    assert "n/a tok, n/a" in md and "$0.0000" not in md


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
    assert price_for("openai/gpt-5-codex") is None
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
    models = {"gpt-5-codex": Usage(input_tokens=10)}
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


def test_missing_usage_is_priced_as_unknown_not_as_zero():
    """When the agent recorded no usage, a priced model with a zero total must not make the run
    look free: the cost stays unknown (n/a) and is not marked partial."""
    run = _run({"claude-opus-5-5": Usage()})
    run.usage_source = "missing"
    pricing.apply_costs(run)
    assert run.cost is None
    assert not run.cost_partial
    assert "**Cost:** n/a" in render(run, 0, "Low", [], "md")
    assert json.loads(render(run, 0, "Low", [], "json"))["totals"]["cost_usd"] is None


def test_reported_zero_usage_keeps_its_zero_cost():
    """A run whose usage was reported (all counters zero is a real measurement) keeps $0.00."""
    run = _run({"claude-opus-5-5": Usage()})
    run.usage_source = "messages"
    pricing.apply_costs(run)
    assert run.cost == 0.0
    assert not run.cost_partial


# --- partial costs and unattributed tokens -------------------------------------

@pytest.mark.parametrize("fmt", ["md", "html", "json"])
def test_partial_cost_is_marked_as_a_lower_bound(fmt):
    run = _run({"claude-opus-5-5": Usage(input_tokens=1_000_000), "claude-mystery-9": Usage(input_tokens=10)})
    pricing.apply_costs(run)
    assert run.cost == pytest.approx(cost_of(Usage(input_tokens=1_000_000), "claude-opus-5-5"))
    assert run.cost_partial
    assert pricing.partial_note(run) == "1 model unpriced"
    result = render(run, 0, "Low", [], fmt)
    if fmt == "json":
        totals = json.loads(result)["totals"]
        assert totals["cost_partial"] is True
        assert totals["unknown_models"] == ["claude-mystery-9"]
    else:
        assert "1 model unpriced" in result
    if fmt == "md":
        assert "≥ $" in result


@pytest.mark.parametrize("fmt", ["md", "html", "json"])
def test_unattributed_tokens_do_not_ask_for_a_price(fmt):
    run = _run({"claude-opus-5-5": Usage(input_tokens=10), pricing.UNATTRIBUTED: Usage(input_tokens=10)})
    pricing.apply_costs(run)
    assert unknown_models(run) == []
    assert run.cost_partial
    assert pricing.partial_note(run) == "unattributed tokens unpriced"
    result = render(run, 0, "Low", [], fmt)
    assert pricing.PRICING_HELP not in result
    if fmt == "json":
        payload = json.loads(result)
        assert payload["pricing_note"] is None
        assert payload["totals"]["cost_partial"] is True
    else:
        assert "cannot be credited to one model" in result


def test_complete_cost_is_not_partial():
    run = _run({"claude-opus-5-5": Usage(input_tokens=10), "claude-mystery-9": Usage()})
    pricing.apply_costs(run)
    assert not run.cost_partial
    assert pricing.partial_note(run) == ""


# --- F5: normalization, verified prices, 1h cache, long-context, staleness ----

@pytest.mark.parametrize("model, expected", [
    ("claude-opus-4-6[1m]", ("opus", "4.6")),
    ("claude-sonnet-4-5-20250929[1m]", ("sonnet", "4.5")),
    ("Claude-Opus-4-6[1M]", ("opus", "4.6")),
    ("claude-opus-4-1[1m]", ("opus", "4.1")),
    ("custom:claude-opus-4-5-0", ("opus", "4.5")),
    ("custom:Claude-Opus-4.5-[Proxy]-0", ("opus", "4.5")),
    ("anthropic/claude-3.5-haiku-latest", ("haiku", "3.5")),
    ("models/gemini-2.5-pro", ("gemini", "2.5-pro")),
    ("openai/gpt-5-codex-2026-10-01", ("gpt", "5-codex")),
    ("anthropic.claude-sonnet-4-5-20250929-v1:0", ("sonnet", "4.5")),
    ("us.anthropic.claude-opus-4-6-v1:0", ("opus", "4.6")),
    ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", ("haiku", "4.5")),
    ("global.anthropic.claude-opus-4-6-v1", ("opus", "4.6")),
])
def test_normalize_model_brackets_custom_and_provider_prefixes(model, expected):
    assert model_key(model) == expected
    assert price_for("claude-opus-4-6[1m]") == (5.0, 25.0, 0.5)
    assert price_for("custom:Claude-Opus-4.5-[Proxy]-0") == (5.0, 25.0, 0.5)


def test_unverified_variants_stay_unpriced():
    assert price_for("claude-opus-4-6-fast") is None
    assert price_for("gpt-5-chat-latest") is None
    assert price_for("gemini-2.5-pro-preview-06-05") is None
    assert price_for("gemini-3.1-pro-preview") is None
    assert price_for("gemini-3-flash-preview") == (0.5, 3.0, 0.05)


def test_verified_openai_models_in_prices_json():
    assert price_for("gpt-6-astra") == (10.0, 50.0, 1.0)
    assert price_for("gpt-6.1-sol") == (2.0, 10.0, 0.1)
    assert price_for("gpt-6-luna") == (0.1, 0.5, 0.01)
    assert price_for("gpt-5.6-sol") == (4.0, 20.0, 0.4)
    assert price_for("gpt-5.6-cyber") == (12.5, 75.0, 1.25)
    assert price_for("gpt-5.3-codex") == (1.75, 14.0, 0.175)
    assert price_for("gpt-4o") is None
    assert price_for("gpt-5-codex") is None


def test_verified_google_gemini_models_in_prices_json():
    assert price_for("gemini-4-argon") == (2.0, 10.0, 0.1)
    assert price_for("gemini-4-argon", date="2026-12-01") == (4.0, 20.0, 0.2)
    assert price_for("gemini-3.8-flash") == (0.75, 3.75, 0.075)
    assert price_for("gemini-3.8-flash", date="2027-01-01") == (1.5, 7.5, 0.15)
    assert price_for("gemini-3.6-flash") == (0.75, 3.75, 0.075)
    assert price_for("gemini-3.6-flash", date="2027-01-01") == (1.5, 7.5, 0.15)
    assert price_for("gemini-3.5-flash-lite") == (0.3, 2.5, 0.03)
    assert price_for("gemini-3.1-flash-lite") == (0.25, 1.5, 0.025)
    assert price_for("gemini-3-flash-preview") == (0.5, 3.0, 0.05)
    assert price_for("gemini-2.5-pro") == (1.25, 10.0, 0.125)
    assert price_for("gemini-2.5-flash") == (0.3, 2.5, 0.03)
    assert price_for("gemini-2.5-flash-lite") == (0.1, 0.4, 0.01)


def test_all_twenty_v060_anthropic_keys_preserved():
    v060_expected = {
        ("haiku", "5.5"): (0.1, 0.5, 0.01),
        ("sonnet", "5.5"): (2.0, 10.0, 0.1),
        ("opus", "5.5"): (4.0, 20.0, 0.2),
        ("sonnet", "5"): (2.0, 10.0, 0.2),
        ("opus", "5"): (5.0, 25.0, 0.5),
        ("opus", "4.8"): (5.0, 25.0, 0.5),
        ("opus", "4.7"): (5.0, 25.0, 0.5),
        ("opus", "4.6"): (5.0, 25.0, 0.5),
        ("sonnet", "4.6"): (3.0, 15.0, 0.3),
        ("opus", "4.5"): (5.0, 25.0, 0.5),
        ("sonnet", "4.5"): (3.0, 15.0, 0.3),
        ("haiku", "4.5"): (1.0, 5.0, 0.1),
        ("opus", "4.1"): (15.0, 75.0, 1.5),
        ("opus", "4"): (15.0, 75.0, 1.5),
        ("sonnet", "4"): (3.0, 15.0, 0.3),
        ("haiku", "3.5"): (0.8, 4.0, 0.08),
        ("fable", "5.1"): (10.0, 50.0, 0.25),
        ("fable", "5"): (10.0, 50.0, 1.0),
        ("mythos", "5.1"): (10.0, 50.0, 0.25),
        ("mythos", "5"): (10.0, 50.0, 1.0),
    }
    for k, v in v060_expected.items():
        assert pricing.PRICES[k] == v


def test_backward_compatible_three_field_prices_json(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({
        "opus:4.6": {"input": 5, "output": 25, "cache_read": 0.5},
    }), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_PRICES", str(legacy))
    table = load_prices()
    monkeypatch.setattr(pricing, "PRICES", table)
    assert price_for("claude-opus-4-6") == (5.0, 25.0, 0.5)
    assert cost_of(Usage(cache_write_tokens=1_000_000), "claude-opus-4-6") == pytest.approx(5.0 * 1.25)


def test_invalid_runledger_prices_warns_once_per_process(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_PRICES", str(bad))
    load_prices()
    load_prices()
    load_prices()
    err = capsys.readouterr().err
    assert err.count("warning:") == 1


def test_staleness_flag_in_receipts_not_on_list(tmp_path, monkeypatch, capsys):
    from runledger.cli import main
    monkeypatch.setattr(pricing, "CHECKED", "2020-01-01")
    assert pricing.is_stale() is True
    run = _run({"claude-opus-4-6": Usage(input_tokens=1000)})
    pricing.apply_costs(run)
    for fmt in ("json", "md", "html"):
        out = render(run, 0, "Low", [], fmt)
        assert "2020-01-01" in out
        if fmt == "json":
            assert "2020-01-01" in (json.loads(out)["pricing_note"] or "")
    # Non-receipt commands do not spam stderr with staleness warnings
    proj = tmp_path / "projects" / "demo"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(json.dumps({
        "sessionId": "s1",
        "cwd": str(tmp_path),
        "type": "user",
        "message": {"content": "hello"},
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    capsys.readouterr()
    assert main(["list", "--all"]) == 0
    assert capsys.readouterr().err == ""


def test_one_hour_cache_writes_billed_at_2x_in_parser_and_cost_of(tmp_path):
    path = tmp_path / "1h.jsonl"
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {
            "id": "m1",
            "model": "claude-opus-4-6",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 1_000_000,
                "cache_read_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 1_000_000,
                },
            },
            "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}],
        },
    }) + "\n", encoding="utf-8")
    run = parse_session(path)
    assert run.usage.cache_write_1h_tokens == 1_000_000
    assert run.usage.cache_write_tokens == 1_000_000
    pricing.apply_costs(run)
    assert run.cost == pytest.approx(10.0)


def test_long_context_tier_applies_per_request_when_threshold_exceeded():
    # gemini-2.5-pro threshold is 200k prompt tokens
    assert cost_of(Usage(input_tokens=250_000, output_tokens=10_000), "gemini-2.5-pro") == pytest.approx(
        0.25 * 2.50 + 0.01 * 15.0
    )
    assert cost_of(Usage(input_tokens=150_000, output_tokens=10_000), "gemini-2.5-pro") == pytest.approx(
        0.15 * 1.25 + 0.01 * 10.0
    )
    # gpt-6-astra threshold is 256k prompt tokens ($20 input / $75 output vs $10 / $50)
    assert cost_of(Usage(input_tokens=300_000), "gpt-6-astra") == pytest.approx(0.30 * 20.0)
    # opus 4.6 has NO long-context premium even past 200k
    assert cost_of(Usage(input_tokens=500_000), "claude-opus-4-6[1m]") == pytest.approx(0.5 * 5.0)


def test_long_context_tier_evaluated_per_request_not_session_total(tmp_path):
    path = tmp_path / "three_turns.jsonl"
    lines = []
    for i in range(1, 4):
        lines.append(json.dumps({
            "type": "assistant",
            "message": {
                "id": f"m{i}",
                "model": "gemini-2.5-pro",
                "usage": {"input_tokens": 100_000, "output_tokens": 0},
                "content": [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": "a.py"}}],
            },
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run = parse_session(path)
    pricing.apply_costs(run)
    # 3 requests x 100k <= 200k threshold -> base tier $1.25/MTok -> $0.375 (not $0.75)
    assert run.usage.input_tokens == 300_000
    assert run.cost == pytest.approx(0.375)


def test_haiku_5_5_long_context_tier_is_restored():
    # A request above the 100k prompt threshold bills whole at tier 2 ($0.50/$2.50/$0.05)
    assert cost_of(Usage(input_tokens=150_000, output_tokens=1_000), "claude-haiku-5-5") == pytest.approx(0.0775)
    assert cost_of(Usage(input_tokens=60_000, cache_read_tokens=50_000), "claude-haiku-5-5") == pytest.approx(0.0325)
    assert cost_of(Usage(input_tokens=100_000), "claude-haiku-5-5") == pytest.approx(0.01)
    # Cache writes count toward the prompt, so their own tier follows the same rule
    assert cost_of(Usage(cache_write_tokens=1_000_000, cache_write_1h_tokens=1_000_000),
                   "claude-haiku-5-5") == pytest.approx(1.0)
    assert cost_of(Usage(cache_write_tokens=50_000, cache_write_1h_tokens=50_000),
                   "claude-haiku-5-5") == pytest.approx(0.01)
    assert cost_of(Usage(cache_write_tokens=1_000_000), "claude-haiku-5-5") == pytest.approx(0.625)
    assert cost_of(Usage(cache_write_tokens=50_000), "claude-haiku-5-5") == pytest.approx(0.00625)


def test_haiku_5_5_long_context_tier_is_evaluated_per_request(tmp_path):
    def transcript(name, turns):
        lines = [json.dumps({
            "type": "assistant",
            "message": {
                "id": f"m{i}",
                "model": "claude-haiku-5-5",
                "usage": usage,
                "content": [{"type": "tool_use", "id": f"t{i}", "name": "Read",
                             "input": {"file_path": "a.py"}}],
            },
        }) for i, usage in enumerate(turns, start=1)]
        path = tmp_path / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return parse_session(path)

    one_long = transcript("long.jsonl", [{"input_tokens": 150_000, "output_tokens": 1_000}])
    pricing.apply_costs(one_long)
    assert one_long.cost == pytest.approx(0.0775)

    two_short = transcript("short.jsonl", [{"input_tokens": 75_000, "output_tokens": 500},
                                           {"input_tokens": 75_000, "output_tokens": 500}])
    pricing.apply_costs(two_short)
    assert two_short.cost == pytest.approx(0.0155)

    cached = transcript("cached.jsonl", [{"input_tokens": 60_000, "cache_read_input_tokens": 50_000}])
    pricing.apply_costs(cached)
    assert cached.cost == pytest.approx(0.0325)

    at_threshold = transcript("threshold.jsonl", [{"input_tokens": 100_000}])
    pricing.apply_costs(at_threshold)
    assert at_threshold.cost == pytest.approx(0.01)


def test_inactive_user_price_rows_keep_the_bundled_rates(tmp_path, monkeypatch):
    user = tmp_path / "override.json"
    user.write_text(json.dumps({
        "sonnet:4.5": {"input": 1, "output": 1, "cache_read": 1, "valid_until": "2020-01-01"},
    }), encoding="utf-8")
    monkeypatch.setattr(pricing, "PRICES", load_prices(user))
    assert cost_of(Usage(input_tokens=1_000_000), "claude-sonnet-4-5") == pytest.approx(3.0)

    user.write_text(json.dumps({
        "sonnet:4.5": {"input": 1, "output": 1, "cache_read": 1, "valid_from": "2999-01-01"},
        "mystery:9": {"input": 1, "output": 1, "cache_read": 1, "valid_until": "2020-01-01"},
    }), encoding="utf-8")
    monkeypatch.setattr(pricing, "PRICES", load_prices(user))
    assert cost_of(Usage(input_tokens=1_000_000), "claude-sonnet-4-5") == pytest.approx(3.0)
    assert price_for("mystery-9") is None

    user.write_text(json.dumps({
        "sonnet:4.5": {"input": 1, "output": 1, "cache_read": 1,
                       "valid_from": "2020-01-01", "valid_until": "2999-01-01"},
    }), encoding="utf-8")
    monkeypatch.setattr(pricing, "PRICES", load_prices(user))
    assert cost_of(Usage(input_tokens=1_000_000), "claude-sonnet-4-5") == pytest.approx(1.0)


def test_windowed_prices_use_the_system_date_without_an_explicit_date(monkeypatch):
    monkeypatch.setattr(pricing, "_today", lambda: "2026-10-17")
    assert price_for("gemini-4-argon") == (2.0, 10.0, 0.1)
    assert cost_of(Usage(input_tokens=10_000, output_tokens=10_000, cache_read_tokens=10_000),
                   "gemini-4-argon") == pytest.approx(0.121)
    monkeypatch.setattr(pricing, "_today", lambda: "2026-12-05")
    assert price_for("gemini-4-argon") == (4.0, 20.0, 0.2)
    assert cost_of(Usage(input_tokens=10_000, output_tokens=10_000, cache_read_tokens=10_000),
                   "gemini-4-argon") == pytest.approx(0.242)
    # A row whose window closed and has no successor is unpriced, never stale
    assert price_for("gpt-5.6-sol") is None
    monkeypatch.setattr(pricing, "_today", lambda: "2026-11-21")
    assert price_for("gpt-5.6-sol") == (4.0, 20.0, 0.4)


def test_pricing_help_carries_the_stale_price_policy():
    assert "120 days" in pricing.PRICING_HELP


def test_parser_retains_user_prompts_starting_with_angle_bracket(tmp_path):
    p1 = tmp_path / "html_prompt.jsonl"
    p1.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "<div>fix layout</div>"}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "<Button prop='x'> fails"}]}}),
    ]) + "\n", encoding="utf-8")
    run1 = parse_session(p1)
    assert run1.prompts == ["<div>fix layout</div>", "<Button prop='x'> fails"]

    p2 = tmp_path / "noise_prompt.jsonl"
    p2.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "<command-name>/clear</command-name>"}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "<system-reminder>hi</system-reminder>"}]}}),
    ]) + "\n", encoding="utf-8")
    run2 = parse_session(p2)
    assert run2.prompts == []

