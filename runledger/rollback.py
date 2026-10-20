"""Local Git checkpoints. No HEAD, real-index, branch, or stash writes."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import redirects
from .paths import is_abs, owned_hook_command
from .userconfig import load_user_config

PREFIX = "refs/runledger/checkpoints/"
ID_RE = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8}\Z")
HOOK_COMMAND = "runledger checkpoint --hook"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit")
DURATION_RE = re.compile(r"(\d+)([dwh])\Z", re.I)
DURATION_SECONDS = {"d": 86400, "w": 604800, "h": 3600}
DURATION_LIMIT_SECONDS = 365000 * 86400  # a thousand years: past this a value is a typo
# The largest `keep` the hook accepts: past this the value is a typo rather than a retention
# policy, and the documented default applies instead (LOCAL-R2-RET-01).
MAX_PRUNE_KEEP = 100000
TZ_RE = re.compile(r"[+-][0-9]{4}\Z")


def parse_duration(text: Any) -> Optional[timedelta]:
    """`30d`, `2w` or `6h` as a timedelta; None for anything else (never raises). A value past
    the retention ceiling is refused rather than kept, so a misread setting can only fall back
    to the documented default and never overflow the age arithmetic."""
    if not isinstance(text, str):
        return None
    m = DURATION_RE.match(text.strip())
    if not m:
        return None
    digits = m.group(1)
    if len(digits) > 12:  # the ceiling is 11 digits of seconds; never build a huge int
        return None
    seconds = int(digits) * DURATION_SECONDS[m.group(2).lower()]
    if seconds > DURATION_LIMIT_SECONDS:
        return None
    return timedelta(seconds=seconds)


@dataclass
class Settings:
    """The `rollback` section of the user's config, with the documented defaults:
    auto-prune on, keeping the newest 50 checkpoints and no age limit, and size warnings
    at 500 MB in total or 50 MB for a single file (a threshold of 0 switches it off)."""
    auto_prune: bool = True
    auto_prune_keep: int = 50
    auto_prune_older_than: Optional[timedelta] = None
    warn_mb: float = 500.0
    warn_file_mb: float = 50.0


def _positive_int(value: Any) -> Optional[int]:
    """A whole number the hook can act on: at least 1 and at most `MAX_PRUNE_KEEP`. Anything
    else - a wrong type, a negative value or one past the ceiling - is unusable and keeps the
    documented default instead of silently retaining an arbitrary number of checkpoints."""
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_PRUNE_KEEP:
        return value
    return None


def _size_mb(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number < 0 or number == float("inf"):
        return None
    return number


WARN_MARKER = Path(".runledger") / ".size-warned"
WARN_WINDOW = timedelta(hours=24)
SIZE_SCAN_LIMIT = 200000


def _human_bytes(size: int) -> str:
    """A size a person reads: `512 bytes`, `4.2 MB`, `1.2 GB`."""
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} bytes"


def size_warning_text(files: int, total: int, largest: Tuple[str, int],
                      settings: Settings) -> str:
    """The warning one checkpoint tree deserves, or "" while it is under the thresholds.
    A threshold of 0 switches that half of the check off."""
    largest_path, largest_size = largest
    over_total = settings.warn_mb > 0 and total > settings.warn_mb * 1024 * 1024
    over_file = settings.warn_file_mb > 0 and largest_size > settings.warn_file_mb * 1024 * 1024
    if not (over_total or over_file):
        return ""
    return (f"warning: this checkpoint holds {_human_bytes(total)} in {files:,} files "
            f"(largest: {largest_path} {_human_bytes(largest_size)}). "
            "Add big or generated folders to .gitignore.")


def checkpoint_age(checkpoint: "Checkpoint") -> Optional[datetime]:
    """When a checkpoint was made, read from Git's own commit date. The metadata copy is the
    fallback for a checkpoint built in a test or written by an older RunLedger."""
    if checkpoint.committed_at is not None:
        return checkpoint.committed_at
    try:
        parsed = datetime.fromisoformat(checkpoint.created_at)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parsed_time(value: str) -> Optional[datetime]:
    """An ISO timestamp that carries its own timezone, or None: a naive one has no instant to
    compare against."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def load_settings(user: Optional[Dict[str, Any]] = None) -> Settings:
    """The `rollback` user settings. Only the user's own file counts: a project
    `.runledger.json` must never be able to trigger deletions. A value that is not exactly
    the documented shape keeps the default instead of being guessed. `user` is an
    already-read user config, or None to read `~/.runledger/config.json` once."""
    if user is None:
        user = load_user_config()
    settings = Settings()
    raw = user.get("rollback")
    if not isinstance(raw, dict):
        return settings
    auto = raw.get("auto_prune")
    if auto is False:
        settings.auto_prune = False
    elif isinstance(auto, dict):
        keep = _positive_int(auto.get("keep"))
        if keep is not None:
            settings.auto_prune_keep = keep
        settings.auto_prune_older_than = parse_duration(auto.get("older_than"))
    warn_mb = _size_mb(raw.get("warn_mb"))
    if warn_mb is not None:
        settings.warn_mb = warn_mb
    warn_file_mb = _size_mb(raw.get("warn_file_mb"))
    if warn_file_mb is not None:
        settings.warn_file_mb = warn_file_mb
    return settings


