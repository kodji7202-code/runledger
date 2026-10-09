"""review.py and the --review flags: schema normalisation, what is sent, escalation, costs,
and failure paths. Every model call goes to the local stub; no real network is used."""
import json

import pytest

from runledger import adapters, llm, review
from runledger.cli import build, main
from runledger.client import build_payload
from runledger.review import REVIEW_SCHEMA, ai_review
from runledger.risk import Risk, assess

from stub_anthropic import (ANTHROPIC_KEY, AWS_KEY, GITHUB_TOKEN, StubAnthropic, error_body,
                            tool_reply)

RESULT_KEYS = {"model", "verdict", "summary", "risk_assessments", "diff_explanations", "cost_usd", "tokens"}


@pytest.fixture
def stub(monkeypatch):
    s = StubAnthropic()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", s.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.delenv("RUNLEDGER_ESCALATE_OPUS", raising=False)
    monkeypatch.delenv("RUNLEDGER_SUMMARY_MODEL", raising=False)
    monkeypatch.setattr(llm.time, "sleep", s.sleeps.append)
    yield s
    s.close()


def step(tool, inp, **extra):
    base = {"tool": tool, "input": inp, "model": "claude-sonnet-5-5", "timestamp": "2026-10-08T09:00:00Z",
            "result_text": "", "is_error": False}
    base.update(extra)
    return base


def session(tmp_path, steps, prompts=None, cwd="/work/app", final="All done.", name="session.json"):
    doc = {"runledger_format": 1, "agent": "demo-agent", "session_id": "rev-1", "cwd": cwd,
           "prompts": prompts if prompts is not None else ["Fix the price cache."],
           "final_message": final,
           "models": {"claude-sonnet-5-5": {"input_tokens": 1000, "output_tokens": 100}}, "steps": steps}
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def load(tmp_path, steps, **kwargs):
    path = session(tmp_path, steps, **kwargs)
    return adapters.detect(path).parse(path)


def risk(step_no, code, severity="high", reason="a rule fired"):
    return Risk(severity=severity, code=code, reason=reason, step=step_no)


EDIT = step("Edit", {"file_path": "/work/app/src/prices.py",
                     "old_string": "def lookup(sku):", "new_string": "@lru_cache\ndef lookup(sku):"})
READ = step("Read", {"file_path": "/work/app/src/prices.py"})
BASH_RM = step("Bash", {"command": "rm -rf /work/app/build", "description": "Clean the build"})


def answer(verdict="needs_review", summary="The agent edited the price lookup.", assessments=None,
           diffs=None):
    return {"verdict": verdict, "summary": summary,
            "risk_assessments": assessments if assessments is not None else [],
            "diff_explanations": diffs if diffs is not None else []}


def _run_data(body):
    text = body["messages"][0]["content"]
    start = text.index("<run_data>") + len("<run_data>")
    end = text.index("</run_data>")
    return json.loads(text[start:end])


# ---------------------------------------------------------------- when no call is made

