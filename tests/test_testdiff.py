"""Test-file verdicts from Git diffs (runledger.testdiff), their risks and the quality note."""
import os
import shutil
import subprocess

import pytest

from runledger import gitinfo
from runledger.cli import reconcile_git
from runledger.parser import Run, Step, Usage
from runledger.quality import score_run
from runledger.risk import assess
from runledger.summarize import file_changes
from runledger.testdiff import analyze, classify

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _diff(removed, added, path="tests/test_x.py"):
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1 +1 @@"]
    lines += ["-" + l for l in removed] + ["+" + l for l in added]
    return "\n".join(lines) + "\n"


def test_replaced_python_test():
    change = classify("tests/test_x.py", _diff(
        ["def test_delete_user_removes_tasks(client):", "    assert remaining == ['keep']"],
        ["def test_delete_user_is_soft(client):", "    assert gone == 404", "    assert kept == 3"]))
    assert change.verdict == "replaced"
    assert change.removed_tests == ["test_delete_user_removes_tasks"]
    assert change.added_tests == ["test_delete_user_is_soft"]
    assert change.note == ("tests/test_x.py: test_delete_user_removes_tasks replaced by "
                           "test_delete_user_is_soft (1 assertion removed, 2 added).")


def test_replacement_named_after_the_request():
    diff = _diff(["def test_hard_delete():", "    assert x"], ["def test_archive_keeps_rows():", "    assert y"])
    assert classify("t.py", diff).verdict == "weakened"
    assert classify("t.py", diff, ["Archive rows instead of deleting them"]).verdict == "replaced"


def test_replacement_with_fewer_assertions_is_weakened():
    change = classify("tests/test_x.py", _diff(
        ["def test_total():", "    assert a == 1", "    assert b == 2"], ["def test_total_sum():", "    assert a"]))
    assert change.verdict == "weakened"


def test_removed_test_without_replacement_is_weakened():
    change = classify("tests/test_x.py", _diff(["def test_refund_rejects_negative():", "    assert r.status == 400"], []))
    assert change.verdict == "weakened" and change.removed_tests == ["test_refund_rejects_negative"]


def test_net_assertion_loss_is_weakened():
    assert classify("t.py", _diff(["    assert a == 1", "    assert b == 2"], ["    assert a == 1"])).verdict == "weakened"


def test_changed_assertion_is_neutral():
    change = classify("t.py", _diff(["    assert names == ['001.sql']"], ["    assert names == ['001.sql', '002.sql']"]))
    assert change.verdict == "changed"


def test_skip_marker_added():
    assert classify("t.py", _diff([], ["@pytest.mark.skip", "def test_new():"])).verdict == "skipped"


def test_js_tests_are_named_by_their_title():
    change = classify("src/cart.test.ts", _diff(
        ["it('removes the item from the cart', () => {", "  expect(cart.items).toEqual([])"],
        ["it('removes the item and keeps the total', () => {", "  expect(cart.items).toEqual([])",
         "  expect(cart.total).toBe(0)"]))
    assert change.verdict == "replaced"
    assert change.removed_tests == ["removes the item from the cart"]


def test_additions_only_give_no_verdict():
    assert classify("t.py", _diff([], ["def test_new():", "    assert 1"])) is None
    assert analyze({}) == []


# ---------------------------------------------------------------- with Git