class CheckpointError(RuntimeError):
    """A safe-to-display checkpoint or rollback failure."""


class NotAGitRepository(CheckpointError):
    """Raised when the target directory is not inside a Git working tree."""


def _unsafe_path(name: str) -> bool:
    """The same names the checkpoint reader refuses, checked on a repo-relative POSIX path."""
    parts = name.split("/")
    if not name or "\\" in name or any(
            p in ("", ".", "..") or p.rstrip(". ").casefold() in (".git", "git~1", ".git~1")
            for p in parts):
        return True
    if os.name != "nt":
        return False
    reserved = {"con", "prn", "aux", "nul"}
    reserved.update(f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10))
    return any(any(c in '<>:"|?*' or ord(c) < 32 for c in p) or p != p.rstrip(". ") or
               p.split(".")[0].casefold() in reserved for p in parts)


def _fold_case(name: str) -> str:
    """The case mapping a volume compares names with, used for the folded retry only: the
    simple `lower` mapping, never `casefold`. NTFS and APFS keep `Stra\u00dfe` apart from
    `Strasse`, so the wider Unicode folding would restore files in a folder the value never
    named."""
    return name.lower()


def _folded_spellings(names: Iterable[str]) -> Dict[str, str]:
    """The repository's own spelling per folded key, for the folded retry. A key two different
    names share gives no canonical spelling, so it is left out: a scoped restore that cannot
    tell the two apart refuses the value instead of widening it to both."""
    folded: Dict[str, str] = {}
    ambiguous = set()
    for name in sorted(names):
        key = _fold_case(name)
        if key in folded:
            ambiguous.add(key)
        else:
            folded[key] = name
    for key in ambiguous:
        del folded[key]
    return folded


def _reparse_entries_unchanged(before: bytes, after: bytes) -> None:
    """The staged entries under a reparse point a scoped snapshot skips must be exactly the ones
    the index already had: a difference means staging read through the point and wrote what it
    found outside the repository, so the snapshot is refused instead of being pinned."""
    if after != before:
        raise CheckpointError("Staging read through a junction outside the selection. "
                              "Nothing was restored.")


def _within(name: str, prefixes: Tuple[str, ...]) -> bool:
    """A path is selected by itself, or by a folder above it. Literal (no globs), prefix-safe:
    `src` never selects `src2`. An empty prefix (the repository root, `--path .`) selects all."""
    return any(name == prefix or name.startswith(prefix + "/") or not prefix
               for prefix in prefixes)