def test_nothing_to_review_makes_no_call_and_needs_no_key(stub, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = load(tmp_path, [READ])
    result = ai_review(run, [])
    assert result["verdict"] == "looks_safe"
    assert result["risk_assessments"] == [] and result["diff_explanations"] == []
    assert stub.requests == []
    assert run.ai_review is result


def test_no_call_result_has_the_exact_schema(stub, tmp_path):
    run = load(tmp_path, [READ])
    result = ai_review(run, [])
    assert set(result) == RESULT_KEYS
    assert set(result["tokens"]) == {"input", "output"}
    assert result["cost_usd"] == 0.0


def test_changed_files_without_risks_are_still_reviewed(stub, tmp_path):
    stub.queue(body=tool_reply(answer(verdict="looks_safe", diffs=[
        {"file": "src/prices.py", "explanation": "Adds a cache to the lookup."}])))
    run = load(tmp_path, [EDIT])
    result = ai_review(run, [])
    assert len(stub.requests) == 1
    assert result["verdict"] == "looks_safe"
    assert result["diff_explanations"] == [{"file": "src/prices.py", "explanation": "Adds a cache to the lookup."}]


def test_risks_without_changed_files_are_reviewed(stub, tmp_path):
    stub.queue(body=tool_reply(answer(assessments=[
        {"step": 1, "code": "rm_rf", "assessment": "confirmed", "explanation": "Deletes the build folder."}])))
    run = load(tmp_path, [BASH_RM])
    result = ai_review(run, [risk(1, "rm_rf")])
    assert len(stub.requests) == 1
    assert result["risk_assessments"][0]["assessment"] == "confirmed"


# ---------------------------------------------------------------- the schema and normalisation

def test_result_keys_are_exactly_the_schema(stub, tmp_path):
    stub.queue(body=tool_reply(answer(assessments=[
        {"step": 1, "code": "rm_rf", "assessment": "confirmed", "explanation": "x"}])))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert set(result) == RESULT_KEYS
    assert set(result["risk_assessments"][0]) == {"step", "code", "assessment", "explanation"}


def test_missing_assessments_are_filled_as_uncertain(stub, tmp_path):
    stub.queue(body=tool_reply(answer(assessments=[
        {"step": 1, "code": "rm_rf", "assessment": "false_positive", "explanation": "Harmless here."}])))
    run = load(tmp_path, [BASH_RM, EDIT])
    result = ai_review(run, [risk(1, "rm_rf"), risk(2, "git_internals")])
    by_key = {(a["step"], a["code"]): a for a in result["risk_assessments"]}
    assert len(result["risk_assessments"]) == 2
    assert by_key[(1, "rm_rf")]["assessment"] == "false_positive"
    assert by_key[(2, "git_internals")]["assessment"] == "uncertain"
    assert "no assessment" in by_key[(2, "git_internals")]["explanation"]


def test_unknown_steps_and_codes_are_dropped(stub, tmp_path):
    stub.queue(body=tool_reply(answer(assessments=[
        {"step": 1, "code": "rm_rf", "assessment": "confirmed", "explanation": "real"},
        {"step": 99, "code": "rm_rf", "assessment": "confirmed", "explanation": "no such step"},
        {"step": 1, "code": "invented_code", "assessment": "confirmed", "explanation": "no such risk"}])))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert [(a["step"], a["code"]) for a in result["risk_assessments"]] == [(1, "rm_rf")]


def test_duplicate_answers_keep_the_first(stub, tmp_path):
    stub.queue(body=tool_reply(answer(assessments=[
        {"step": 1, "code": "rm_rf", "assessment": "confirmed", "explanation": "first"},
        {"step": 1, "code": "rm_rf", "assessment": "false_positive", "explanation": "second"}])))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert len(result["risk_assessments"]) == 1
    assert result["risk_assessments"][0]["explanation"] == "first"


def test_repeated_finding_on_one_step_is_one_entry(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf"), risk(1, "rm_rf", reason="again")])
    assert len(result["risk_assessments"]) == 1


def test_invalid_assessment_value_becomes_uncertain_and_keeps_the_text(stub, tmp_path):
    stub.queue(body=tool_reply(answer(assessments=[
        {"step": 1, "code": "rm_rf", "assessment": "maybe", "explanation": "not sure which"}])))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert result["risk_assessments"][0] == {"step": 1, "code": "rm_rf", "assessment": "uncertain",
                                             "explanation": "not sure which"}


def test_string_step_numbers_from_the_model_are_matched(stub, tmp_path):
    stub.queue(body=tool_reply(answer(assessments=[
        {"step": "1", "code": "rm_rf", "assessment": "confirmed", "explanation": "ok"}])))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert result["risk_assessments"][0]["assessment"] == "confirmed"


@pytest.mark.parametrize("verdict", ["looks_safe", "needs_review", "dangerous"])
def test_valid_verdicts_are_kept(stub, tmp_path, verdict):
    stub.queue(body=tool_reply(answer(verdict=verdict)))
    assert ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])["verdict"] == verdict


