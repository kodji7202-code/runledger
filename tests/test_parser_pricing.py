import json
import os
import re
from pathlib import Path

import pytest

from runledger import pricing
from runledger.parser import Run, Usage, encode_project_path, find_sessions
from runledger.pricing import cost_of, load_prices, model_key, unknown_models


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
    ("gpt-4o", None),
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


def test_override_rejects_malformed_entry(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"opus:5.5": {"input": 1.0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="cache_read"):
        load_prices(bad)


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
