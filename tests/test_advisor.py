"""Cost recommendations (runledger/advisor.py): downgrade math against the price table,
thresholds, sorting, unknown pricing and the cache, retry and large-read checks."""
from __future__ import annotations

import pytest

from runledger.advisor import HAIKU, _is_read_only, estimated_cost, recommend
from runledger.parser import Run, Step, Usage
from runledger.pricing import cost_of, price_for

SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
MYSTERY = "claude-mystery-9"
ROOT = "/work/app"
KINDS = {"model_downgrade", "cache_misses", "retry_loop", "large_reads", "unknown_pricing"}


def _usage(inp=0, out=0, cw=0, cr=0):
    return Usage(input_tokens=inp, output_tokens=out, cache_write_tokens=cw, cache_read_tokens=cr)


def _step(index, tool, inp=None, result="", is_error=False, model=SONNET, usage=None):
    return Step(index=index, tool=tool, input=dict(inp or {}), tool_use_id=f"t{index}", model=model,
                timestamp=None, usage=usage or Usage(), result_text=result, is_error=is_error)


def _bash(index, command, result="", is_error=False, **kw):
    return _step(index, "Bash", {"command": command}, result, is_error, **kw)


def _read(index, path="src/a.py", **kw):
    return _step(index, "Read", {"file_path": f"{ROOT}/{path}"}, **kw)


def _edit(index, path="src/a.py", **kw):
    return _step(index, "Edit", {"file_path": f"{ROOT}/{path}", "old_string": "a", "new_string": "b"}, **kw)


def _run(steps):
    steps = list(steps)
    models = {}
    for s in steps:
        models.setdefault(s.model or "unknown", Usage()).add(s.usage)
    total = Usage()
    for usage in models.values():
        total.add(usage)
    return Run(session_id="s1", path="x.jsonl", cwd=ROOT, git_branch=None, started=None, ended=None,
               prompts=["Do the task."], steps=steps, final_message="Done.", usage=total, models=models)


def _recs(run, kind):
    return [r for r in recommend(run) if r["kind"] == kind]


def _rec(run, kind):
    found = _recs(run, kind)
    assert len(found) == 1, f"expected one {kind} recommendation, got {[r['kind'] for r in recommend(run)]}"
    return found[0]


# ---------------------------------------------------------------- model downgrade

def test_downgrade_saving_matches_the_price_table():
    # 1M input tokens: $2.00 on Sonnet 5.5, $0.10 on Haiku 5.5
    run = _run([_read(1, model=SONNET, usage=_usage(inp=1_000_000))])
    rec = _rec(run, "model_downgrade")
    assert rec["est_savings_usd"] == pytest.approx(1.9, abs=1e-4)
    assert rec["steps"] == [1]
    assert rec["title"] == "Run read-only steps on Haiku 5.5"
    assert "estimated" in rec["detail"].lower()


def test_opus_steps_are_priced_at_opus_rates():
    # 0.5M input tokens: $2.00 on Opus 5.5 ($4/M), $0.05 on Haiku 5.5
    run = _run([_step(1, "Grep", {"pattern": "retry"}, model=OPUS, usage=_usage(inp=500_000))])
    assert _rec(run, "model_downgrade")["est_savings_usd"] == pytest.approx(1.95, abs=1e-4)


def test_downgrade_counts_only_the_read_only_steps():
    run = _run([
        _read(1, model=SONNET, usage=_usage(inp=1_000_000)),
        _edit(2, model=SONNET, usage=_usage(inp=1_000_000)),  # an edit stays on its model
        _bash(3, "npm test", result="Tests: 1 passed, 1 total", model=SONNET, usage=_usage(inp=1_000_000)),
    ])
    rec = _rec(run, "model_downgrade")
    assert rec["steps"] == [1]
    assert rec["est_savings_usd"] == pytest.approx(1.9, abs=1e-4)


