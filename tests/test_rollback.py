"""All worktree writes happen in disposable repositories, never the source checkout."""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from runledger import guard, redirects, rollback
from runledger.cli import build, main
from runledger.receipt import render


def git(folder, *args, data=None, check=True):
    result = subprocess.run(["git", "-C", str(folder), *args], input=data,
                            capture_output=True, check=check)
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "a project ü"
    folder.mkdir()
    git(folder, "init", "-q")
    git(folder, "config", "core.autocrlf", "false")
    (folder / ".gitignore").write_text("cache/\n.secret\n", encoding="utf-8")
    (folder / "tracked.txt").write_bytes(b"staged\n")
    git(folder, "add", ".")
    (folder / "tracked.txt").write_bytes(b"unstaged\n")
    (folder / "untracked.txt").write_bytes(b"untracked\n")
    (folder / "cache").mkdir()
    (folder / "cache" / "private.txt").write_text("do not touch\n", encoding="utf-8")
    (folder / ".secret").write_text("synthetic ignored data\n", encoding="utf-8")
    return rollback.Repository(str(folder))


def tree_checkpoint(repo, tree=None):
    return rollback.Checkpoint("20261015T120000000000Z-01234567", "", tree or repo.snapshot_tree(),
                               "2026-10-15T12:00:00+00:00", "Synthetic checkpoint", "session-test")


@pytest.fixture
def tree_safety(repo, monkeypatch):
    """Exercise real Git trees/checkout independently of local author configuration.

    Ref/commit publication is covered separately by identity-dependent integration tests.
    """
    saved = []

    def save(label="", run_id=None, *, tree=None, kind="checkpoint"):
        cp = tree_checkpoint(repo, tree)
        cp.kind = kind
        saved.append(cp)
        return cp

    monkeypatch.setattr(repo, "create", save)
    return saved


@pytest.fixture
def committed_repo(repo):
    # Never supply or override a Git author/committer identity in tests.
    for role in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        probe = subprocess.run(["git", "-C", str(repo.root), "var", role], capture_output=True)
        if probe.returncode:
            pytest.skip("Environment configuration failure: Git author/committer identity is missing")
    git(repo.root, "commit", "-qm", "Initial test tree")
    return repo


@pytest.fixture
def identity_repo(tmp_path, monkeypatch):
    home, xdg = tmp_path / "home", tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    for key in list(os.environ):
        if (key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_"))
                or key == "EMAIL"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    # Also forbid Git's machine-dependent automatic identity guesses.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    folder = tmp_path / "identity-repo"
    folder.mkdir()
    git(folder, "init", "-q")
    (folder / "file.txt").write_text("before\n", encoding="utf-8")
    git(folder, "add", ".")
    return folder


def commit_identity(folder, oid):
    return git(folder, "show", "-s", "--format=%an <%ae>%n%cn <%ce>", oid).decode().splitlines()


def test_checkpoint_rollback_and_hook_without_identity(identity_repo, monkeypatch, capsys):
    folder = identity_repo
    for role in ("AUTHOR", "COMMITTER"):
        result = subprocess.run(["git", "-C", str(folder), "var", f"GIT_{role}_IDENT"],
                                capture_output=True)
        assert result.returncode != 0
    config = (folder / ".git" / "config").read_bytes()
    index = (folder / ".git" / "index").read_bytes()
    environment = dict(os.environ)
    calls = []
    real_run = subprocess.run

    def record(args, **kwargs):
        if "env" in kwargs:
            calls.append((args, dict(kwargs["env"])))
        return real_run(args, **kwargs)

    monkeypatch.setattr(rollback.subprocess, "run", record)
    assert main(["checkpoint", "--cwd", str(folder)]) == 0
    checkpoint_id = capsys.readouterr().out.strip()
    repo = rollback.Repository(str(folder))
    cp = repo.select(checkpoint_id)
    (folder / "file.txt").write_text("after\n", encoding="utf-8")
    assert main(["rollback", cp.id, "--yes", "--cwd", str(folder)]) == 0
    capsys.readouterr()
    assert (folder / "file.txt").read_text(encoding="utf-8") == "before\n"
    safety = repo.select()
    assert safety.kind == "safety"
    assert main(["rollback", safety.id, "--yes", "--cwd", str(folder)]) == 0
    capsys.readouterr()
    assert (folder / "file.txt").read_text(encoding="utf-8") == "after\n"
    event = dict(cwd=str(folder), session_id="no-identity-hook", hook_event_name="SessionStart")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main(["checkpoint", "--hook"]) == 0
    output = capsys.readouterr()
    assert output.out == output.err == ""
    hook = repo.select(run_id="no-identity-hook")
    assert hook.label == "SessionStart"
    for checkpoint in repo.checkpoints():
        assert commit_identity(folder, checkpoint.oid) == ["RunLedger <runledger@localhost>"] * 2
    identity = {f"GIT_{role}_{field}": value
                for role in ("AUTHOR", "COMMITTER")
                for field, value in (("NAME", "RunLedger"), ("EMAIL", "runledger@localhost"))}
    for args, env in calls:
        supplied = {key: env[key] for key in identity if key in env}
        assert supplied == (identity if "commit-tree" in args else {})
    assert repo.env == rollback.Repository(str(folder)).env
    assert os.environ == environment
    assert (folder / ".git" / "config").read_bytes() == config
    assert (folder / ".git" / "index").read_bytes() == index
    assert not list(Path(os.environ["HOME"]).iterdir())
    assert not list(Path(os.environ["XDG_CONFIG_HOME"]).iterdir())
    assert git(folder, "rev-parse", "--verify", "HEAD", check=False) == b""
    assert git(folder, "for-each-ref", "refs/heads", "refs/stash") == b""


@pytest.mark.parametrize("config, env, expected", [
    ({"user.name": "Configured User", "user.email": "user@example.test"}, {},
     ["Configured User <user@example.test>"] * 2),
    ({}, {"GIT_AUTHOR_NAME": "Env Author", "GIT_AUTHOR_EMAIL": "author@example.test",
          "GIT_COMMITTER_NAME": "Env Committer", "GIT_COMMITTER_EMAIL": "committer@example.test"},
     ["Env Author <author@example.test>", "Env Committer <committer@example.test>"]),
    ({"user.name": "Configured User"}, {},
     ["Configured User <runledger@localhost>"] * 2),
    ({"user.email": "user@example.test"}, {},
     ["RunLedger <user@example.test>"] * 2),
    ({}, {"GIT_AUTHOR_NAME": "Env Author", "GIT_AUTHOR_EMAIL": "author@example.test"},
     ["Env Author <author@example.test>", "RunLedger <runledger@localhost>"]),
    ({"author.name": "Config Author", "author.email": "author@example.test",
      "committer.name": "Config Committer", "committer.email": "committer@example.test"}, {},
     ["Config Author <author@example.test>", "Config Committer <committer@example.test>"]),
    ({"user.name": "Configured User", "user.email": "user@example.test"},
     {"GIT_AUTHOR_NAME": "Env Author"},
     ["Env Author <user@example.test>", "Configured User <user@example.test>"]),
])
def test_checkpoint_preserves_configured_and_partial_identity(identity_repo, monkeypatch,
                                                            config, env, expected):
    for number, (key, value) in enumerate(config.items(), 1):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{number}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{number}", value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(config) + 1))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    original_config = (identity_repo / ".git" / "config").read_bytes()
    original_env = dict(os.environ)
    repo = rollback.Repository(str(identity_repo))
    cp = repo.create()
    assert commit_identity(identity_repo, cp.oid) == expected
    assert (identity_repo / ".git" / "config").read_bytes() == original_config
    assert os.environ == original_env


def index_bytes(repo):
    return (repo.root / ".git" / "index").read_bytes()


def normalized_path(path):
    return os.path.normcase(os.path.realpath(os.fspath(path)))


@pytest.fixture(params=["attributes", "tag", "junction-api"])
def junction_probe(request, monkeypatch):
    # Isolate RunLedger's OS mocks: tempfile/shutil must keep the real os.walk.
    local_os = SimpleNamespace(**vars(os))
    local_os.path = SimpleNamespace(**vars(os.path))
    local_os.path.isjunction = None
    monkeypatch.setattr(rollback, "os", local_os)
    monkeypatch.setattr(redirects, "os", local_os)

    def mark(junction):
        target = normalized_path(junction)
        if request.param == "junction-api":
            local_os.path.isjunction = lambda path: normalized_path(path) == target
            return
        real_lstat = Path.lstat

        def lstat(candidate, *args, **kwargs):
            info = real_lstat(candidate, *args, **kwargs)
            if normalized_path(candidate) == target:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_file_attributes=(stat.FILE_ATTRIBUTE_REPARSE_POINT
                                        if request.param == "attributes" else 0),
                    # IO_REPARSE_TAG_MOUNT_POINT; the named constant is Windows-only.
                    st_reparse_tag=(0xA0000003 if request.param == "tag" else 0))
            return info

        monkeypatch.setattr(Path, "lstat", lstat)

    return mark


@pytest.fixture(params=[False, True], ids=["native-cleanup", "windows-312-cleanup"])
def index_cleanup_walk(request, monkeypatch):
    if request.param:
        real_cleanup = rollback.tempfile.TemporaryDirectory.cleanup

        def cleanup(temporary):
            # Windows Python 3.12+ shutil.rmtree also walks temporary index folders.
            list(os.walk(temporary.name, topdown=False))
            return real_cleanup(temporary)

        monkeypatch.setattr(rollback.tempfile.TemporaryDirectory, "cleanup", cleanup)


def test_checkpoint_publication_plumbing_without_supplying_identity(repo, monkeypatch):
    """Verify commit/ref arguments without attempting a commit when identity is absent."""
    real_git = repo._git
    calls = []
    oid = "1" * 40

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "commit-tree":
            return subprocess.CompletedProcess(args, 0, stdout=(oid + "\n").encode(), stderr=b"")
        if args[0] == "update-ref":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(repo, "_git", run)
    cp = repo.create("Before task", "session-test")
    commit = next((args, kwargs) for args, kwargs in calls if args[0] == "commit-tree")
    assert commit[0] == ("commit-tree", cp.tree)  # unborn HEAD, no parent
    meta = json.loads(commit[1]["data"].decode().split("\n\n", 1)[1])
    assert meta["id"] == cp.id and meta["run_id"] == "session-test"
    assert meta["label"] == "Before task" and meta["kind"] == "checkpoint"
    assert calls[-1][0] == ("update-ref", "--no-deref", cp.ref, oid, "")


