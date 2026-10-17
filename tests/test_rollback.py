"""All worktree writes happen in disposable repositories, never the source checkout."""
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from runledger import guard, rollback
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
    checkpoints = [first, second, safety]
    real_git = repo._git
    deleted = []

    def run(*args, **kwargs):
        if args[0] == "for-each-ref":
            lines = [f"{cp.ref} {cp.oid} commit " for cp in checkpoints]
            lines += [f"{rollback.PREFIX}20261015T150000000000Z-00000000 {first.oid} commit refs/heads/main",
                      f"{rollback.PREFIX}unknown {'4' * 40} commit ",
                      f"{rollback.PREFIX}20261015T150000000000Z-00000001 {first.tree} tree "]
            output = "\n".join(lines).encode()
        elif args[0] == "cat-file":
            cp = next(cp for cp in checkpoints if cp.oid == args[-1])
            meta = dict(version=1, id=cp.id, label=cp.label, created_at=cp.created_at,
                        run_id=cp.run_id, kind=cp.kind)
            output = (f"tree {cp.tree}\n\nRunLedger checkpoint\n\n" + json.dumps(meta)).encode()
        elif args[0] == "update-ref":
            deleted.append(args)
            output = b""
        else:
            return real_git(*args, **kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(repo, "_git", run)
    assert repo.checkpoints() == [safety, second, first]
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


def test_hook_failure_is_nonblocking(repo, monkeypatch, capsys):
    (repo.root / ".git" / "refs" / "runledger").write_text("blocker\n", encoding="utf-8")
    event = dict(cwd=str(repo.root), session_id="s", hook_event_name="SessionStart")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main(["checkpoint", "--hook"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "checkpoint unavailable" in out.err


def test_checkpoints_listing_spawn_count_constant(repo, monkeypatch):
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

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(rollback.subprocess, "run", spy_run)
    listed = rollback.Repository(str(repo.root)).checkpoints()
    assert [cp.id for cp in listed] == [cp.id for cp in reversed(created)]
    assert len(calls) <= 3



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
