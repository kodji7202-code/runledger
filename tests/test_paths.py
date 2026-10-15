import json

import pytest

from runledger.cli import main
from runledger.parser import Run, Step, Usage
from runledger.paths import relative
from runledger.shell_writes import shell_deletes
from runledger.summarize import file_changes


@pytest.mark.parametrize("path,cwd,expected", [
    ("/p/src/a.py", "/p", "src/a.py"),
    ("/p/src/a.py", "/p/", "src/a.py"),
    ("/p/./src/../src/a.py", "/p", "src/a.py"),
    ("/pp/a.py", "/p", "/pp/a.py"),
    ("/p", "/p", "/p"),
    ("/other/a.py", "/p", "/other/a.py"),
    ("src/a.py", "/p", "src/a.py"),
    ("C:\\repo\\src\\a.py", "C:\\repo", "src/a.py"),
    ("c:/Repo/src/A.py", "C:\\repo", "src/A.py"),
    ("C:\\repo\\src/a.py", "C:/repo/", "src/a.py"),
    ("C:\\repo2\\a.py", "C:\\repo", "C:\\repo2\\a.py"),
    ("D:\\repo\\a.py", "C:\\repo", "D:\\repo\\a.py"),
    ("src\\a.py", "C:\\repo", "src/a.py"),
    ("/p/a.py", None, "/p/a.py"),
    ("", "/p", ""),
])
def test_relative_handles_posix_and_windows(path, cwd, expected):
    assert relative(path, cwd) == expected


@pytest.mark.parametrize("command,expected", [
    ('rm "a b.txt"', ["a b.txt"]),
    ("rm -rf build/ && pytest -q", ["build"]),
    ("rm -f a.py b.py; echo done > log.txt", ["a.py", "b.py"]),
    ("cd sub && rm x.py", ["sub/x.py"]),
    ("git rm old.py", ["old.py"]),
    ("git rm -r --cached secrets.env", []),
    ("git -C . rm --cached k.py", []),
    ("git -C repo rm k.py", ["repo/k.py"]),
    ("unlink stale.lock", ["stale.lock"]),
    ("sudo rm -- -weird.txt", []),
    ("rm *.pyc $TMP/x", []),
    ("rm /dev/null", []),
    ("echo rm a.py", []),
    ("rm 'unterminated", []),
])
def test_shell_deletes(command, expected):
    assert shell_deletes(command) == expected


def _changes(command, cwd="/p"):
    run = Run("s", "/x.jsonl", cwd, None, None, None, [],
              [Step(1, "Bash", {"command": command}, "t", None, None)], "", Usage(), {})
    return file_changes(run)


def test_rm_targets_are_relative_unquoted_and_skip_outside_and_temp():
    fc = _changes('rm "a b.txt" /p/src/x.py /tmp/t.log /etc/other.conf; git rm --cached k.py')
    assert sorted(fc) == ["a b.txt", "src/x.py"]
    assert all(c.deleted and c.shell for c in fc.values())


def test_windows_session_lists_files_relative_and_once(tmp_path, capsys):
    cwd = "C:\\Users\\dev\\app"
    steps = [
        {"tool": "Write", "input": {"file_path": cwd + "\\src\\a.py", "content": "x = 1\n"}},
        {"tool": "Edit", "input": {"file_path": "c:/users/dev/app/src/a.py",
                                   "old_string": "x = 1", "new_string": "x = 2"}},
        {"tool": "Read", "input": {"file_path": cwd + "\\README.md"}},
        {"tool": "Write", "input": {"file_path": "D:\\elsewhere\\b.py", "content": "y\n"}},
    ]
    path = tmp_path / "win.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "session_id": "win-1", "cwd": cwd,
                                "steps": steps}), encoding="utf-8")
    assert main(["receipt", str(path), "--format", "json", "-o", "-"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert sorted(f["path"] for f in data["files"]) == ["D:\\elsewhere\\b.py", "src/a.py"]
    assert data["steps"][2]["summary"] == "Read README.md"
    for fmt in ("md", "html"):
        assert main(["receipt", str(path), "--format", fmt, "-o", "-"]) == 0
        assert "src/a.py" in capsys.readouterr().out
