"""Files written through the shell, and reconciling them with the Git working tree."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from runledger import gitinfo
from runledger.adapters import droid
from runledger.cli import build, reconcile_git
from runledger.parser import Run, Step, Usage
from runledger.receipt import to_dict
from runledger.shell_writes import ShellWrite, shell_deletes, shell_writes
from runledger.summarize import file_changes

FIX = Path(__file__).parent / "fixtures" / "droid_shell_edits.jsonl"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _paths(command):
    return [(w.path, w.added, w.deleted) for w in shell_writes(command)]


@pytest.mark.parametrize("command, expected", [
    ("echo hi > out.txt", [("out.txt", 0, False)]),
    ("echo hi >> log.txt 2>/dev/null", [("log.txt", 0, False)]),
    ("pytest -q 2>&1 | tee -a run.log", [("run.log", 0, False)]),
    ("ls > /dev/null; echo x &> all.log", [("all.log", 0, False)]),
    ("sed -i 's/a/b/' x.py y.py", [("x.py", 0, False), ("y.py", 0, False)]),
    ("sed -i.bak -e 's/a/b/' x.py", [("x.py", 0, False)]),
    ("perl -pi -e 's/x/y/' q.pl", [("q.pl", 0, False)]),
    ("touch a.txt b.txt", [("a.txt", 0, False), ("b.txt", 0, False)]),
    ("cp src.txt dst.txt", [("dst.txt", 0, False)]),
    ("cp a.txt b.txt out/", [("out/a.txt", 0, False), ("out/b.txt", 0, False)]),
    ("mv old.py new.py", [("new.py", 0, False), ("old.py", 0, True)]),
    ("cd sub && echo x > f.txt", [("sub/f.txt", 0, False)]),
    ("cd /repo && cd app && echo x > f.txt", [("/repo/app/f.txt", 0, False)]),
    ("echo x>>'my file.txt'", [("my file.txt", 0, False)]),
    ("cat > m.sql <<'EOF'\nline 1\nline 2\nEOF\necho done > d.txt", [("m.sql", 2, False), ("d.txt", 0, False)]),
    ("cat <<-EOF > g\n\tone\n\tEOF", [("g", 1, False)]),
    ("python3 -c \"open('z.txt', 'w').write('1')\"", [("z.txt", 0, False)]),
    ("python3 - <<'EOF'\np='a.py'\nopen(p).read()\nopen(p,'w').write('x')\np='b.md'\nopen(p, mode='a').write('y')\nEOF",
     [("a.py", 0, False), ("b.md", 0, False)]),
    ("python - <<EOF\nfrom pathlib import Path\nPath('c.txt').write_text('x')\nEOF", [("c.txt", 0, False)]),
    ("echo '<<EOF'\necho x > a.txt", [("a.txt", 0, False)]),
    ('echo "<<EOF"\necho x > b.txt', [("b.txt", 0, False)]),
    ("cat <<<word\necho x > c.txt", [("c.txt", 0, False)]),
    ("echo $((1<<2))\necho x > d.txt", [("d.txt", 0, False)]),
    ("# <<EOF\necho x > e.txt", [("e.txt", 0, False)]),
    ("echo $'it\\'s <<EOF'\necho x > f.txt", [("f.txt", 0, False)]),
    ("python3 - <<'EOF'\nopen('a.py', 'w').write('1\\n')\nEOF\necho x > b.txt", [("a.py", 0, False), ("b.txt", 0, False)]),
    ('cat <<"EOF" > c.txt\nhi\nEOF', [("c.txt", 1, False)]),
    ("cat <<-EOF > d.txt\n\thi\n\tEOF", [("d.txt", 1, False)]),
    ("cat <<EOF > u.txt\nline1\nline2", [("u.txt", 2, False)]),
])
def test_detects_shell_writes(command, expected):
    assert _paths(command) == expected


@pytest.mark.parametrize("command", [
    "grep 'a > b' f.txt",
    "sed -n 1p f.txt",
    "cat README.md | head",
    "python3 -c \"print(open('r.txt').read())\"",
    "echo x > $OUT",
    "echo x > *.log",
    "cd $DIR && echo x > f.txt",
    "cd && echo x > f.txt",
    "echo 'unbalanced > x",
    "cat <<EOF\n> not a redirect.txt\nEOF",
])
def test_ignores_reads_and_unresolvable_targets(command):
    assert shell_writes(command) == []


@pytest.mark.parametrize("command, expected", [
    (r"echo hi > E:\Work\Client y\z.txt", [("E:\\Work\\Client", 0, False)]),
    (r"echo hi > E:/Work/Client y/z.txt", [("E:\\Work\\Client", 0, False)]),
    (r"echo hi >> 'E:\Work\Client y\z.txt'", [("E:\\Work\\Client y\\z.txt", 0, False)]),
    (r"echo hi > E:\Work\Client X\secret.txt", [("E:\\Work\\Client", 0, False)]),
    (r"touch E:\Work\Client y\z.txt", [("E:\\Work\\Client", 0, False), ("yz.txt", 0, False)]),
])
def test_an_unquoted_spaced_drive_path_is_the_shells_own_words(command, expected):
    """CORE-SCR-R4-001: a shell splits an unquoted path at its spaces, so the write targets are
    its own words; only a quoted path is one word. Showing the folder name whole is a label
    output renders (`runledger.labels`), never the reading file changes are computed from."""
    assert _paths(command) == expected


def test_a_spaced_drive_write_inside_the_project_lists_the_shells_own_files():
    """R5-003: `touch C:/repo/My Folder/x.txt` writes the two files a shell writes, so a receipt
    counts exactly those; the whole path is a label a finding shows, never a file key. The drive
    path is spelled out so the check reads the same on every host."""
    run = _run("C:/repo", ["touch C:/repo/My Folder/x.txt"])
    assert sorted(file_changes(run)) == ["Folder/x.txt", "My"]


@pytest.mark.parametrize("command, deleted, written", [
    (r"rm C:/repo/a.txt ../outside.txt", [r"C:\repo\a.txt", "../outside.txt"], []),
    (r"rm C:/repo/a.txt > C:/repo/log ../outside.txt",
     [r"C:\repo\a.txt", "../outside.txt"], [r"C:\repo\log"]),
    (r"rm < C:/repo/input ../outside.txt", ["../outside.txt"], []),
])
def test_spaced_drive_path_does_not_swallow_a_relative_operand(command, deleted, written):
    """CORE-SCR-R2-004: `../outside.txt` is an argument of its own, so the delete stays visible
    instead of being read as the tail of the drive path (or of a redirect target) before it."""
    assert shell_deletes(command) == deleted
    assert [w.path for w in shell_writes(command)] == written


@pytest.mark.parametrize("command, expected", [
    ("echo <<EOF $'first\nsecond'\npayload\nEOF\nrm k.txt", ["k.txt"]),
])
def test_a_queued_heredoc_starts_after_the_command_line(command, expected):
    """CORE-SCR-R3-002: a nested newline inside a multiline `$'...'` string is not the end of
    the command line, so the heredoc body starts at the real terminator and the delete after
    its `EOF` line stays visible."""
    assert shell_deletes(command) == expected


@pytest.mark.parametrize("command, expected", [
    ("echo ok # <<EOF\nrm k.txt", ["k.txt"]),
    ("echo a#b; rm k.txt", ["k.txt"]),
    ("printf x # note\nrm k.txt", ["k.txt"]),
])
def test_comment_ends_at_the_newline(command, expected):
    # shlex would swallow the comment's newline and read the next line as arguments.
    assert shell_deletes(command) == expected


def test_droid_fixture_lists_shell_edited_files():
    run = droid.parse(FIX)
    changes = file_changes(run)
    assert sorted(changes) == ["README.md", "app/routes.py", "migrations/002_users_soft_delete.sql",
                               "tests/test_api.py"]
    assert all(fc.shell and not fc.verified for fc in changes.values())
    assert changes["migrations/002_users_soft_delete.sql"].added == 1
    # The backup copy in /tmp is outside the project.
    assert not any("tasklite.db.bak" in p for p in changes)


def _run(cwd, commands, branch=None):
    steps = [Step(i + 1, "Bash", {"command": c}, f"c{i}", "m", None) for i, c in enumerate(commands)]
    return Run("s", "p", str(cwd), branch, None, None, ["go"], steps, "", Usage(), {})


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "feat/shell")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "app.py").write_text("a\nb\nc\n")
    (tmp_path / "gone.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


@needs_git
def test_git_fills_line_counts_for_shell_writes(repo):
    (repo / "app.py").write_text("a\nB\nc\nd\n")
    (repo / "new.sql").write_text("one\ntwo\nthree\n")
    (repo / "gone.txt").unlink()
    (repo / "user_edit.txt").write_text("not from the run\n")
    run = _run(repo, ["python3 - <<'EOF'\nopen('app.py','w').write('...')\nEOF",
                      "echo one > new.sql", "mv gone.txt elsewhere.txt"])
    reconcile_git(run)
    assert set(run.git_changes) <= {"app.py", "new.sql", "gone.txt", "elsewhere.txt"}
    changes = file_changes(run)
    assert "user_edit.txt" not in changes
    app, new, gone = changes["app.py"], changes["new.sql"], changes["gone.txt"]
    assert (app.added, app.removed, app.created, app.verified) == (2, 1, False, True)
    assert (new.added, new.removed, new.created, new.verified) == (3, 0, True, True)
    assert gone.deleted and gone.verified and gone.removed == 1
    assert not changes["elsewhere.txt"].verified  # the move target does not exist
    files = {f["path"]: f for f in to_dict(run, 0, "low", [])["files"]}
    assert files["app.py"]["verified"] and files["app.py"]["shell"]


@needs_git
def test_tool_edits_keep_their_own_counts(repo):
    (repo / "app.py").write_text("changed\n")
    run = _run(repo, [])
    run.steps.append(Step(1, "Edit", {"file_path": str(repo / "app.py"), "old_string": "a", "new_string": "x\ny"},
                          "e", "m", None))
    reconcile_git(run)
    assert run.git_changes == {}
    fc = file_changes(run)["app.py"]
    assert (fc.added, fc.removed, fc.verified) == (2, 1, False)


@needs_git
def test_branch_comes_from_git_when_the_log_has_none(repo):
    run = _run(repo, [])
    reconcile_git(run)
    assert run.git_branch == "feat/shell"


@needs_git
def test_recorded_branch_wins(repo):
    run = _run(repo, [], branch="from-log")
    reconcile_git(run)
    assert run.git_branch == "from-log"


@needs_git
def test_detached_head_has_no_branch(repo):
    _git(repo, "checkout", "-q", "--detach")
    assert gitinfo.current_branch(str(repo)) is None


@needs_git
def test_repository_without_commits(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("1\n2\n")
    assert gitinfo.working_changes(str(tmp_path)) == {"a.txt": {"added": 2, "removed": 0, "status": "untracked",
                                                        "binary": False}}
    assert gitinfo.current_branch(str(tmp_path)) == "main"


def test_no_repository_or_folder(tmp_path):
    assert gitinfo.current_branch(str(tmp_path)) is None
    assert gitinfo.working_changes(str(tmp_path)) == {}
    assert gitinfo.current_branch(str(tmp_path / "missing")) is None
    assert gitinfo.current_branch(None) is None


@needs_git
def test_build_receipt_uses_git(repo, tmp_path_factory):
    (repo / "notes.md").write_text("x\ny\n")
    session = tmp_path_factory.mktemp("s") / "sess.jsonl"
    events = [
        {"type": "session_start", "id": "abc", "title": "t", "cwd": str(repo)},
        {"type": "message", "id": "m1", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "write notes"}]}},
        {"type": "message", "id": "m2", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Execute",
                                                         "input": {"command": "printf 'x\\ny\\n' > notes.md"}}]}},
    ]
    session.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    run, *_ = build(str(session))
    assert run.git_branch == "feat/shell"
    fc = file_changes(run)["notes.md"]
    assert (fc.added, fc.created, fc.verified) == (2, True, True)


def test_shell_write_dataclass_defaults():
    assert ShellWrite("a") == ShellWrite("a", 0, False)
