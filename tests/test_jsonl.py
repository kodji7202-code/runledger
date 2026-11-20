"""Shared session reader tolerance and adapter integration."""
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from runledger.adapters import codex, droid
from runledger.jsonl import read_objects


@pytest.mark.parametrize("strip_lines", [False, True])
@pytest.mark.parametrize("data, expected", [
    (b'', []),
    (b'\xef\xbb\xbf{"ok":1}\r\n', [{"ok": 1}]),
    (b'\n \t\nnot json\n{"partial":\n{"ok":2}\n{"final":', [{"ok": 2}]),
    (b'null\ntrue\n42\n"text"\n[]\n{}', [{}]),
    (b'{"text":"\xff"}\n\xff\n{}', [{"text": "\ufffd"}, {}]),
    (b'[' * 2000 + b']' * 2000 + b'\n{}', [{}]),
    (b'{"complete":true}', [{"complete": True}]),
])
def test_read_objects(tmp_path, strip_lines, data, expected):
    path = tmp_path / "session.jsonl"
    path.write_bytes(data)
    assert list(read_objects(path, strip_lines=strip_lines)) == expected


@pytest.mark.parametrize("strip_lines", [False, True])
def test_unicode_whitespace(tmp_path, strip_lines):
    path = tmp_path / "session.jsonl"
    path.write_text('\u00a0{"ok":1}\u00a0\n', encoding="utf-8")
    assert list(read_objects(path, strip_lines=strip_lines)) == ([{"ok": 1}] if strip_lines else [])


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(read_objects(tmp_path / "missing.jsonl"))


@pytest.mark.parametrize("adapter", [codex, droid])
def test_adapter_fixture_with_malformed_and_partial_lines(tmp_path, adapter):
    source = Path(__file__).parent / "fixtures" / f"{adapter.NAME}_session.jsonl"
    path = tmp_path / source.name
    shutil.copyfile(source, path)
    settings = source.with_suffix(".settings.json")
    if settings.exists():
        shutil.copyfile(settings, path.with_suffix(".settings.json"))
    expected = asdict(adapter.parse(path))
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + b'bad json\n\nnull\n{"partial":\n'
                     + b''.join(lines[1:]) + b'\n{"truncated":')
    assert asdict(adapter.parse(path)) == expected