def test_metadata_selection_and_prune_reject_symbolic_or_foreign_refs(repo, monkeypatch):
    first = tree_checkpoint(repo)
    first.oid = "1" * 40
    second = rollback.Checkpoint("20261015T130000000000Z-89abcdef", "2" * 40, first.tree,
                                 "2026-10-15T13:00:00+00:00", "Prompt", first.run_id)
    safety = rollback.Checkpoint("20261015T140000000000Z-fedcba98", "3" * 40, first.tree,
                                 "2026-10-15T14:00:00+00:00", "Safety", None, "safety")
    listed_at = datetime.fromtimestamp(1760533200, timezone.utc)  # the date the fake prints
    first.committed_at = second.committed_at = safety.committed_at = listed_at
    checkpoints = [first, second, safety]
    real_git = repo._git
    deleted = []

    def record(ref, oid, obj_type, symref, tree, body, date="1760533200 +0000"):
        fields = [ref.encode(), oid.encode(), obj_type.encode(), symref.encode(),
                  tree.encode(), date.encode(), str(len(body)).encode(), body]
        return b"\x00".join(fields) + b"\x1e\n"

    def checkpoint_body(cp):
        meta = dict(version=1, id=cp.id, label=cp.label, created_at=cp.created_at,
                    run_id=cp.run_id, kind=cp.kind)
        return ("RunLedger checkpoint\n\n" + json.dumps(meta)).encode()

    def run(*args, **kwargs):
        if args[0] == "for-each-ref":
            output = b"".join(record(cp.ref, cp.oid, "commit", "", cp.tree, checkpoint_body(cp))
                              for cp in checkpoints)
            output += record(f"{rollback.PREFIX}20261015T150000000000Z-00000000", first.oid,
                             "commit", "refs/heads/main", first.tree, checkpoint_body(first))
            output += record(f"{rollback.PREFIX}unknown", "4" * 40, "commit", "", first.tree, b"")
            output += record(f"{rollback.PREFIX}20261015T150000000000Z-00000001", first.tree,
                             "tree", "", first.tree, b"")
        elif args[0] == "cat-file":
            cp = next(cp for cp in checkpoints if cp.oid == args[-1])
            output = (f"tree {cp.tree}\n\nRunLedger checkpoint\n\n"
                      + json.dumps(dict(version=1, id=cp.id, label=cp.label,
                                        created_at=cp.created_at, run_id=cp.run_id,
                                        kind=cp.kind))).encode()
        elif args[0] == "update-ref":
            deleted.append(args)
            output = b""
        else:
            return real_git(*args, **kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(repo, "_git", run)
    listed = repo.checkpoints()
    assert listed == [safety, second, first]
    assert [cp.committed_at for cp in listed] == [listed_at] * 3
    assert repo.select(run_id=first.run_id) == first
    assert repo.select() == safety
    assert repo.select(second.ref) == second
    assert repo.prune(1) == [second, first]
    assert deleted == [("update-ref", "--no-deref", "-d", cp.ref, cp.oid) for cp in (second, first)]
    with pytest.raises(rollback.CheckpointError, match="No matching"):
        repo.select("HEAD")
    with pytest.raises(rollback.CheckpointError, match="No matching"):
        repo.select(run_id="missing")
    with pytest.raises(rollback.CheckpointError, match="--keep"):
        repo.prune(-1)


def test_snapshot_preserves_split_index_and_leaves_no_shared_index_artifacts(repo):
    git(repo.root, "update-index", "--split-index")
    before = index_bytes(repo)
    shared = {p.name: p.read_bytes() for p in (repo.root / ".git").glob("sharedindex.*")}
    assert shared
    repo.snapshot_tree()
    assert index_bytes(repo) == before
    assert {p.name: p.read_bytes() for p in (repo.root / ".git").glob("sharedindex.*")} == shared


def test_snapshot_includes_unstaged_and_untracked_not_ignored(repo):
    before = index_bytes(repo)
    cp = tree_checkpoint(repo)
    assert set(repo.entries(cp.tree)) == {".gitignore", "tracked.txt", "untracked.txt"}
    assert git(repo.root, "show", f"{cp.tree}:tracked.txt") == b"unstaged\n"
    assert git(repo.root, "show", ":tracked.txt") == b"staged\n"
    assert index_bytes(repo) == before
    assert (repo.root / "untracked.txt").read_bytes() == b"untracked\n"
    assert git(repo.root, "for-each-ref", "--format=%(refname)") == b""
    assert git(repo.root, "rev-parse", "--verify", "HEAD", check=False) == b""


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_snapshot_captures_index_optimisation_flags_without_changing_index(repo, flag):
    git(repo.root, "update-index", flag, "tracked.txt")
    before = index_bytes(repo)
    tree = repo.snapshot_tree()
    assert git(repo.root, "show", f"{tree}:tracked.txt") == b"unstaged\n"
    assert index_bytes(repo) == before


def test_tracked_ignored_files_are_in_snapshot(repo):
    git(repo.root, "add", "-f", ".secret")
    assert ".secret" in repo.entries(repo.snapshot_tree())


def test_restore_new_deleted_modified_and_undo_using_real_git_trees(repo, tree_safety):
    cp = tree_checkpoint(repo)
    before_index = index_bytes(repo)
    (repo.root / "tracked.txt").write_text("new state\n", encoding="utf-8")
    (repo.root / "untracked.txt").unlink()
    (repo.root / "after.txt").write_text("created after snapshot\n", encoding="utf-8")
    before = repo.snapshot_tree()
    plan = repo.plan(cp)
    assert plan.restore == ["tracked.txt", "untracked.txt"]
    assert plan.remove == ["after.txt"]
    safety = repo.rollback(plan)
    assert tree_safety == [safety] and safety.kind == "safety"
    assert safety.tree == before
    assert repo.snapshot_tree() == cp.tree
    assert not (repo.root / "after.txt").exists()
    assert (repo.root / "cache" / "private.txt").read_text(encoding="utf-8") == "do not touch\n"
    assert (repo.root / ".secret").read_text(encoding="utf-8") == "synthetic ignored data\n"
    assert index_bytes(repo) == before_index
    repo.rollback(repo.plan(safety))
    assert repo.snapshot_tree() == before
    assert index_bytes(repo) == before_index


def test_safety_checkpoint_failure_makes_no_worktree_writes(repo, monkeypatch):
    cp = tree_checkpoint(repo)
    (repo.root / "tracked.txt").write_text("keep me\n", encoding="utf-8")
    (repo.root / "new.txt").write_text("keep too\n", encoding="utf-8")
    plan = repo.plan(cp)

    def fail(*args, **kwargs):
        assert (repo.root / "tracked.txt").read_text(encoding="utf-8") == "keep me\n"
        assert (repo.root / "new.txt").exists()
        raise rollback.CheckpointError("Synthetic safety checkpoint failure")

    monkeypatch.setattr(repo, "create", fail)
    with pytest.raises(rollback.CheckpointError, match="safety checkpoint failure"):
        repo.rollback(plan)
    assert (repo.root / "tracked.txt").read_text(encoding="utf-8") == "keep me\n"
    assert (repo.root / "new.txt").exists()


@pytest.mark.parametrize("direction", ["file-to-directory", "directory-to-file"])
def test_file_directory_replacements(repo, tree_safety, direction):
    name = repo.root / "shape"
    if direction == "file-to-directory":
        name.mkdir()
        (name / "child.txt").write_text("child\n", encoding="utf-8")
        cp = tree_checkpoint(repo)
        (name / "child.txt").unlink()
        name.rmdir()
        name.write_text("file\n", encoding="utf-8")
    else:
        name.write_text("file\n", encoding="utf-8")
        cp = tree_checkpoint(repo)
        name.unlink()
        name.mkdir()
        (name / "child.txt").write_text("child\n", encoding="utf-8")
    repo.rollback(repo.plan(cp))
    assert repo.snapshot_tree() == cp.tree


def test_ignored_contents_block_directory_replacement(repo, tree_safety):
    path = repo.root / "shape"
    path.write_text("file\n", encoding="utf-8")
    cp = tree_checkpoint(repo)
    path.unlink()
    path.mkdir()
    (path / "known").write_text("new\n", encoding="utf-8")
    (path / ".secret").write_text("private\n", encoding="utf-8")
    with pytest.raises(rollback.CheckpointError, match="blocks rollback"):
        repo.rollback(repo.plan(cp))
    assert tree_safety == []
    assert (path / ".secret").read_text(encoding="utf-8") == "private\n"
    assert (path / "known").exists()


def test_ignored_junction_is_refused_before_directory_traversal(
        repo, tree_safety, monkeypatch, junction_probe, index_cleanup_walk):
    path = repo.root / "shape"
    path.write_bytes(b"snapshot")
    cp = tree_checkpoint(repo)
    path.unlink()
    path.mkdir()
    junction = path / ".secret"
    junction.mkdir()
    plan = repo.plan(cp)
    real_walk = os.walk
    visited = []
    junction_probe(junction)

    def walk(*args, **kwargs):
        for entry in real_walk(*args, **kwargs):
            visited.append(normalized_path(entry[0]))
            yield entry

    monkeypatch.setattr(rollback.os, "walk", walk)
    with pytest.raises(rollback.CheckpointError, match="reparse point blocks rollback"):
        repo.rollback(plan)
    assert visited == [normalized_path(path)]
    assert tree_safety == []
    assert junction.is_dir()


def test_junction_at_restore_destination_is_not_traversed(
        repo, tree_safety, monkeypatch, junction_probe, index_cleanup_walk):
    path = repo.root / "shape"
    path.write_bytes(b"snapshot")
    cp = tree_checkpoint(repo)
    path.unlink()
    path.mkdir()
    plan = repo.plan(cp)
    junction_probe(path)

    def walk(*args, **kwargs):
        pytest.fail("Rollback must not traverse a junction at a restore destination")

    monkeypatch.setattr(rollback.os, "walk", walk)
    with pytest.raises(rollback.CheckpointError, match="reparse point blocks rollback"):
        repo.rollback(plan)
    assert tree_safety == []
    assert path.is_dir()


def test_junction_parent_blocks_restore(repo, tree_safety, junction_probe):
    parent = repo.root / "shape"
    parent.mkdir()
    child = parent / "child"
    child.write_bytes(b"snapshot")
    cp = tree_checkpoint(repo)
    child.unlink()
    plan = repo.plan(cp)
    junction_probe(parent)
    with pytest.raises(rollback.CheckpointError, match="Unsafe parent blocks rollback"):
        repo.rollback(plan)
    assert tree_safety == []
    assert parent.is_dir()


def test_snapshot_refuses_junction_before_git_add(repo, monkeypatch, junction_probe):
    junction = repo.root / "junction"
    junction.mkdir()
    (junction / "child").write_bytes(b"must not be staged through a junction")
    junction_probe(junction)
    # Exercise the Windows snapshot scan even when running these tests on Linux.
    monkeypatch.setattr(rollback.os, "name", "nt")
    real_git = repo._git
    commands = []

    def run(*args, **kwargs):
        commands.append(args[0])
        return real_git(*args, **kwargs)

    monkeypatch.setattr(repo, "_git", run)
    with pytest.raises(rollback.CheckpointError, match="reparse points"):
        repo.snapshot_tree()
    assert "add" not in commands


def test_reparse_detection_preserves_git_symlinks(repo, junction_probe):
    # Synthetic metadata avoids requiring Windows symlink privileges.
    link = repo.root / "link"
    junction_probe(link)
    info = SimpleNamespace(st_mode=stat.S_IFLNK,
                           st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                           st_reparse_tag=0xA000000C)  # IO_REPARSE_TAG_SYMLINK
    assert not rollback._unsupported_reparse_point(link, info)


def test_ignored_untracked_file_blocks_overwrite(repo, tree_safety):
    path = repo.root / "later"
    path.write_text("before\n", encoding="utf-8")
    cp = tree_checkpoint(repo)
    path.write_text("ignored new data\n", encoding="utf-8")
    with (repo.root / ".gitignore").open("a", encoding="utf-8") as out:
        out.write("later\n")
    with pytest.raises(rollback.CheckpointError, match="blocks rollback"):
        repo.rollback(repo.plan(cp))
    assert tree_safety == []
    assert path.read_text(encoding="utf-8") == "ignored new data\n"


def test_new_tracked_but_now_ignored_file_is_not_removed(repo, tree_safety):
    cp = tree_checkpoint(repo)
    path = repo.root / ".secret"
    git(repo.root, "add", "-f", ".secret")
    plan = repo.plan(cp)
    assert plan.keep_ignored == [".secret"] and plan.remove == []
    repo.rollback(plan)
    assert path.read_text(encoding="utf-8") == "synthetic ignored data\n"


def test_stale_preview_is_refused_before_safety_checkpoint(repo, tree_safety):
    plan = repo.plan(tree_checkpoint(repo))
    (repo.root / "tracked.txt").write_text("changed after preview\n", encoding="utf-8")
    with pytest.raises(rollback.CheckpointError, match="changed since"):
        repo.rollback(plan)
    assert tree_safety == []


@pytest.mark.parametrize("name", ["with spaces ü.bin", "[literal].bin", "-option.bin"])
def test_binary_and_literal_filenames_restore(repo, tree_safety, name):
    path = repo.root / name
    path.write_bytes(b"\0\xfforiginal\r\n")
    cp = tree_checkpoint(repo)
    path.write_bytes(b"\0\xfenew")
    repo.rollback(repo.plan(cp))
    assert path.read_bytes() == b"\0\xfforiginal\r\n"


def test_partial_restore_error_exposes_safety_and_undo(repo, tree_safety, monkeypatch):
    cp = tree_checkpoint(repo)
    (repo.root / "tracked.txt").write_bytes(b"changed\n")
    before = repo.snapshot_tree()
    real_git = repo._git

    def fail_checkout(*args, **kwargs):
        if args[0] == "checkout-index":
            raise rollback.CheckpointError("Synthetic checkout failure")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(repo, "_git", fail_checkout)
    with pytest.raises(rollback.CheckpointError, match="Undo: runledger rollback") as exc:
        repo.rollback(repo.plan(cp))
    assert tree_safety[0].tree == before
    assert tree_safety[0].id in str(exc.value)
    monkeypatch.setattr(repo, "_git", real_git)
    repo.rollback(repo.plan(tree_safety[0]))
    assert repo.snapshot_tree() == before


def test_reparse_point_scan_does_not_follow_symlinks(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"outside")
    path = repo.root / "link"
    try:
        path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with repo._index() as index:
        repo._check_reparse_points(index)


def test_symlink_parent_is_removed_without_touching_destination(repo, tree_safety, tmp_path):
    path = repo.root / "folder"
    path.mkdir()
    (path / "child").write_text("snapshot\n", encoding="utf-8")
    cp = tree_checkpoint(repo)
    (path / "child").unlink()
    path.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "child").write_text("outside unchanged\n", encoding="utf-8")
    try:
        path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    repo.rollback(repo.plan(cp))
    assert not path.is_symlink()
    assert (path / "child").read_text(encoding="utf-8") == "snapshot\n"
    assert (outside / "child").read_text(encoding="utf-8") == "outside unchanged\n"


def test_ignored_symlink_parent_is_never_followed(repo, tree_safety, tmp_path):
    path = repo.root / "folder"
    path.mkdir()
    (path / "child").write_text("snapshot\n", encoding="utf-8")
    cp = tree_checkpoint(repo)
    (path / "child").unlink()
    path.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "child").write_text("outside\n", encoding="utf-8")
    try:
        path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with (repo.root / ".gitignore").open("a", encoding="utf-8") as out:
        out.write("folder\n")
    with pytest.raises(rollback.CheckpointError, match="parent blocks rollback"):
        repo.rollback(repo.plan(cp))
    assert tree_safety == [] and path.is_symlink()
    assert (outside / "child").read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize("path", [".git/config", ".GiT/HEAD", "../outside", "/absolute",
                                  "a/../b", "a//b", r"a\..\b", ".git./config", "git~1/config"])
def test_unsafe_paths_refused(repo, path):
    with pytest.raises(rollback.CheckpointError, match="unsafe path"):
        repo._path(path)


@pytest.mark.parametrize("path", [
    "C:/outside", "file:stream", "CON.txt", "dir/NUL", "com1.log", "trailing.",
    "trailing ", "bad?.txt", "bad*.txt", "bad|name", "bad<name", "line\nbreak",
])
def test_windows_unsupported_paths_refused_on_any_platform(repo, monkeypatch, path):
    monkeypatch.setattr(rollback, "os", SimpleNamespace(name="nt"))
    with pytest.raises(rollback.CheckpointError, match="unsupported on Windows"):
        repo._path(path)


def test_submodule_tree_refused(repo):
    # A gitlink can refer to a missing object; no submodule checkout or network needed.
    git(repo.root, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},nested")
    tree = git(repo.root, "write-tree").decode().strip()
    with pytest.raises(rollback.CheckpointError, match="Submodules"):
        repo.entries(tree)


def test_cli_dry_run_and_non_tty_refusal(repo, monkeypatch, capsys):
    cp = tree_checkpoint(repo)
    (repo.root / "tracked.txt").write_text("new\n", encoding="utf-8")
    (repo.root / "after.txt").write_text("after\n", encoding="utf-8")
    monkeypatch.setattr(rollback.Repository, "select", lambda self, *a, **kw: cp)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    before = index_bytes(repo)
    assert main(["rollback", cp.id, "--dry-run", "--cwd", str(repo.root)]) == 0
    output = capsys.readouterr().out
    assert "restore 'tracked.txt'" in output and "remove  'after.txt'" in output
    assert "Dry run" in output
    assert main(["rollback", "--last", "--cwd", str(repo.root)]) == 1
    assert "requires confirmation" in capsys.readouterr().err
    assert (repo.root / "tracked.txt").read_text(encoding="utf-8") == "new\n"
    assert (repo.root / "after.txt").exists()
    assert index_bytes(repo) == before
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == b""