OLD = '''def test_delete_user_removes_tasks(client, app, user):
    assert client.delete(f"/users/{user['id']}").status_code == 204
    assert client.get(f"/users/{user['id']}").status_code == 404
    remaining = tasks(app)
    assert remaining == ["keep"]


def test_migrations(conn):
    assert apply_migrations(conn) == ["001_init.sql"]
'''
NEW = '''def test_delete_user_is_soft_and_keeps_tasks(client, app, user):
    assert client.delete(f"/users/{user['id']}").status_code == 204
    assert client.get(f"/users/{user['id']}").status_code == 404
    assert client.delete(f"/users/{user['id']}").status_code == 404
    assert sorted(tasks(app)) == ["keep", "one", "two"]
    assert deleted_at(app, user)


def test_migrations(conn):
    assert apply_migrations(conn) == ["001_init.sql", "002_users_soft_delete.sql"]
'''
WEAK = '''def test_migrations(conn):
    assert apply_migrations(conn)
'''


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text(OLD)
    (tmp_path / "tests" / "fixtures").mkdir()
    (tmp_path / "tests" / "fixtures" / "data.json").write_text("{}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _run(repo, steps, prompt="Soft delete users and keep their tasks"):
    return Run("s", "p", str(repo), None, None, None, [prompt], steps, "", Usage(), {})


def _shell_rewrite(repo):
    return Step(1, "Bash", {"command": f"cd {repo} && python3 - <<'EOF'\np='tests/test_api.py'\n"
                                       "open(p,'w').write(s)\nEOF"}, "b1", None, None)


@needs_git
def test_shell_replaced_test_is_a_neutral_note(repo):
    (repo / "tests" / "test_api.py").write_text(NEW)
    run = _run(repo, [_shell_rewrite(repo)])
    reconcile_git(run)
    assert list(run.test_diffs) == ["tests/test_api.py"]
    score, level, risks = assess(run)
    assert [r.code for r in risks] == []
    signals = {s["name"]: s for s in score_run(run, risks)["signals"]}
    assert signals["test_tampering"]["impact"] == 0
    assert signals["test_changes"]["impact"] == 0
    assert signals["test_changes"]["note"] == (
        "tests/test_api.py: test_delete_user_removes_tasks replaced by "
        "test_delete_user_is_soft_and_keeps_tasks (2 assertions removed, 4 added).")


@needs_git
def test_shell_weakened_test_is_flagged(repo):
    (repo / "tests" / "test_api.py").write_text(WEAK)
    run = _run(repo, [_shell_rewrite(repo)])
    reconcile_git(run)
    _, _, risks = assess(run)
    assert [(r.code, r.reason, r.step) for r in risks] == [
        ("test_weakened", "Removed test_delete_user_removes_tasks from test_api.py with no replacement", 1)]
    signals = {s["name"]: s for s in score_run(run, risks)["signals"]}
    assert signals["test_tampering"]["impact"] == -5
    assert "test_changes" not in signals


@needs_git
def test_diff_verdict_wins_over_the_edit_rule(repo):
    (repo / "tests" / "test_api.py").write_text(NEW)
    old_body = OLD.split("\n\n\n")[0]
    new_body = NEW.split("\n\n\n")[0]
    edit = Step(1, "Edit", {"file_path": str(repo / "tests" / "test_api.py"),
                            "old_string": old_body + "\n    assert a\n    assert b",
                            "new_string": new_body.replace("assert", "check")}, "e1", None, None)
    run = _run(repo, [edit])
    alone = assess(_run(repo, [edit]))[2]
    assert [r.code for r in alone] == ["test_weakened"]  # the per-step rule alone
    reconcile_git(run)
    assert [r.code for r in assess(run)[2]] == []


@needs_git
def test_file_diffs_skip_unchanged_untracked_and_unwanted(repo):
    (repo / "tests" / "test_new.py").write_text("def test_a():\n    assert 1\n")
    (repo / "tests" / "fixtures" / "data.json").write_text("[]\n")
    assert gitinfo.file_diffs(str(repo), ["tests/test_api.py", "tests/test_new.py"]) == {}
    (repo / "tests" / "test_api.py").write_text(WEAK)
    diffs = gitinfo.file_diffs(str(repo), ["tests/test_api.py"])
    assert list(diffs) == ["tests/test_api.py"]
    assert "-def test_delete_user_removes_tasks" in diffs["tests/test_api.py"]


def test_file_diffs_without_repository(tmp_path):
    assert gitinfo.file_diffs(str(tmp_path), ["tests/test_a.py"]) == {}
    assert gitinfo.file_diffs(None, ["tests/test_a.py"]) == {}


# ------------------------------------------------- changes committed after the session start

def _commit_at(repo, when, message):
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True,
                   capture_output=True, env=env)


@pytest.fixture
def dated_repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text(OLD)
    _commit_at(tmp_path, "2026-10-01T09:00:00+00:00", "init")
    return tmp_path


def _session(repo, started, steps):
    run = _run(repo, steps)
    run.started = started
    return run


def _shell_writes(repo):
    return Step(1, "Bash", {"command": f"cd {repo} && python3 - <<'EOF'\n"
                                       "open('tests/test_api.py','w').write(s)\n"
                                       "open('notes.txt','w').write(n)\nEOF"}, "b1", None, None)


@needs_git
def test_changes_committed_after_the_session_start(dated_repo):
    base = subprocess.run(["git", "-C", str(dated_repo), "rev-parse", "--short", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    (dated_repo / "tests" / "test_api.py").write_text(NEW)
    (dated_repo / "notes.txt").write_text("a\nb\nc\n")
    _commit_at(dated_repo, "2026-10-01T11:00:00+00:00", "agent work")
    run = _session(dated_repo, "2026-10-01T10:00:00.500Z", [_shell_writes(dated_repo)])
    reconcile_git(run)
    assert run.git_base == base
    assert list(run.test_diffs) == ["tests/test_api.py"]
    assert run.git_changes["notes.txt"] == {"added": 3, "removed": 0, "status": "added", "binary": False}
    assert (run.git_changes["tests/test_api.py"]["added"], run.git_changes["tests/test_api.py"]["removed"]) == (5, 4)
    _, _, risks = assess(run)
    signals = {s["name"]: s for s in score_run(run, risks)["signals"]}
    assert signals["test_changes"]["note"].startswith(
        "tests/test_api.py: test_delete_user_removes_tasks replaced by test_delete_user_is_soft_and_keeps_tasks")
    files = file_changes(run)
    assert files["notes.txt"].created and files["notes.txt"].verified


@needs_git
def test_uncommitted_changes_still_compare_with_head(dated_repo):
    (dated_repo / "tests" / "test_api.py").write_text(NEW)
    run = _session(dated_repo, "2026-10-01T10:00:00Z", [_shell_writes(dated_repo)])
    reconcile_git(run)
    assert run.git_base is None
    assert list(run.test_diffs) == ["tests/test_api.py"]
    assert run.git_changes["tests/test_api.py"]["status"] == "modified"


@needs_git
@pytest.mark.parametrize("started", [None, "2026-09-01T10:00:00Z", "not a time"])
def test_no_base_keeps_head(dated_repo, started):
    # No start time, or every commit is newer than the start (as in a shallow clone).
    (dated_repo / "tests" / "test_api.py").write_text(NEW)
    _commit_at(dated_repo, "2026-10-01T11:00:00+00:00", "agent work")
    run = _session(dated_repo, started, [_shell_writes(dated_repo)])
    reconcile_git(run)
    assert run.git_base is None
    assert run.test_diffs == {}
    assert gitinfo.session_base(str(dated_repo), None) is None
