"""llm.py: the stdlib Anthropic client. Every request goes to the local stub (ANTHROPIC_BASE_URL),
so no real network is used. Retry sleeps are recorded instead of waited for."""
import json
import os
import socket

import pytest

from runledger import adapters, llm, summarize
from runledger.llm import LLMError
from runledger.parser import Usage
from runledger.pricing import cost_of
from runledger.summarize import apply_templates

from stub_anthropic import (ANTHROPIC_KEY, AWS_KEY, GITHUB_TOKEN, StubAnthropic, error_body,
                            message_reply, tool_reply)

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}


@pytest.fixture
def stub(monkeypatch):
    s = StubAnthropic()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", s.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.delenv("RUNLEDGER_SUMMARY_MODEL", raising=False)
    monkeypatch.setattr(llm.time, "sleep", s.sleeps.append)  # record backoff, never wait
    yield s
    s.close()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _native_run(tmp_path, steps, prompts=None):
    doc = {"runledger_format": 1, "agent": "demo-agent", "session_id": "llm-1", "cwd": "/work/app",
           "prompts": prompts or ["Summarise the change."], "final_message": "All done.",
           "models": {"claude-haiku-5-5": {"input_tokens": 100, "output_tokens": 20}}, "steps": steps}
    path = tmp_path / "session.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    run = adapters.detect(path).parse(path)
    apply_templates(run)
    return run


# ---------------------------------------------------------------- call(): the request

def test_call_returns_the_parsed_reply(stub):
    stub.queue(body=message_reply("hi there", usage={"input_tokens": 12, "output_tokens": 3}))
    reply = llm.call([{"role": "user", "content": "hello"}], "claude-haiku-5-5")
    assert reply["content"][0]["text"] == "hi there"
    assert llm.text_of(reply) == "hi there"
    assert llm.usage(reply).input_tokens == 12


def test_headers_and_path_follow_the_messages_api(stub):
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    req = stub.requests[0]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == ANTHROPIC_KEY
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["headers"]["content-type"].startswith("application/json")


def test_body_carries_model_max_tokens_and_messages(stub):
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-sonnet-5-5", max_tokens=321)
    body = stub.bodies()[0]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 321
    assert body["messages"] == [{"role": "user", "content": "x"}]
    assert "system" not in body and "tools" not in body and "tool_choice" not in body


def test_default_max_tokens_is_1024(stub):
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert stub.bodies()[0]["max_tokens"] == 1024


def test_string_system_prompt_is_a_cached_block(stub):
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5", system="STATIC RULES")
    assert stub.bodies()[0]["system"] == [
        {"type": "text", "text": "STATIC RULES", "cache_control": {"type": "ephemeral"}}]


def test_cache_false_leaves_out_cache_control(stub):
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5", system="S", cache=False)
    assert "cache_control" not in stub.bodies()[0]["system"][0]