def test_cli_confirmation_no_and_yes(repo, monkeypatch, capsys):
    cp = tree_checkpoint(repo)
    (repo.root / "tracked.txt").write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(rollback.Repository, "select", lambda self, *a, **kw: cp)
    monkeypatch.setattr("sys.stdin", type("Terminal", (io.StringIO,), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["rollback", "--cwd", str(repo.root)]) == 1
    assert "Cancelled" in capsys.readouterr().out
    called = []
    monkeypatch.setattr(rollback.Repository, "rollback", lambda self, plan: called.append(plan) or cp)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert main(["rollback", "--cwd", str(repo.root)]) == 0
    assert len(called) == 1
    assert "Undo: runledger rollback" in capsys.readouterr().out


def test_listing_counts_differing_files_that_must_remain_ignored(repo, monkeypatch, capsys):
    cp = tree_checkpoint(repo)
    git(repo.root, "add", "-f", ".secret")
    monkeypatch.setattr(rollback.Repository, "checkpoints", lambda self: [cp])
    assert main(["checkpoints", "--cwd", str(repo.root)]) == 0
    output = capsys.readouterr().out
    assert "(1 file(s) differ)" in output
    assert "keep ignored '.secret'" in output


def test_actual_cli_yes_restore_with_safety_and_undo(committed_repo, capsys):
    repo = committed_repo
    head = git(repo.root, "rev-parse", "HEAD")
    branch = git(repo.root, "symbolic-ref", "HEAD")
    index = index_bytes(repo)
    refs = git(repo.root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/stash")
    assert main(["checkpoint", "--cwd", str(repo.root), "--label", "Before run", "--run", "run-1"]) == 0
    checkpoint_id = capsys.readouterr().out.strip()
    cp = repo.select(checkpoint_id)
    assert cp.label == "Before run" and cp.run_id == "run-1"
    (repo.root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo.root / "new.txt").write_text("new\n", encoding="utf-8")
    before = repo.snapshot_tree()
    assert main(["rollback", "--run", "run-1", "--yes", "--cwd", str(repo.root)]) == 0
    assert "Undo: runledger rollback" in capsys.readouterr().out
    assert repo.snapshot_tree() == cp.tree
    safety = repo.select()
    assert safety.kind == "safety" and safety.tree == before
    assert main(["rollback", safety.id, "--yes", "--cwd", str(repo.root)]) == 0
    assert repo.snapshot_tree() == before
    assert git(repo.root, "rev-parse", "HEAD") == head
    assert git(repo.root, "symbolic-ref", "HEAD") == branch
    assert index_bytes(repo) == index
    assert git(repo.root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/stash") == refs


def test_actual_checkpoint_creation_does_not_change_worktree_or_index(committed_repo):
    repo = committed_repo
    before = index_bytes(repo)
    tree = repo.snapshot_tree()
    cp = repo.create("label with\nnewline", "session-1")
    assert cp.tree == tree
    assert repo.select(cp.id) == cp
    assert cp.id in cp.ref
    assert repo.snapshot_tree() == tree
    assert index_bytes(repo) == before


def test_run_selection_is_earliest_and_last_includes_safety(committed_repo):
    repo = committed_repo
    first = repo.create("Start", "session-1")
    repo.create("Next prompt", "session-1")
    last = repo.create(kind="safety")
    assert repo.select(run_id="session-1") == first
    assert repo.select() == last
    with pytest.raises(rollback.CheckpointError, match="No matching"):
        repo.select(run_id="missing")
    with pytest.raises(rollback.CheckpointError, match="No matching"):
        repo.select("HEAD")


def test_checkpoints_listing_and_pruning_only_owned_refs(committed_repo, capsys):
    repo = committed_repo
    first = repo.create("First", "run-1")
    last = repo.create("Last", "run-2")
    git(repo.root, "update-ref", "refs/runledger/other", first.oid)
    (repo.root / "new.txt").write_text("after\n", encoding="utf-8")
    assert main(["checkpoints", "--cwd", str(repo.root)]) == 0
    out = capsys.readouterr().out
    assert "First" in out and "Last" in out and "new.txt" in out and "file(s) differ" in out
    assert main(["checkpoints", "prune", "--keep", "1", "--cwd", str(repo.root)]) == 0
    assert repo.checkpoints() == [last]
    assert git(repo.root, "rev-parse", "refs/runledger/other").decode().strip() == first.oid
    assert main(["checkpoints", "--cwd", str(repo.root), "prune", "--keep", "0"]) == 0
    assert repo.checkpoints() == []
    with pytest.raises(SystemExit) as exc:
        main(["checkpoints", "prune", "--cwd", str(repo.root), "--keep", "-1"])
    assert exc.value.code == 2
    assert "--keep" in capsys.readouterr().err


def test_checkpoint_cli_environment_association(repo, monkeypatch, capsys):
    calls = []

    def create(self, label="", run_id=None, **kwargs):
        calls.append((label, run_id))
        return tree_checkpoint(self)

    monkeypatch.setattr(rollback.Repository, "create", create)
    monkeypatch.setenv("RUNLEDGER_RUN_ID", "env-run")
    assert main(["checkpoint", "--cwd", str(repo.root), "--label", "Label"]) == 0
    assert calls == [("Label", "env-run")]
    assert rollback.ID_RE.fullmatch(capsys.readouterr().out.strip())
    assert main(["checkpoint", "--cwd", str(repo.root), "--run", "explicit"]) == 0
    assert calls[-1][1] == "explicit"


@pytest.mark.parametrize("command", [
    ["checkpoint"], ["checkpoints"], ["rollback", "--last", "--dry-run"],
    ["checkpoints", "prune", "--keep", "1"],
])
def test_non_git_directory_friendly_error(tmp_path, command, capsys):
    assert main(command + ["--cwd", str(tmp_path)]) == 1
    assert "Not a Git working tree" in capsys.readouterr().err


def test_missing_git_is_friendly(repo, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "")
    assert main(["checkpoint", "--cwd", str(repo.root)]) == 1
    assert "Git is not installed" in capsys.readouterr().err


def test_inherited_git_overrides_cannot_redirect_cwd(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong-index"))
    other = rollback.Repository(str(repo.root))
    assert other.root == repo.root and other.snapshot_tree() == repo.snapshot_tree()
    assert not (tmp_path / "wrong-index").exists()


def test_hook_uses_event_session_without_storing_prompt(repo, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(rollback.Repository, "create",
                        lambda self, **kwargs: calls.append((self.root, kwargs)))
    event = dict(cwd=str(repo.root), session_id="hook-session", hook_event_name="UserPromptSubmit",
                 prompt="Do not store this user content")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main(["checkpoint", "--hook"]) == 0
    assert calls == [(repo.root, {"label": "UserPromptSubmit", "run_id": "hook-session"})]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("event", [
    {}, [], "{bad", {"hook_event_name": "PreToolUse", "cwd": "/unused", "session_id": "s"},
    {"hook_event_name": "SessionStart", "cwd": [], "session_id": "s"},
])
def test_invalid_hook_input_is_nonblocking(event, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(event if isinstance(event, str) else json.dumps(event)))
    assert main(["checkpoint", "--hook"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_outside_repo_is_silent(tmp_path, monkeypatch, capsys):
    for event_name in rollback.HOOK_EVENTS:
        event = dict(cwd=str(tmp_path), session_id="s", hook_event_name=event_name)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
        assert main(["checkpoint", "--hook"]) == 0
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""


def test_non_repository_is_distinguished_from_git_failure(repo, tmp_path):
    with pytest.raises(rollback.NotAGitRepository):
        rollback.Repository(str(tmp_path))
    config = repo.root / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + "malformed line here\n", encoding="utf-8")
    with pytest.raises(rollback.CheckpointError) as failure:
        rollback.Repository(str(repo.root))
    assert not isinstance(failure.value, rollback.NotAGitRepository)


def test_hook_warns_when_repository_discovery_fails(repo, monkeypatch, capsys):
    config = repo.root / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + "malformed line here\n", encoding="utf-8")
    event = dict(cwd=str(repo.root), session_id="s", hook_event_name="SessionStart")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main(["checkpoint", "--hook"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "checkpoint unavailable" in out.err
    assert main(["checkpoint", "--cwd", str(repo.root)]) == 1
    err = capsys.readouterr().err
    assert "bad config" in err and "Not a Git working tree" not in err


def test_hook_failure_is_nonblocking(repo, monkeypatch, capsys):
    (repo.root / ".git" / "refs" / "runledger").write_text("blocker\n", encoding="utf-8")
    event = dict(cwd=str(repo.root), session_id="s", hook_event_name="SessionStart")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main(["checkpoint", "--hook"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "checkpoint unavailable" in out.err


def forge_checkpoint_with_separator(repo, checkpoint_id, tree, tail=b"\x1e" + b"junk\n"):
    """Publish a ref whose commit body is a valid record followed by a raw record separator."""
    metadata = dict(version=1, id=checkpoint_id, created_at="2026-10-15T15:00:00+00:00",
                    label="forged", run_id="run-forged", kind="checkpoint")
    message = ("RunLedger checkpoint\n\n" + json.dumps(metadata) + "\n").encode() + tail
    oid = repo._git("commit-tree", tree, data=message,
                    extra_env=repo._checkpoint_identity()).stdout.decode("ascii").strip()
    git(repo.root, "update-ref", rollback.PREFIX + checkpoint_id, oid)
    return oid


def test_checkpoints_listing_spawn_count_constant(repo, tmp_path, monkeypatch):
    created = [repo.create(label=f"cp-{i}", run_id=f"run-{i}") for i in range(15)]

    # Inject a symbolic ref, a non-commit ref, a malformed ref name, a foreign commit, and a malformed commit body under PREFIX.
    git(repo.root, "symbolic-ref", f"{rollback.PREFIX}20261015T150000000000Z-00000000", "refs/heads/main")
    git(repo.root, "update-ref", f"{rollback.PREFIX}not-a-valid-id", created[0].oid)
    git(repo.root, "update-ref", f"{rollback.PREFIX}20261015T150000000000Z-00000001", created[0].tree)
    foreign_oid = repo._git(
        "commit-tree", created[0].tree, data=b"Foreign commit\n", extra_env=repo._checkpoint_identity()
    ).stdout.decode("ascii").strip()
    git(repo.root, "update-ref", f"{rollback.PREFIX}20261015T150000000000Z-00000002", foreign_oid)
    malformed_oid = repo._git(
        "commit-tree", created[0].tree, data=b"RunLedger checkpoint\n\n{not-json}\n",
        extra_env=repo._checkpoint_identity()
    ).stdout.decode("ascii").strip()
    git(repo.root, "update-ref", f"{rollback.PREFIX}20261015T150000000000Z-00000003", malformed_oid)
    forge_checkpoint_with_separator(repo, "20261015T150000000000Z-00000004", created[0].tree)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    single = tmp_path / "single"
    single.mkdir()
    git(single, "init", "-q")
    git(single, "config", "core.autocrlf", "false")
    (single / "file.txt").write_text("only\n", encoding="utf-8")
    git(single, "add", ".")
    many = rollback.Repository(str(repo.root))
    one = rollback.Repository(str(single))
    one.create(label="only", run_id="run-only")

    calls.clear()
    listed = many.checkpoints()
    assert [cp.id for cp in listed] == [cp.id for cp in reversed(created)]
    assert len(calls) <= 3
    many_refs = len(calls)

    calls.clear()
    assert [cp.id for cp in one.checkpoints()] != []
    assert len(calls) == many_refs


def test_records_containing_the_record_separator_are_rejected(repo, capsys):
    genuine = repo.create(label="genuine", run_id="run-1")
    early = "20261015T150000000000Z-0badc0de"
    late = "20261231T235959000000Z-0badc0de"
    forge_checkpoint_with_separator(repo, early, genuine.tree)
    forge_checkpoint_with_separator(repo, late, genuine.tree)

    assert [cp.id for cp in repo.checkpoints()] == [genuine.id]
    assert main(["checkpoints", "--cwd", str(repo.root)]) == 0
    printed = capsys.readouterr().out
    assert genuine.id in printed
    assert early not in printed and late not in printed
    assert main(["rollback", early, "--dry-run", "--cwd", str(repo.root)]) == 1
    assert "No matching checkpoint" in capsys.readouterr().err


def test_listing_falls_back_to_per_ref_reads_when_git_rejects_the_format(repo, monkeypatch):
    created = repo.create(label="fallback", run_id="run-fallback")
    real_git = repo._git

    def run(*args, **kwargs):
        if args[0] == "for-each-ref" and any("%(contents:size)" in arg for arg in args):
            return subprocess.CompletedProcess(args, 128, stdout=b"",
                                               stderr=b"fatal: unknown field name: contents:size\n")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(repo, "_git", run)
    assert [cp.id for cp in repo.checkpoints()] == [created.id]



def test_install_uninstall_preserves_other_lifecycle_hooks(tmp_path):
    path = tmp_path / "settings.json"
    original = {"env": {"SAFE": "1"}, "hooks": {
        "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo ready"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo check"}]}],
    }}
    path.write_text(json.dumps(original), encoding="utf-8")
    guard.install_hook(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for event in rollback.HOOK_EVENTS:
        assert data["hooks"][event][0] == original["hooks"][event][0]
        assert data["hooks"][event][1] == {"hooks": [
            {"type": "command", "command": f'"{sys.executable}" -m runledger checkpoint --hook', "timeout": 120}]}
    assert guard.install_hook(path) == (False, None)
    guard.uninstall_hook(path)
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_install_covers_all_sessions_when_existing_checkpoint_hook_is_restricted(tmp_path):
    path = tmp_path / "settings.json"
    restricted = {"matcher": "resume", "hooks": [
        {"type": "command", "command": rollback.HOOK_COMMAND, "timeout": 120}]}
    path.write_text(json.dumps({"hooks": {"SessionStart": [restricted]}}), encoding="utf-8")
    assert guard.install_hook(path)[0]
    groups = json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert groups[0] == restricted
    assert any(group.get("matcher") in (None, "", "*") and
               any(rollback.is_checkpoint_hook(hook) for hook in group["hooks"])
               for group in groups)
    assert guard.install_hook(path) == (False, None)


@pytest.mark.parametrize("command", [
    "runledger checkpoint --hook",
    '"/a folder/runledger" checkpoint --hook',
    r'"C:\Program Files\RunLedger\runledger.exe" checkpoint --hook',
    "'/a folder/runledger' checkpoint --hook",
    "python -m runledger checkpoint --hook",
    '"/a folder/python3.9" -m runledger checkpoint --hook',
    r'"C:\Program Files\Python 3\python.exe" -m runledger checkpoint --hook',
])
def test_checkpoint_hook_recognises_exact_commands(command):
    assert rollback.is_checkpoint_hook({"type": "command", "command": command})


@pytest.mark.parametrize("command", [
    r'"C:\Python313\python3.13t.exe" -m runledger checkpoint --hook',
    '"/opt/py313t/bin/python3.13t" -m runledger checkpoint --hook',
    '"pythonw.exe" -m runledger checkpoint --hook',
])
def test_checkpoint_hook_recognises_free_threaded_interpreters(command):
    """CORE-SCR-R2-007: the installer writes the literal sys.executable, so the
    checkpoint recognizer must accept the same suffixed interpreter names as guard."""
    assert rollback.is_checkpoint_hook({"type": "command", "command": command})


@pytest.mark.parametrize("command", [
    "echo runledger checkpoint --hook",
    "echo python -m runledger checkpoint --hook",
    "runledger checkpoint --hook && echo keep",
    "runledger checkpoint --hook; echo keep",
    "runledger checkpoint --hook | another-command",
    "not-runledger checkpoint --hook",
    '"runledger checkpoint --hook"',
])
def test_unrelated_checkpoint_commands_are_not_removed(tmp_path, command):
    path = tmp_path / "settings.json"
    original = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": command}]}]}}
    path.write_text(json.dumps(original), encoding="utf-8")
    assert not rollback.is_checkpoint_hook(original["hooks"]["SessionStart"][0]["hooks"][0])
    guard.install_hook(path)
    guard.uninstall_hook(path)
    assert json.loads(path.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("command", [
    "true;/usr/bin/python3.13t -m runledger checkpoint --hook",
    "true&&/usr/bin/python3.13t -m runledger checkpoint --hook",
    "true||/usr/bin/python3.13t -m runledger checkpoint --hook",
    "true|/usr/bin/python3.13t -m runledger checkpoint --hook",
    "true&/usr/bin/python3.13t -m runledger checkpoint --hook",
    '"/usr/bin/python3.13t" -m runledger checkpoint --hook > hook.log',
    "(/usr/bin/python3.13t -m runledger checkpoint --hook)",
    "/usr/bin/python3.13t -m runledger checkpoint --hook\nrm -rf ./build",
    "/usr/bin/python3.13t -m runledger checkpoint --hook;echo hi",
    "true;runledger checkpoint --hook",
])
def test_checkpoint_hook_rejects_attached_shell_operators(command):
    """CORE-SCR-R3-004: `shlex.split(posix=False)` leaves `true;/usr/bin/python3.13t` in one
    word, so the operator has to be rejected before any basename handling."""
    assert rollback.is_checkpoint_hook({"type": "command", "command": command}) is False


@pytest.mark.parametrize("command", [
    '"/usr/$(printf bin)/python3.13t" -m runledger checkpoint --hook',
    '"/usr/`printf bin`/python3.13t" -m runledger checkpoint --hook',
    '"$PYTHON3" -m runledger checkpoint --hook',
    r'"/usr/\$python/python3.13t" -m runledger checkpoint --hook',
])
def test_checkpoint_hook_rejects_executable_substitutions(command):
    """CORE-SCR-R4-004: an executable word with a `$` or a backtick computes its own path even
    inside double quotes, so it is a foreign entry and stays untouched. A backslash-escaped `$`
    counts as an operator too, the conservative reading of the same rule."""
    assert rollback.is_checkpoint_hook({"type": "command", "command": command}) is False


def test_install_preserves_a_substitution_bearing_checkpoint_hook(tmp_path):
    """The round-4 reproducer for the checkpoint hook: install appends beside the foreign entry
    and uninstall leaves it exactly as it was."""
    path = tmp_path / "settings.json"
    original = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command",
         "command": '"/usr/$(printf bin)/python3.13t" -m runledger checkpoint --hook'}]}]}}
    path.write_text(json.dumps(original), encoding="utf-8")

    assert guard.install_hook(path)[0] is True
    groups = json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert groups[0] == original["hooks"]["SessionStart"][0]
    assert len(groups) == 2

    assert guard.uninstall_hook(path)[0] is True
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_install_preserves_a_compound_checkpoint_hook(tmp_path):
    """The round-3 reproducer: v0.6.0 preserved this foreign entry, so install must append
    beside it and uninstall must leave it (VAL-CORE-036/037)."""
    path = tmp_path / "settings.json"
    original = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command",
         "command": "true;/usr/bin/python3.13t -m runledger checkpoint --hook"}]}]}}
    path.write_text(json.dumps(original), encoding="utf-8")

    assert guard.install_hook(path)[0] is True
    groups = json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert groups[0] == original["hooks"]["SessionStart"][0]
    assert len(groups) == 2

    assert guard.uninstall_hook(path)[0] is True
    assert json.loads(path.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("executable, command, owned", [
    (r"D:\tools\bin\agent-python.exe",
     r'"D:\tools\bin\agent-python.exe" -m runledger checkpoint --hook', True),
    (r"D:\Tools\Agent Python\agent-python.exe",
     r'"d:\tools\agent python\AGENT-PYTHON.EXE" -m runledger checkpoint --hook', True),
    ("/opt/agents/bin/agent-python", '"/opt/agents/bin/agent-python" -m runledger checkpoint --hook', True),
    (r"D:\Tools & Runtimes\agent-python.exe",
     r'"D:\Tools & Runtimes\agent-python.exe" -m runledger checkpoint --hook', True),
    ("/opt/agents/bin/agent-python", '"/opt/Agents/bin/agent-python" -m runledger checkpoint --hook', False),
    (r"D:\tools\bin\agent-python.exe",
     r'"D:\other\bin\agent-python.exe" -m runledger checkpoint --hook', False),
    (r"D:\tools\bin\agent-python.exe", r'"D:\tools\bin\agent-python.exe" -m runledger guard', False),
])
def test_checkpoint_hook_follows_the_current_interpreter(monkeypatch, executable, command, owned):
    """CORE-SCR-R3-003: `checkpoint_hook_command` also stores the literal `sys.executable`."""
    monkeypatch.setattr(sys, "executable", executable)
    assert rollback.is_checkpoint_hook({"type": "command", "command": command}) is owned


def test_checkpoint_hook_round_trips_with_a_renamed_interpreter(tmp_path, monkeypatch):
    """A renamed interpreter is a complete lifecycle: install, a no-op second install and
    uninstall, exactly as for `python3.13t.exe`."""
    renamed = r"D:\rl-ready\bin\agent-python.exe"
    monkeypatch.setattr(sys, "executable", renamed)
    path = tmp_path / "settings.json"

    assert guard.install_hook(path)[0] is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == \
        fr'"{renamed}" -m runledger checkpoint --hook'
    assert guard.install_hook(path) == (False, None)
    assert guard.uninstall_hook(path)[0] is True
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_install_invalid_lifecycle_list_leaves_file_untouched(tmp_path):
    path = tmp_path / "settings.json"
    original = json.dumps({"hooks": {"SessionStart": {}}})
    path.write_text(original, encoding="utf-8")
    with pytest.raises(guard.InstallError, match="SessionStart"):
        guard.install_hook(path)
    assert path.read_text(encoding="utf-8") == original
    assert not path.with_name("settings.json.bak").exists()


def test_uninstall_checkpoint_only_hooks(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "runledger checkpoint --hook"}]}]}}), encoding="utf-8")
    assert guard.uninstall_hook(path)[0]
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_receipt_lookup_and_formats(repo, monkeypatch):
    cp = tree_checkpoint(repo)
    monkeypatch.setattr(rollback.Repository, "select", lambda self, **kwargs: cp)
    assert rollback.checkpoint_for_run(str(repo.root), "session-test") == cp.id
    assert rollback.checkpoint_for_run(None, "session-test") is None
    fixture = Path(__file__).parent / "fixtures" / "droid_session.jsonl"
    monkeypatch.setattr(rollback, "checkpoint_for_run", lambda cwd, run_id: cp.id)
    run, score, level, risks, _ = build(str(fixture))
    expected = f"runledger rollback {cp.id}"
    assert expected in render(run, score, level, risks, "html")
    assert expected in render(run, score, level, risks, "md")
    payload = json.loads(render(run, score, level, risks, "json"))
    assert payload["rollback"] == {"checkpoint_id": cp.id, "command": expected}
    run.checkpoint_id = "malicious; shell-command"
    assert json.loads(render(run, score, level, risks, "json"))["rollback"] is None
    assert "malicious" not in render(run, score, level, risks, "html")
    run.checkpoint_id = None
    assert "Rollback:" not in render(run, score, level, risks, "md")


