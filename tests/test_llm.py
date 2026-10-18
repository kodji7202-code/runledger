"""llm.py: the stdlib Anthropic client. Every request goes to the local stub (ANTHROPIC_BASE_URL),
so no real network is used. Retry sleeps are recorded instead of waited for."""
import io
import json
import os
import re
import socket
import urllib.error

import pytest

from runledger import adapters, llm, summarize
from runledger.cli import main
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


def _native_file(tmp_path, steps, prompts=None, name="session.json"):
    doc = {"runledger_format": 1, "agent": "demo-agent", "session_id": "llm-1", "cwd": "/work/app",
           "prompts": prompts or ["Summarise the change."], "final_message": "All done.",
           "models": {"claude-haiku-5-5": {"input_tokens": 100, "output_tokens": 20}}, "steps": steps}
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _native_run(tmp_path, steps, prompts=None):
    path = _native_file(tmp_path, steps, prompts)
    run = adapters.detect(path).parse(path)
    apply_templates(run)
    return run


def _read_step(index):
    return {"tool": "Read", "input": {"file_path": f"/work/app/f{index}.py"}, "model": "claude-haiku-5-5",
            "result_text": "", "is_error": False}


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


def test_system_block_list_text_is_redacted_without_mutating_the_caller(stub):
    blocks = [{"type": "text", "text": f"token {GITHUB_TOKEN}", "cache_control": {"type": "ephemeral"}}]
    stub.queue()
    llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5", system=blocks)
    sent = stub.bodies()[0]["system"]
    assert GITHUB_TOKEN not in json.dumps(sent)
    assert "[REDACTED]" in sent[0]["text"]
    assert GITHUB_TOKEN in blocks[0]["text"]


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


# ---------------------------------------------------------------- privacy (LLM1)

HOME_WIN = "C:" + "\\" + "Users" + "\\" + "claud" + "\\" + "project" + "\\" + "src" + "\\" + "a.py"
HOME_MAC = "/" + "Users/bob/app/x"
HOME_LINUX = "/" + "home/carol/app/y"
NAMES = ("claud", "bob", "carol")
SPACED_WIN = "C:" + "\\" + "Users" + "\\" + "Jane Smith" + "\\" + "app" + "\\" + "x.py"
SPACED_WIN_HOME = "C:" + "\\" + "Users" + "\\" + "Jane Smith"
SPACED_MAC = "/Users/Jane Smith/app/x"
SPACED_NAMES = ("Jane", "Smith")


def _home_leaks(text):
    """The user names that still appear as a folder after `Users`/`home`, JSON escaping included."""
    found = re.findall(r"Users[\\/]{1,2}([A-Za-z0-9._-]+)", text) + re.findall(r"home/([A-Za-z0-9._-]+)", text)
    return [name for name in found if name in NAMES]


def test_scrub_masks_emails_and_home_paths():
    text = f"git config user.email ana@example.com && cat {HOME_WIN} && cat {HOME_MAC} && cat {HOME_LINUX}"
    out = llm.scrub(text)
    assert "ana@example.com" not in out and "[EMAIL]" in out
    for name in NAMES:
        assert name not in out
    assert out.count("~") == 3
    assert "git config user.email" in out and "cat" in out      # the command shape stays


def test_scrub_keeps_ordinary_text_and_is_idempotent():
    assert llm.scrub("npm test -- --watch") == "npm test -- --watch"
    assert llm.scrub(None) == ""
    once = llm.scrub(f"cd {HOME_WIN} && ls")
    assert once == llm.scrub(once)
    assert llm.scrub_paths("cd ~/work") == "cd ~/work"


def test_scrub_masks_a_windows_home_without_a_trailing_file():
    assert llm.scrub_paths("C:" + "\\" + "Users" + "\\" + "claud") == "~"
    assert llm.scrub_paths("cd C:/Users/claud && ls") == "cd ~ && ls"


def test_scrub_masks_a_home_name_with_spaces():
    """SGD-011: the whole user name goes, quoted or not, and the rest of the path stays."""
    assert llm.scrub_paths(f'type "{SPACED_WIN}"') == 'type "~' + "\\app\\x.py\""
    assert llm.scrub_paths(f"cat '{SPACED_MAC}'") == "cat '~/app/x'"
    assert llm.scrub_paths(f"cp {SPACED_WIN} D:\\dest && echo done") == "cp ~\\app\\x.py D:\\dest && echo done"
    assert llm.scrub_paths(f"cd {SPACED_WIN_HOME}") == "cd ~"
    assert llm.scrub_paths("cd /Users/Jane Smith") == "cd ~"
    both = llm.scrub(f"echo {SPACED_WIN} {SPACED_MAC}")
    assert both == "echo ~\\app\\x.py ~/app/x", both
    assert not any(name in both for name in SPACED_NAMES)


def test_scrub_keeps_command_boundaries_after_a_spaced_home():
    assert llm.scrub_paths(f"cat {SPACED_MAC} && echo done; cd /tmp") == "cat ~/app/x && echo done; cd /tmp"
    assert llm.scrub_paths(f"cat {SPACED_WIN} | wc -l") == "cat ~\\app\\x.py | wc -l"


