"""The CLI reports failures as one `error:` line, never a traceback."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from runledger import adapters, cli, parser
from runledger.cli import main

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "tests" / "fixtures" / "sample_session.jsonl"


def _cli(*args, cwd, **env):
    environ = {k: v for k, v in os.environ.items() if k != "RUNLEDGER_DEBUG"}
    environ.update(PYTHONPATH=str(ROOT), HOME=str(cwd), **env)
    return subprocess.run([sys.executable, "-m", "runledger", *args], cwd=cwd, capture_output=True,
                          env=environ, timeout=60)


def test_unexpected_errors_print_one_line(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("something\nbroke")
    monkeypatch.delenv("RUNLEDGER_DEBUG", raising=False)
    monkeypatch.setattr(cli, "build", boom)
    assert main(["receipt", str(SAMPLE), "--format", "md", "-o", "-"]) == 1
    err = capsys.readouterr().err
    assert err == "error: something broke (set RUNLEDGER_DEBUG=1 for details)\n"


def test_debug_env_shows_the_traceback(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setenv("RUNLEDGER_DEBUG", "1")
    monkeypatch.setattr(cli, "build", boom)
    with pytest.raises(RuntimeError, match="boom"):
        main(["receipt", str(SAMPLE), "--format", "md", "-o", "-"])


def test_keyboard_interrupt_exits_130(monkeypatch, capsys):
    def stop(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "build", stop)
    assert main(["receipt", str(SAMPLE), "--format", "md", "-o", "-"]) == 130
    assert capsys.readouterr().err == "interrupted\n"


def test_missing_output_folder_is_a_clear_error(tmp_path):
    result = _cli("receipt", str(SAMPLE), "--format", "md", "-o", str(tmp_path / "no" / "x.md"), cwd=tmp_path)
    err = result.stderr.decode()
    assert result.returncode == 1
    assert err.startswith("error: cannot write ") and "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


@pytest.mark.parametrize("fmt", ["md", "html", "json"])
def test_cp1252_console_gets_utf8_receipts(tmp_path, fmt):
    result = _cli("receipt", str(SAMPLE), "--format", fmt, "-o", "-", cwd=tmp_path, PYTHONIOENCODING="cp1252")
    assert result.returncode == 0, result.stderr.decode()
    assert b"Traceback" not in result.stderr
    result.stdout.decode("utf-8")  # valid UTF-8, including non-cp1252 characters
    if fmt == "md":
        assert "−".encode("utf-8") in result.stdout


def test_cp1252_console_status_line_does_not_crash(tmp_path):
    result = _cli("receipt", str(SAMPLE), "--format", "md", "-o", str(tmp_path / "r.md"),
                  cwd=tmp_path, PYTHONIOENCODING="cp1252")
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.startswith(b"Receipt: ")


def test_broken_pipe_exits_quietly(monkeypatch, capsys):
    # A real `| head` cannot be reproduced reliably: some sandboxes never report EPIPE.
    def closed(*a, **k):
        raise BrokenPipeError(32, "Broken pipe")
    monkeypatch.setattr(cli, "_write_stdout", closed)
    assert main(["receipt", str(SAMPLE), "--format", "md", "-o", "-"]) == 1
    assert capsys.readouterr().err == ""


def test_vanished_files_do_not_break_sorting(tmp_path):
    gone = tmp_path / "gone.jsonl"
    assert parser.mtime(gone) == 0.0
    here = tmp_path / "here.jsonl"
    here.write_text("", encoding="utf-8")
    assert parser.mtime(here) > 0


def test_list_skips_a_session_that_vanishes_after_listing(tmp_path, monkeypatch, capsys):
    gone = tmp_path / "gone.jsonl"
    keep = tmp_path / "keep.jsonl"
    keep.write_bytes(SAMPLE.read_bytes())
    monkeypatch.setattr(adapters, "find_sessions", lambda project, agent: [gone, keep])
    real_detect = adapters.detect

    class Ghost:
        @staticmethod
        def parse(path):
            run = real_detect(keep).parse(keep)
            return run

    monkeypatch.setattr(adapters, "detect", lambda p: Ghost if p == gone else real_detect(p))
    assert main(["list", "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert len([line for line in out.splitlines() if "steps" in line and "when" not in line]) == 1


def test_find_sessions_tolerates_a_file_removed_during_sort(tmp_path, monkeypatch):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("", encoding="utf-8")

    class OneAdapter:
        NAME = "x"

        @staticmethod
        def find_sessions(project):
            return [a, b]  # b was listed but deleted before the sort

    monkeypatch.setattr(adapters, "adapters", lambda: [OneAdapter])
    assert adapters.find_sessions(str(tmp_path)) == [a, b]


def test_guard_with_tty_stdin_prints_help_and_exits_2(monkeypatch, capsys):
    class FakeTTY:
        def isatty(self):
            return True

        def read(self, *args, **kwargs):
            raise AssertionError("must not read from TTY stdin")

    monkeypatch.setattr(sys, "stdin", FakeTTY())
    rc = main(["guard"])
    captured = capsys.readouterr()
    assert rc == 2
    for subcmd in ("install", "uninstall", "test"):
        assert subcmd in captured.out