def test_actual_receipt_resolves_session_checkpoint(committed_repo, tmp_path):
    repo = committed_repo
    cp = repo.create(run_id="receipt-session")
    session = tmp_path / "receipt-session.jsonl"
    session.write_text(json.dumps({"type": "user", "sessionId": "receipt-session",
                                  "cwd": str(repo.root), "message": {"role": "user", "content": "Synthetic task"}})
                       + "\n", encoding="utf-8")
    run, score, level, risks, _ = build(str(session))
    assert run.checkpoint_id == cp.id
    assert json.loads(render(run, score, level, risks, "json"))["rollback"]["checkpoint_id"] == cp.id


# ---------------------------------------------------------------- user settings (rollback.*)

def test_settings_defaults_when_the_user_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(tmp_path / "missing.json"))
    settings = rollback.load_settings()
    assert settings.auto_prune is True
    assert (settings.auto_prune_keep, settings.auto_prune_older_than) == (50, None)
    assert (settings.warn_mb, settings.warn_file_mb) == (500.0, 50.0)


def test_settings_read_the_documented_user_keys():
    settings = rollback.load_settings({"rollback": {
        "auto_prune": {"keep": 3, "older_than": "30d"}, "warn_mb": 12.5, "warn_file_mb": 1}})
    assert settings.auto_prune is True
    assert settings.auto_prune_keep == 3
    assert settings.auto_prune_older_than == timedelta(days=30)
    assert (settings.warn_mb, settings.warn_file_mb) == (12.5, 1.0)


def test_auto_prune_can_be_switched_off():
    assert rollback.load_settings({"rollback": {"auto_prune": False}}).auto_prune is False
    assert rollback.load_settings({"rollback": {"auto_prune": True}}).auto_prune is True


@pytest.mark.parametrize("value", ["yes", 0, 1, None, [], {"keep": "many"}, {"keep": 0},
                                   {"older_than": "soon"}, {"keep": 2.5}])
def test_unusable_rollback_values_keep_the_documented_defaults(value):
    """A hook deletes refs from this config, so anything that is not exactly the documented
    shape (or `false`) keeps the default instead of guessing."""
    settings = rollback.load_settings({"rollback": {"auto_prune": value, "warn_mb": "big",
                                                    "warn_file_mb": -1}})
    assert settings.auto_prune is True
    assert (settings.auto_prune_keep, settings.auto_prune_older_than) == (50, None)
    assert (settings.warn_mb, settings.warn_file_mb) == (500.0, 50.0)


def test_a_zero_size_threshold_switches_the_warning_off():
    settings = rollback.load_settings({"rollback": {"warn_mb": 0, "warn_file_mb": 0}})
    assert (settings.warn_mb, settings.warn_file_mb) == (0.0, 0.0)