PAREN_WIN = "C:" + "\\" + "Users" + "\\" + "Jane (Smith)" + "\\" + "app" + "\\" + "x.py"
PAREN_WIN_HOME = "C:" + "\\" + "Users" + "\\" + "Jane (Smith)"
PAREN_MAC = "/Users/Jane (Smith)/app/x"
PAREN_NAMES = ("Jane", "Smith")


def test_scrub_masks_a_quoted_home_name_with_punctuation():
    """SGD-021: inside quotes the name runs to the next separator, so 'Jane (Smith)' goes whole."""
    text = 'type "' + PAREN_WIN + '"' + " && cat '" + PAREN_MAC + "' && git status"
    out = llm.scrub_paths(text)
    assert out == 'type "~\\app\\x.py" && cat \'~/app/x\' && git status', out
    assert not any(name in out for name in PAREN_NAMES)


def test_scrub_masks_a_quoted_home_folder_without_a_trailing_file():
    assert llm.scrub_paths('"' + PAREN_WIN_HOME + '"') == '"~"'
    assert llm.scrub_paths("'" + "/Users/Jane (Smith)" + "'") == "'~'"


def test_scrub_masks_an_unquoted_home_name_with_punctuation():
    assert llm.scrub_paths(f"cd {PAREN_WIN_HOME}") == "cd ~"
    assert llm.scrub_paths("cd /Users/Jane (Smith)/app && ls | wc -l") == "cd ~/app && ls | wc -l"


def test_scrub_masks_a_quoted_home_name_inside_json_text():
    out = llm.scrub(json.dumps({"command": PAREN_WIN}))
    assert out == '{"command": "~\\\\app\\\\x.py"}', out
    assert not any(name in out for name in PAREN_NAMES)


def test_scrub_masks_a_home_path_inside_json_text():
    """Serialized JSON doubles the separators; the API-boundary scrub still masks the name."""
    doubled = json.dumps({"new_string": SPACED_WIN})[len('{"new_string": "'):-2]
    out = llm.scrub(doubled)
    assert out == "~\\\\app\\\\x.py", out
    assert not any(name in out for name in SPACED_NAMES)
    assert llm.scrub(json.dumps({"command": SPACED_WIN})) == '{"command": "~\\\\app\\\\x.py"}'


def test_outgoing_text_masks_emails_and_home_paths(stub):
    text = f"cat {HOME_WIN}; git config user.email ana@example.com; cat {HOME_LINUX}"
    stub.queue()
    llm.call([{"role": "user", "content": text}], "claude-haiku-5-5",
             system=f"a secret home {HOME_MAC}")
    raw = stub.requests[0]["raw"]
    assert "ana@example.com" not in raw
    assert _home_leaks(raw) == []
    assert "~" in raw


def test_summary_prompt_masks_emails_and_home_paths(stub, tmp_path):
    step = {"tool": "Bash", "input": {"command": f"cat {HOME_WIN} && git config user.email ana@example.com"},
            "model": "claude-haiku-5-5", "result_text": f"wrote {HOME_MAC}", "is_error": False}
    run = _native_run(tmp_path, [step], prompts=[f"fix {HOME_WIN} for ana@example.com"])
    stub.queue(body=message_reply(json.dumps({"overview": "Done.", "steps": [{"n": 1, "summary": "Read it."}]})))
    summarize.ai_summaries(run, api_key=ANTHROPIC_KEY, model="claude-haiku-5-5")
    raw = stub.requests[0]["raw"]
    assert "ana@example.com" not in raw
    assert _home_leaks(raw) == []
    assert "git config user.email" in raw
    assert run.overall_summary == "Done."


def test_summary_prompt_masks_nested_step_inputs(stub, tmp_path):
    """SGD-012: nested input strings are scrubbed before the step JSON is written."""
    step = {"tool": "MultiEdit",
            "input": {"file_path": "src/a.py",
                      "edits": [{"old_string": "old = 1", "new_string": SPACED_WIN},
                                {"old_string": "a", "new_string": SPACED_MAC}],
                      "notes": [SPACED_WIN_HOME, "keep me"]},
            "model": "claude-haiku-5-5", "result_text": "ok", "is_error": False}
    run = _native_run(tmp_path, [step])
    stub.queue(body=message_reply(json.dumps({"overview": "Edited.", "steps": [{"n": 1, "summary": "Edited it."}]})))
    summarize.ai_summaries(run, api_key=ANTHROPIC_KEY, model="claude-haiku-5-5")
    raw = stub.requests[0]["raw"]
    assert not any(name in raw for name in SPACED_NAMES)
    assert "Users" not in raw
    assert "old = 1" in raw and "keep me" in raw      # the readable shape stays