@pytest.mark.parametrize("bad", ["fine", "SAFE", "", None, 3])
def test_invalid_verdict_becomes_needs_review(stub, tmp_path, bad):
    stub.queue(body=tool_reply(answer(verdict=bad)))
    assert ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])["verdict"] == "needs_review"


def test_summary_is_capped_at_600_characters(stub, tmp_path):
    stub.queue(body=tool_reply(answer(summary="word " * 400)))
    summary = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])["summary"]
    assert len(summary) <= 600
    assert summary.endswith("…")


def test_explanations_are_capped_at_400_characters(stub, tmp_path):
    stub.queue(body=tool_reply(answer(assessments=[
        {"step": 1, "code": "rm_rf", "assessment": "confirmed", "explanation": "e" * 2000}])))
    text = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])["risk_assessments"][0]["explanation"]
    assert len(text) <= 400


def test_missing_summary_gets_a_default(stub, tmp_path):
    stub.queue(body=tool_reply({"verdict": "needs_review", "risk_assessments": [],
                                "diff_explanations": []}))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert result["summary"] == "The reviewer returned no summary."


def test_diff_explanations_only_for_changed_files_once_each(stub, tmp_path):
    stub.queue(body=tool_reply(answer(diffs=[
        {"file": "src/prices.py", "explanation": "Adds a cache."},
        {"file": "src/prices.py", "explanation": "Duplicate."},
        {"file": "/etc/passwd", "explanation": "Not a changed file."}])))
    result = ai_review(load(tmp_path, [EDIT]), [])
    assert result["diff_explanations"] == [{"file": "src/prices.py", "explanation": "Adds a cache."}]


def test_missing_diff_explanation_gets_a_fallback(stub, tmp_path):
    stub.queue(body=tool_reply(answer(diffs=[])))
    result = ai_review(load(tmp_path, [EDIT]), [])
    assert result["diff_explanations"] == [
        {"file": "src/prices.py", "explanation": "The reviewer gave no explanation for this file."}]


@pytest.mark.parametrize(("path", "cwd", "expected"), [
    (r"d:\work\app\src\x.py", r"D:\Work\App", r"src\x.py"),
    ("D:/Work/App/src/x.py", r"D:\Work\App", r"src\x.py"),
])
def test_windows_paths_are_made_relative_case_insensitively(path, cwd, expected):
    assert review._rel(path, cwd) == expected


# ---------------------------------------------------------------- the rule score is never changed

def test_rule_score_and_risks_are_unchanged_by_the_review(stub, tmp_path):
    secret_write = step("Write", {"file_path": "/work/app/config.py", "content": f"KEY = '{AWS_KEY}'\n"})
    run = load(tmp_path, [secret_write, EDIT, BASH_RM])
    score, level, risks = assess(run)
    before = (score, level, [(r.severity, r.code, r.step) for r in risks])
    stub.queue(body=tool_reply(answer(verdict="dangerous", assessments=[
        {"step": 1, "code": "secret_in_content", "assessment": "false_positive", "explanation": "Example key."}])))
    ai_review(run, risks)
    after_score, after_level, after_risks = assess(run)
    assert (after_score, after_level, [(r.severity, r.code, r.step) for r in after_risks]) == before


# ---------------------------------------------------------------- what is sent