def test_rollback_settings_are_read_from_the_user_file_only(tmp_path, monkeypatch):
    """A project `.runledger.json` must not be able to trigger deletions."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"rollback": {"auto_prune": False}}), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(config))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".runledger.json").write_text(
        json.dumps({"rollback": {"auto_prune": {"keep": 1}}}), encoding="utf-8")
    monkeypatch.chdir(project)
    assert rollback.load_settings().auto_prune is False


@pytest.mark.parametrize("text,expected", [
    ("30d", timedelta(days=30)),
    ("2w", timedelta(weeks=2)),
    ("6h", timedelta(hours=6)),
    ("1D", timedelta(days=1)),
    ("0d", timedelta(0)),
])
def test_parse_duration_accepts_days_weeks_and_hours(text, expected):
    assert rollback.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "30", "d30", "-1d", "1.5d", "30dd", "1 day", None, 30])
def test_parse_duration_rejects_anything_else(text):
    assert rollback.parse_duration(text) is None

# ---------------------------------------------------------------- selective restore (rollback --path)

@pytest.fixture
def scoped_repo(repo):
    """A repository whose checkpoints publish real refs with no Git identity configured:
    `Repository.create` fills the documented fallback only for its own `commit-tree`."""
    return repo


def _write(root, name, text):
    path = root.joinpath(*name.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _volume_folds_case(folder):
    """Whether this filesystem folds case for `folder`: Windows and macOS do, a Linux CI
    volume does not, so a spelling test must ask the volume instead of the platform."""
    probe = folder / "RunLedgerCaseProbe.txt"
    probe.write_text("probe\n", encoding="utf-8")
    try:
        return (folder / "runledgercaseprobe.TXT").exists()
    finally:
        probe.unlink()


def _tree_from(repo, entries):
    """A checkpoint tree built by hand, for names no working tree can hold on every platform
    (reserved names, `.git` overlap, case collisions). `mktree` writes them, unlike the index."""
    roots = {}
    for name, content in entries:
        blob = git(repo.root, "hash-object", "-w", "--stdin", data=content).decode().strip()
        level = roots
        parts = name.split("/")
        for part in parts[:-1]:
            level = level.setdefault(part, {})
        level[parts[-1]] = blob
    return _write_tree(repo, roots)


def _write_tree(repo, level):
    lines = []
    for name, value in sorted(level.items()):
        if isinstance(value, dict):
            lines.append(f"040000 tree {_write_tree(repo, value)}\t{name}")
        else:
            lines.append(f"100644 blob {value}\t{name}")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    return git(repo.root, "mktree", "--missing", data=data).decode().strip()


def test_path_plan_keeps_the_whole_tree_without_selection(repo):
    cp = tree_checkpoint(repo)
    (repo.root / "tracked.txt").write_text("new\n", encoding="utf-8")
    plan = repo.plan(cp)
    assert plan.paths == ()
    assert plan.restore == ["tracked.txt"]
    assert plan.remove == []


def test_path_restores_only_the_selected_file(scoped_repo, monkeypatch, capsys):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "b.txt", "one\n")
    _write(repo.root, "sub/c.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "b.txt", "modified\n")
    _write(repo.root, "sub/c.txt", "modified\n")
    (repo.root / "new.txt").write_text("untracked\n", encoding="utf-8")
    monkeypatch.chdir(repo.root)
    assert main(["rollback", checkpoint.id, "--path", "a.txt", "--yes"]) == 0
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "b.txt").read_text(encoding="utf-8") == "modified\n"
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "modified\n"
    assert (repo.root / "new.txt").read_text(encoding="utf-8") == "untracked\n"


@pytest.mark.parametrize("scope", ["sub", "sub/", "./sub"]
                         + (["sub\\"] if os.name == "nt" else []))
def test_path_restores_a_directory_subtree(scoped_repo, capsys, scope):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "sub/c.txt", "one\n")
    _write(repo.root, "sub/d/e.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "sub/c.txt", "modified\n")
    _write(repo.root, "sub/scratch.txt", "after\n")
    assert main(["rollback", checkpoint.id, "--path", scope, "--yes",
                 "--cwd", str(repo.root)]) == 0
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "sub" / "d" / "e.txt").read_text(encoding="utf-8") == "one\n"
    assert not (repo.root / "sub" / "scratch.txt").exists()
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "modified\n"


def test_path_multiple_selections_restore_exactly_their_scope(scoped_repo):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "b.txt", "one\n")
    _write(repo.root, "sub/c.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "b.txt", "modified\n")
    _write(repo.root, "sub/c.txt", "modified\n")
    plan = repo.plan(checkpoint, paths=["a.txt", "sub"])
    assert plan.paths == ("a.txt", "sub")
    repo.rollback(plan)
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "b.txt").read_text(encoding="utf-8") == "modified\n"


def test_path_matching_is_literal_and_prefix_safe(scoped_repo):
    repo = scoped_repo
    _write(repo.root, "src/x.txt", "one\n")
    _write(repo.root, "src2/y.txt", "one\n")
    _write(repo.root, "weird[1].txt", "one\n")
    checkpoint = repo.create()
    for name in ("src/x.txt", "src2/y.txt", "weird[1].txt"):
        _write(repo.root, name, "modified\n")
    plan = repo.plan(checkpoint, paths=["src"])
    assert plan.restore == ["src/x.txt"]
    repo.rollback(plan)
    assert (repo.root / "src" / "x.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "src2" / "y.txt").read_text(encoding="utf-8") == "modified\n"
    assert repo.plan(checkpoint, paths=["weird[1].txt"]).restore == ["weird[1].txt"]
    repo.rollback(repo.plan(checkpoint, paths=["weird[1].txt"]))
    assert (repo.root / "weird[1].txt").read_text(encoding="utf-8") == "one\n"
    with pytest.raises(rollback.CheckpointError, match="no such path"):
        repo.plan(checkpoint, paths=["*.txt"])
    assert (repo.root / "src2" / "y.txt").read_text(encoding="utf-8") == "modified\n"


def test_path_resolves_against_the_cwd_and_accepts_absolute_paths(scoped_repo, monkeypatch, capsys):
    repo = scoped_repo
    _write(repo.root, "sub/c.txt", "one\n")
    _write(repo.root, "sub/d.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "sub/c.txt", "modified\n")
    _write(repo.root, "sub/d.txt", "modified\n")
    other = repo.root.parent / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    relative = "sub\\c.txt" if os.name == "nt" else "sub/c.txt"
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root), "--path", relative,
                 "--yes"]) == 0
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "sub" / "d.txt").read_text(encoding="utf-8") == "modified\n"
    _write(repo.root, "sub/c.txt", "modified\n")
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root),
                 "--path", str(repo.root / "sub" / "c.txt"), "--yes"]) == 0
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "one\n"


def test_path_is_relative_to_the_invocation_cwd(scoped_repo, capsys):
    """Design 4.1: a relative value is read against `--cwd`, not the Git top level, so
    `--cwd REPO/sub --path a.txt` restores `REPO/sub/a.txt` and never `REPO/a.txt`."""
    repo = scoped_repo
    _write(repo.root, "a.txt", "root\n")
    _write(repo.root, "sub/a.txt", "sub\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "root modified\n")
    _write(repo.root, "sub/a.txt", "sub modified\n")
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root / "sub"),
                 "--path", "a.txt", "--yes"]) == 0
    capsys.readouterr()
    assert (repo.root / "sub" / "a.txt").read_text(encoding="utf-8") == "sub\n"
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "root modified\n"


def test_path_is_relative_to_the_current_folder_without_cwd(scoped_repo, monkeypatch, capsys):
    """Without `--cwd` the invocation folder is the current one, so running from `REPO/sub`
    restores `REPO/sub/a.txt`."""
    repo = scoped_repo
    _write(repo.root, "a.txt", "root\n")
    _write(repo.root, "sub/a.txt", "sub\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "root modified\n")
    _write(repo.root, "sub/a.txt", "sub modified\n")
    monkeypatch.chdir(repo.root / "sub")
    assert main(["rollback", checkpoint.id, "--path", "a.txt", "--yes"]) == 0
    capsys.readouterr()
    assert (repo.root / "sub" / "a.txt").read_text(encoding="utf-8") == "sub\n"
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "root modified\n"


def test_path_relative_to_a_subfolder_cwd_does_not_fall_back_to_the_root(scoped_repo, capsys):
    """A value that exists relative to the repository root only is absent for a `--cwd` of a
    subfolder: it must be refused, not silently restored from the top level."""
    repo = scoped_repo
    _write(repo.root, "a.txt", "root\n")
    _write(repo.root, "sub/keep.txt", "keep\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "root modified\n")
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root / "sub"),
                 "--path", "a.txt", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "no such path" in err and "a.txt" in err
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "root modified\n"
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs


def test_path_scope_does_not_restore_the_rest_of_the_tree(scoped_repo, capsys):
    repo = scoped_repo
    _write(repo.root, "sub/c.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "sub/c.txt", "modified\n")
    _write(repo.root, "other.txt", "after\n")
    assert main(["rollback", checkpoint.id, "--path", "sub", "--yes", "--cwd", str(repo.root)]) == 0
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "other.txt").read_text(encoding="utf-8") == "after\n"


def test_path_dry_run_lists_only_the_selected_entries_and_changes_nothing(scoped_repo, capsys):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "b.txt", "one\n")
    _write(repo.root, "sub/c.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "b.txt", "modified\n")
    _write(repo.root, "sub/scratch.txt", "after\n")
    snapshot = repo.snapshot_tree()
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--path", "a.txt", "--path", "sub",
                 "--dry-run", "--cwd", str(repo.root)]) == 0
    output = capsys.readouterr().out
    assert "scope: a.txt, sub" in output
    assert "restore  'a.txt'" in output
    assert "delete   'sub/scratch.txt'" in output
    assert "not in checkpoint" in output
    assert "2 file(s) would change" in output
    assert "Everything outside the scope is untouched" in output
    assert "'b.txt'" not in output
    assert repo.snapshot_tree() == snapshot
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs


def test_path_preview_counts_the_restored_bytes(scoped_repo, capsys):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "b.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "a much longer modified body\n")
    _write(repo.root, "b.txt", "modified too\n")
    scratch = _write(repo.root, "scratch.txt", "after\n")
    assert main(["rollback", checkpoint.id, "--path", "a.txt", "--path", "scratch.txt",
                 "--dry-run", "--cwd", str(repo.root)]) == 0
    output = capsys.readouterr().out
    restored = repo.tree_sizes(checkpoint.tree)["a.txt"]   # the checkpoint blob, not the edited file
    deleted = len(scratch.read_bytes())
    assert f"2 file(s) would change ({restored + deleted} bytes" in output
    assert "1 restore" not in output  # the whole-tree summary line is not printed in a scoped run


def test_whole_tree_preview_counts_the_bytes(scoped_repo, capsys):
    """VAL-RB-031: the full-tree dry run summarizes the bytes it would change, like the
    scoped preview does."""
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "a much longer modified body\n")
    assert main(["rollback", checkpoint.id, "--dry-run", "--cwd", str(repo.root)]) == 0
    output = capsys.readouterr().out
    size = repo.tree_sizes(checkpoint.tree)["a.txt"]  # the checkpoint blob, not the edited file
    assert f"1 restore, 0 remove ({size} bytes)" in output
    assert "HEAD, branch, and index stay unchanged" in output


def test_path_nothing_to_restore_names_the_path_and_creates_no_ref(scoped_repo, capsys):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    checkpoint = repo.create()
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--path", "a.txt", "--yes",
                 "--cwd", str(repo.root)]) == 0
    output = capsys.readouterr().out
    assert f"Nothing to restore for a.txt (already matches {checkpoint.id})." in output
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs


def test_path_combines_with_run_last_and_the_default_checkpoint(identity_repo):
    folder = identity_repo
    repo = rollback.Repository(str(folder))
    file = folder / "file.txt"
    file.write_text("v1\n", encoding="utf-8")
    first = repo.create(run_id="R1")
    file.write_text("v2\n", encoding="utf-8")
    second = repo.create()
    other = folder / "other.txt"
    other.write_text("untouched\n", encoding="utf-8")
    file.write_text("modified\n", encoding="utf-8")
    assert repo.plan(repo.select(), paths=["file.txt"]).checkpoint == second
    assert repo.plan(repo.select(run_id="R1"), paths=["file.txt"]).checkpoint == first
    assert repo.plan(second, paths=["file.txt"]).restore == ["file.txt"]
    assert repo.plan(first, paths=["file.txt"]).restore == ["file.txt"]
    assert other.read_text(encoding="utf-8") == "untouched\n"


@pytest.mark.parametrize("scope", ["sub/../../outside.txt"]
                         + (["..\\outside.txt"] if os.name == "nt" else []))
def test_path_outside_the_repository_is_rejected(scoped_repo, capsys, scope):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--path", scope, "--yes",
                 "--cwd", str(repo.root)]) == 1
    assert "outside the repository" in capsys.readouterr().err
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "modified\n"
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs


def test_path_absolute_outside_the_repository_is_rejected(scoped_repo, tmp_path, capsys):
    repo = scoped_repo
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    checkpoint = repo.create()
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--path", str(outside), "--yes",
                 "--cwd", str(repo.root)]) == 1
    assert "outside the repository" in capsys.readouterr().err
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs


@pytest.mark.parametrize("scope", [".git", ".git/HEAD", 
                                         ".git\\config" if os.name == "nt" else ".git/config", ""])
def test_path_git_and_empty_selections_are_rejected(scoped_repo, capsys, scope):
    repo = scoped_repo
    checkpoint = repo.create()
    config = repo.root / ".git" / "config"
    before = config.read_bytes()
    assert main(["rollback", checkpoint.id, "--path", scope, "--yes",
                 "--cwd", str(repo.root)]) == 1
    assert capsys.readouterr().err.strip()
    assert config.read_bytes() == before


def test_path_absent_from_checkpoint_and_working_tree_is_an_error(scoped_repo, capsys):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--path", "does/not/exist.txt", "--yes",
                 "--cwd", str(repo.root)]) == 1
    err = capsys.readouterr().err
    assert "no such path" in err and "does/not/exist.txt" in err
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "modified\n"
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs


def test_path_restores_a_file_deleted_since_the_checkpoint(scoped_repo, capsys):
    repo = scoped_repo
    _write(repo.root, "gone.txt", "keep me\n")
    checkpoint = repo.create()
    (repo.root / "gone.txt").unlink()
    assert main(["rollback", checkpoint.id, "--path", "gone.txt", "--yes",
                 "--cwd", str(repo.root)]) == 0
    assert (repo.root / "gone.txt").read_text(encoding="utf-8") == "keep me\n"


def test_path_created_after_the_checkpoint_is_previewed_as_a_delete(scoped_repo, capsys):
    repo = scoped_repo
    checkpoint = repo.create()
    _write(repo.root, "new.txt", "after\n")
    assert main(["rollback", checkpoint.id, "--path", "new.txt", "--dry-run",
                 "--cwd", str(repo.root)]) == 0
    output = capsys.readouterr().out
    assert "delete   'new.txt'" in output
    assert (repo.root / "new.txt").read_text(encoding="utf-8") == "after\n"


def test_path_stale_preview_is_refused_for_changes_outside_the_scope(scoped_repo):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "b.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "b.txt", "modified\n")
    plan = repo.plan(checkpoint, paths=["a.txt"])
    _write(repo.root, "b.txt", "changed after the preview\n")
    with pytest.raises(rollback.CheckpointError, match="changed since the preview"):
        repo.rollback(plan)
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "modified\n"
    assert (repo.root / "b.txt").read_text(encoding="utf-8") == "changed after the preview\n"


def test_path_safety_checkpoint_snapshots_the_whole_tree_and_undoes_the_rollback(scoped_repo):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "b.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "b.txt", "modified\n")
    (repo.root / "new.txt").write_text("untracked\n", encoding="utf-8")
    before = repo.snapshot_tree()
    safety = repo.rollback(repo.plan(checkpoint, paths=["a.txt"]))
    assert safety.kind == "safety"
    assert safety.tree == before
    assert (repo.root / "b.txt").read_text(encoding="utf-8") == "modified\n"
    (repo.root / "new.txt").unlink()
    repo.rollback(repo.plan(safety))
    assert repo.snapshot_tree() == before


@pytest.mark.parametrize("direction", ["file-to-directory", "directory-to-file"])
def test_path_file_directory_replacements_inside_the_scope(scoped_repo, direction):
    repo = scoped_repo
    shape = repo.root / "shape"
    if direction == "file-to-directory":
        shape.write_text("file\n", encoding="utf-8")
        checkpoint = repo.create()
        shape.unlink()
        shape.mkdir()
        (shape / "child.txt").write_text("child\n", encoding="utf-8")
        scope = ["shape"]
    else:
        shape.mkdir()
        (shape / "child.txt").write_text("child\n", encoding="utf-8")
        checkpoint = repo.create()
        (shape / "child.txt").unlink()
        shape.rmdir()
        shape.write_text("file\n", encoding="utf-8")
        scope = ["shape"]
    repo.rollback(repo.plan(checkpoint, paths=scope))
    assert repo.snapshot_tree() == checkpoint.tree


def test_path_reparse_blocker_inside_the_scope_is_refused(scoped_repo, junction_probe,
                                                          monkeypatch, capsys):
    """VAL-RB-014: a junction inside the selected path is still refused, with its own message
    and before anything is written (a selectable checkpoint makes the CLI reach planning)."""
    monkeypatch.setattr(rollback.os, "name", "nt")  # exercise the Windows scan everywhere
    repo = scoped_repo
    _write(repo.root, "keep/inner.txt", "one\n")
    junction = repo.root / "keep" / "junc"
    junction.mkdir()
    (junction / "child.txt").write_text("through the junction\n", encoding="utf-8")
    checkpoint = repo.create()
    _write(repo.root, "keep/inner.txt", "modified\n")
    junction_probe(junction)
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--path", "keep", "--yes",
                 "--cwd", str(repo.root)]) == 1
    err = capsys.readouterr().err
    assert "Windows junctions and other non-symlink reparse points are not supported" in err
    assert (repo.root / "keep" / "inner.txt").read_text(encoding="utf-8") == "modified\n"
    assert junction.is_dir() and (junction / "child.txt").is_file()
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs


def test_path_reparse_blocker_outside_the_scope_does_not_block(scoped_repo, junction_probe,
                                                               monkeypatch):
    """VAL-RB-014: a scoped restore is only stopped by a blocker in its own selection, so an
    unrelated junction elsewhere in the repository leaves the restore working."""
    monkeypatch.setattr(rollback.os, "name", "nt")
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "keep/inner.txt", "one\n")
    junction = repo.root / "keep" / "junc"
    junction.mkdir()
    (junction / "child.txt").write_text("through the junction\n", encoding="utf-8")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "keep/inner.txt", "modified\n")
    junction_probe(junction)
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--path", "a.txt", "--yes",
                 "--cwd", str(repo.root)]) == 0
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "keep" / "inner.txt").read_text(encoding="utf-8") == "modified\n"
    assert junction.is_dir() and (junction / "child.txt").is_file()
    # The whole-tree safety checkpoint is still created for a scoped restore.
    assert len(git(repo.root, "for-each-ref", rollback.PREFIX).splitlines()) == \
        len(refs.splitlines()) + 1


def test_path_symlink_at_a_restore_destination_is_replaced_without_following_it(
        scoped_repo, tmp_path, capsys):
    """A file symlink standing where the checkpoint has a file is the entry being replaced:
    the link is removed, never followed, so its target stays untouched."""
    repo = scoped_repo
    _write(repo.root, "keep/inner.txt", "one\n")
    checkpoint = repo.create()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inner.txt").write_text("outside\n", encoding="utf-8")
    inner = repo.root / "keep" / "inner.txt"
    inner.unlink()
    try:
        inner.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert main(["rollback", checkpoint.id, "--path", "keep", "--yes",
                 "--cwd", str(repo.root)]) == 0
    capsys.readouterr()
    assert not inner.is_symlink()
    assert inner.read_text(encoding="utf-8") == "one\n"
    assert (outside / "inner.txt").read_text(encoding="utf-8") == "outside\n"


def test_path_symlink_outside_the_scope_does_not_block(scoped_repo, tmp_path, capsys):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "keep/inner.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    inner = repo.root / "keep" / "inner.txt"
    inner.unlink()
    try:
        inner.symlink_to(tmp_path)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert main(["rollback", checkpoint.id, "--path", "a.txt", "--yes",
                 "--cwd", str(repo.root)]) == 0
    capsys.readouterr()
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert inner.is_symlink()


@pytest.mark.parametrize("entries,scope", [
    ([("sub/.git/x", b"x\n"), ("a.txt", b"a\n")], "sub/.git"),
])
def test_path_scope_does_not_hide_a_git_overlapping_checkpoint_name(scoped_repo, entries,
                                                                     scope):
    repo = scoped_repo
    bad = _tree_from(repo, entries)
    checkpoint = rollback.Checkpoint("20260101T000000000000Z-01234567", "", bad,
                                     "2026-01-01T00:00:00+00:00", "hand-built", None)
    with pytest.raises(rollback.CheckpointError) as excinfo:
        repo.plan(checkpoint, paths=[scope])
    # The same refusal as a whole-tree rollback: the unsafe name is checked whatever the scope.
    assert any(text in str(excinfo.value).casefold()
               for text in ("unsafe path", "unsupported on windows", "case-colliding"))
    with pytest.raises(rollback.CheckpointError):
        repo.plan(checkpoint)


@pytest.mark.skipif(os.name != "nt", reason="the unsafe names are Windows reserved names")
def test_path_unquoted_backslash_spelling_outside_the_repository_is_rejected(
        scoped_repo, capsys):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--path", "..\\outside.txt", "--yes",
                 "--cwd", str(repo.root)]) == 1
    assert "outside the repository" in capsys.readouterr().err
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "modified\n"
    assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs


@pytest.mark.skipif(os.name != "nt", reason="reserved names are unsafe on Windows only")
def test_path_scope_does_not_hide_a_reserved_checkpoint_name(scoped_repo):
    repo = scoped_repo
    bad = _tree_from(repo, [("CON", b"x\n"), ("sub/keep.txt", b"keep\n")])
    checkpoint = rollback.Checkpoint("20260101T000000000000Z-01234567", "", bad,
                                     "2026-01-01T00:00:00+00:00", "hand-built", None)
    for scope in ("CON", "sub"):
        with pytest.raises(rollback.CheckpointError) as excinfo:
            repo.plan(checkpoint, paths=[scope])
        assert "unsupported on windows" in str(excinfo.value).casefold()


def test_path_scope_does_not_hide_a_case_colliding_checkpoint_name(scoped_repo):
    repo = scoped_repo
    bad = _tree_from(repo, [("sub/A.txt", b"A\n"), ("sub/a.txt", b"a\n")])
    checkpoint = rollback.Checkpoint("20260101T000000000000Z-01234567", "", bad,
                                     "2026-01-01T00:00:00+00:00", "hand-built", None)
    if os.name == "nt":
        # Windows cannot tell the two entries apart, so the whole tree is refused.
        with pytest.raises(rollback.CheckpointError) as excinfo:
            repo.plan(checkpoint, paths=["sub"])
        assert "case-colliding" in str(excinfo.value).casefold()
    else:
        # A case-sensitive volume holds both; the scope selects the folder they are in.
        plan = repo.plan(checkpoint, paths=["sub"])
        assert plan.restore == ["sub/A.txt", "sub/a.txt"]


def test_path_dot_restores_the_whole_tree(scoped_repo, capsys):
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    _write(repo.root, "sub/c.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "sub/c.txt", "modified\n")
    _write(repo.root, "sub/scratch.txt", "after\n")
    (repo.root / "new.txt").write_text("untracked\n", encoding="utf-8")
    assert main(["rollback", checkpoint.id, "--path", ".", "--yes", "--cwd", str(repo.root)]) == 0
    assert repo.snapshot_tree() == checkpoint.tree
    assert not (repo.root / "new.txt").exists()
    assert (repo.root / "cache" / "private.txt").read_text(encoding="utf-8") == "do not touch\n"


def test_path_ignored_files_are_never_removed(scoped_repo):
    repo = scoped_repo
    (repo.root / "cache" / "private.txt").write_text("do not touch\n", encoding="utf-8")
    checkpoint = repo.create()
    _write(repo.root, "cache/scratch.txt", "ignored\n")
    plan = repo.plan(checkpoint, paths=["cache"])
    assert plan.remove == []
    repo.rollback(plan)
    assert (repo.root / "cache" / "private.txt").read_text(encoding="utf-8") == "do not touch\n"
    assert (repo.root / "cache" / "scratch.txt").read_text(encoding="utf-8") == "ignored\n"


def test_path_matches_case_insensitively_on_a_case_insensitive_volume(scoped_repo):
    repo = scoped_repo
    _write(repo.root, "readme.md", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "readme.md", "modified\n")
    if not _volume_folds_case(repo.root):
        pytest.skip("needs a case-insensitive filesystem")
    plan = repo.plan(checkpoint, paths=["README.md"])
    assert plan.paths == ("readme.md",)
    assert plan.restore == ["readme.md"]
    repo.rollback(plan)
    assert (repo.root / "readme.md").read_text(encoding="utf-8") == "one\n"


def test_path_directory_scope_folds_case_on_a_case_insensitive_volume(scoped_repo, monkeypatch):
    """Design 4.1: the folded retry covers a folder name too, so `--path SUB` selects the
    entries of `sub` and the preview shows the repository's spelling."""
    repo = scoped_repo
    _write(repo.root, "sub/c.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "sub/c.txt", "modified\n")
    monkeypatch.setattr(repo, "folded", True)
    plan = repo.plan(checkpoint, paths=["SUB"])
    assert plan.paths == ("sub",)
    assert plan.restore == ["sub/c.txt"]
    repo.rollback(plan)
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "one\n"


def test_path_directory_scope_folds_case_for_a_deleted_directory(scoped_repo, monkeypatch):
    """A folder that is gone from the working tree is still selectable through its own
    entries, whatever case the value carries."""
    repo = scoped_repo
    _write(repo.root, "sub/c.txt", "one\n")
    checkpoint = repo.create()
    (repo.root / "sub" / "c.txt").unlink()
    (repo.root / "sub").rmdir()
    monkeypatch.setattr(repo, "folded", True)
    plan = repo.plan(checkpoint, paths=["SUB"])
    assert plan.paths == ("sub",)
    assert plan.restore == ["sub/c.txt"]
    repo.rollback(plan)
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "one\n"


def test_path_directory_scope_matches_case_through_the_real_volume(scoped_repo, capsys):
    """The same request through the real CLI, on whichever volume CI gives us: Windows and
    macOS fold, the case-sensitive Linux runners are covered by the literal test below."""
    repo = scoped_repo
    _write(repo.root, "sub/c.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "sub/c.txt", "modified\n")
    if not _volume_folds_case(repo.root):
        pytest.skip("case-sensitive filesystem: SUB is a different folder from sub")
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root), "--path", "SUB",
                 "--yes"]) == 0
    capsys.readouterr()
    assert (repo.root / "sub" / "c.txt").read_text(encoding="utf-8") == "one\n"


