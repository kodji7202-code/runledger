import json
import os
import shutil
import subprocess

import pytest

from runledger import gitinfo
from runledger.cli import main, reconcile_git
from runledger.parser import Run, Step, Usage
from runledger.receipt import render
from runledger.summarize import file_changes

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
windows_invalid_name = pytest.mark.skipif(os.name == "nt", reason="not a valid file name on Windows")


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
@pytest.mark.parametrize("name", [
    "tests/test a.py",
    pytest.param('tests/test "q".py', marks=windows_invalid_name),
    pytest.param("tests/test\tt.py", marks=windows_invalid_name),
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


@needs_git
def test_pathspec_magic_is_literal(tmp_path, capsys):
    repo = _init(tmp_path)
    for name, content in (("[id].py", "x = 1\n"), ("d.py", "d0 = 0\n"), ("a[1].py", "a = 1\n"), ("a1.py", "a = 0\n")):
        (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "magic")
    (repo / "d.py").write_text("d0 = 0\nd1 = 1\nd2 = 2\nd3 = 3\nd4 = 4\nd5 = 5\n")
    (repo / "a1.py").write_text("a = 0\nb1 = 1\nb2 = 2\nb3 = 3\nb4 = 4\nb5 = 5\n")
    (repo / "[id].py").write_text("x = 1\ny = 2\n")
    (repo / "a[1].py").write_text("a = 1\nb = 2\n")

    wc = gitinfo.working_changes(str(repo), ["[id].py"])
    assert list(wc) == ["[id].py"]
    assert wc["[id].py"]["added"] == 1 and wc["[id].py"]["removed"] == 0

    diffs = gitinfo.file_diffs(str(repo), ["[id].py"])
    assert list(diffs) == ["[id].py"]
    assert "+y = 2" in diffs["[id].py"] and "d1 = 1" not in diffs["[id].py"]

    for target in ("[id].py", "a[1].py"):
        sess = repo / f"sess-{target}.runledger.json"
        sess.write_text(json.dumps({
            "runledger_format": 1,
            "session_id": "s-magic",
            "cwd": str(repo),
            "steps": [{"tool": "Edit", "input": {"file_path": str(repo / target)}}],
        }), encoding="utf-8")
        assert main(["receipt", str(sess), "--format", "json", "-o", "-", "--no-safe"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert [f["path"] for f in data["files"]] == [target]
        assert data["files"][0]["added"] == 1

    if os.name != "nt":
        colon = repo / ":!a.py"
        colon.write_text("c = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "colon")
        colon.write_text("c = 1\nc = 2\n")
        wc_colon = gitinfo.working_changes(str(repo), [":!a.py"])
        assert wc_colon == {":!a.py": {"added": 1, "removed": 0, "status": "modified", "binary": False}}
        diff_colon = gitinfo.file_diffs(str(repo), [":!a.py"])
        assert ":!a.py" in diff_colon and "+c = 2" in diff_colon[":!a.py"]


@needs_git
def test_git_env_hardened(tmp_path, monkeypatch):
    (tmp_path / "repo_a").mkdir()
    (tmp_path / "repo_b").mkdir()
    repo_a = _init(tmp_path / "repo_a")
    repo_b = _init(tmp_path / "repo_b")
    _git(repo_b, "checkout", "-q", "-b", "bogus-branch")
    (repo_a / "app.py").write_text("a\nb\nc\n")
    (repo_b / "app.py").write_text("a\nb\nc\nd\ne\n")

    monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo_b))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repo_b / ".git" / "index"))
    monkeypatch.setenv("GIT_PREFIX", "sub/")

    seen_envs = []
    real_run = subprocess.run

    def spy_run(*args, **kwargs):
        if "env" in kwargs and kwargs["env"] is not None:
            seen_envs.append(dict(kwargs["env"]))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(gitinfo.subprocess, "run", spy_run)
    if hasattr(gitinfo, "clear_cache"):
        gitinfo.clear_cache()

    assert gitinfo.current_branch(str(repo_a)) == "main"
    changes = gitinfo.working_changes(str(repo_a), ["app.py"])
    assert changes == {"app.py": {"added": 1, "removed": 0, "status": "modified", "binary": False}}
    assert seen_envs
    for env in seen_envs:
        assert env.get("GIT_OPTIONAL_LOCKS") == "0"
        assert env.get("GIT_LITERAL_PATHSPECS") == "1"
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        assert env.get("GIT_PAGER") == "cat"
        for forbidden in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
            assert forbidden not in env


@needs_git
def test_reconcile_spawn_count_bounded(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    repo = _init(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_a():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add test")
    (repo / "app.py").write_text("a\nb\nc\n")
    (repo / "new.txt").write_text("hello\n")
    (repo / "tests" / "test_app.py").write_text("def test_a():\n    pass\n")

    if hasattr(gitinfo, "clear_cache"):
        gitinfo.clear_cache()
    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(gitinfo.subprocess, "run", spy_run)
    run = _run(repo, "echo x > app.py && echo hello > new.txt && echo pass > tests/test_app.py")
    run.started = datetime(2099, 1, 1, tzinfo=timezone.utc)
    reconcile_git(run)

    assert run.git_branch == "main"
    assert run.git_changes["app.py"]["added"] == 1
    assert run.git_changes["new.txt"]["status"] == "untracked"
    assert "tests/test_app.py" in run.test_diffs
    assert len(calls) <= 5

