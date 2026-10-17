"""Local Git checkpoints. No HEAD, real-index, branch, or stash writes."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

PREFIX = "refs/runledger/checkpoints/"
ID_RE = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8}\Z")
HOOK_COMMAND = "runledger checkpoint --hook"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit")
_PYTHON_RE = re.compile(r"python(?:w|[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?\Z", re.I)


class CheckpointError(RuntimeError):
    """A safe-to-display checkpoint or rollback failure."""


class NotAGitRepository(CheckpointError):
    """Raised when the target directory is not inside a Git working tree."""


def _unsupported_reparse_point(path: Path, info: os.stat_result) -> bool:
    """Detect directory junctions without following them, on old and new Python versions."""
    if stat.S_ISLNK(info.st_mode):
        return False  # Git can save symlinks; callers must still avoid walking through them.
    if (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or getattr(info, "st_reparse_tag", 0)):
        return True
    is_junction = getattr(os.path, "isjunction", None)  # Python 3.12+, absent on 3.9
    return bool(is_junction and is_junction(path))


def _repo_discovery_error(result: subprocess.CompletedProcess) -> CheckpointError:
    """Only a directory that is not in a repository is a silent, expected outcome."""
    stderr = (result.stderr or b"").decode("utf-8", "replace")
    if "not a git repository" in stderr.casefold():
        return NotAGitRepository("Not a Git working tree. Use --cwd DIR inside a Git repository.")
    detail = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    if detail.casefold().startswith("fatal:"):
        detail = detail[len("fatal:"):].strip()
    return CheckpointError("Git could not read this repository: " + (detail or "no reason reported"))


def _split_ref_records(data: bytes) -> Optional[List[Tuple[bytes, ...]]]:
    """Split a for-each-ref stream that carries the declared size of every ref's contents.

    Each record is refname, objectname, objecttype, symref, tree, contents size and the
    contents, NUL-separated and terminated by the record separator. Reading the contents by
    their declared size keeps a separator inside a commit message inside its own record.
    Returns None when the stream is not framed as expected, so callers can read refs one by one.
    """
    records: List[Tuple[bytes, ...]] = []
    pos, end = 0, len(data)
    while pos < end:
        while pos < end and data[pos:pos + 1] in (b"\r", b"\n"):
            pos += 1
        if pos >= end:
            break
        fields = []
        for _ in range(6):
            stop = data.find(b"\x00", pos)
            if stop < 0:
                return None
            fields.append(data[pos:stop])
            pos = stop + 1
        body = b""
        if fields[5]:
            if not fields[5].isdigit():
                return None
            size = int(fields[5])
            body = data[pos:pos + size]
            if len(body) != size:
                return None
        if data[pos + len(body):pos + len(body) + 1] != b"\x1e":
            return None
        pos += len(body) + 1
        records.append(tuple(fields[:5]) + (body,))
    return records


@dataclass
class Checkpoint:
    id: str
    oid: str
    tree: str
    created_at: str
    label: str
    run_id: Optional[str]
    kind: str = "checkpoint"

    @property
    def ref(self) -> str:
        return PREFIX + self.id


@dataclass
class Plan:
    checkpoint: Checkpoint
    current_tree: str
    restore: List[str]
    remove: List[str]
    keep_ignored: List[str]


class Repository:
    def __init__(self, cwd: Optional[str] = None):
        self.root = Path(cwd or os.getcwd()).expanduser().resolve()
        self._trees: Dict[str, Dict[str, Tuple[str, str]]] = {}
        self.env = dict(os.environ)
        # An inherited alternate index/worktree must not redirect an explicit --cwd.
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                     "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX",
                     "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS",
                     "GIT_ICASE_PATHSPECS"):
            self.env.pop(name, None)
        self.env["GIT_OPTIONAL_LOCKS"] = "0"
        result = self._git("rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir",
                           check=False, extra_env={"LC_ALL": "C", "LANG": "C"})
        if result.returncode:
            raise _repo_discovery_error(result)
        lines = [ln.rstrip("\r") for ln in os.fsdecode(result.stdout).rstrip("\r\n").split("\n")]
        if len(lines) == 3 and all(lines):
            top, abs_git_dir, common_git_dir = lines
            self.root = Path(top).resolve()
            self.git_dirs = [(self.root / Path(abs_git_dir)).resolve(),
                             (self.root / Path(common_git_dir)).resolve()]
        else:
            self.root = Path(os.fsdecode(self._git("rev-parse", "--show-toplevel").stdout).rstrip("\r\n")).resolve()
            self.git_dirs = []
            for flag in ("--absolute-git-dir", "--git-common-dir"):
                folder = Path(os.fsdecode(self._git("rev-parse", flag).stdout).rstrip("\r\n"))
                self.git_dirs.append((self.root / folder).resolve())
        if self._git("config", "--bool", "--get", "core.sparseCheckout", check=False).stdout.strip() == b"true":
            raise CheckpointError("Sparse checkouts are not supported; use a full working tree.")

    def _git(self, *args: str, index: Optional[str] = None,
             data: Optional[bytes] = None, check: bool = True,
             extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        if index is not None:
            env["GIT_INDEX_FILE"] = index
        options = ["-c", "core.splitIndex=false", "-c", "core.fsmonitor=false"] if index else []
        try:
            result = subprocess.run(["git", "-C", str(self.root), *options, *args], input=data,
                                    capture_output=True, env=env, timeout=120)
        except FileNotFoundError:
            raise CheckpointError("Git is not installed, or the working directory does not exist.") from None
        except (OSError, subprocess.SubprocessError):
            raise CheckpointError("Git could not finish the checkpoint operation.") from None
        if check and result.returncode:
            # Do not echo arbitrary filter output, which may contain file content.
            raise CheckpointError(f"Git {args[0]} failed. Check repository permissions, "
                                  "Git filters, and whether another Git operation is running.")
        return result

    def _checkpoint_identity(self) -> Dict[str, str]:
        """Fill missing identities only for commit-tree, without changing Git config."""
        env = {}
        for role in ("AUTHOR", "COMMITTER"):
            if self._git("var", f"GIT_{role}_IDENT", check=False).returncode == 0:
                continue
            for field, fallback in (("NAME", "RunLedger"), ("EMAIL", "runledger@localhost")):
                key = f"GIT_{role}_{field}"
                value = self.env.get(key)
                if value is None:
                    for section in (role.lower(), "user"):
                        result = self._git("config", "--get", f"{section}.{field.lower()}",
                                           check=False)
                        if result.returncode == 0:
                            value = os.fsdecode(result.stdout).strip()
                            break
                if value is None and field == "EMAIL":
                    value = self.env.get("EMAIL")
                env[key] = value or fallback
        return env

    @contextmanager
    def _index(self, tree: Optional[str] = None) -> Iterator[str]:
        with tempfile.TemporaryDirectory(prefix="runledger-index-") as folder:
            index = str(Path(folder) / "index")
            if tree is not None:
                self._git("read-tree", tree, index=index)
            else:
                source = os.fsdecode(self._git("rev-parse", "--git-path", "index").stdout).rstrip("\r\n")
                source_path = Path(source)
                if not source_path.is_absolute():
                    source_path = self.root / source_path
                if source_path.exists():
                    shutil.copyfile(source_path, index)
                else:
                    self._git("read-tree", "--empty", index=index)
            yield index

    def snapshot_tree(self) -> str:
        """Capture tracked and non-ignored untracked files using only a temporary index."""
        with self._index() as index:
            if os.name == "nt":
                self._check_reparse_points(index)
            tracked = self._git("ls-files", "-z", index=index).stdout
            if tracked:
                self._git("update-index", "--no-assume-unchanged", "-z", "--stdin",
                          index=index, data=tracked)
                self._git("update-index", "--no-skip-worktree", "-z", "--stdin",
                          index=index, data=tracked)
            self._git("add", "-A", "--", ".", index=index)
            tree = self._git("write-tree", index=index).stdout.decode("ascii").strip()
        self.entries(tree)  # reject submodules and unsafe names before publishing any ref
        return tree

    def _check_reparse_points(self, index: str) -> None:
        # Git may traverse NTFS junctions as directories. Refuse them before staging contents.
        names = self._git("ls-files", "--cached", "--others", "--exclude-standard", "-z",
                          index=index).stdout
        checked = set()
        for raw in names.split(b"\0"):
            if not raw:
                continue
            path = self._path(os.fsdecode(raw))
            chain = [*reversed(list(path.parents)), path]
            for candidate in chain:
                if candidate == self.root or self.root not in candidate.parents:
                    continue
                if candidate in checked:
                    continue
                try:
                    mode = candidate.lstat()
                except (FileNotFoundError, NotADirectoryError):
                    break
                if stat.S_ISLNK(mode.st_mode):
                    break
                if _unsupported_reparse_point(candidate, mode):
                    raise CheckpointError("Windows junctions and other non-symlink reparse points "
                                          "are not supported.")
                checked.add(candidate)

    def _path(self, name: str) -> Path:
        parts = name.split("/")
        if (not name or "\\" in name or any(
                p in ("", ".", "..") or p.rstrip(". ").casefold() in (".git", "git~1", ".git~1")
                for p in parts)):
            raise CheckpointError("Checkpoint contains an unsafe path.")
        if os.name == "nt":
            reserved = {"con", "prn", "aux", "nul"}
            reserved.update(f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10))
            if any(any(c in '<>:"|?*' or ord(c) < 32 for c in p) or p != p.rstrip(". ") or
                   p.split(".")[0].casefold() in reserved for p in parts):
                raise CheckpointError("Checkpoint contains a path unsupported on Windows.")
        path = self.root.joinpath(*parts)
        if any(path == folder or folder in path.parents for folder in self.git_dirs):
            raise CheckpointError("Checkpoint path overlaps Git's internal directory.")
        return path

    def entries(self, tree: str) -> Dict[str, Tuple[str, str]]:
        """Read a tree once per repository; tree objects never change."""
        cached = self._trees.get(tree)
        if cached is not None:
            return cached
        entries = {}
        for record in self._git("ls-tree", "-r", "-z", "--full-tree", tree).stdout.split(b"\0"):
            if not record:
                continue
            header, raw_path = record.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split()
            if kind != "blob" or mode not in ("100644", "100755", "120000"):
                raise CheckpointError("Submodules and nested Git repositories are not supported.")
            name = os.fsdecode(raw_path)
            self._path(name)
            entries[name] = (mode, oid)
        if os.name == "nt" and len({p.casefold() for p in entries}) != len(entries):
            raise CheckpointError("Checkpoint contains case-colliding Windows paths.")
        self._trees[tree] = entries
        return entries

    def create(self, label: str = "", run_id: Optional[str] = None, *,
               tree: Optional[str] = None, kind: str = "checkpoint") -> Checkpoint:
        tree = tree or self.snapshot_tree()
        self.entries(tree)
        now = datetime.now(timezone.utc)
        checkpoint_id = now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        metadata = dict(version=1, id=checkpoint_id, created_at=now.isoformat(),
                        label=label, run_id=run_id, kind=kind)
        message = ("RunLedger checkpoint\n\n" + json.dumps(metadata, ensure_ascii=True) + "\n").encode()
        args = ["commit-tree", tree]
        head = self._git("rev-parse", "--verify", "HEAD", check=False)
        if head.returncode == 0:
            args += ["-p", head.stdout.decode("ascii").strip()]
        oid = self._git(*args, data=message,
                        extra_env=self._checkpoint_identity()).stdout.decode("ascii").strip()
        checkpoint = Checkpoint(checkpoint_id, oid, tree, metadata["created_at"], label, run_id, kind)
        # Empty old value means "create only", never overwrite an existing ref.
        self._git("update-ref", "--no-deref", checkpoint.ref, oid, "")
        return checkpoint

    @staticmethod
    def _parse_checkpoint_body(checkpoint_id: str, oid: str, tree: str, body: bytes) -> Optional[Checkpoint]:
        if b"\x1e" in body:
            # The record separator frames the bulk listing, so a body may never contain it.
            return None
        try:
            text = body.decode("utf-8").replace("\r\n", "\n")
            title, metadata = text.split("\n\n", 1)
            meta = json.loads(metadata)
            if (title != "RunLedger checkpoint" or meta["version"] != 1
                    or meta["id"] != checkpoint_id
                    or not isinstance(meta["label"], str)
                    or not isinstance(meta["created_at"], str)
                    or not (meta["run_id"] is None or isinstance(meta["run_id"], str))
                    or meta["kind"] not in ("checkpoint", "safety")):
                return None
            return Checkpoint(checkpoint_id, oid, tree, meta["created_at"],
                              meta["label"], meta["run_id"], meta["kind"])
        except (ValueError, KeyError, TypeError, UnicodeError):
            return None

    @staticmethod
    def _checkpoint_from_ref(ref: str, oid: str, tree: str, body: bytes) -> Optional[Checkpoint]:
        if not ref.startswith(PREFIX) or not oid or not tree:
            return None
        checkpoint_id = ref[len(PREFIX):]
        if not ID_RE.fullmatch(checkpoint_id):
            return None
        return Repository._parse_checkpoint_body(checkpoint_id, oid, tree, body)

    def checkpoints(self) -> List[Checkpoint]:
        result = []
        fmt = ("%(refname)%00%(objectname)%00%(objecttype)%00%(symref)%00%(tree)"
               "%00%(contents:size)%00%(contents)%1e")
        probe = self._git("for-each-ref", f"--format={fmt}", PREFIX, check=False)
        records = _split_ref_records(probe.stdout) if probe.returncode == 0 else None
        if records is not None:
            for ref_b, oid_b, obj_type_b, symref_b, tree_b, contents_b in records:
                if obj_type_b != b"commit" or symref_b:
                    continue  # symbolic refs must never redirect a rollback/prune to a branch
                try:
                    ref = os.fsdecode(ref_b)
                    oid = oid_b.decode("ascii").strip()
                    tree = tree_b.decode("ascii").strip()
                except UnicodeError:
                    continue
                cp = self._checkpoint_from_ref(ref, oid, tree, contents_b)
                if cp is not None:
                    result.append(cp)
            return sorted(result, key=lambda cp: cp.id, reverse=True)
        refs = self._git("for-each-ref",
                         "--format=%(refname) %(objectname) %(objecttype) %(symref)", PREFIX).stdout
        for line in os.fsdecode(refs).splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[2] != "commit":
                continue  # symbolic refs must never redirect a rollback/prune to a branch
            ref, oid, _ = fields
            raw = self._git("cat-file", "commit", oid).stdout
            try:
                header, body = raw.split(b"\n\n", 1)
                tree = header.splitlines()[0].decode("ascii").removeprefix("tree ")
            except (ValueError, IndexError, UnicodeError):
                continue
            cp = self._checkpoint_from_ref(ref, oid, tree, body)
            if cp is not None:
                result.append(cp)
        return sorted(result, key=lambda cp: cp.id, reverse=True)

    def select(self, checkpoint_id: Optional[str] = None, *,
               run_id: Optional[str] = None) -> Checkpoint:
        checkpoints = self.checkpoints()
        if checkpoint_id is not None:
            checkpoints = [cp for cp in checkpoints if checkpoint_id in (cp.id, cp.ref)]
        elif run_id is not None:
            # The earliest checkpoint returns to the start of the whole run, not its last prompt.
            checkpoints = sorted((cp for cp in checkpoints if cp.run_id == run_id
                                  and cp.kind == "checkpoint"), key=lambda cp: cp.id)
        if not checkpoints:
            raise CheckpointError("No matching checkpoint found. Run 'runledger checkpoints' to list them.")
        return checkpoints[0]

    def _ignored(self, names: List[str]) -> set:
        if not names:
            return set()
        out = self._git("check-ignore", "--no-index", "-z", "--stdin", check=False,
                        data=b"".join(os.fsencode(p) + b"\0" for p in names))
        if out.returncode not in (0, 1):
            raise CheckpointError("Could not check ignored paths; nothing was restored.")
        return {os.fsdecode(p) for p in out.stdout.split(b"\0") if p}

    def plan(self, checkpoint: Checkpoint, current_tree: Optional[str] = None) -> Plan:
        current_tree = current_tree or self.snapshot_tree()
        current, target = self.entries(current_tree), self.entries(checkpoint.tree)
        restore = sorted(p for p in target if current.get(p) != target[p])
        extra = set(current) - set(target)
        ignored = self._ignored(sorted(extra))
        return Plan(checkpoint, current_tree, restore, sorted(extra - ignored), sorted(ignored))

    def _preflight(self, plan: Plan, current: Dict[str, Tuple[str, str]]) -> List[str]:
        removing = set(plan.remove) | (set(plan.restore) & set(current))
        for name in plan.restore + plan.remove:
            path = self._path(name)
            # Never walk through an existing symlink, even when it points inside the repo.
            parent = self.root
            blocked = False
            for part in path.relative_to(self.root).parts[:-1]:
                parent = parent / part
                try:
                    mode = parent.lstat()
                except FileNotFoundError:
                    break
                if (not stat.S_ISDIR(mode.st_mode) or
                        _unsupported_reparse_point(parent, mode)):
                    relative = parent.relative_to(self.root).as_posix()
                    if relative not in removing:
                        raise CheckpointError(f"Unsafe parent blocks rollback: {relative!r}")
                    blocked = True
                    break
            if blocked:
                continue
            try:
                mode = path.lstat()
            except FileNotFoundError:
                continue
            if _unsupported_reparse_point(path, mode):
                raise CheckpointError(f"Windows reparse point blocks rollback: {name!r}")
            if stat.S_ISDIR(mode.st_mode):
                for folder, dirs, files in os.walk(path, followlinks=False):
                    for child in files + dirs:
                        candidate = Path(folder) / child
                        relative = candidate.relative_to(self.root).as_posix()
                        self._path(relative)
                        mode = candidate.lstat()
                        if _unsupported_reparse_point(candidate, mode):
                            raise CheckpointError(f"Windows reparse point blocks rollback: {relative!r}")
                        if not stat.S_ISDIR(mode.st_mode):
                            if relative not in removing:
                                raise CheckpointError(f"Ignored or unsupported file blocks rollback: {relative!r}")
            elif name not in current:
                raise CheckpointError(f"Ignored or unsupported file blocks rollback: {name!r}")
        return sorted(removing, key=lambda p: (p.count("/"), p), reverse=True)

    def rollback(self, plan: Plan) -> Checkpoint:
        """Refuse stale previews, then save an undo checkpoint before the first worktree write."""
        if self.snapshot_tree() != plan.current_tree:
            raise CheckpointError("Working tree changed since the preview. Retry rollback.")
        current = self.entries(plan.current_tree)
        removing = self._preflight(plan, current)
        safety = self.create(label=f"Before rollback {plan.checkpoint.id}",
                             tree=plan.current_tree, kind="safety")
        try:
            with self._index(plan.checkpoint.tree) as index:
                for name in removing:
                    path = self._path(name)
                    if os.path.lexists(path):
                        mode = path.lstat().st_mode
                        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                            raise CheckpointError(f"Unsupported file blocks rollback: {name!r}")
                        path.unlink()
                    # Only remove empty directories, never recursively delete a directory.
                    parent = path.parent
                    while parent != self.root:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                for name in plan.restore:
                    path = self._path(name)
                    if path.is_dir() and not path.is_symlink():
                        # Empty directory at a file's destination.
                        path.rmdir()
                if plan.restore:
                    self._git("checkout-index", "--force", "-z", "--stdin", index=index,
                              data=b"".join(os.fsencode(p) + b"\0" for p in plan.restore))
        except (CheckpointError, OSError, KeyboardInterrupt) as exc:
            raise CheckpointError(f"Rollback stopped: {exc}. Safety checkpoint: {safety.id}. "
                                  f"Undo: runledger rollback {safety.id}") from None
        return safety

    def prune(self, keep: int) -> List[Checkpoint]:
        if keep < 0:
            raise CheckpointError("--keep must be zero or greater.")
        removed = self.checkpoints()[keep:]
        for checkpoint in removed:
            self._git("update-ref", "--no-deref", "-d", checkpoint.ref, checkpoint.oid)
        return removed


_DEFAULT_SELECT = Repository.select


def checkpoint_for_run(cwd: Optional[str], run_id: str) -> Optional[str]:
    """Read-only lookup; missing Git or pruned checkpoints must not break receipts."""
    if not cwd or not run_id or not os.path.isabs(cwd):
        return None
    if Repository.select is _DEFAULT_SELECT:
        from .gitinfo import toplevel
        top = toplevel(cwd)
        if not top:
            return None
        dot_git = Path(top) / ".git"
        if (dot_git.is_dir()
                and not (dot_git / "refs" / "runledger" / "checkpoints").exists()
                and not (dot_git / "reftable").exists()):
            packed = dot_git / "packed-refs"
            try:
                has_packed = packed.is_file() and b"refs/runledger/checkpoints/" in packed.read_bytes()
            except OSError:
                has_packed = True
            if not has_packed:
                return None
    try:
        return Repository(cwd).select(run_id=run_id).id
    except (CheckpointError, OSError, ValueError):
        return None


def is_checkpoint_hook(hook) -> bool:
    if (not isinstance(hook, dict) or hook.get("type") != "command"
            or not isinstance(hook.get("command"), str)):
        return False
    try:
        # Keep Windows backslashes, and accept quoted executable paths on either platform.
        parts = shlex.split(hook["command"], posix=False)
    except ValueError:
        return False
    parts = [p[1:-1] if len(p) >= 2 and p[0] in ("'", '"') and p[-1] == p[0] else p
             for p in parts]
    if not parts or parts[-2:] != ["checkpoint", "--hook"]:
        return False
    executable = parts[0].replace("\\", "/").rsplit("/", 1)[-1]
    # Match only our complete command, never an echo, pipeline, or compound user hook.
    return ((len(parts) == 3 and executable.casefold() in ("runledger", "runledger.exe"))
            or (len(parts) == 5 and _PYTHON_RE.fullmatch(executable) is not None
                and parts[1:3] == ["-m", "runledger"]))


def hook_main() -> int:
    """Lifecycle hook: no stdout/context injection, no prompt content stored, never blocks."""
    try:
        event = json.loads(sys.stdin.read())
        if not isinstance(event, dict) or event.get("hook_event_name") not in HOOK_EVENTS:
            return 0
        cwd, run_id = event.get("cwd"), event.get("session_id")
        if not isinstance(cwd, str) or not cwd or not isinstance(run_id, str) or not run_id:
            return 0
        Repository(cwd).create(label=event["hook_event_name"], run_id=run_id)
    except NotAGitRepository:
        return 0
    except (CheckpointError, OSError, ValueError):
        print("RunLedger checkpoint unavailable. Run 'runledger checkpoint' manually for details.",
              file=sys.stderr)
    return 0