def test_path_matching_stays_literal_when_the_volume_does_not_fold_case(scoped_repo, monkeypatch):
    """Design 4.1: the folded retry belongs to a case-insensitive volume. With folding off a
    differently cased spelling never selects the file that is there; on a real case-sensitive
    volume the same value is reported absent (the next test)."""
    repo = scoped_repo
    _write(repo.root, "readme.md", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "readme.md", "modified\n")
    monkeypatch.setattr(repo, "folded", False)
    try:
        plan = repo.plan(checkpoint, paths=["README.md"])
    except rollback.CheckpointError as exc:
        assert "no such path" in str(exc)
    else:
        assert plan.paths == ("README.md",)
        assert plan.restore == [] and plan.remove == []
    assert (repo.root / "readme.md").read_text(encoding="utf-8") == "modified\n"


def test_path_case_sensitive_volume_never_selects_a_differently_cased_file(scoped_repo, capsys):
    """On the case-sensitive Ubuntu runners an absent `README.md` must never select and
    overwrite the `readme.md` that is there (VAL-RB-012)."""
    repo = scoped_repo
    _write(repo.root, "readme.md", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "readme.md", "modified\n")
    if _volume_folds_case(repo.root):
        pytest.skip("needs a case-sensitive filesystem")
    refs = git(repo.root, "for-each-ref", rollback.PREFIX)
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root), "--path", "README.md",
                 "--yes"]) == 1
    err = capsys.readouterr().err
    assert "no such path" in err and "README.md" in err
    assert (repo.root / "readme.md").read_text(encoding="utf-8") == "modified\n"

# --------------------------------------- round-2 scrutiny regressions (LOCAL-R2-RB-01/02)

def _make_junction(link, target):
    """A real NTFS directory junction: the one shape Git's own directory walk follows."""
    result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True)
    if result.returncode:
        pytest.skip(f"Junctions unavailable: {result.stderr.decode('utf-8', 'replace').strip()}")


def _remove_junction(link):
    os.rmdir(link)  # a junction is a directory entry; removing it never touches its target


def _blobs_holding(repo, payload):
    """Every blob in the object store whose bytes contain `payload`. Staging that follows a
    junction writes one for the target's file, and `git add -A` writes it even for a dry run."""
    listing = git(repo.root, "cat-file", "--batch-all-objects",
                  "--batch-check=%(objectname) %(objecttype)").decode("ascii")
    return [oid for oid, _, kind in (line.partition(" ") for line in listing.splitlines())
            if kind == "blob" and payload in git(repo.root, "cat-file", "blob", oid)]


def _tree_names(repo, tree):
    return git(repo.root, "ls-tree", "-r", "--name-only", tree).decode("utf-8").splitlines()


def _sharp_s_folders_stay_apart(folder):
    """Whether this volume keeps `Straße` and `Strasse` apart, as NTFS and APFS do. A volume
    that folds them into one name cannot hold the scenario at all."""
    first, second = folder / "Stra\u00dfe", folder / "Strasse"
    try:
        first.mkdir()
        second.mkdir()
        if os.path.samefile(first, second):
            return False
        (first / "probe.txt").write_text("one\n", encoding="utf-8")
        (second / "probe.txt").write_text("two\n", encoding="utf-8")
        return ((first / "probe.txt").read_text(encoding="utf-8") == "one\n"
                and (second / "probe.txt").read_text(encoding="utf-8") == "two\n")
    except OSError:
        return False
    finally:
        for path in (first / "probe.txt", second / "probe.txt", first, second):
            try:
                path.unlink() if path.is_file() else path.rmdir()
            except OSError:
                pass


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_scoped_snapshot_never_captures_an_out_of_scope_junction_target(
        scoped_repo, tmp_path, capsys):
    """LOCAL-R2-RB-01: passing an out-of-scope junction through must not mean following it. The
    whole-tree snapshot behind the stale-preview tree and the safety checkpoint has to stay out
    of the target, or its outside contents land in Git objects and in the checkpoint."""
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"synthetic outside-repository marker, never a real secret\n"
    marker = outside / "private-marker.txt"
    marker.write_bytes(payload)
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    link = repo.root / "elsewhere"
    _make_junction(link, outside)
    try:
        assert main(["rollback", checkpoint.id, "--path", "a.txt", "--dry-run",
                     "--cwd", str(repo.root)]) == 0
        assert _blobs_holding(repo, payload) == []
        assert main(["rollback", checkpoint.id, "--path", "a.txt", "--yes",
                     "--cwd", str(repo.root)]) == 0
        assert link.is_dir() and (link / "private-marker.txt").read_bytes() == payload
        refs = git(repo.root, "for-each-ref", rollback.PREFIX)
        assert main(["rollback", checkpoint.id, "--path", "elsewhere", "--dry-run",
                     "--cwd", str(repo.root)]) == 1
        assert "Windows junctions and other non-symlink reparse points" in capsys.readouterr().err
        assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs
    finally:
        _remove_junction(link)
    safety = repo.select()
    assert safety.kind == "safety"
    assert not [name for name in _tree_names(repo, safety.tree) if name.startswith("elsewhere")]
    assert _blobs_holding(repo, payload) == []
    assert (repo.root / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert marker.read_bytes() == payload


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_scoped_snapshot_keeps_the_repository_entry_a_junction_shadows(scoped_repo, tmp_path):
    """A junction standing where the repository has a path must not become that path's content:
    the entry keeps its own blob and the target's bytes stay out of the object store."""
    repo = scoped_repo
    _write(repo.root, "elsewhere/shadowed.txt", "repository content\n")
    _write(repo.root, "a.txt", "one\n")
    git(repo.root, "add", "elsewhere/shadowed.txt")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"synthetic outside-repository shadow, never a real secret\n"
    (outside / "shadowed.txt").write_bytes(payload)
    (repo.root / "elsewhere" / "shadowed.txt").unlink()
    (repo.root / "elsewhere").rmdir()
    link = repo.root / "elsewhere"
    _make_junction(link, outside)
    try:
        assert main(["rollback", checkpoint.id, "--path", "a.txt", "--yes",
                     "--cwd", str(repo.root)]) == 0
    finally:
        _remove_junction(link)
    safety = repo.select()
    assert safety.kind == "safety"
    assert repo.entries(safety.tree)["elsewhere/shadowed.txt"] == \
        repo.entries(checkpoint.tree)["elsewhere/shadowed.txt"]
    assert _blobs_holding(repo, payload) == []
    assert (outside / "shadowed.txt").read_bytes() == payload


def test_blocked_reparse_entries_guard_refuses_a_traversed_junction():
    """The snapshot re-reads the staged entries under a blocked reparse point, so a Git walk
    that followed it anyway is a refusal, not a silently pinned copy of the target."""
    staged = b"100644 0123456789012345678901234567890123456789 0\telsewhere/marker.txt\0"
    rollback._reparse_entries_unchanged(b"", b"")        # nothing blocked, nothing to check
    rollback._reparse_entries_unchanged(staged, staged)  # the index kept its own entry
    with pytest.raises(rollback.CheckpointError, match="junction outside the selection"):
        rollback._reparse_entries_unchanged(b"", staged)


def test_folded_spelling_table_never_widens_through_unicode_casefolding():
    """LOCAL-R2-RB-02: the folded retry uses the case mapping a volume compares names with.
    `Straße` and `Strasse` are two folders on NTFS and APFS; `casefold` folds them into one
    key, the simple mapping does not, and a key two names share keeps no spelling at all."""
    assert rollback._fold_case("Stra\u00dfe") == "stra\u00dfe"
    folded = rollback._folded_spellings({"Stra\u00dfe", "Strasse", "Stra\u00dfe/a.txt",
                                         "sub", "Sub"})
    assert folded["stra\u00dfe"] == "Stra\u00dfe"
    assert folded["stra\u00dfe/a.txt"] == "Stra\u00dfe/a.txt"
    assert folded["strasse"] == "Strasse"
    assert "sub" not in folded


@pytest.mark.parametrize("scope, restored", [
    ("Stra\u00dfe", "Stra\u00dfe/a.txt"),
    ("Strasse", "Strasse/b.txt"),
])
def test_path_exact_directory_selection_never_restores_a_casefolded_sibling(
        scoped_repo, monkeypatch, scope, restored):
    """LOCAL-R2-RB-02: a value that matches a folder exactly selects that folder and nothing a
    Python casefold happens to fold onto it, so the sibling keeps its modified file."""
    repo = scoped_repo
    if not _sharp_s_folders_stay_apart(repo.root.parent):
        pytest.skip("this volume folds Stra\u00dfe and Strasse into one name")
    monkeypatch.setattr(repo, "folded", True)
    after = {"Stra\u00dfe/a.txt": "sharp-s after\n", "Strasse/b.txt": "unselected after\n"}
    for name in after:
        _write(repo.root, name, "one\n")
    checkpoint = repo.create()
    for name, text in after.items():
        _write(repo.root, name, text)
    plan = repo.plan(checkpoint, paths=[scope])
    assert plan.paths == (scope,)
    assert plan.restore == [restored]
    repo.rollback(plan)
    for name, text in after.items():
        expected = "one\n" if name == restored else text
        assert repo.root.joinpath(*name.split("/")).read_text(encoding="utf-8") == expected


def test_path_scope_never_selects_a_casefolded_sibling_of_a_hand_built_tree(
        scoped_repo, monkeypatch):
    """The same rule without a filesystem to ask: a Git tree spells the two folders out, so the
    exact value must select one of them on every volume, including a case-sensitive one."""
    repo = scoped_repo
    monkeypatch.setattr(repo, "folded", True)
    tree = _tree_from(repo, [("Stra\u00dfe/a.txt", b"sharp-s\n"),
                             ("Strasse/b.txt", b"plain-s\n")])
    checkpoint = rollback.Checkpoint("20260101T000000000000Z-01234567", "", tree,
                                     "2026-01-01T00:00:00+00:00", "hand-built", None)
    for scope, restored in (("Stra\u00dfe", "Stra\u00dfe/a.txt"), ("Strasse", "Strasse/b.txt")):
        plan = repo.plan(checkpoint, paths=[scope])
        assert plan.paths == (scope,)
        assert plan.restore == [restored]


def test_path_sharp_s_directory_scope_through_the_real_volume(scoped_repo, capsys):
    """The same request through the real CLI, on whichever volume CI gives us: NTFS, APFS and an
    ext4 runner all keep the two folders apart, and only the named one is restored."""
    repo = scoped_repo
    if not _sharp_s_folders_stay_apart(repo.root.parent):
        pytest.skip("this volume folds Stra\u00dfe and Strasse into one name")
    _write(repo.root, "Stra\u00dfe/a.txt", "one\n")
    _write(repo.root, "Strasse/b.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "Stra\u00dfe/a.txt", "modified\n")
    _write(repo.root, "Strasse/b.txt", "unselected after\n")
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root), "--path", "Stra\u00dfe",
                 "--yes"]) == 0
    capsys.readouterr()
    assert (repo.root / "Stra\u00dfe" / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "Strasse" / "b.txt").read_text(encoding="utf-8") == "unselected after\n"


def _kelvin_folders_stay_apart(folder):
    """Whether this volume keeps U+212A KELVIN SIGN apart from `K`, as this NTFS volume does.
    Python's `str.lower` maps both to `k`, so a volume that folds them together cannot hold the
    scenario at all."""
    first, second = folder / "\u212a", folder / "K"
    try:
        first.mkdir()
        second.mkdir()
        if os.path.samefile(first, second):
            return False
        (first / "probe.txt").write_text("one\n", encoding="utf-8")
        (second / "probe.txt").write_text("two\n", encoding="utf-8")
        return ((first / "probe.txt").read_text(encoding="utf-8") == "one\n"
                and (second / "probe.txt").read_text(encoding="utf-8") == "two\n")
    except OSError:
        return False
    finally:
        for path in (first / "probe.txt", second / "probe.txt", first, second):
            try:
                path.unlink() if path.is_file() else path.rmdir()
            except OSError:
                pass