def _on_the_way(name: str, prefixes: Tuple[str, ...]) -> bool:
    """A path and a selection are on the same vertical line: inside it, equal to it, or above
    it (`sub` is on the way to `sub/x.txt`). Used to keep a blocker in scope when it is an
    ancestor of the selected path. An empty prefix (the repository root) is the whole tree."""
    return any(not prefix or name == prefix or name.startswith(prefix + "/")
               or prefix.startswith(name + "/") for prefix in prefixes)


def _case_insensitive_volume(folder: Path) -> bool:
    """Whether the volume holding `folder` folds case. Windows is known from its own path
    rules; elsewhere the folder is asked for under a different case, so a case-sensitive
    volume (the Linux CI runners) keeps every spelling literal."""
    if os.path.normcase("A") != "A":
        return True
    for candidate in [folder, *folder.parents]:
        name = candidate.name
        variant = name.upper() if name != name.upper() else name.lower()
        if variant == name:
            continue
        probe = candidate.parent / variant
        try:
            return bool(probe.exists() and os.path.samefile(probe, candidate))
        except OSError:
            continue
    return False


def _unsupported_reparse_point(path: Path, info: os.stat_result) -> bool:
    """Detect directory junctions without following them, on old and new Python versions."""
    return redirects.unsupported_reparse_point(path, info)


def _redirects(path: Path) -> bool:
    """Whether `path` itself is a link or another reparse point. A checkpoint hook owns no part
    of the working tree, so bookkeeping must never read or write through one of those."""
    return redirects.is_redirect(path)


