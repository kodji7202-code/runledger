import json
import shutil
import subprocess

import pytest

from runledger import gitinfo
from runledger.cli import main, reconcile_git
from runledger.parser import Run, Step, Usage
from runledger.receipt import render
from runledger.summarize import file_changes

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _init(path, commit=True):
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    if commit:
        (path / "app.py").write_text("a\nb\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "init")
    return path


def _run(cwd, *commands, branch=None):
    steps = [Step(i + 1, "Bash", {"command": c}, f"t{i}", None, None) for i, c in enumerate(commands)]
    return Run("s", "/x.jsonl", str(cwd), branch, None, None, ["go"], steps, "", Usage(), {})


def test_no_git_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert gitinfo.current_branch(str(tmp_path)) is None
    assert gitinfo.working_changes(str(tmp_path)) == {}
    assert gitinfo.file_diffs(str(tmp_path), ["tests/test_a.py"]) == {}


@needs_git
def test_not_a_repo_gives_static_counts_and_no_branch(tmp_path, capsys):
    run = _run(tmp_path, "printf 'x\\ny\\n' > out.txt")
    reconcile_git(run)
    assert run.git_branch is None and run.git_changes == {}
    md = render(run, 0, "Low", [], "md")
    assert "**Branch:** `-`" in md


@needs_git
def test_missing_folder(tmp_path):
    missing = str(tmp_path / "gone")
    assert gitinfo.current_branch(missing) is None
    assert gitinfo.working_changes(missing) == {}


@needs_git
def test_detached_head_has_no_branch(tmp_path):
    repo = _init(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    assert gitinfo.current_branch(str(repo)) is None
    (repo / "app.py").write_text("a\nc\nd\n")
    assert gitinfo.working_changes(str(repo), ["app.py"])["app.py"]["added"] == 2


@needs_git
def test_repo_without_commits_counts_new_files(tmp_path):
    repo = _init(tmp_path, commit=False)
    (repo / "new.py").write_text("1\n2\n3\n")
    changes = gitinfo.working_changes(str(repo), ["new.py"])
    assert changes == {"new.py": {"added": 3, "removed": 0, "status": "untracked", "binary": False}}
    assert gitinfo.file_diffs(str(repo), ["new.py"]) == {}
    assert gitinfo.current_branch(str(repo)) == "main"


@needs_git
def test_binary_files_are_marked_not_counted(tmp_path):
    repo = _init(tmp_path)
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x01" * 8)
    _git(repo, "add", "logo.png")
    _git(repo, "commit", "-q", "-m", "logo")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x02" * 9)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    changes = gitinfo.working_changes(str(repo), ["logo.png", "blob.bin"])
    assert changes["logo.png"]["binary"] is True and changes["logo.png"]["added"] == 0
    assert changes["blob.bin"] == {"added": 0, "removed": 0, "status": "untracked", "binary": True}

    run = _run(repo, "cp /tmp/new.png logo.png", "head -c 3 /dev/urandom > blob.bin")
    reconcile_git(run)
    fc = file_changes(run)
    assert fc["logo.png"].binary and fc["blob.bin"].binary
    md = render(run, 0, "Low", [], "md")
    assert "- `logo.png` (binary)" in md and "- `blob.bin` (new) (binary)" in md
    page = render(run, 0, "Low", [], "html")
    assert page.count('<td class="num">(binary)</td>') == 2
    data = json.loads(render(run, 0, "Low", [], "json"))
    assert {f["path"]: f["binary"] for f in data["files"]} == {"logo.png": True, "blob.bin": True}


@needs_git
def test_mv_rename_shows_source_deleted_and_destination_new(tmp_path):
    repo = _init(tmp_path)
    _git(repo, "mv", "app.py", "main.py")
    _git(repo, "reset", "-q")  # leave the rename in the working tree only
    run = _run(repo, "mv app.py main.py")
    reconcile_git(run)
    fc = file_changes(run)
    assert fc["app.py"].deleted and fc["app.py"].removed == 2
    assert fc["main.py"].created and fc["main.py"].added == 2


@needs_git
@pytest.mark.parametrize("name", ["tests/test a.py", 'tests/test "q".py', "tests/test\tt.py",
                                  "tests/tést_ü.py"])
def test_diffs_of_test_files_with_unusual_names(tmp_path, name):
    repo = _init(tmp_path)
    target = repo / name
    target.parent.mkdir(exist_ok=True)
    target.write_text("def test_x():\n    assert 1\n    assert 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tests")
    target.write_text("def test_x():\n    pass\n")
    diffs = gitinfo.file_diffs(str(repo), [name])
    assert list(diffs) == [name]
    assert "-    assert 1" in diffs[name]


@pytest.mark.parametrize("line,expected", [
    ("--- a/plain.py\n", "plain.py"),
    ("+++ b/with space.py\t\n", "with space.py"),
    ('--- "a/tab\\there.py"\n', "tab\there.py"),
    ('--- "a/q\\"uote.py"\n', 'q"uote.py'),
    ('--- "a/\\303\\251t\\303\\251.py"\n', "été.py"),
    ("--- /dev/null\n", None),
])
def test_header_path_decoding(line, expected):
    assert gitinfo._header_path(line) == expected


@needs_git
def test_receipt_in_a_repo_without_commits(tmp_path, capsys):
    repo = _init(tmp_path, commit=False)
    path = repo / "s.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "session_id": "nc", "cwd": str(repo),
                                "steps": [{"tool": "Bash", "input": {"command": "echo hi > a.txt"}}]}),
                    encoding="utf-8")
    (repo / "a.txt").write_text("hi\n")
    for fmt in ("html", "md", "json"):
        assert main(["receipt", str(path), "--format", fmt, "-o", "-"]) == 0
    capsys.readouterr()