def test_system_block_list_is_sent_as_given(stub):
    blocks = [{"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "B"}]
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5", system=blocks)
    assert stub.bodies()[0]["system"] == blocks


def test_tools_and_tool_choice_pass_through(stub):
    tools = [{"name": "t", "description": "d", "input_schema": SCHEMA}]
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5", tools=tools,
             tool_choice={"type": "tool", "name": "t"})
    body = stub.bodies()[0]
    assert body["tools"] == tools
    assert body["tool_choice"] == {"type": "tool", "name": "t"}


def test_base_url_with_trailing_slash_still_posts_to_messages(stub, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", stub.url + "/")
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert stub.requests[0]["path"] == "/v1/messages"


def test_explicit_api_key_argument_is_used(stub, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    explicit = "sk-ant-api03-" + "e" * 30
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5", api_key=explicit)
    assert stub.requests[0]["headers"]["x-api-key"] == explicit


def test_missing_key_raises_before_any_request(stub, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert stub.requests == []


def test_non_json_reply_is_an_llm_error(stub):
    stub.queue(body="<html>not json</html>")
    with pytest.raises(LLMError) as exc:
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert exc.value.status == 200


# ---------------------------------------------------------------- retries and backoff

def test_retries_on_529_then_succeeds(stub):
    stub.queue(529, error_body("overloaded")).queue(529, error_body("overloaded")).queue(body=message_reply("ok"))
    reply = llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert llm.text_of(reply) == "ok"
    assert len(stub.requests) == 3
    assert len(stub.sleeps) == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_other_transient_statuses_are_retried(stub, status):
    stub.queue(status, error_body("try later")).queue(body=message_reply("ok"))
    assert llm.text_of(llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")) == "ok"
    assert len(stub.requests) == 2


def test_retry_after_header_is_honoured(stub):
    stub.queue(429, error_body("slow down"), {"retry-after": "7"}).queue(body=message_reply("ok"))
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert stub.sleeps == [7.0]


def test_retry_after_is_capped_at_the_maximum(stub):
    stub.queue(429, error_body("slow down"), {"retry-after": "3600"}).queue(body=message_reply("ok"))
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert stub.sleeps == [llm.MAX_RETRY_AFTER_S]


def test_backoff_grows_exponentially_with_jitter(stub):
    for _ in range(3):
        stub.queue(503, error_body("busy"))
    stub.queue(body=message_reply("ok"))
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert len(stub.sleeps) == 3
    for sleep, ceiling in zip(stub.sleeps, (1.0, 2.0, 4.0)):
        assert 0.5 * ceiling <= sleep <= ceiling


def test_gives_up_after_max_retries_with_the_last_status(stub):
    for _ in range(llm.MAX_RETRIES + 1):
        stub.queue(503, error_body("busy"))
    with pytest.raises(LLMError) as exc:
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert exc.value.status == 503
    assert len(stub.requests) == llm.MAX_RETRIES + 1
    assert len(stub.sleeps) == llm.MAX_RETRIES


def test_client_error_is_not_retried(stub):
    stub.queue(400, error_body("bad request: field 'model' is wrong"))
    with pytest.raises(LLMError) as exc:
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert exc.value.status == 400
    assert "field 'model' is wrong" in str(exc.value)
    assert len(stub.requests) == 1 and stub.sleeps == []


def test_network_failure_is_retried_then_raised_without_status(stub, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    with pytest.raises(LLMError, match="network error") as exc:
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert exc.value.status is None
    assert len(stub.sleeps) == llm.MAX_RETRIES


def test_network_error_message_never_contains_the_key(stub, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    with pytest.raises(LLMError) as exc:
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert ANTHROPIC_KEY not in str(exc.value)


# ---------------------------------------------------------------- errors never carry the key

def test_auth_error_message_hides_a_key_the_server_echoes(stub):
    stub.queue(401, error_body(f"invalid x-api-key {ANTHROPIC_KEY}", kind="authentication_error"))
    with pytest.raises(LLMError) as exc:
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert ANTHROPIC_KEY not in str(exc.value)
    assert exc.value.status == 401
    assert "[key]" in str(exc.value)


def test_exhausted_retry_message_hides_the_key(stub):
    for _ in range(llm.MAX_RETRIES + 1):
        stub.queue(500, error_body(f"internal failure, key {ANTHROPIC_KEY}"))
    with pytest.raises(LLMError) as exc:
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert ANTHROPIC_KEY not in str(exc.value)
    assert "gave up after" in str(exc.value)


def test_error_text_is_short_and_one_line(stub):
    stub.queue(400, error_body("line one\nline two " + "z" * 1000))
    with pytest.raises(LLMError) as exc:
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    assert "\n" not in str(exc.value)
    assert len(str(exc.value)) <= 330


# ---------------------------------------------------------------- redaction before sending

def test_message_secrets_are_redacted_before_sending(stub):
    text = f"deploy with {ANTHROPIC_KEY} and {AWS_KEY} and {GITHUB_TOKEN}"
    stub.queue()
    llm.call([{"role": "user", "content": text}], "claude-haiku-5-5")
    raw = stub.requests[0]["raw"]
    assert ANTHROPIC_KEY not in raw and AWS_KEY not in raw and GITHUB_TOKEN not in raw
    assert raw.count("[REDACTED]") == 3


def test_system_and_list_content_are_redacted_too(stub):
    stub.queue()
    llm.call([{"role": "user", "content": [{"type": "text", "text": f"token {GITHUB_TOKEN}"}]}],
             "claude-haiku-5-5", system=f"system with {AWS_KEY}")
    raw = stub.requests[0]["raw"]
    assert GITHUB_TOKEN not in raw and AWS_KEY not in raw


def test_key_value_secrets_keep_their_label(stub):
    stub.queue()
    llm.call([{"role": "user", "content": 'config password = "hunter2hunter2hunter2"'}], "claude-haiku-5-5")
    sent = stub.bodies()[0]["messages"][0]["content"]
    assert "hunter2" not in sent
    assert "password" in sent


def test_redact_masks_the_three_provider_formats():
    out = llm.redact(f"a {ANTHROPIC_KEY} b {AWS_KEY} c {GITHUB_TOKEN} d")
    assert out == "a [REDACTED] b [REDACTED] c [REDACTED] d"


def test_redact_keeps_ordinary_text_untouched():
    assert llm.redact("npm test -- --watch") == "npm test -- --watch"
    assert llm.redact(None) == ""


def test_redact_masks_emails_only_when_asked():
    text = "reply to dev@example.com please"
    assert llm.redact(text) == text
    assert llm.redact(text, emails=True) == "reply to [EMAIL] please"


def test_redact_masks_bearer_tokens():
    out = llm.redact("curl -H 'Authorization: Bearer abcdefghijklmnop123456'")
    assert "abcdefghijklmnop123456" not in out


# ---------------------------------------------------------------- structured output

def test_structured_forces_the_named_tool_and_returns_its_input(stub):
    stub.queue(body=tool_reply({"verdict": "looks_safe"}, tool="record_x"))
    data = llm.structured("check it", SCHEMA, "claude-haiku-5-5", tool_name="record_x",
                          tool_description="Record it.")
    assert data == {"verdict": "looks_safe"}
    body = stub.bodies()[0]
    assert body["tools"] == [{"name": "record_x", "description": "Record it.", "input_schema": SCHEMA}]
    assert body["tool_choice"] == {"type": "tool", "name": "record_x"}
    assert body["messages"] == [{"role": "user", "content": "check it"}]


def test_structured_sends_a_cached_system_prompt(stub):
    stub.queue(body=tool_reply({"verdict": "looks_safe"}, tool="record_result"))
    llm.structured("p", SCHEMA, "claude-haiku-5-5", system="LONG STATIC PROMPT")
    assert stub.bodies()[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_structured_response_also_returns_the_reply(stub):
    stub.queue(body=tool_reply({"verdict": "dangerous"}, tool="record_result",
                               usage={"input_tokens": 7, "output_tokens": 9}))
    data, reply = llm.structured_response("p", SCHEMA, "claude-haiku-5-5")
    assert data["verdict"] == "dangerous"
    assert llm.usage(reply) == Usage(input_tokens=7, output_tokens=9)


def test_structured_without_the_tool_call_is_an_error(stub):
    stub.queue(body=message_reply("I think it is fine."))
    with pytest.raises(LLMError, match="did not return the structured result"):
        llm.structured("p", SCHEMA, "claude-haiku-5-5")


def test_structured_with_the_wrong_tool_name_is_an_error(stub):
    stub.queue(body=tool_reply({"verdict": "x"}, tool="something_else"))
    with pytest.raises(LLMError):
        llm.structured("p", SCHEMA, "claude-haiku-5-5", tool_name="record_x")


def test_structured_cut_off_at_max_tokens_is_an_error(stub):
    stub.queue(body=tool_reply({"verdict": "x"}, stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="cut off"):
        llm.structured("p", SCHEMA, "claude-haiku-5-5")


# ---------------------------------------------------------------- usage and pricing

def test_usage_maps_cache_fields_and_missing_values_to_zero():
    reply = {"usage": {"input_tokens": 5, "output_tokens": 6,
                       "cache_creation_input_tokens": 7, "cache_read_input_tokens": 8}}
    assert llm.usage(reply) == Usage(5, 6, 7, 8)
    assert llm.usage({}) == Usage(0, 0, 0, 0)


def test_cost_of_a_reply_uses_the_price_table(stub):
    stub.queue(body=message_reply(usage={"input_tokens": 100, "output_tokens": 20}))
    reply = llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5")
    # Haiku 5.5: $0.10 per million input tokens, $0.50 per million output tokens.
    assert cost_of(llm.usage(reply), "claude-haiku-5-5") == pytest.approx((100 * 0.1 + 20 * 0.5) / 1_000_000)


def test_text_of_joins_only_text_blocks():
    reply = {"content": [{"type": "text", "text": "a"}, {"type": "tool_use", "name": "t", "input": {}},
                         {"type": "text", "text": "b"}]}
    assert llm.text_of(reply) == "ab"


# ---------------------------------------------------------------- summarize.py on the client

def test_summarize_default_model_is_haiku_5_5():
    if "RUNLEDGER_SUMMARY_MODEL" in os.environ:
        pytest.skip("RUNLEDGER_SUMMARY_MODEL overrides the default")
    assert summarize.DEFAULT_AI_MODEL == "claude-haiku-5-5"


def test_ai_summaries_uses_the_client_and_redacts(stub, tmp_path):
    run = _native_run(tmp_path, [{"tool": "Bash", "input": {"command": f"curl -H 'k: {ANTHROPIC_KEY}' x"},
                                  "model": "claude-haiku-5-5", "result_text": "ok", "is_error": False}])
    answer = json.dumps({"overview": "Checked the API.", "steps": [{"n": 1, "summary": "Called the API."}]})
    stub.queue(body=message_reply(answer, usage={"input_tokens": 50, "output_tokens": 9}))
    spent = summarize.ai_summaries(run, api_key=ANTHROPIC_KEY, model="claude-haiku-5-5")
    assert spent == Usage(50, 9)
    assert run.overall_summary == "Checked the API."
    assert run.steps[0].summary == "Called the API."
    body = stub.bodies()[0]
    assert body["model"] == "claude-haiku-5-5"
    assert ANTHROPIC_KEY not in stub.requests[0]["raw"]


def test_ai_summaries_without_a_key_keeps_the_old_message(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = _native_run(tmp_path, [{"tool": "Read", "input": {"file_path": "/work/app/a.py"}, "model": "m",
                                  "result_text": "", "is_error": False}])
    with pytest.raises(RuntimeError, match="Set ANTHROPIC_API_KEY to use --ai summaries"):
        summarize.ai_summaries(run)


def test_ai_summaries_api_error_is_a_runtime_error(stub, tmp_path):
    run = _native_run(tmp_path, [{"tool": "Read", "input": {"file_path": "/work/app/a.py"}, "model": "m",
                                  "result_text": "", "is_error": False}])
    stub.queue(400, error_body("model not found"))
    with pytest.raises(RuntimeError, match="400"):
        summarize.ai_summaries(run, api_key=ANTHROPIC_KEY)


def test_summary_clip_redacts_before_cutting():
    text = "x" * 595 + ANTHROPIC_KEY
    out = summarize._clip(text, 600)
    assert "sk-ant" not in out and "api03" not in out
    assert out.startswith("x" * 595)