def test_read_steps_already_on_haiku_are_not_downgraded():
    run = _run([_read(1, model=HAIKU, usage=_usage(inp=1_000_000)), _edit(2, model=SONNET, usage=_usage(inp=1000))])
    assert _recs(run, "model_downgrade") == []


@pytest.mark.parametrize("command, read_only", [
    ("git status", True),
    ("git push origin main", False),
    ("ls -la src 2>&1", True),
    ("cat README.md | grep x", False),
    ("echo hi > out.txt", False),
    ("npm test", False),
    ("find . -name '*.py' -delete", False),
    ("rg -n retry src", True),
    ("Get-ChildItem src", True),
])
def test_read_only_shell_classification(command, read_only):
    assert _is_read_only(_bash(1, command, model=SONNET)) is read_only


def test_small_saving_inside_an_expensive_run_is_dropped():
    # A $5 Opus edit and one tiny Sonnet read that would save about $0.002
    run = _run([
        _edit(1, model=OPUS, usage=_usage(inp=1_250_000)),
        _read(2, model=SONNET, usage=_usage(inp=1000)),
    ])
    assert _recs(run, "model_downgrade") == []


def test_saving_of_a_fifth_of_the_run_is_material_under_a_cent():
    # Saving about $0.0050 against a run cost of about $0.0200 (25%)
    run = _run([
        _read(1, model=SONNET, usage=_usage(inp=2632)),
        _edit(2, model=SONNET, usage=_usage(out=1470)),
    ])
    rec = _rec(run, "model_downgrade")
    assert rec["est_savings_usd"] == pytest.approx(0.0050, abs=1e-4)


def test_runs_under_one_cent_get_no_priced_recommendation():
    # Cost about $0.002: a cheap run, so nothing is recommended even though all of it is downgradable
    run = _run([_read(1, model=SONNET, usage=_usage(inp=1000))])
    assert recommend(run) == []


def test_haiku_only_short_run_gets_no_recommendations():
    run = _run([_read(i, path=f"f{i}.py", model=HAIKU, usage=_usage(inp=20_000)) for i in range(1, 6)])
    assert recommend(run) == []


# ---------------------------------------------------------------- prompt cache

def _long_session(n=24, inp_each=100_000, cache_each=0):
    return _run([
        _edit(i, path=f"f{i}.py", model=SONNET, usage=_usage(inp=inp_each, out=100, cr=cache_each))
        for i in range(1, n + 1)
    ])


def test_long_session_without_cache_reads_is_flagged():
    run = _long_session()
    rec = _rec(run, "cache_misses")
    # half of 2.4M uncached input tokens, at $2.00 less $0.10 per million
    assert rec["est_savings_usd"] == pytest.approx(0.5 * 2.4 * (2.0 - 0.1), abs=1e-3)
    assert rec["steps"] == list(range(1, 25))


def test_long_session_with_good_cache_reuse_is_not_flagged():
    assert _recs(_long_session(cache_each=900_000), "cache_misses") == []


def test_short_session_is_not_flagged_for_cache_misses():
    assert _recs(_long_session(n=5), "cache_misses") == []


# ---------------------------------------------------------------- retry loops

def test_repeated_failing_commands_cost_the_retries():
    # Each failing run costs $0.20; the first is not counted as waste, so two retries = $0.40
    run = _run([_bash(i, "make lint", result="2 errors", is_error=True, usage=_usage(inp=100_000))
                for i in (1, 2, 3)])
    rec = _rec(run, "retry_loop")
    assert rec["est_savings_usd"] == pytest.approx(0.4, abs=1e-4)
    assert rec["steps"] == [1, 2, 3]
    assert "make lint" in rec["detail"]


def test_a_single_failure_is_not_a_retry_loop():
    run = _run([_bash(1, "make lint", result="2 errors", is_error=True, usage=_usage(inp=100_000))])
    assert _recs(run, "retry_loop") == []


# ---------------------------------------------------------------- large reads