def _read_warn_marker(marker: Path) -> Dict[str, Any]:
    """The remembered size warnings, or nothing at all when the marker is missing, unreadable
    or a redirect the repository controls."""
    if _redirects(marker.parent) or _redirects(marker):
        return {}
    try:
        data = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_warn_marker(marker: Path, text: str) -> None:
    """Write the marker without following a redirect, and truncate only once the open file is
    known to be the marker itself. `.runledger` is repository content, so a link there must
    never turn a size warning into a write to another file."""
    folder = marker.parent
    if _redirects(folder) or _redirects(marker):
        return
    if not folder.is_dir():
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        if _redirects(folder):
            return
    fd = redirects.open_without_following(marker, os.O_WRONLY | os.O_CREAT, 0o600)
    if fd is None:
        return
    try:
        os.ftruncate(fd, 0)
        os.write(fd, text.encode("utf-8"))
    except (OSError, ValueError):
        return
    finally:
        os.close(fd)


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

    Each record is refname, objectname, objecttype, symref, tree, committer date, contents
    size and the contents, NUL-separated and terminated by the record separator. Reading the
    contents by their declared size keeps a separator inside a commit message inside its own
    record. Returns None when the stream is not framed as expected, so callers can read refs
    one by one.
    """
    records: List[Tuple[bytes, ...]] = []
    pos, end = 0, len(data)
    while pos < end:
        while pos < end and data[pos:pos + 1] in (b"\r", b"\n"):
            pos += 1
        if pos >= end:
            break
        fields = []
        for _ in range(7):
            stop = data.find(b"\x00", pos)
            if stop < 0:
                return None
            fields.append(data[pos:stop])
            pos = stop + 1
        body = b""
        if fields[6]:
            if not fields[6].isdigit():
                return None
            size = int(fields[6])
            body = data[pos:pos + size]
            if len(body) != size:
                return None
        if data[pos + len(body):pos + len(body) + 1] != b"\x1e":
            return None
        pos += len(body) + 1
        records.append(tuple(fields[:6]) + (body,))
    return records


def _commit_date(raw: bytes) -> Optional[datetime]:
    """`%(committerdate:raw)`, or a `committer` header from `cat-file commit`: the time is the
    second-to-last field, right before the timezone offset."""
    parts = raw.decode("ascii", "replace").split()
    if len(parts) < 2 or not parts[-2].isdigit() or not TZ_RE.fullmatch(parts[-1]):
        return None
    try:
        return datetime.fromtimestamp(int(parts[-2]), timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


@dataclass
class Checkpoint:
    id: str
    oid: str
    tree: str
    created_at: str
    label: str
    run_id: Optional[str]
    kind: str = "checkpoint"
    committed_at: Optional[datetime] = field(default=None, compare=False)
    """When Git recorded the commit. It is the age a stale checkpoint is pruned by, because
    that is the date `git log` and `%(committerdate)` show for the ref. Read from the ref, so
    it never takes part in identifying a checkpoint."""

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
    paths: Tuple[str, ...] = ()


class Repository:
    def __init__(self, cwd: Optional[str] = None):
        # The folder the command was invoked with: relative `--path` values are read against
        # this one, while `root` is Git's top level, which is what all names are stored as.
        self.cwd = Path(cwd or os.getcwd()).expanduser().resolve()
        self.root = self.cwd
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
        self.folded = _case_insensitive_volume(self.root)

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

    def snapshot_tree(self, scope: Optional[Tuple[str, ...]] = None) -> str:
        """Capture tracked and non-ignored untracked files using only a temporary index.
        `scope` limits the Windows reparse-point refusal to the selected paths, so a junction
        outside a `rollback --path` selection cannot stop a scoped restore; None refuses any
        unignored reparse point, which is the whole-tree behaviour. A reparse point a selection
        passes over stays out of the snapshot entirely: Git's own walk follows a junction, and
        the target's contents would be pinned in the checkpoint (LOCAL-R2-RB-01)."""
        with self._index() as index:
            blocked = self._check_reparse_points(index, scope) if os.name == "nt" else ()
            tracked = self._git("ls-files", "-z", index=index).stdout
            if tracked:
                self._git("update-index", "--no-assume-unchanged", "-z", "--stdin",
                          index=index, data=tracked)
                self._git("update-index", "--no-skip-worktree", "-z", "--stdin",
                          index=index, data=tracked)
            self._stage_worktree(index, blocked)
            tree = self._git("write-tree", index=index).stdout.decode("ascii").strip()
        self.entries(tree)  # reject submodules and unsafe names before publishing any ref
        return tree

    def _stage_worktree(self, index: str, blocked: Tuple[str, ...]) -> None:
        """`git add -A` over the working tree, with Git's own directory walk kept out of every
        reparse point a scoped snapshot has to pass over. `add` alone follows a Windows junction
        and stages what it finds outside the repository, so a pathspec exclusion prunes the walk
        and the entries under the point are read back and compared (LOCAL-R2-RB-01)."""
        if not blocked:
            self._git("add", "-A", "--", ".", index=index)
            return
        skipped = [f":(exclude,literal){path}" for path in blocked]
        literals = [f":(literal){path}" for path in blocked]
        before = self._git("ls-files", "-s", "-z", "--", *literals, index=index).stdout
        self._git("add", "-A", "--", ".", *skipped, index=index)
        after = self._git("ls-files", "-s", "-z", "--", *literals, index=index).stdout
        _reparse_entries_unchanged(before, after)

    def _check_reparse_points(self, index: str,
                              scope: Optional[Tuple[str, ...]] = None) -> Tuple[str, ...]:
        """Git may traverse NTFS junctions as directories. Refuse them before staging contents.
        With a selection the refusal covers the selected paths and their parents only; every
        other reparse point comes back as a path the staging walk must skip, so passing one over
        never means following it (LOCAL-R2-RB-01)."""
        names = self._git("ls-files", "--cached", "--others", "--exclude-standard", "-z",
                          index=index).stdout
        checked = set()
        blocked: List[str] = []
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
                    relative = candidate.relative_to(self.root).as_posix()
                    if scope is not None and not self._blocks_scope(relative, scope):
                        if relative not in blocked:
                            blocked.append(relative)
                        continue
                    raise CheckpointError("Windows junctions and other non-symlink reparse points "
                                          "are not supported.")
                checked.add(candidate)
        return tuple(blocked)

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
    def _parse_checkpoint_body(checkpoint_id: str, oid: str, tree: str, body: bytes,
                               committed_at: Optional[datetime] = None) -> Optional[Checkpoint]:
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
                              meta["label"], meta["run_id"], meta["kind"], committed_at)
        except (ValueError, KeyError, TypeError, UnicodeError):
            return None

    @staticmethod
    def _checkpoint_from_ref(ref: str, oid: str, tree: str, body: bytes,
                             committed_at: Optional[datetime] = None) -> Optional[Checkpoint]:
        if not ref.startswith(PREFIX) or not oid or not tree:
            return None
        checkpoint_id = ref[len(PREFIX):]
        if not ID_RE.fullmatch(checkpoint_id):
            return None
        return Repository._parse_checkpoint_body(checkpoint_id, oid, tree, body, committed_at)

    def snapshot_stats(self, tree: str) -> Tuple[int, int, Tuple[str, int]]:
        """`(files, bytes, (largest path, its bytes))` of one tree, from a single
        `git ls-tree -r -l -z`. Submodule entries have no blob size and are skipped.
        Read-only: it reports how much a checkpoint holds, it never writes."""
        result = self._git("ls-tree", "-r", "-l", "-z", tree)
        files = 0
        total = 0
        largest: Tuple[str, int] = ("", 0)
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            fields = meta.split()
            if len(fields) < 4 or fields[0] == b"160000":
                continue
            try:
                size = int(fields[3])
            except ValueError:
                continue
            path = os.fsdecode(name)
            files += 1
            total += size
            if size > largest[1]:
                largest = (path, size)
        return files, total, largest

    def checkpoints(self) -> List[Checkpoint]:
        result = []
        fmt = ("%(refname)%00%(objectname)%00%(objecttype)%00%(symref)%00%(tree)"
               "%00%(committerdate:raw)%00%(contents:size)%00%(contents)%1e")
        probe = self._git("for-each-ref", f"--format={fmt}", PREFIX, check=False)
        records = _split_ref_records(probe.stdout) if probe.returncode == 0 else None
        if records is not None:
            for ref_b, oid_b, obj_type_b, symref_b, tree_b, date_b, contents_b in records:
                if obj_type_b != b"commit" or symref_b:
                    continue  # symbolic refs must never redirect a rollback/prune to a branch
                try:
                    ref = os.fsdecode(ref_b)
                    oid = oid_b.decode("ascii").strip()
                    tree = tree_b.decode("ascii").strip()
                except UnicodeError:
                    continue
                cp = self._checkpoint_from_ref(ref, oid, tree, contents_b, _commit_date(date_b))
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
                line_date = next((ln for ln in header.splitlines()
                                  if ln.startswith(b"committer ")), b"")
            except (ValueError, IndexError, UnicodeError):
                continue
            cp = self._checkpoint_from_ref(ref, oid, tree, body, _commit_date(line_date))
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

    def tree_sizes(self, tree: str) -> Dict[str, int]:
        """Blob sizes of one tree in a single `git ls-tree -r -l -z`; submodules are skipped.
        Read-only, and used to count the bytes a preview is about to change."""
        sizes: Dict[str, int] = {}
        for record in self._git("ls-tree", "-r", "-l", "-z", tree).stdout.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            fields = meta.split()
            if len(fields) < 4 or fields[0] == b"160000":
                continue
            try:
                sizes[os.fsdecode(name)] = int(fields[3])
            except ValueError:
                continue
        return sizes

    def _resolve(self, raw: str) -> str:
        """One `--path` value as a repo-relative POSIX path. Relative values are read against
        the invocation folder (`--cwd`, or the current folder), not Git's top level, so
        `--cwd REPO/sub --path a.txt` means `REPO/sub/a.txt`; `.` becomes the whole tree. The
        value keeps the spelling the user typed, so a case-insensitive volume can still match
        the repository's own spelling instead of the case the filesystem reports."""
        if not isinstance(raw, str) or not raw.strip():
            raise CheckpointError("--path needs a file or folder inside the repository.")
        root = os.path.normcase(str(self.root))
        if is_abs(raw):
            resolved = os.path.normpath(raw)
        else:
            # On Windows both separators split a path; elsewhere a backslash is a file name.
            inside = raw.replace("\\", "/") if os.sep == "\\" else raw
            resolved = os.path.normpath(os.path.join(str(self.cwd), inside))
        candidate = os.path.normcase(resolved)
        if candidate == root:
            return ""
        if not candidate.startswith(root + os.sep):
            raise CheckpointError(f"path is outside the repository: {raw}")
        # Strip the root as text rather than with `relpath`, which would fold the case on a
        # case-insensitive volume and lose the spelling the user typed.
        name = resolved[len(str(self.root)) + 1:]
        return name.replace(os.sep, "/")

    @staticmethod
    def _spellings(trees: Tuple[Dict[str, Tuple[str, str]], ...]) -> set:
        """Every file and folder name the trees hold, in the repository's own spelling.
        Folders are included because `--path sub` selects `sub/...` even though only the file
        entries are stored."""
        known = set()
        for tree in trees:
            for name in tree:
                parts = name.split("/")
                for depth in range(1, len(parts) + 1):
                    known.add("/".join(parts[:depth]))
        return known

    def _selects(self, name: str, scope: Tuple[str, ...]) -> bool:
        """Whether an entry is inside one of the selected prefixes. The literal prefix is asked
        first; the folded retry uses the volume's own simple case mapping and only on a
        case-insensitive volume, so `--path SUB` selects `sub/...` while an exact `Stra\u00dfe`
        never reaches a `Strasse` beside it (LOCAL-R2-RB-02)."""
        if _within(name, scope):
            return True
        return self.folded and _within(_fold_case(name),
                                       tuple(_fold_case(prefix) for prefix in scope))

    def _blocks_scope(self, name: str, scope: Tuple[str, ...]) -> bool:
        """Whether a reparse point is part of the selection: inside it, or on the way to it.
        The fold is the volume's simple one, so a folder next to the selection is never counted
        as being on the way to it."""
        if _on_the_way(name, scope):
            return True
        return self.folded and _on_the_way(_fold_case(name),
                                           tuple(_fold_case(prefix) for prefix in scope))

    def _selected(self, paths: List[str], trees: Tuple[Dict[str, Tuple[str, str]], ...]) -> Tuple[str, ...]:
        """The repository's own spelling for every `--path` value. A value must be a file or
        folder either tree holds, or something that exists on disk. Spellings are matched
        exactly first and case-insensitively second, and only on a case-insensitive volume:
        a case-sensitive one must treat `README.md` literally."""
        known = self._spellings(trees)
        folded = _folded_spellings(known) if self.folded else {}
        selected = []
        for raw in paths:
            name = self._resolve(raw)
            spelling = name if name in known else folded.get(_fold_case(name))
            if name and spelling is None:
                # A folder that no entry starts with, and that is not on disk either, is
                # simply absent. (A folder whose entries are in a tree is a valid selection.)
                if _unsafe_path(name) or not os.path.lexists(self._path(name)):
                    raise CheckpointError(
                        f"no such path in the checkpoint or working tree: {raw}")
            if name:
                self._path(name)  # refuse `.git`, reserved names and other unsafe selections
            selected.append(spelling or name)
        return tuple(selected)

    def plan(self, checkpoint: Checkpoint, current_tree: Optional[str] = None,
             paths: Optional[List[str]] = None) -> Plan:
        """The diff between the checkpoint and the working tree. With `paths`, only entries
        inside those selections (literal file or folder prefixes) are restored or removed;
        every other entry stays untouched and the stale-preview check still covers the whole
        tree, so a change anywhere is caught. The Windows reparse-point refusal covers the
        selection alone: a blocker outside it must not stop a scoped restore."""
        if not paths:
            current_tree = current_tree or self.snapshot_tree()
            current, target = self.entries(current_tree), self.entries(checkpoint.tree)
            restore = sorted(p for p in target if current.get(p) != target[p])
            extra = set(current) - set(target)
            ignored = self._ignored(sorted(extra))
            return Plan(checkpoint, current_tree, restore, sorted(extra - ignored), sorted(ignored))
        scope = tuple(self._resolve(raw) for raw in paths)
        current_tree = current_tree or self.snapshot_tree(scope=scope)
        current, target = self.entries(current_tree), self.entries(checkpoint.tree)
        selected = self._selected(paths, (target, current))
        restore = [p for p in sorted(target) if current.get(p) != target[p]
                   and self._selects(p, selected)]
        extra = [p for p in sorted(current) if p not in target and self._selects(p, selected)]
        ignored = self._ignored(extra)
        return Plan(checkpoint, current_tree, restore, [p for p in extra if p not in ignored],
                    [], selected)

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
        """Refuse stale previews, then save an undo checkpoint before the first worktree write.
        The stale check re-reads the whole tree under the same reparse-point scope the plan
        used, so an out-of-scope junction cannot block a scoped restore here either."""
        if self.snapshot_tree(scope=plan.paths or None) != plan.current_tree:
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

    def prune(self, keep: Optional[int] = None, older_than: Optional[timedelta] = None,
              now: Optional[datetime] = None) -> List[Checkpoint]:
        """Delete checkpoint refs, newest first in the returned list. `keep` protects the newest
        N and `older_than` the checkpoints younger than the limit; giving both deletes only what
        is beyond the window *and* older, so the two rules can never add up to a wider deletion.
        Only refs are removed: the commits stay until Git reclaims them."""
        removed = self._prune_candidates(keep, older_than, now)
        self._delete_refs(removed)
        return removed

    def auto_prune(self, settings: Settings, *, run_id: Optional[str] = None,
                   protect: Iterable[str] = ()) -> List[Checkpoint]:
        """Retention for the checkpoint hook, under the user's own settings. The checkpoints
        named in `protect` (the one just created) and every checkpoint of the session in
        progress are never removed, so the run the agent is in can always be rolled back."""
        if not settings.auto_prune:
            return []
        protected = set(protect)
        removed = [cp for cp in self._prune_candidates(settings.auto_prune_keep,
                                                       settings.auto_prune_older_than, None)
                   if cp.id not in protected and not (run_id and cp.run_id == run_id)]
        self._delete_refs(removed)
        return removed

    def _prune_candidates(self, keep: Optional[int], older_than: Optional[timedelta],
                          now: Optional[datetime]) -> List[Checkpoint]:
        if keep is None and older_than is None:
            raise CheckpointError("--keep or --older-than is required for prune.")
        if keep is not None and keep < 0:
            raise CheckpointError("--keep must be zero or greater.")
        listed = self.checkpoints()
        beyond = listed[keep:] if keep is not None else listed
        if older_than is None:
            return list(beyond)
        cutoff = (now or datetime.now(timezone.utc)) - older_than
        window = {cp.id for cp in beyond}
        removed = []
        for checkpoint in listed:
            if keep is not None and checkpoint.id not in window:
                continue
            age = checkpoint_age(checkpoint)
            if age is not None and age < cutoff:
                removed.append(checkpoint)
        return removed

    def _delete_refs(self, checkpoints: List[Checkpoint]) -> None:
        for checkpoint in checkpoints:
            self._git("update-ref", "--no-deref", "-d", checkpoint.ref, checkpoint.oid)

    def size_warning(self, tree: str, settings: Settings) -> str:
        """The size warning one tree deserves, or "" when it is small enough. One
        `git ls-tree -r -l`; skipped entirely when both thresholds are off, and when the tree
        holds more entries than the scan is worth (`SIZE_SCAN_LIMIT`)."""
        if settings.warn_mb <= 0 and settings.warn_file_mb <= 0:
            return ""
        if len(self.entries(tree)) > SIZE_SCAN_LIMIT:
            return ""
        files, total, largest = self.snapshot_stats(tree)
        return size_warning_text(files, total, largest, settings)

    def warn_size_once(self, message: str, session_id: str,
                       now: Optional[datetime] = None) -> bool:
        """Print a size warning to stderr at most once per session per day. The marker file is
        best effort: a checkpoint hook must never fail because of it, and one the repository can
        redirect (a link or another reparse point at `.runledger` or at the marker itself) is
        left alone instead of being read or written through."""
        when = now or datetime.now(timezone.utc)
        marker = self.root / WARN_MARKER
        seen = _read_warn_marker(marker)
        recorded = seen.get(session_id)
        if isinstance(recorded, str):
            last = _parsed_time(recorded)
            if last is not None and when - last < WARN_WINDOW:
                return False
        print(message, file=sys.stderr)
        # Keep the marker small: sessions that have not been warned for a week are forgotten.
        remembered = {key: value for key, value in seen.items()
                      if isinstance(value, str) and (parsed := _parsed_time(value)) is not None
                      and when - parsed < WARN_WINDOW * 7}
        remembered[session_id] = when.isoformat()
        _write_warn_marker(marker, json.dumps(remembered))
        return True


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
    """True for the complete RunLedger checkpoint hook command, written with either the
    `runledger` command or the interpreter the installer stores. A compound entry (an attached
    shell operator, a pipeline, a redirection, a second command) or an unrelated hook stays
    foreign, so install appends beside it and uninstall leaves it alone."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    return owned_hook_command(hook.get("command"), ("checkpoint", "--hook")) is not None


def hook_main() -> int:
    """Lifecycle hook: no stdout/context injection, no prompt content stored, never blocks."""
    try:
        event = json.loads(sys.stdin.read())
        if not isinstance(event, dict) or event.get("hook_event_name") not in HOOK_EVENTS:
            return 0
        cwd, run_id = event.get("cwd"), event.get("session_id")
        if not isinstance(cwd, str) or not cwd or not isinstance(run_id, str) or not run_id:
            return 0
        repo = Repository(cwd)
        checkpoint = repo.create(label=event["hook_event_name"], run_id=run_id)
        settings = _hook_settings()
        _hook_auto_prune(repo, settings, run_id, checkpoint)
        _hook_size_warning(repo, settings, run_id, checkpoint)
    except NotAGitRepository:
        return 0
    except (CheckpointError, OSError, ValueError):
        print("RunLedger checkpoint unavailable. Run 'runledger checkpoint' manually for details.",
              file=sys.stderr)
    return 0


def _hook_settings() -> Settings:
    """The retention settings for one hook run. Decoding them is bookkeeping, so any failure at
    all keeps the documented defaults: a config value must never cost the hook its exit code or
    the checkpoint it just made."""
    try:
        return load_settings()
    except Exception:
        return Settings()


def _hook_auto_prune(repo: Repository, settings: Settings, run_id: str,
                     checkpoint: Checkpoint) -> None:
    """Retention after the checkpoint exists. A failure here is swallowed: it must never cost
    the user the checkpoint the hook just made, or the agent its turn."""
    try:
        repo.auto_prune(settings, run_id=run_id, protect=(checkpoint.id,))
    except Exception:
        pass


def _hook_size_warning(repo: Repository, settings: Settings, run_id: str,
                       checkpoint: Checkpoint) -> None:
    """The size warning reaches stderr (never stdout: a lifecycle hook's stdout becomes agent
    context) and at most once per session per day, so a huge tree does not nag on every prompt."""
    try:
        message = repo.size_warning(checkpoint.tree, settings)
        if message:
            repo.warn_size_once(message, run_id)
    except Exception:
        pass