def test_summary_prompt_masks_a_punctuated_quoted_home(stub, tmp_path):
    """SGD-021: no part of a quoted 'Jane (Smith)' home reaches the summary request body."""
    step = {"tool": "MultiEdit",
            "input": {"file_path": "src/a.py",
                      "edits": [{"old_string": "old = 1",
                                 "new_string": 'path = r"' + PAREN_WIN + '"'}],
                      "notes": ['keep "' + PAREN_MAC + '" private']},
            "model": "claude-haiku-5-5", "result_text": "wrote " + PAREN_WIN_HOME, "is_error": False}
    run = _native_run(tmp_path, [step], prompts=['fix "' + PAREN_WIN + '" then test'])
    stub.queue(body=message_reply(json.dumps({"overview": "Edited.", "steps": [{"n": 1, "summary": "Edited it."}]})))
    summarize.ai_summaries(run, api_key=ANTHROPIC_KEY, model="claude-haiku-5-5")
    raw = stub.requests[0]["raw"]
    assert not any(name in raw for name in PAREN_NAMES)
    assert "old = 1" in raw and "keep" in raw and "private" in raw   # the readable shape stays


# ---------------------------------------------------------------- summary JSON (LLM5)

def test_summary_json_tolerates_trailing_prose_with_braces(stub, tmp_path):
    run = _native_run(tmp_path, [_read_step(1)])
    reply = ('Here you go: {"overview":"Added a cache","steps":[{"n":1,"summary":"Read file"}]} '
             "Note: use {braces} freely.")
    stub.queue(body=message_reply(reply))
    summarize.ai_summaries(run, api_key=ANTHROPIC_KEY, model="claude-haiku-5-5")
    assert run.overall_summary == "Added a cache"
    assert run.steps[0].summary == "Read file"


def test_summary_json_ignores_invalid_steps_and_coerces_the_number(stub, tmp_path):
    run = _native_run(tmp_path, [_read_step(1), _read_step(2)])
    stub.queue(body=message_reply(
        '{"overview":"X","steps":["bad", 7, {"n":"two","summary":"ok"}, {"n":1,"summary":"first"}]}'))
    summarize.ai_summaries(run, api_key=ANTHROPIC_KEY, model="claude-haiku-5-5")
    assert run.overall_summary == "X"
    assert run.steps[0].summary == "first"
    assert run.steps[1].summary == "ok"


def test_summary_json_accepts_a_code_fence(stub, tmp_path):
    run = _native_run(tmp_path, [_read_step(1)])
    stub.queue(body=message_reply(
        '```json\n{"overview":"Added a cache","steps":[{"n":1,"summary":"Read file"}]}\n```'))
    summarize.ai_summaries(run, api_key=ANTHROPIC_KEY, model="claude-haiku-5-5")
    assert run.overall_summary == "Added a cache"
    assert run.steps[0].summary == "Read file"


def test_summary_without_json_is_still_an_error(stub, tmp_path):
    run = _native_run(tmp_path, [_read_step(1)])
    stub.queue(body=message_reply("I cannot help with that."))
    with pytest.raises(RuntimeError, match="JSON"):
        summarize.ai_summaries(run, api_key=ANTHROPIC_KEY, model="claude-haiku-5-5")


def test_cli_ai_keeps_the_receipt_when_the_reply_has_no_json(stub, tmp_path, capsys):
    path = _native_file(tmp_path, [_read_step(1)])
    stub.queue(body=message_reply("I cannot help with that."))
    assert main(["receipt", str(path), "--ai", "--format", "md", "-o", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.err.startswith("AI summaries skipped:")
    assert captured.out.strip()


def test_cli_ai_uses_summaries_with_trailing_prose(stub, tmp_path, capsys):
    path = _native_file(tmp_path, [_read_step(1)])
    stub.queue(body=message_reply(
        'Here you go: {"overview":"Added a cache","steps":[{"n":1,"summary":"Read file"}]} '
        "Note: use {braces} freely."))
    assert main(["receipt", str(path), "--ai", "--format", "md", "-o", "-"]) == 0
    captured = capsys.readouterr()
    assert "Added a cache" in captured.out
    assert "Read file" in captured.out
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------- error bodies (LLM6)

class _RecordingBody(io.BytesIO):
    """An error body that remembers being closed, the way a real response must be."""

    def __init__(self, data: bytes = b""):
        super().__init__(data)
        self.closed_by_us = False

    def close(self):
        self.closed_by_us = True
        super().close()


def test_http_error_body_closed():
    body = _RecordingBody(json.dumps(error_body("model not found")).encode("utf-8"))
    exc = urllib.error.HTTPError("http://127.0.0.1/v1/messages", 400, "Bad Request", {}, body)
    assert llm._error_text(exc, ANTHROPIC_KEY) == "model not found"
    assert body.closed_by_us


def test_every_retried_error_body_is_closed(monkeypatch):
    bodies = []

    def open_raising(*_args, **_kwargs):
        body = _RecordingBody(json.dumps(error_body("overloaded")).encode("utf-8"))
        bodies.append(body)
        raise urllib.error.HTTPError("http://127.0.0.1/v1/messages", 529, "Overloaded", {}, body)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_raising)
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)
    with pytest.raises(LLMError, match="529"):
        llm.call([{"role": "user", "content": "x"}], "claude-haiku-5-5", api_key=ANTHROPIC_KEY)
    assert len(bodies) == llm.MAX_RETRIES + 1      # the first attempt plus every retry
    assert all(body.closed_by_us for body in bodies)