def test_file_read_three_times_costs_the_extra_reads():
    run = _run([_read(i, path="src/a.py", result="x", usage=_usage(inp=100_000)) for i in (1, 2, 3, 4)])
    rec = _rec(run, "large_reads")
    assert rec["est_savings_usd"] == pytest.approx(0.4, abs=1e-4)  # reads 3 and 4, $0.20 each
    assert rec["steps"] == [3, 4]


def test_one_very_large_read_is_priced_by_the_context_it_adds():
    # 5000 characters is about 1250 tokens, kept in context for 60 later steps on Opus cache reads
    steps = [_read(1, path="big.log", result="x" * 5000, model=OPUS, usage=_usage(inp=100_000))]
    steps += [_edit(i, path=f"f{i}.py", model=OPUS) for i in range(2, 62)]
    rec = _rec(_run(steps), "large_reads")
    assert rec["est_savings_usd"] == pytest.approx(1250 * 60 * 0.2 / 1_000_000, abs=1e-5)
    assert rec["steps"] == [1]


def test_a_single_large_read_in_a_short_run_is_not_material():
    run = _run([
        _read(1, result="x" * 5000, usage=_usage(inp=1000)),
        _edit(2, usage=_usage(inp=1000)),
    ])
    assert _recs(run, "large_reads") == []


# ---------------------------------------------------------------- unknown pricing

def test_unpriced_model_is_reported_without_an_estimate():
    run = _run([_edit(1, model=MYSTERY, usage=_usage(inp=1000, out=10))])
    rec = _rec(run, "unknown_pricing")
    assert rec["est_savings_usd"] is None
    assert MYSTERY in rec["detail"]
    assert rec["steps"] == [1]


def test_unpriced_model_without_tokens_is_not_reported():
    run = _run([_edit(1, model=MYSTERY, usage=Usage())])
    assert _recs(run, "unknown_pricing") == []


def test_estimated_cost_leaves_out_unpriced_models():
    run = _run([_read(1, model=SONNET, usage=_usage(inp=1_000_000)),
                _edit(2, model=MYSTERY, usage=_usage(inp=5_000_000))])
    assert estimated_cost(run) == pytest.approx(2.0)
    assert estimated_cost(_run([_edit(1, model=MYSTERY, usage=_usage(inp=10))])) is None


# ---------------------------------------------------------------- ordering and schema

def test_recommendations_are_sorted_by_saving_with_unpriced_last():
    run = _run([
        _read(1, model=SONNET, usage=_usage(inp=1_000_000)),                 # downgrade, $1.90
        _bash(2, "make lint", result="x", is_error=True, usage=_usage(inp=100_000)),
        _bash(3, "make lint", result="x", is_error=True, usage=_usage(inp=100_000)),  # retry, $0.20
        _edit(4, model=MYSTERY, usage=_usage(inp=1000)),                     # unpriced
    ])
    kinds = [r["kind"] for r in recommend(run)]
    assert kinds == ["model_downgrade", "retry_loop", "unknown_pricing"]


def test_recommendation_schema_is_exact():
    run = _run([
        _read(1, model=SONNET, usage=_usage(inp=1_000_000)),
        _edit(2, model=MYSTERY, usage=_usage(inp=1000)),
    ])
    recs = recommend(run)
    assert recs
    for rec in recs:
        assert set(rec) == {"kind", "title", "detail", "est_savings_usd", "steps"}
        assert rec["kind"] in KINDS
        assert isinstance(rec["title"], str) and isinstance(rec["detail"], str)
        assert rec["est_savings_usd"] is None or isinstance(rec["est_savings_usd"], float)
        assert isinstance(rec["steps"], list) and all(isinstance(i, int) for i in rec["steps"])


def test_empty_run_has_no_recommendations():
    assert recommend(_run([])) == []


def test_price_table_has_the_current_model_ids():
    assert price_for(HAIKU) is not None
    assert cost_of(_usage(inp=1_000_000), HAIKU) == pytest.approx(0.1)
    assert cost_of(_usage(inp=1_000_000), SONNET) == pytest.approx(2.0)
    assert cost_of(_usage(inp=1_000_000), OPUS) == pytest.approx(4.0)