def test_entry_filtering_is_literal_and_never_folds_case(scoped_repo, monkeypatch):
    """LOCAL-R3-RB-01: resolving the value the user typed to one spelling is the only case
    mapping a selection does. The scope that comes out of it is applied literally, so `sub`
    never selects `SUB` and `\u212a` never selects `K` - two folders this volume keeps
    apart though Python maps both names to `k`."""
    repo = scoped_repo
    monkeypatch.setattr(repo, "folded", True)
    assert repo._selects("sub/c.txt", ("sub",))
    assert not repo._selects("SUB/c.txt", ("sub",))
    assert repo._selects("\u212a/a.txt", ("\u212a",))
    assert not repo._selects("K/b.txt", ("\u212a",))
    assert repo._blocks_scope("sub/junc", ("sub",))
    assert not repo._blocks_scope("K/junc", ("\u212a",))


def test_path_scope_never_widens_through_a_simple_case_mapping(scoped_repo, monkeypatch):
    """LOCAL-R3-RB-01: `\u212a` (U+212A KELVIN SIGN) and `K` are distinct folders on NTFS and
    both lowercase to `k`. Selecting one of them restores its own files and leaves the other
    spelling's file exactly as it is, whatever the volume folds."""
    repo = scoped_repo
    monkeypatch.setattr(repo, "folded", True)
    kelvin = "\u212a"
    tree = _tree_from(repo, [(f"{kelvin}/a.txt", b"kelvin\n"), ("K/b.txt", b"ascii\n")])
    checkpoint = rollback.Checkpoint("20260101T000000000000Z-01234567", "", tree,
                                     "2026-01-01T00:00:00+00:00", "hand-built", None)
    for scope, restored in ((kelvin, f"{kelvin}/a.txt"), ("K", "K/b.txt")):
        plan = repo.plan(checkpoint, paths=[scope])
        assert plan.paths == (scope,)
        assert plan.restore == [restored]


