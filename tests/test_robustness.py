import json

import pytest

from runledger.cli import main
from runledger.parser import Run, Usage, _ts, parse_session

USER = {"type": "user", "sessionId": "robust", "timestamp": "2026-01-02T10:00:00Z",
        "cwd": "/p", "message": {"role": "user", "content": "do the thing"}}


def _assistant(content, **msg):
    return {"type": "assistant", "timestamp": "2026-01-02T10:01:00Z",
            "message": {"role": "assistant", "id": "m1", "model": "claude-sonnet-4-5",
                        "content": content, **msg}}


def _write(tmp_path, lines, name="s.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n",
                    encoding="utf-8")
    return path


def _all_formats(capsys, path):
    for fmt in ("html", "md", "json"):
        assert main(["receipt", str(path), "--format", fmt, "-o", "-"]) == 0
        out = capsys.readouterr().out
        if fmt == "json":
            json.loads(out)
        assert "Traceback" not in out


BAD_LINES = {
    "list": "[]",
    "number": "1",
    "string": '"x"',
    "null": "null",
    "truncated": '{"type": "assistant", "message": {"role"',
    "deep": "[" * 100_000 + "]" * 100_000,
    "deep_object": '{"a":' * 100_000 + "1" + "}" * 100_000,
    "message_string": {"type": "assistant", "message": "x"},
    "message_list": {"type": "user", "message": [1, 2]},
    "content_int": _assistant(5),
    "content_dict": {"type": "user", "message": {"role": "user", "content": {"a": 1}}},
    "text_null": _assistant([{"type": "text", "text": None}]),
    "user_text_null": {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": None}]}},
    "input_string": _assistant([{"type": "tool_use", "id": "a", "name": "Bash", "input": "ls"}]),
    "name_int": _assistant([{"type": "tool_use", "id": 7, "name": 3, "input": {}}]),
    "result_id_list": {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": ["a"], "content": "ok"}]}},
    "result_text_null": {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": None}]}]}},
    "usage_string": _assistant([], usage={"input_tokens": "lots", "output_tokens": "12"}),
    "usage_list": _assistant([], usage=[1, 2]),
    "usage_negative": _assistant([], usage={"input_tokens": -5, "output_tokens": float("inf")}),
    "type_list": {"type": ["user"], "sessionId": "robust"},
    "timestamp_int": {"type": "user", "timestamp": 5, "message": {"role": "user", "content": "x"}},
    "cwd_dict": {"type": "user", "cwd": {"a": 1}, "message": {"role": "user", "content": "x"}},
    "model_dict": _assistant([], model={"a": 1}),
}


@pytest.mark.parametrize("name", sorted(BAD_LINES))
def test_bad_lines_are_skipped_and_every_format_renders(tmp_path, capsys, name):
    good_call = _assistant([{"type": "tool_use", "id": "a", "name": "Read",
                             "input": {"file_path": "/p/a.py"}}],
                           usage={"input_tokens": 10, "output_tokens": 2})
    path = _write(tmp_path, [USER, BAD_LINES[name], good_call])
    run = parse_session(path)
    assert run.session_id == "robust"
    assert any(s.tool == "Read" for s in run.steps)
    _all_formats(capsys, path)


def test_non_utf8_bytes_are_replaced(tmp_path, capsys):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(json.dumps(USER).encode()[:-3] + b' \xff"}}\n')
    run = parse_session(path)
    assert run.prompts and "\ufffd" in run.prompts[0]
    _all_formats(capsys, path)


@pytest.mark.parametrize("content", [b"", b"\n\n", json.dumps({"type": "summary", "summary": "x"}).encode()])
def test_empty_sessions_render_with_no_steps(tmp_path, capsys, content):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(content)
    run = parse_session(path)
    assert run.steps == [] and run.usage == Usage()
    _all_formats(capsys, path)


@pytest.mark.parametrize("content", ['{"hello": "world"}', "not json at all", '[1, 2, 3]'])
def test_non_session_files_give_a_clear_error(tmp_path, capsys, content):
    path = tmp_path / "notes.jsonl"
    path.write_text(content + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a supported session file"):
        parse_session(path)
    assert main(["receipt", str(path), "--format", "md", "-o", "-"]) == 1
    err = capsys.readouterr().err
    assert "not a supported session file" in err and "Traceback" not in err


@pytest.mark.parametrize("start,end,seconds", [
    ("2026-01-01T00:00:00", "2026-01-01T00:01:00Z", 60.0),             # naive vs aware
    ("2026-01-01T01:00:00+01:00", "2026-01-01T00:01:00Z", 60.0),      # mixed offsets
    ("2026-01-01T00:00:00.123456789Z", "2026-01-01T00:01:00.5Z", 60.376544),  # nanoseconds
    ("2026-01-01T00:00:00Z", "garbage", None),
    ("2026-01-01T00:02:00Z", "2026-01-01T00:01:00Z", 0.0),            # clock went backwards
])
def test_duration_handles_mixed_and_long_timestamps(start, end, seconds):
    run = Run("s", "/x", None, None, start, end, [], [], "", Usage(), {})
    if seconds is None:
        assert run.duration_seconds is None
    else:
        assert run.duration_seconds == pytest.approx(seconds)


def test_long_fraction_fallback_works_without_native_support(monkeypatch):
    # Python 3.9 and 3.10 reject more than 6 fractional digits; force that path on newer versions.
    import runledger.parser as parser_module

    real = parser_module.datetime

    class Strict(real):
        @classmethod
        def fromisoformat(cls, text):
            if parser_module._LONG_FRACTION.search(text):
                raise ValueError("too many digits")
            return real.fromisoformat(text)

    monkeypatch.setattr(parser_module, "datetime", Strict)
    assert _ts("2026-01-01T00:00:00.123456789Z").microsecond == 123456