def test_request_uses_sonnet_cached_system_and_forced_tool(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")], api_key=ANTHROPIC_KEY)
    body = stub.bodies()[0]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tool_choice"] == {"type": "tool", "name": review.REVIEW_TOOL}
    assert body["tools"][0]["input_schema"] == REVIEW_SCHEMA


def test_custom_model_is_used_for_the_request(stub, tmp_path):
    stub.queue(body=tool_reply(answer(), model="claude-haiku-5-5"))
    ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")], model="claude-haiku-5-5")
    assert stub.bodies()[0]["model"] == "claude-haiku-5-5"


def test_secrets_in_commands_edits_and_writes_are_redacted(stub, tmp_path):
    steps = [
        step("Bash", {"command": f"curl -H 'Authorization: {GITHUB_TOKEN}' https://example.com"}),
        step("Edit", {"file_path": "/work/app/a.py", "old_string": f"A = '{AWS_KEY}'",
                      "new_string": f"A = '{GITHUB_TOKEN}'"}),
        step("Write", {"file_path": "/work/app/b.py", "content": f"K = '{ANTHROPIC_KEY}'"}),
    ]
    stub.queue(body=tool_reply(answer()))
    run = load(tmp_path, steps)
    ai_review(run, [risk(1, "curl_secret")])
    raw = stub.requests[0]["raw"]
    for secret in (AWS_KEY, GITHUB_TOKEN, ANTHROPIC_KEY):
        assert secret not in raw
    assert "[REDACTED]" in raw


def test_tool_results_and_the_final_message_are_not_sent(stub, tmp_path):
    steps = [step("Bash", {"command": "rm -rf /work/app/build"}, result_text="RESULT-TEXT-MARKER-4471")]
    stub.queue(body=tool_reply(answer()))
    ai_review(load(tmp_path, steps, final="FINAL-MESSAGE-MARKER-9913"), [risk(1, "rm_rf")])
    raw = stub.requests[0]["raw"]
    assert "RESULT-TEXT-MARKER-4471" not in raw
    assert "FINAL-MESSAGE-MARKER-9913" not in raw


def test_paths_are_sent_relative_and_the_working_folder_is_not(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    run = load(tmp_path, [EDIT, READ])
    ai_review(run, [])
    payload = _run_data(stub.bodies()[0])
    assert payload["steps"][0]["file"] == "src/prices.py"
    assert payload["files_changed"][0]["file"] == "src/prices.py"
    assert "/work/app" not in stub.requests[0]["raw"]


def test_commands_are_truncated_to_300_characters(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    long_cmd = "echo " + "c" * 1000
    ai_review(load(tmp_path, [step("Bash", {"command": long_cmd})]), [risk(1, "x")])
    sent = _run_data(stub.bodies()[0])["steps"][0]["command"]
    assert len(sent) <= 301 and sent.startswith("echo ")


def test_edit_snippets_are_truncated_to_800_characters_each(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    edit = step("Edit", {"file_path": "/work/app/a.py", "old_string": "o" * 2000, "new_string": "n" * 2000})
    ai_review(load(tmp_path, [edit]), [])
    sent = _run_data(stub.bodies()[0])["steps"][0]["edits"][0]
    assert len(sent["old"]) <= 801 and len(sent["new"]) <= 801


def test_written_content_is_sent_as_a_capped_snippet(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    write = step("Write", {"file_path": "/work/app/new.py", "content": "w" * 5000})
    ai_review(load(tmp_path, [write]), [])
    assert len(_run_data(stub.bodies()[0])["steps"][0]["new"]) <= 801


def test_only_five_user_prompts_are_sent_and_each_is_capped(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    prompts = [f"request {i} " + "p" * 800 for i in range(7)]
    ai_review(load(tmp_path, [EDIT], prompts=prompts), [])
    sent = _run_data(stub.bodies()[0])["user_requests"]
    assert len(sent) == 5
    assert all(len(p) <= 501 for p in sent)


def test_step_list_is_limited_and_risky_steps_come_first(stub, tmp_path):
    reads = [step("Read", {"file_path": f"/work/app/f{i}.py"}) for i in range(9)]
    steps = reads[:4] + [BASH_RM] + reads[4:]          # the risky step is number 5 of 10
    stub.queue(body=tool_reply(answer()))
    ai_review(load(tmp_path, steps), [risk(5, "rm_rf")], max_steps=2)
    payload = _run_data(stub.bodies()[0])
    # Two slots: the risky step first, then the earliest remaining step. Run order is kept.
    assert [s["n"] for s in payload["steps"]] == [1, 5]
    assert payload["steps_omitted"] == 8


def test_max_steps_caps_the_number_of_steps_sent(stub, tmp_path):
    reads = [step("Read", {"file_path": f"/work/app/f{i}.py"}) for i in range(10)]
    stub.queue(body=tool_reply(answer()))
    ai_review(load(tmp_path, reads), [risk(1, "x")], max_steps=3)
    payload = _run_data(stub.bodies()[0])
    assert len(payload["steps"]) == 3
    assert payload["steps_omitted"] == 7
    assert payload["steps"][0]["n"] == 1                 # the risky step is always among them


def test_rule_risks_and_files_are_listed_in_the_payload(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    ai_review(load(tmp_path, [EDIT, BASH_RM]), [risk(2, "rm_rf", reason="deletes build")])
    payload = _run_data(stub.bodies()[0])
    assert payload["rule_risks"] == [{"step": 2, "code": "rm_rf", "severity": "high", "reason": "deletes build"}]
    files = {f["file"]: f for f in payload["files_changed"]}
    assert "src/prices.py" in files
    assert "/work/app/build" not in files and "build" in files   # rm target, made relative
    assert set(files["src/prices.py"]) == {"file", "added", "removed", "created", "deleted"}


def test_sent_text_is_redacted_even_in_rule_reasons(stub, tmp_path):
    stub.queue(body=tool_reply(answer()))
    ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf", reason=f"contains {ANTHROPIC_KEY}")])
    assert ANTHROPIC_KEY not in stub.requests[0]["raw"]


# ---------------------------------------------------------------- costs and tokens

def test_cost_is_computed_from_the_reply_usage(stub, tmp_path):
    stub.queue(body=tool_reply(answer(), usage={"input_tokens": 1000, "output_tokens": 200}))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    # Sonnet 5.5: $2.00 per million input tokens, $10.00 per million output tokens.
    assert result["cost_usd"] == pytest.approx((1000 * 2.0 + 200 * 10.0) / 1_000_000)
    assert result["tokens"] == {"input": 1000, "output": 200}


def test_cache_tokens_count_as_input_and_are_priced_as_reads(stub, tmp_path):
    usage = {"input_tokens": 1000, "output_tokens": 200,
             "cache_creation_input_tokens": 300, "cache_read_input_tokens": 500}
    stub.queue(body=tool_reply(answer(), usage=usage))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert result["tokens"] == {"input": 1800, "output": 200}
    expected = (1000 * 2.0 + 300 * 2.0 * 1.25 + 500 * 0.1 + 200 * 10.0) / 1_000_000
    assert result["cost_usd"] == pytest.approx(expected)


def test_unpriced_model_gives_a_null_cost(stub, tmp_path):
    stub.queue(body=tool_reply(answer(), usage={"input_tokens": 5, "output_tokens": 5}))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")], model="claude-mystery-9")
    assert result["cost_usd"] is None
    assert result["model"] == "claude-mystery-9"


# ---------------------------------------------------------------- Opus escalation

def test_dangerous_verdict_escalates_to_opus_when_enabled(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_ESCALATE_OPUS", "1")
    stub.queue(body=tool_reply(answer(verdict="dangerous"), model="claude-sonnet-5-5",
                               usage={"input_tokens": 1000, "output_tokens": 200}))
    stub.queue(body=tool_reply(answer(verdict="needs_review", summary="Second opinion."), model="claude-opus-5-5",
                               usage={"input_tokens": 2000, "output_tokens": 300}))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert [b["model"] for b in stub.bodies()] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert result["model"] == "claude-opus-5-5"
    assert result["verdict"] == "needs_review"
    assert result["summary"] == "Second opinion."


def test_escalation_adds_both_costs_and_tokens(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_ESCALATE_OPUS", "1")
    stub.queue(body=tool_reply(answer(verdict="dangerous"), usage={"input_tokens": 1000, "output_tokens": 200}))
    stub.queue(body=tool_reply(answer(verdict="dangerous"), model="claude-opus-5-5",
                               usage={"input_tokens": 2000, "output_tokens": 300}))
    result = ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    sonnet = (1000 * 2.0 + 200 * 10.0) / 1_000_000    # Sonnet 5.5 prices
    opus = (2000 * 4.0 + 300 * 20.0) / 1_000_000      # Opus 5.5 prices
    assert result["cost_usd"] == pytest.approx(sonnet + opus)
    assert result["tokens"] == {"input": 3000, "output": 500}


def test_escalation_is_off_without_the_environment_flag(stub, tmp_path):
    stub.queue(body=tool_reply(answer(verdict="dangerous")))
    ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert len(stub.requests) == 1


@pytest.mark.parametrize("flag", ["0", "true", "yes", ""])
def test_escalation_needs_exactly_one(stub, tmp_path, monkeypatch, flag):
    monkeypatch.setenv("RUNLEDGER_ESCALATE_OPUS", flag)
    stub.queue(body=tool_reply(answer(verdict="dangerous")))
    ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert len(stub.requests) == 1


def test_no_escalation_for_a_non_dangerous_verdict(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_ESCALATE_OPUS", "1")
    stub.queue(body=tool_reply(answer(verdict="needs_review")))
    ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert len(stub.requests) == 1


def test_no_escalation_when_already_on_opus(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_ESCALATE_OPUS", "1")
    stub.queue(body=tool_reply(answer(verdict="dangerous"), model="claude-opus-5-5"))
    ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")], model="claude-opus-5-5")
    assert len(stub.requests) == 1


def test_failed_opus_call_leaves_no_review(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_ESCALATE_OPUS", "1")
    stub.queue(body=tool_reply(answer(verdict="dangerous")))
    stub.queue(400, error_body("model not available"))
    run = load(tmp_path, [BASH_RM])
    with pytest.raises(llm.LLMError, match="model not available"):
        ai_review(run, [risk(1, "rm_rf")])
    assert run.ai_review is None


# ---------------------------------------------------------------- failures

def test_api_failure_raises_and_leaves_no_review(stub, tmp_path):
    stub.queue(400, error_body("bad request"))
    run = load(tmp_path, [BASH_RM])
    with pytest.raises(llm.LLMError) as exc:
        ai_review(run, [risk(1, "rm_rf")])
    assert exc.value.status == 400
    assert run.ai_review is None


def test_missing_key_with_something_to_review_raises(stub, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])
    assert stub.requests == []


def test_structured_reply_without_the_tool_is_an_error(stub, tmp_path):
    stub.queue(body=tool_reply(answer(), tool="other_tool"))
    with pytest.raises(llm.LLMError, match="did not return"):
        ai_review(load(tmp_path, [BASH_RM]), [risk(1, "rm_rf")])


# ---------------------------------------------------------------- CLI: receipt --review

def test_build_with_review_attaches_the_result(stub, tmp_path):
    stub.queue(body=tool_reply(answer(verdict="needs_review", assessments=[
        {"step": 1, "code": "x", "assessment": "confirmed", "explanation": "yes"}])))
    path = session(tmp_path, [BASH_RM])
    run, score, level, risks, note = build(str(path), review=True, review_model="claude-sonnet-5-5")
    assert note is None
    assert run.ai_review["verdict"] == "needs_review"
    assert len(stub.requests) == 1


def test_build_without_review_makes_no_call(stub, tmp_path):
    path = session(tmp_path, [BASH_RM])
    run, *_rest = build(str(path))
    assert run.ai_review is None
    assert stub.requests == []


def test_build_keeps_the_score_with_and_without_review(stub, tmp_path):
    stub.queue(body=tool_reply(answer(verdict="dangerous")))
    path = session(tmp_path, [BASH_RM, EDIT])
    _run, score_plain, level_plain, risks_plain, _ = build(str(path))
    _run, score_review, level_review, risks_review, _ = build(str(path), review=True)
    assert (score_plain, level_plain) == (score_review, level_review)
    assert [(r.code, r.step) for r in risks_plain] == [(r.code, r.step) for r in risks_review]


def test_build_failure_keeps_the_receipt_and_says_why(stub, tmp_path):
    stub.queue(400, error_body("nope"))
    path = session(tmp_path, [BASH_RM])
    run, score, level, risks, note = build(str(path), review=True)
    assert run.ai_review is None
    assert note.startswith("AI risk review skipped:")
    assert "\n" not in note
    assert score > 0


def test_cli_receipt_review_writes_the_receipt(stub, tmp_path, capsys):
    stub.queue(body=tool_reply(answer()))
    path = session(tmp_path, [EDIT, BASH_RM])
    out = tmp_path / "receipt.json"
    assert main(["receipt", str(path), "--review", "--format", "json", "-o", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["session_id"] == "rev-1"
    assert len(stub.requests) == 1


def test_cli_review_model_flag_is_sent(stub, tmp_path, capsys):
    stub.queue(body=tool_reply(answer(), model="claude-opus-5-5"))
    path = session(tmp_path, [BASH_RM])
    assert main(["receipt", str(path), "--review", "--review-model", "claude-opus-5-5",
                 "--format", "json", "-o", "-"]) == 0
    assert stub.bodies()[0]["model"] == "claude-opus-5-5"


def test_cli_without_review_flag_makes_no_request(stub, tmp_path, capsys):
    path = session(tmp_path, [BASH_RM])
    assert main(["receipt", str(path), "--format", "json", "-o", "-"]) == 0
    assert stub.requests == []


def test_cli_review_failure_is_a_note_not_an_error(stub, tmp_path, capsys):
    stub.queue(500, error_body("down"))
    stub.queue(500, error_body("down"))
    stub.queue(500, error_body("down"))
    stub.queue(500, error_body("down"))
    path = session(tmp_path, [BASH_RM])
    assert main(["receipt", str(path), "--review", "--format", "json", "-o", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.err.startswith("AI risk review skipped:")
    assert json.loads(captured.out)["session_id"] == "rev-1"


def test_cli_review_without_a_key_keeps_the_receipt(stub, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = session(tmp_path, [BASH_RM])
    assert main(["receipt", str(path), "--review", "--format", "md", "-o", "-"]) == 0
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err
    assert captured.out.strip() != ""


# ---------------------------------------------------------------- push --review

def test_push_payload_includes_the_review(stub, tmp_path):
    stub.queue(body=tool_reply(answer(verdict="looks_safe")))
    path = session(tmp_path, [EDIT, BASH_RM])
    notes = []
    payload = build_payload(path, user="dev", review=True, notes=notes)
    assert notes == []
    assert payload["ai_review"]["verdict"] == "looks_safe"


def test_push_payload_without_review_has_no_review_key(stub, tmp_path):
    path = session(tmp_path, [BASH_RM])
    assert build_payload(path, user="dev").get("ai_review") is None   # the receipt renders the key as null
    assert stub.requests == []


def test_push_payload_review_failure_is_a_note(stub, tmp_path):
    stub.queue(400, error_body("rejected"))
    path = session(tmp_path, [BASH_RM])
    notes = []
    payload = build_payload(path, user="dev", review=True, notes=notes)
    assert payload.get("ai_review") is None
    assert len(notes) == 1 and notes[0].startswith("AI risk review skipped:")


def test_cli_push_with_review_reports_the_unreachable_server(stub, tmp_path, capsys):
    stub.queue(body=tool_reply(answer()))
    path = session(tmp_path, [BASH_RM])
    code = main(["push", str(path), "--server", "http://127.0.0.1:9", "--key", "k", "--review", "--user", "dev"])
    assert code == 1
    assert len(stub.requests) == 1
    assert "Could not reach" in capsys.readouterr().err