def test_path_kelvin_sign_directory_scope_through_the_real_volume(scoped_repo, capsys):
    """The same request through the real CLI on whichever volume CI gives us: NTFS keeps the
    Kelvin sign and `K` apart, so selecting one restores it and overwrites nothing of the
    other's (a volume that folds them together cannot hold the scenario and skips)."""
    repo = scoped_repo
    if not _kelvin_folders_stay_apart(repo.root.parent):
        pytest.skip("this volume folds U+212A KELVIN SIGN and K into one name")
    kelvin = "\u212a"
    _write(repo.root, f"{kelvin}/a.txt", "one\n")
    _write(repo.root, "K/b.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, f"{kelvin}/a.txt", "selected after\n")
    _write(repo.root, "K/b.txt", "unselected after\n")
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root), "--path", kelvin,
                 "--yes"]) == 0
    capsys.readouterr()
    assert (repo.root / kelvin / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "K" / "b.txt").read_text(encoding="utf-8") == "unselected after\n"
    assert main(["rollback", checkpoint.id, "--cwd", str(repo.root), "--path", "K",
                 "--yes"]) == 0
    capsys.readouterr()
    assert (repo.root / "K" / "b.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / kelvin / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_path_junction_in_a_sharp_s_sibling_folder_does_not_block_the_restore(
        scoped_repo, junction_probe, monkeypatch):
    """The blocker scan asks the volume's own case question, so a junction inside `Straße` is
    not on the way to `Strasse` and a scoped restore of `Strasse` still runs."""
    monkeypatch.setattr(rollback.os, "name", "nt")
    repo = scoped_repo
    if not _sharp_s_folders_stay_apart(repo.root.parent):
        pytest.skip("this volume folds Stra\u00dfe and Strasse into one name")
    monkeypatch.setattr(repo, "folded", True)
    _write(repo.root, "Stra\u00dfe/a.txt", "one\n")
    _write(repo.root, "Strasse/b.txt", "one\n")
    junction = repo.root / "Stra\u00dfe" / "junc"
    junction.mkdir()
    (junction / "child.txt").write_text("through the junction\n", encoding="utf-8")
    checkpoint = repo.create()
    _write(repo.root, "Strasse/b.txt", "modified\n")
    junction_probe(junction)
    assert main(["rollback", checkpoint.id, "--path", "Strasse", "--yes",
                 "--cwd", str(repo.root)]) == 0
    assert (repo.root / "Strasse" / "b.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo.root / "Stra\u00dfe" / "a.txt").read_text(encoding="utf-8") == "one\n"


# --------------------------------------- round-4 scrutiny regressions (LOCAL-R4-RB-01)

def test_path_scope_resolution_asks_the_working_tree_too(scoped_repo, monkeypatch):
    """LOCAL-R4-RB-01: a value the checkpoint tree does not hold is resolved against the working
    tree as well, so the scope the safety scan is handed is one spelling the repository really
    has - a `sub` folder that appeared after the checkpoint, for instance. Before the fix only
    the entry filters saw the working tree, so the scan was given the value as typed and counted
    a junction there as out of scope (the CLI test below)."""
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    _write(repo.root, "sub/late.txt", "appeared after the checkpoint\n")
    monkeypatch.setattr(repo, "folded", True)
    target = repo.entries(checkpoint.tree)
    assert repo._selected(["SUB"], (target,)) == ("sub",)
    plan = repo.plan(checkpoint, paths=["SUB"])
    assert plan.paths == ("sub",)
    assert plan.restore == []
    assert plan.remove == ["sub/late.txt"]


def test_working_tree_spelling_never_widens_through_a_shared_key(scoped_repo, monkeypatch):
    """Two working-tree folders Python maps to one key give no canonical spelling, so the value
    stays as typed and selects nothing: the working-tree retry follows the same rule as the
    checkpoint retry (LOCAL-R3-RB-01) instead of picking one of the two folders."""
    repo = scoped_repo
    if not _kelvin_folders_stay_apart(repo.root.parent):
        pytest.skip("this volume folds \u212a and K into one name")
    _write(repo.root, "\u212a/late.txt", "one\n")
    _write(repo.root, "K/late.txt", "one\n")
    monkeypatch.setattr(repo, "folded", True)
    assert repo._disk_spelling("k") is None
    assert repo._disk_spelling("gone") is None
    assert repo._disk_spelling("\u212a") == "\u212a"
    assert repo._disk_spelling("K") == "K"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_path_current_only_junction_refuses_both_case_spellings(scoped_repo, tmp_path, capsys):
    """LOCAL-R4-RB-01: a junction the checkpoint never held is resolved to the working tree's own
    spelling, so `SUB` reaches `sub` and the literal in-scope safety scan refuses it, exactly as
    the exact `sub` does. Before the fix `SUB` stayed as typed, the scan counted the junction as
    out of scope, and the plan reported `Nothing to restore` with exit 0."""
    repo = scoped_repo
    _write(repo.root, "a.txt", "one\n")
    checkpoint = repo.create()
    _write(repo.root, "a.txt", "modified\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"synthetic outside-repository marker, never a real secret\n"
    marker = outside / "marker.txt"
    marker.write_bytes(payload)
    link = repo.root / "sub"
    _make_junction(link, outside)
    try:
        refs = git(repo.root, "for-each-ref", rollback.PREFIX)
        for value in ("SUB", "sub"):
            assert main(["rollback", checkpoint.id, "--path", value, "--dry-run",
                         "--cwd", str(repo.root)]) == 1
            assert "Windows junctions and other non-symlink reparse points" in \
                capsys.readouterr().err
            assert main(["rollback", checkpoint.id, "--path", value, "--yes",
                         "--cwd", str(repo.root)]) == 1
            assert "Windows junctions and other non-symlink reparse points" in \
                capsys.readouterr().err
        assert git(repo.root, "for-each-ref", rollback.PREFIX) == refs
        assert _blobs_holding(repo, payload) == []
        assert (repo.root / "a.txt").read_text(encoding="utf-8") == "modified\n"
        assert marker.read_bytes() == payload
    finally:
        _remove_junction(link)


# ---------------------------------------------------------------- retention

def refs_of(repo):
    return sorted(git(repo.root, "for-each-ref", "--format=%(refname)",
                      rollback.PREFIX).decode().split())


def commit_dated(repo, label, run_id, days, monkeypatch):
    """A checkpoint whose Git commit date is `days` in the past, the way a repository that
    has been running for a while looks (`GIT_COMMITTER_DATE` is what Git records)."""
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    monkeypatch.setenv("GIT_AUTHOR_DATE", stamp)
    monkeypatch.setenv("GIT_COMMITTER_DATE", stamp)
    return rollback.Repository(str(repo.root)).create(label=label, run_id=run_id)


def test_prune_needs_keep_or_older_than(repo, capsys):
    repo.create(label="only", run_id="run-1")
    with pytest.raises(rollback.CheckpointError, match="--keep or --older-than is required"):
        repo.prune()
    assert len(repo.checkpoints()) == 1
    assert main(["checkpoints", "prune", "--cwd", str(repo.root)]) == 1
    err = capsys.readouterr().err
    assert "--keep" in err and "--older-than" in err and "required" in err
    assert len(repo.checkpoints()) == 1


def test_prune_older_than_removes_the_aged_checkpoints_only(repo, monkeypatch, capsys):
    recent = [repo.create(label=f"new-{index}", run_id="new") for index in range(3)]
    aged = [commit_dated(repo, f"old-{index}", "old", 40, monkeypatch) for index in range(3)]
    for name in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.delenv(name)
    assert main(["checkpoints", "prune", "--older-than", "30d", "--cwd", str(repo.root)]) == 0
    assert "Pruned 3 checkpoint(s)" in capsys.readouterr().out
    assert refs_of(repo) == sorted(cp.ref for cp in recent)
    assert aged and not any(cp.ref in refs_of(repo) for cp in aged)


@pytest.mark.parametrize("text,age,removed", [
    ("12h", timedelta(hours=13), True),
    ("12h", timedelta(hours=11), False),
    ("2w", timedelta(days=15), True),
    ("2w", timedelta(days=13), False),
    ("30d", timedelta(days=31), True),
])
def test_prune_older_than_accepts_days_weeks_and_hours(repo, monkeypatch, text, age, removed):
    made = repo.create(label="only", run_id="run-1")
    now = datetime.now(timezone.utc)
    made.committed_at = now - age
    monkeypatch.setattr(repo, "checkpoints", lambda: [made])
    assert bool(repo.prune(older_than=rollback.parse_duration(text), now=now)) is removed


def test_prune_keeps_a_checkpoint_exactly_at_the_age_limit(repo, monkeypatch):
    made = repo.create(label="only", run_id="run-1")
    now = datetime.now(timezone.utc)
    limit = timedelta(days=30)
    made.committed_at = now - limit
    monkeypatch.setattr(repo, "checkpoints", lambda: [made])
    assert repo.prune(older_than=limit, now=now) == []
    assert refs_of(repo) == [made.ref]
    made.committed_at = now - limit - timedelta(seconds=1)
    assert repo.prune(older_than=limit, now=now) == [made]


@pytest.mark.parametrize("keep,expected", [
    (6, []),
    (5, [0]),
    (3, [2, 1, 0]),
    (2, [3, 2, 1, 0]),
    (0, [3, 2, 1, 0]),
])
def test_prune_with_keep_and_older_than_removes_only_both(repo, monkeypatch, keep, expected):
    made = [repo.create(label=f"cp-{index}", run_id="run-1") for index in range(6)]
    now = datetime.now(timezone.utc)
    for index, checkpoint in enumerate(made):  # made[0] is the oldest by both id and date
        checkpoint.committed_at = (now - timedelta(days=40) if index < 4
                                   else now - timedelta(hours=1))
    listed = list(reversed(made))
    monkeypatch.setattr(repo, "checkpoints", lambda: listed)
    removed = repo.prune(keep=keep, older_than=timedelta(days=30), now=now)
    assert removed == [made[index] for index in expected]


def test_prune_with_both_flags_keeps_a_young_checkpoint_beyond_the_window(repo, monkeypatch):
    made = [repo.create(label=f"cp-{index}", run_id="run-1") for index in range(3)]
    now = datetime.now(timezone.utc)
    for checkpoint in made:
        checkpoint.committed_at = now - timedelta(hours=1)
    monkeypatch.setattr(repo, "checkpoints", lambda: list(reversed(made)))
    assert repo.prune(keep=1, older_than=timedelta(days=30), now=now) == []


def test_prune_treats_safety_checkpoints_like_any_other(repo, capsys):
    made = [repo.create(label=f"cp-{index}", run_id=f"run-{index}",
                        kind="safety" if index in (1, 4) else "checkpoint")
            for index in range(5)]
    assert main(["checkpoints", "prune", "--keep", "3", "--cwd", str(repo.root)]) == 0
    assert "Pruned 2 checkpoint(s), including 1 safety checkpoint." in capsys.readouterr().out
    assert refs_of(repo) == sorted(cp.ref for cp in (made[4], made[3], made[2]))


def test_prune_reports_the_count_and_runs_no_git_gc(repo, capsys):
    made = [repo.create(label=f"cp-{index}", run_id="run-1") for index in range(3)]
    assert main(["checkpoints", "prune", "--keep", "1", "--cwd", str(repo.root)]) == 0
    out = capsys.readouterr().out
    assert "Pruned 2 checkpoint(s)." in out
    assert "git gc --prune=now" in out
    for checkpoint in made[:2]:
        assert git(repo.root, "cat-file", "-t", checkpoint.oid).strip() == b"commit"
    assert main(["checkpoints", "prune", "--keep", "1", "--cwd", str(repo.root)]) == 0
    assert "Pruned 0 checkpoint(s)." in capsys.readouterr().out


@pytest.mark.parametrize("text", ["30x", "abc", "d30", "-1d", "1.5d", "9999999999d",
                                  "999999999d", "99999999999999999999999h"])
def test_prune_rejects_an_invalid_duration(repo, capsys, text):
    repo.create(label="only", run_id="run-1")
    with pytest.raises(SystemExit) as exc:
        main(["checkpoints", "prune", "--older-than", text, "--cwd", str(repo.root)])
    assert exc.value.code == 2
    assert "--older-than" in capsys.readouterr().err
    assert len(repo.checkpoints()) == 1


def test_durations_are_bounded_and_never_raise():
    """A duration the calendar cannot hold is a wrong value, not a crash (LOCAL-RET-02)."""
    for text in ("999999999d", "9999999999d", "1000000000w", "90000000000h",
                 "999999999999999999999999d"):
        assert rollback.parse_duration(text) is None
    assert rollback.parse_duration("3650d") == timedelta(days=3650)
    assert rollback.parse_duration("2w") == timedelta(days=14)


def test_prune_help_lists_keep_older_than_and_verbose(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["checkpoints", "prune", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--keep" in text and "--older-than" in text and "--verbose" in text
    assert "required" not in text


def test_prune_verbose_names_the_removed_checkpoints(repo, capsys):
    made = [repo.create(label=f"cp-{index}", run_id="run-1") for index in range(3)]
    assert main(["checkpoints", "prune", "--keep", "1", "--verbose",
                 "--cwd", str(repo.root)]) == 0
    out = capsys.readouterr().out
    assert "Pruned 2 checkpoint(s)." in out
    assert f"removed {made[0].id}" in out and f"removed {made[1].id}" in out
    assert made[2].id not in out
    assert refs_of(repo) == [made[2].ref]


def test_prune_without_verbose_keeps_the_plain_report(repo, capsys):
    made = [repo.create(label=f"cp-{index}", run_id="run-1") for index in range(2)]
    assert main(["checkpoints", "prune", "--keep", "1", "--cwd", str(repo.root)]) == 0
    out = capsys.readouterr().out
    assert "Pruned 1 checkpoint(s)." in out
    assert made[0].id not in out and made[1].id not in out
    assert "removed" not in out


# ---------------------------------------------------------------- auto-prune


def write_config(folder, data):
    path = folder / "config.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def run_hook(repo, session_id, monkeypatch, capsys):
    event = dict(cwd=str(repo.root), session_id=session_id, hook_event_name="UserPromptSubmit")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    code = main(["checkpoint", "--hook"])
    return code, capsys.readouterr()


def test_hook_auto_prune_keeps_the_newest_and_this_session(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RUNLEDGER_CONFIG",
                       str(write_config(tmp_path, {"rollback": {"auto_prune": {"keep": 2}}})))
    for index in range(5):
        repo.create(label=f"cp-{index}", run_id=f"other-{index}")
    mine = repo.create(label="SessionStart", run_id="keepme")
    code, captured = run_hook(repo, "keepme", monkeypatch, capsys)
    assert code == 0 and captured.out == "" and captured.err == ""
    remaining = rollback.Repository(str(repo.root)).checkpoints()
    assert [cp.run_id for cp in remaining] == ["keepme", "keepme"]
    assert mine.id in [cp.id for cp in remaining]


def test_hook_auto_prune_follows_the_user_keep_across_runs(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RUNLEDGER_CONFIG",
                       str(write_config(tmp_path, {"rollback": {"auto_prune": {"keep": 3}}})))
    for index in range(6):
        code, captured = run_hook(repo, f"s{index}", monkeypatch, capsys)
        assert code == 0 and captured.out == ""
    checkpoints = rollback.Repository(str(repo.root)).checkpoints()
    assert [cp.run_id for cp in checkpoints] == ["s5", "s4", "s3"]


def test_hook_auto_prune_off_keeps_every_checkpoint(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RUNLEDGER_CONFIG",
                       str(write_config(tmp_path, {"rollback": {"auto_prune": False}})))
    for index in range(4):
        assert run_hook(repo, f"s{index}", monkeypatch, capsys)[0] == 0
    assert len(rollback.Repository(str(repo.root)).checkpoints()) == 4


def test_hook_auto_prune_age_limit_spares_recent_checkpoints(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(write_config(
        tmp_path, {"rollback": {"auto_prune": {"keep": 1, "older_than": "30d"}}})))
    for index in range(4):
        assert run_hook(repo, f"s{index}", monkeypatch, capsys)[0] == 0
    assert len(rollback.Repository(str(repo.root)).checkpoints()) == 4


@pytest.mark.parametrize("broken", ["{bad json", '{"rollback": {"auto_prune": {"keep": "x"}}}'])
def test_hook_with_an_unusable_config_keeps_the_default_and_stays_silent(
        repo, monkeypatch, capsys, tmp_path, broken):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(write_config(tmp_path, broken)))
    for index in range(5):
        code, captured = run_hook(repo, f"s{index}", monkeypatch, capsys)
        assert code == 0 and captured.out == ""
    assert len(rollback.Repository(str(repo.root)).checkpoints()) == 5


def test_hook_auto_prune_failure_is_swallowed(repo, monkeypatch, capsys):
    def explode(self, *args, **kwargs):
        raise OSError("prune exploded")

    monkeypatch.setattr(rollback.Repository, "auto_prune", explode)
    code, captured = run_hook(repo, "s1", monkeypatch, capsys)
    assert code == 0 and captured.out == ""
    assert len(rollback.Repository(str(repo.root)).checkpoints()) == 1


def test_hook_settings_failure_keeps_the_defaults(repo, monkeypatch, capsys):
    """Decoding the settings runs inside the hook's failure isolation (LOCAL-RET-02)."""
    def explode(*args, **kwargs):
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(rollback, "load_settings", explode)
    code, captured = run_hook(repo, "s1", monkeypatch, capsys)
    assert code == 0 and captured.out == "" and captured.err == ""
    assert len(rollback.Repository(str(repo.root)).checkpoints()) == 1


@pytest.mark.parametrize("policy,remaining", [
    # An age limit the calendar cannot hold is refused; the keep beside it still applies.
    ({"keep": 5, "older_than": "9999999999d"}, 5),
    ({"keep": 3, "older_than": "99999999999999999999999h"}, 3),
    # Values that are not the documented shape keep the documented defaults.
    ({"keep": "lots"}, 6),
    ({"keep": -2}, 6),
    ({"keep": 2.5}, 6),
    ({"keep": True}, 6),
    # A keep past the documented maximum falls back the same way; six checkpoints cannot tell
    # the fallback from the huge value, so that one is asserted beyond the default window by
    # test_hook_bounds_a_huge_keep_beyond_the_default_window (LOCAL-R2-RET-01).
    ({"keep": rollback.MAX_PRUNE_KEEP + 1}, 6),
    ({"older_than": 30}, 6),
    ({"older_than": "1.5d"}, 6),
    ({"older_than": "999999999d"}, 6),
])
def test_hook_retention_settings_never_break_the_hook(repo, monkeypatch, capsys, tmp_path,
                                                       policy, remaining):
    """Extreme or invalid retention settings must cost the hook nothing (LOCAL-RET-02)."""
    monkeypatch.setenv("RUNLEDGER_CONFIG",
                       str(write_config(tmp_path, {"rollback": {"auto_prune": policy}})))
    for index in range(6):
        code, captured = run_hook(repo, f"s{index}", monkeypatch, capsys)
        assert code == 0 and captured.out == "" and captured.err == ""
    kept = rollback.Repository(str(repo.root)).checkpoints()
    assert kept[0].run_id == "s5"  # the checkpoint the hook just made survives
    assert len(kept) == remaining


def test_keep_past_the_documented_maximum_decodes_to_the_default():
    """LOCAL-R2-RET-01: `keep` is bounded, so a value above the maximum is unusable exactly
    like a negative one and the hook applies its documented default instead."""
    assert rollback.MAX_PRUNE_KEEP == 100000

    def decode(keep):
        return rollback.load_settings(
            {"rollback": {"auto_prune": {"keep": keep}}}).auto_prune_keep

    assert decode(1) == 1
    assert decode(rollback.MAX_PRUNE_KEEP) == rollback.MAX_PRUNE_KEEP
    assert decode(rollback.MAX_PRUNE_KEEP + 1) == 50
    assert decode(10 ** 30) == 50


def seed_checkpoints(repo, count):
    """Publish `count` real checkpoint refs, one commit each, in a handful of Git calls.

    `Repository.create` resolves the Git identity and lists the tree on every call, so fifty-odd
    seeds through it cost minutes of test time; a retention test only needs the refs to count,
    and every commit carries the message `create` writes."""
    tree = repo.snapshot_tree()
    identity = repo._checkpoint_identity()
    refs = []
    for index in range(count):
        checkpoint_id = f"20261015T120000{index:06d}Z-{index:08x}"
        metadata = dict(version=1, id=checkpoint_id, created_at="2026-10-15T12:00:00+00:00",
                        label=f"cp-{index}", run_id=f"other-{index}", kind="checkpoint")
        message = "RunLedger checkpoint\n\n" + json.dumps(metadata, ensure_ascii=True) + "\n"
        oid = repo._git("commit-tree", tree, data=message.encode(),
                        extra_env=identity).stdout.decode("ascii").strip()
        refs.append((rollback.PREFIX + checkpoint_id, oid))
    repo._git("update-ref", "--stdin",
              data="".join(f"create {ref} {oid}\n" for ref, oid in refs).encode())


def test_hook_bounds_a_huge_keep_beyond_the_default_window(repo, monkeypatch, capsys, tmp_path):
    """LOCAL-R2-RET-01: with more checkpoints than the default window, `keep=10**30` must
    retain the default 50 (its own checkpoint included), not everything on the branch."""
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(write_config(
        tmp_path, {"rollback": {"auto_prune": {"keep": 10 ** 30}}})))
    seed_checkpoints(repo, 51)
    assert len(rollback.Repository(str(repo.root)).checkpoints()) == 51

    code, captured = run_hook(repo, "keepme", monkeypatch, capsys)
    assert code == 0 and captured.out == "" and captured.err == ""

    kept = rollback.Repository(str(repo.root)).checkpoints()
    assert len(kept) == 50
    assert kept[0].run_id == "keepme"  # the checkpoint the hook just made survives


def test_load_settings_never_raises_for_an_unusable_policy():
    settings = rollback.load_settings(
        {"rollback": {"auto_prune": {"keep": "lots", "older_than": "9999999999d"},
                      "warn_mb": "big", "warn_file_mb": 1e400}})
    assert settings.auto_prune and settings.auto_prune_keep == 50
    assert settings.auto_prune_older_than is None
    assert settings.warn_mb == 500.0 and settings.warn_file_mb == 50.0


# ---------------------------------------------------------------- size warning


def test_checkpoint_size_warning_above_the_user_thresholds(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(write_config(
        tmp_path, {"rollback": {"warn_mb": 5, "warn_file_mb": 3}})))
    (repo.root / "big.bin").write_bytes(b"x" * (4 * 1024 * 1024))
    (repo.root / "second.bin").write_bytes(b"y" * (3 * 1024 * 1024))
    assert main(["checkpoint", "--cwd", str(repo.root)]) == 0
    captured = capsys.readouterr()
    assert rollback.ID_RE.fullmatch(captured.out.strip())
    assert "warning:" not in captured.out
    warning = captured.err.strip()
    assert warning.startswith("warning: this checkpoint holds 7.0 MB in ")
    assert "files (largest: big.bin 4.0 MB)" in warning
    assert warning.endswith("Add big or generated folders to .gitignore.")


def test_no_size_warning_for_a_small_checkpoint(repo, capsys):
    assert main(["checkpoint", "--cwd", str(repo.root)]) == 0
    assert capsys.readouterr().err == ""


def test_default_file_threshold_warns_on_one_large_file(repo, capsys):
    (repo.root / "big.bin").write_bytes(b"x" * (60 * 1024 * 1024))
    assert main(["checkpoint", "--cwd", str(repo.root)]) == 0
    captured = capsys.readouterr()
    assert "warning:" in captured.err and "big.bin 60.0 MB" in captured.err
    assert "warning:" not in captured.out


def test_default_total_threshold_ignores_files_each_under_the_file_limit(repo, capsys):
    for index in range(6):
        (repo.root / f"part{index}.bin").write_bytes(b"x" * (10 * 1024 * 1024))
    assert main(["checkpoint", "--cwd", str(repo.root)]) == 0
    assert capsys.readouterr().err == ""


def test_size_threshold_zero_switches_the_warning_off(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(write_config(
        tmp_path, {"rollback": {"warn_mb": 0, "warn_file_mb": 0}})))
    (repo.root / "big.bin").write_bytes(b"x" * (60 * 1024 * 1024))
    assert main(["checkpoint", "--cwd", str(repo.root)]) == 0
    assert capsys.readouterr().err == ""


def test_hook_size_warning_is_stderr_only_and_once_per_session(repo, monkeypatch, capsys,
                                                               tmp_path):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(write_config(
        tmp_path, {"rollback": {"warn_mb": 5, "warn_file_mb": 3}})))
    (repo.root / "big.bin").write_bytes(b"x" * (6 * 1024 * 1024))
    first_code, first = run_hook(repo, "s1", monkeypatch, capsys)
    assert first_code == 0 and first.out == "" and ".gitignore" in first.err
    second_code, second = run_hook(repo, "s1", monkeypatch, capsys)
    assert second_code == 0 and second.out == "" and second.err == ""
    third_code, third = run_hook(repo, "s2", monkeypatch, capsys)
    assert third_code == 0 and third.out == "" and ".gitignore" in third.err


def test_hook_size_warning_repeats_after_the_marker_expires(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(write_config(
        tmp_path, {"rollback": {"warn_mb": 5, "warn_file_mb": 3}})))
    (repo.root / "big.bin").write_bytes(b"x" * (6 * 1024 * 1024))
    assert run_hook(repo, "s1", monkeypatch, capsys)[1].err != ""
    marker = repo.root / ".runledger" / ".size-warned"
    old = json.loads(marker.read_text(encoding="utf-8"))
    old["s1"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    marker.write_text(json.dumps(old), encoding="utf-8")
    assert run_hook(repo, "s1", monkeypatch, capsys)[1].err != ""


def test_a_project_config_cannot_change_retention_or_the_warning(repo, monkeypatch, capsys):
    (repo.root / ".runledger.json").write_text(
        json.dumps({"rollback": {"auto_prune": {"keep": 1}, "warn_mb": 1, "warn_file_mb": 1}}),
        encoding="utf-8")
    (repo.root / "small.bin").write_bytes(b"x" * 1024)
    for index in range(3):
        code, captured = run_hook(repo, f"s{index}", monkeypatch, capsys)
        assert code == 0 and captured.out == "" and captured.err == ""
    assert len(rollback.Repository(str(repo.root)).checkpoints()) == 3


def warning_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(write_config(
        tmp_path, {"rollback": {"warn_mb": 5, "warn_file_mb": 3}})))


def test_hook_never_writes_the_warning_marker_through_a_symlink(repo, monkeypatch, capsys,
                                                                tmp_path):
    """`.runledger/.size-warned` is repository content: a checkpoint hook must not follow a
    symlink there and truncate whatever it points at (LOCAL-RET-01)."""
    warning_thresholds(tmp_path, monkeypatch)
    (repo.root / "big.bin").write_bytes(b"x" * (6 * 1024 * 1024))
    untouched = repo.root / "unrelated.txt"
    untouched.write_text("unrelated data must survive\n", encoding="utf-8")
    folder = repo.root / ".runledger"
    folder.mkdir()
    marker = folder / ".size-warned"
    try:
        marker.symlink_to(untouched)
    except OSError:
        pytest.skip("Symlinks unavailable")
    code, captured = run_hook(repo, "s1", monkeypatch, capsys)
    assert code == 0 and captured.out == "" and ".gitignore" in captured.err
    assert untouched.read_text(encoding="utf-8") == "unrelated data must survive\n"
    assert marker.is_symlink()


def test_hook_does_not_create_the_target_of_a_redirected_marker(repo, monkeypatch, capsys,
                                                                tmp_path):
    warning_thresholds(tmp_path, monkeypatch)
    (repo.root / "big.bin").write_bytes(b"x" * (6 * 1024 * 1024))
    outside = tmp_path / "outside"
    outside.mkdir()
    targeted = outside / "created-by-the-hook.txt"
    folder = repo.root / ".runledger"
    folder.mkdir()
    try:
        (folder / ".size-warned").symlink_to(targeted)
    except OSError:
        pytest.skip("Symlinks unavailable")
    code, captured = run_hook(repo, "s1", monkeypatch, capsys)
    assert code == 0 and captured.out == ""
    assert not targeted.exists()
    assert list(outside.iterdir()) == []


def test_hook_never_writes_the_marker_through_a_symlinked_folder(repo, monkeypatch, capsys,
                                                                 tmp_path):
    """A redirect at `.runledger` itself is refused too, so the marker cannot land outside the
    repository (LOCAL-RET-01)."""
    warning_thresholds(tmp_path, monkeypatch)
    (repo.root / "big.bin").write_bytes(b"x" * (6 * 1024 * 1024))
    outside = tmp_path / "outside"
    outside.mkdir()
    untouched = outside / ".size-warned"
    untouched.write_text("outside marker stays\n", encoding="utf-8")
    link = repo.root / ".runledger"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    code, captured = run_hook(repo, "s1", monkeypatch, capsys)
    assert code == 0 and captured.out == ""
    assert untouched.read_text(encoding="utf-8") == "outside marker stays\n"
    assert [path.name for path in outside.iterdir()] == [".size-warned"]
    assert link.is_symlink()
