"""`runledger stats`: one summary over the local sessions.

No Git, no network and no AI: every session is parsed, costed, risk-assessed and
quality-scored with the same rule code a receipt uses, then grouped by agent, model, day,
week or project. The parsed facts are cached in `~/.runledger/cache/stats-v1.json`, keyed
by session path + mtime + size plus every path the parse read or looked for beside it (a Droid
settings sidecar or child settings file, the folders a Claude sub-agent transcript could live in
and the transcripts themselves), so a second run only reads what changed. A parse that could not
track all of those - a sub-agent tree past the folder watch cap, a folder that could not be
listed - is not cached at all and is parsed again on every run, so a warm report still equals
`--no-cache`. Costs are recomputed from the cached token and per-request numbers on every run,
so a price-table change needs no invalidation and the figures match `runledger receipt`. The
cache holds counts, model ids, token counts, session ids and cwd paths: never prompts, commands,
risk reasons or anything else a session said.

Cost honesty follows the rest of RunLedger: a model with no price gives `n/a`, a session
that mixes priced and unpriced tokens gives a `>=` lower bound, and no figure is ever a
fake `$0.00`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import adapters, pricing
from .guard import LOG_DIR, LOG_FILE, POLICY_FILE
from .parser import (SOURCE_MISSING, SOURCE_OK, SOURCE_REJECTED, Run, Usage, _ts,
                     mtime)
from .quality import analyze
from .risk import assess
from .userconfig import user_config_path

CACHE_VERSION = 1
# 4: a record may only be built from a parse that tracked every dependency it depends on, so
# every record an earlier version wrote is dropped once and rebuilt (LOCAL-R3-STATS-01).
RECORD_VERSION = 4
CACHE_FILE = "stats-v1.json"
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 500
DEFAULT_TOP = 5
GROUP_KINDS = ("agent", "model", "day", "week", "project")
PROGRESS_AFTER = 25           # parse-cache misses before a progress line is worth printing
MAX_CACHE_ENTRIES = 10000     # oldest records are dropped beyond this, so the file stays bounded
GUARD_LOG_MAX_LINE = 64 * 1024
EMPTY_FILE_BYTES = 64 * 1024   # a file with no byte but whitespace is not a session
UNKNOWN_PROJECT = "(unknown)"
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")

ACCEPTED_WINDOW = "7d, 4w, 1m or YYYY-MM-DD"
_UNITS = {"d": 1, "w": 7, "m": 30}
_RELATIVE = re.compile(r"^(\d+)([dwm])$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SinceError(ValueError):
    """A `--since`/`--until` value that is not `<n>d|w|m` or an ISO date."""


# ---------------------------------------------------------------- the window

def local_now() -> datetime:
    """Now, in local time, without sub-second noise so two runs share a window."""
    return datetime.now().replace(microsecond=0)


def parse_since(value: str, now: Optional[datetime] = None) -> datetime:
    """The window start for `<n>d`, `<n>w`, `<n>m` (30-day months) or a `YYYY-MM-DD` date.

    Relative values count back from `now` (local time); a date is that day's local midnight.
    Anything else raises `SinceError`, which the CLI turns into a usage error."""
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        raise SinceError(f"invalid value {value!r}: use {ACCEPTED_WINDOW}")
    match = _RELATIVE.match(raw.lower())
    if match:
        days = int(match.group(1)) * _UNITS[match.group(2)]
        return (now or local_now()) - timedelta(days=days)
    if _ISO_DAY.match(raw):
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            day = None
        if day is not None:
            return datetime(day.year, day.month, day.day)
    raise SinceError(f"invalid value {value!r}: use {ACCEPTED_WINDOW}")


def parse_until(value: str, now: Optional[datetime] = None) -> datetime:
    """The window end, read like `--since`; a date covers that whole local day."""
    raw = value.strip() if isinstance(value, str) else ""
    if _ISO_DAY.match(raw):
        start = parse_since(raw, now)
        return start + timedelta(hours=23, minutes=59, seconds=59)
    return parse_since(raw, now)


def default_since(now: Optional[datetime] = None) -> datetime:
    """The default window: the last `DEFAULT_DAYS` days."""
    return (now or local_now()) - timedelta(days=DEFAULT_DAYS)


def window_now(now: Optional[datetime] = None) -> datetime:
    """The clock the window is resolved against: the current minute.

    Resolving to the minute keeps two runs in the same minute byte-identical (except for
    `generated`), which is what makes the cached and the uncached report comparable."""
    return (now or local_now()).replace(second=0, microsecond=0)


def default_until(now: datetime) -> datetime:
    """The default window end: the end of the current minute, so a session that started
    seconds ago is inside the window."""
    return now + timedelta(minutes=1)


def window_label(since_raw: Optional[str], until_raw: Optional[str],
                 since: datetime, until: datetime) -> str:
    """How the window reads in a heading: "last 30 days", "since 2026-09-24", "... until ..."."""
    if since_raw:
        match = _RELATIVE.match(since_raw.strip().lower())
        if match:
            count, unit = int(match.group(1)), match.group(2)
            name = {"d": "day", "w": "week", "m": "month"}[unit]
            head = f"last {count} {name}{'' if count == 1 else 's'}"
        else:
            head = f"since {since.date().isoformat()}"
    else:
        head = f"last {DEFAULT_DAYS} days"
    if until_raw:
        return f"{head} until {until.date().isoformat()}"
    return head


def no_sessions_note(since: datetime, until: datetime) -> str:
    """The one line printed when sessions exist but none started inside the window."""
    return (f"No sessions found between {since.date().isoformat()} and "
            f"{until.date().isoformat()}. Widen the window with --since "
            f"({ACCEPTED_WINDOW}).")


# ---------------------------------------------------------------- the parse cache

def cache_path() -> Path:
    """`~/.runledger/cache/stats-v1.json`, beside the session-discovery cache."""
    base = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    home = Path(base) if base else Path.home()
    return home / ".runledger" / "cache" / CACHE_FILE


def cache_disabled() -> bool:
    """`RUNLEDGER_NO_SESSION_CACHE=1` makes the command read-only: no cache file is read
    or written (`runledger doctor` counts sessions that way)."""
    return os.environ.get("RUNLEDGER_NO_SESSION_CACHE", "").strip().lower() in ("1", "true", "yes")


def policy_signature(cwd: Optional[str]) -> List[Optional[int]]:
    """`(mtime_ns, size)` of the user config and of the project's `.runledger.json`.

    Both files change what `risk.assess` finds, so a record scored under a different pair
    is stale even though the session file itself did not change."""
    signature: List[Optional[int]] = []
    paths = [user_config_path()]
    paths.append(Path(cwd) / POLICY_FILE if cwd else None)
    for path in paths:
        if path is None:
            signature.extend([None, None])
            continue
        try:
            stat = os.stat(str(path))
        except OSError:
            signature.extend([None, None])
        else:
            signature.extend([stat.st_mtime_ns, stat.st_size])
    return signature


def _read_entries(path: Path) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """The cache's entries and whether the file on disk has to be rewritten."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}, False
    except ValueError:  # not decodable text
        return {}, True
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}, True
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}, True
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}, True
    clean = {key: value for key, value in entries.items()
             if isinstance(key, str) and isinstance(value, dict)}
    return clean, False


def _write_atomic(path: Path, data: bytes) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{CACHE_FILE}.{os.getpid()}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


@dataclass
class SessionSummary:
    """What one session contributes to the summary: counts, tokens and scores, no text.

    `sources` are the paths the parse read or looked for beside the transcript itself (a Droid
    settings sidecar, its child settings files, the folders a Claude sub-agent transcript could
    live in and the transcripts), each as `[path, mtime_ns, size, state]` - `state` is `ok`,
    `missing` or `rejected` - so the cache can watch them too.

    `complete` is the parse's own answer to whether it could track all of them. Nothing is
    cached for a summary that says no: sources that were never watched cannot invalidate a
    record, so the only safe record is none at all (LOCAL-R3-STATS-01)."""
    path: str
    agent: str
    session_id: str
    cwd: Optional[str]
    started: datetime
    steps: int
    models: Dict[str, Dict[str, Any]]
    has_usage: bool
    score: int
    level: str
    codes: Dict[str, int]
    grade: str
    rec_kinds: Dict[str, int]
    sources: List[List[Any]] = field(default_factory=list)
    complete: bool = True

    @property
    def tokens(self) -> Dict[str, int]:
        out = {name: 0 for name in TOKEN_FIELDS}
        for bucket in self.models.values():
            for name in TOKEN_FIELDS:
                out[name] += int(bucket.get(name) or 0)
        return out


class StatsCache:
    """The on-disk parse cache, keyed by session path.

    A record is valid while the session file's `(mtime_ns, size)`, every path the parse read or
    looked for beside it is still in the state the record was built from (a path that was
    missing then must still be missing, so one that appears drops the record), the record
    version and the policy files it was scored with are unchanged."""

    def __init__(self, entries: Optional[Dict[str, Dict[str, Any]]] = None,
                 path: Optional[Path] = None, dirty: bool = False):
        self.entries: Dict[str, Dict[str, Any]] = dict(entries or {})
        self.path = Path(path) if path is not None else cache_path()
        self.dirty = dirty

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "StatsCache":
        cache = cls(path=path)
        if cache_disabled():
            return cache
        entries, rewrite = _read_entries(cache.path)
        cache.entries = entries
        cache.dirty = rewrite
        return cache

    def get(self, path: Path, stat_result: os.stat_result) -> Optional[SessionSummary]:
        record = self.entries.get(str(path))
        if not isinstance(record, dict) or record.get("version") != RECORD_VERSION:
            return None
        if record.get("mtime_ns") != stat_result.st_mtime_ns:
            return None
        if record.get("size") != stat_result.st_size:
            return None
        if record.get("policy") != policy_signature(record.get("cwd")):
            return None
        if not _sources_valid(record.get("sources")):
            return None
        try:
            return _summary_from_record(str(path), record)
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, path: Path, stat_result: os.stat_result, summary: SessionSummary) -> None:
        """Store one summary - unless the parse could not track every path it depends on
        (a sub-agent folder tree past the watch cap, a folder that could not be listed), in
        which case no record is stored and an older one is dropped instead. A truncated record
        would look valid while missing a transcript the watch never saw, so the session is
        parsed again on every run and a warm report always equals `--no-cache`
        (LOCAL-R3-STATS-01)."""
        if not summary.complete:
            self.drop(path)
            return
        self.entries[str(path)] = _record(stat_result, summary)
        self.dirty = True
        if len(self.entries) > MAX_CACHE_ENTRIES:
            self._trim()

    def drop(self, path: Path) -> None:
        if self.entries.pop(str(path), None) is not None:
            self.dirty = True

    def _trim(self) -> None:
        order = sorted(self.entries, key=lambda key: int(self.entries[key].get("mtime_ns") or 0))
        for key in order[: len(self.entries) - MAX_CACHE_ENTRIES]:
            self.entries.pop(key, None)

    def save(self) -> None:
        """Write the whole file atomically. A cache that cannot be written never fails a run."""
        if not self.dirty or cache_disabled():
            return
        payload = json.dumps({"version": CACHE_VERSION, "entries": self.entries},
                             separators=(",", ":")).encode("utf-8")
        try:
            _write_atomic(self.path, payload)
        except OSError:
            pass


def _record(stat_result: os.stat_result, summary: SessionSummary) -> Dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "mtime_ns": stat_result.st_mtime_ns,
        "size": stat_result.st_size,
        "policy": policy_signature(summary.cwd),
        "sources": summary.sources,
        "agent": summary.agent,
        "session_id": summary.session_id,
        "cwd": summary.cwd,
        "started": summary.started.isoformat(timespec="seconds"),
        "steps": summary.steps,
        "models": summary.models,
        "has_usage": summary.has_usage,
        "risk": {"score": summary.score, "level": summary.level, "codes": summary.codes},
        "quality": {"grade": summary.grade},
        "rec_kinds": summary.rec_kinds,
    }


def _source_entries(sources: Iterable[Tuple[str, str]]) -> List[List[Any]]:
    """`[path, mtime_ns, size, marker]` for every path a parse read or looked for beside the
    transcript. A path the parse did not find is recorded as `missing` with nulls, so the
    record is dropped as soon as it appears; one that was there keeps its size and time, so
    repairing or filling it in drops the record too."""
    out: List[List[Any]] = []
    for raw, state in sources:
        path = str(raw)
        if state == SOURCE_MISSING:
            out.append([path, None, None, SOURCE_MISSING])
            continue
        try:
            stat = os.stat(path)
        except OSError:
            out.append([path, None, None, SOURCE_MISSING])
            continue
        out.append([path, stat.st_mtime_ns, stat.st_size,
                    SOURCE_REJECTED if state == SOURCE_REJECTED else SOURCE_OK])
    return out


def _sources_valid(sources: Any) -> bool:
    """True while every path a parse read or looked for beside the transcript is in the state
    the record was built from. A path recorded as missing must still be missing, so a sidecar
    or sub-agent transcript that appears later drops the record; one recorded with a size and
    modification time must still have them. A file that was unreadable then and is unreadable
    now is fine; anything else invalidates the record, so the session is parsed again."""
    if not isinstance(sources, list):
        return False
    for entry in sources:
        if not (isinstance(entry, list) and len(entry) in (3, 4)
                and isinstance(entry[0], str)):
            return False
        missing = entry[1] is None and entry[2] is None
        if len(entry) == 4 and entry[3] == SOURCE_MISSING:
            missing = True
        try:
            stat = os.stat(entry[0])
        except OSError:
            if missing:
                continue
            return False
        if missing:  # the path was not there when the record was built, and now it is
            return False
        if entry[1] != stat.st_mtime_ns or entry[2] != stat.st_size:
            return False
    return True


def _summary_from_record(path: str, record: Dict[str, Any]) -> SessionSummary:
    models = {}
    for model, bucket in record["models"].items():
        if not isinstance(model, str) or not isinstance(bucket, dict):
            raise ValueError("bad model bucket")
        models[model] = _clean_bucket(bucket)
    cwd = record["cwd"]
    sources = record.get("sources")
    return SessionSummary(
        path=path,
        agent=str(record["agent"]),
        session_id=str(record["session_id"]),
        cwd=str(cwd) if isinstance(cwd, str) else None,
        started=datetime.fromisoformat(str(record["started"])),
        steps=int(record["steps"]),
        models=models,
        has_usage=bool(record["has_usage"]),
        score=int(record["risk"]["score"]),
        level=str(record["risk"]["level"]),
        codes={str(code): int(count) for code, count in record["risk"]["codes"].items()},
        grade=str(record["quality"]["grade"]),
        rec_kinds={str(kind): int(count) for kind, count in record["rec_kinds"].items()},
        sources=[list(entry) for entry in sources] if isinstance(sources, list) else [],
    )


# ---------------------------------------------------------------- one session

def summarize(path: Path) -> SessionSummary:
    """Parse, cost, risk-assess and quality-score one session file.

    Raises `OSError`/`ValueError` for a file that cannot be read; the caller reports it as
    skipped and keeps going. A file with no bytes at all is not a session, whatever its
    name; a session that holds no steps is still a session and is counted as one."""
    session = Path(path)
    if _is_empty_file(session):
        raise ValueError(f"{session}: not a supported session file")
    run = _parse(session)
    pricing.apply_costs(run)
    score, level, risks = assess(run)
    analyze(run, risks)
    return SessionSummary(
        path=str(session),
        agent=run.agent,
        session_id=run.session_id,
        cwd=run.cwd,
        started=_local(_ts(run.started)) or _file_time(session),
        steps=len(run.steps),
        models={model: _bucket(usage) for model, usage in run.models.items()},
        has_usage=run.usage_source != "missing",
        score=int(score),
        level=str(level).lower(),
        codes=_counted(risk.code for risk in risks),
        grade=str((run.quality or {}).get("grade") or ""),
        rec_kinds=_counted(rec.get("kind") for rec in run.recommendations),
        sources=_source_entries(run.sources),
        complete=run.dependencies_complete,
    )


def _parse(path: Path) -> Run:
    """Parse one session the way a receipt does, except that a Droid relative `cwd` is
    recovered without a Git lookup: `stats` never runs git (VAL-STATS-018)."""
    adapter = adapters.detect(path)
    if adapter.NAME == "droid":
        return adapter.parse(path, git_root=False)
    return adapter.parse(path)


def _is_empty_file(path: Path) -> bool:
    """True for a file that holds no byte but whitespace: an empty file, not a session."""
    try:
        with path.open("rb") as handle:
            return not handle.read(EMPTY_FILE_BYTES).strip()
    except OSError:
        return False


def _file_time(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(os.stat(str(path)).st_mtime)
    except OSError:
        return local_now()


def _local(moment: Optional[datetime]) -> Optional[datetime]:
    """A session timestamp as naive local time, the clock the window and `--by day` use."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def _bucket(usage: Usage) -> Dict[str, Any]:
    """A usage as cache-friendly numbers: the token counts plus the request-tier numbers
    `pricing` needs, and nothing the session said."""
    out: Dict[str, Any] = {name: max(0, getattr(usage, f"{name}_tokens")) for name in TOKEN_FIELDS}
    _tier_numbers(usage, out)
    return out


def _request_bucket(usage: Usage) -> Dict[str, Any]:
    """One request of a session, compact: only the token fields it used."""
    out: Dict[str, Any] = {}
    for name in TOKEN_FIELDS:
        value = max(0, getattr(usage, f"{name}_tokens"))
        if value:
            out[name] = value
    _tier_numbers(usage, out)
    return out


def _tier_numbers(usage: Usage, out: Dict[str, Any]) -> None:
    """The per-request pricing metadata the receipt path reads: each request's own token
    counts (so a long-context tier is decided per request, not for the session total), the
    request's prompt size, and whether the usage is only a cumulative estimate. Numbers
    only: no prompt text, no command."""
    one_hour = getattr(usage, "cache_write_1h_tokens", 0)
    if one_hour:
        out["cache_write_1h"] = int(one_hour)
    prompt = getattr(usage, "_request_prompt_tokens", None)
    if prompt:
        out["prompt"] = int(prompt)
    if getattr(usage, "_cumulative_only", False):
        out["cumulative"] = True
    requests = getattr(usage, "_requests", None)
    if requests:
        out["requests"] = [_request_bucket(request) for request in requests]


def _usage(bucket: Dict[str, Any]) -> Usage:
    usage = Usage(
        input_tokens=int(bucket.get("input") or 0),
        output_tokens=int(bucket.get("output") or 0),
        cache_write_tokens=int(bucket.get("cache_write") or 0),
        cache_read_tokens=int(bucket.get("cache_read") or 0),
        cache_write_1h_tokens=int(bucket.get("cache_write_1h") or 0),
    )
    if bucket.get("prompt"):
        usage._request_prompt_tokens = int(bucket["prompt"])
    if bucket.get("cumulative"):
        usage._cumulative_only = True
    for request in bucket.get("requests") or []:
        usage._requests.append(_usage(request))
    return usage


def _clean_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    """A cached bucket as numbers only; a malformed one raises `ValueError`, so the caller
    reparses the session instead of pricing nonsense."""
    if not isinstance(bucket, dict):
        raise ValueError("bad token bucket")
    out: Dict[str, Any] = {name: int(bucket.get(name) or 0) for name in TOKEN_FIELDS}
    for key in ("cache_write_1h", "prompt"):
        if bucket.get(key) is not None:
            out[key] = int(bucket[key])
    if bucket.get("cumulative"):
        out["cumulative"] = True
    requests = bucket.get("requests")
    if requests is not None:
        if not isinstance(requests, list):
            raise ValueError("bad request list")
        out["requests"] = [_clean_bucket(item) for item in requests]
    return out


def _counted(values: Iterable[Any]) -> Dict[str, int]:
    counts: Counter = Counter(value for value in values if value)
    return {str(key): int(count) for key, count in counts.items()}


def _bucket_total(bucket: Dict[str, Any]) -> int:
    return sum(int(bucket.get(name) or 0) for name in TOKEN_FIELDS)


def _bucket_cost(model: str, bucket: Dict[str, Any]) -> Optional[float]:
    if not _bucket_total(bucket):
        return None
    return pricing.cost_of(_usage(bucket), model)


def session_cost(summary: SessionSummary) -> Optional[float]:
    """The session's cost, or `None` when no token can be priced (never a fake zero)."""
    if not summary.has_usage:
        return None
    total = 0.0
    known = False
    for model, bucket in summary.models.items():
        cost = _bucket_cost(model, bucket)
        if cost is not None:
            total += cost
            known = True
    return total if known and total > 0 else None


def session_unknown_models(summary: SessionSummary) -> List[str]:
    """Model ids with tokens but no price: adding a price would change the figures."""
    return [model for model, bucket in summary.models.items()
            if model != pricing.UNATTRIBUTED and _bucket_total(bucket)
            and pricing.price_for(model) is None]


def session_cost_partial(summary: SessionSummary) -> bool:
    """True when the session's cost covers only its priced tokens."""
    if session_cost(summary) is None:
        return False
    return any(model == pricing.UNATTRIBUTED or pricing.price_for(model) is None
               for model, bucket in summary.models.items() if _bucket_total(bucket))


def session_is_complete(summary: SessionSummary) -> bool:
    return session_cost(summary) is not None and not session_cost_partial(summary)


# ---------------------------------------------------------------- discovery

def discover(project: Optional[str], agent: Optional[str] = None) -> List[Path]:
    """The session files to aggregate, newest first.

    Like `adapters.find_sessions`, except that Aider is only looked for in the project
    folder itself, with or without `--agent aider`: its other layouts walk up to the Git top
    level, which runs git, and `stats` never runs git."""
    chosen = [adapters.get(agent)] if agent else adapters.adapters()
    found: List[Path] = []
    for adapter in chosen:
        if adapter.NAME == "aider":
            found.extend(_aider_history(project, adapter.HISTORY_FILE))
            continue
        found.extend(adapter.find_sessions(project))
    return sorted(set(found), key=lambda path: (mtime(path), str(path)), reverse=True)


def _aider_history(project: Optional[str], history_file: str) -> List[Path]:
    """`<project>/<history file>`, the one Aider layout that needs no Git lookup."""
    if not project:
        return []
    history = Path(project) / history_file
    try:
        return [history] if history.is_file() else []
    except OSError:
        return []


# ---------------------------------------------------------------- collecting

def collect(files: Sequence[Path], since: datetime, until: datetime,
            cache: Optional[StatsCache] = None, limit: Optional[int] = None
            ) -> Tuple[List[SessionSummary], List[Dict[str, str]], int]:
    """Summaries of the sessions that started inside `[since, until]`, newest first.

    Returns `(summaries, skipped, found)`: `found` is the count before `--limit` truncation.
    A file that cannot be read, or holds nothing readable, is reported in `skipped` with a
    reason and never stops the run."""
    skipped: List[Dict[str, str]] = []
    found: List[SessionSummary] = []
    misses = 0
    for path in files:
        try:
            stat_result = os.stat(str(path))
        except OSError:
            skipped.append({"path": str(path), "reason": "the file is gone"})
            continue
        if stat_result.st_mtime < since.timestamp():
            continue  # written before the window opened, so it cannot hold a newer session
        summary = cache.get(path, stat_result) if cache is not None else None
        if summary is None:
            misses += 1
            if misses == PROGRESS_AFTER + 1:
                print(f"stats: parsing sessions that are not in the cache yet "
                      f"({len(files)} file(s) listed)...", file=sys.stderr)
            try:
                summary = summarize(path)
            except (OSError, ValueError) as exc:
                skipped.append({"path": str(path), "reason": _reason(path, exc)})
                if cache is not None:
                    cache.drop(path)
                continue
            if cache is not None:
                cache.put(path, stat_result, summary)
        if since <= summary.started <= until:
            found.append(summary)
    found.sort(key=lambda item: (item.started, item.path), reverse=True)
    total = len(found)
    if limit is not None:
        found = found[:limit]
    if cache is not None:
        cache.save()
    return found, skipped, total


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


def _reason(path: Path, exc: BaseException) -> str:
    """The message without the path the report already shows."""
    text = _one_line(exc)
    prefix = f"{path}: "
    return text[len(prefix):] if text.startswith(prefix) else text


# ---------------------------------------------------------------- the guard log

def _guard_log_paths(cwd: str) -> List[Path]:
    """The guard log candidates for a session's working folder: the folder itself and the
    project root above it (the first ancestor holding `.runledger.json` or `.git`)."""
    if not cwd:
        return []
    paths = [Path(cwd) / LOG_DIR / LOG_FILE]
    try:
        root = Path(cwd)
        for folder in root.parents:
            if (folder / POLICY_FILE).exists() or (folder / ".git").exists():
                paths.append(folder / LOG_DIR / LOG_FILE)
                break
    except OSError:
        pass
    return paths


def _guard_records(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            while True:
                line = handle.readline(GUARD_LOG_MAX_LINE + 1)
                if not line:
                    break
                if len(line) > GUARD_LOG_MAX_LINE:
                    while line and not line.endswith("\n"):
                        line = handle.readline(GUARD_LOG_MAX_LINE + 1)
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def guard_counts(cwds: Iterable[Optional[str]], since: datetime, until: datetime) -> Dict[str, int]:
    """Guard decisions recorded for these working folders inside the window.

    Decisions come from `<project>/.runledger/guard.log`; a missing, unreadable or corrupt
    log counts as nothing. `approved` counts the decisions an approval server resolved,
    `error` the hook failures, and entries without a readable timestamp are not placed."""
    counts = {"allow": 0, "ask": 0, "deny": 0, "approved": 0, "error": 0}
    seen = set()
    for cwd in cwds:
        if not cwd:
            continue
        for path in _guard_log_paths(str(cwd)):
            try:
                key = os.path.normcase(os.path.abspath(str(path)))
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if not path.is_file():
                continue
            for record in _guard_records(path):
                when = _local(_ts(record.get("ts")))
                if when is None or not (since <= when <= until):
                    continue
                decision = record.get("decision")
                if decision in counts:
                    counts[decision] += 1
                if record.get("approval") == "approved":
                    counts["approved"] += 1
    return counts


# ---------------------------------------------------------------- aggregating

def _block(summaries: Sequence[SessionSummary]) -> Dict[str, Any]:
    tokens = {name: 0 for name in TOKEN_FIELDS}
    steps = 0
    cost = 0.0
    known = False
    partial = False
    for summary in summaries:
        steps += summary.steps
        for name in TOKEN_FIELDS:
            tokens[name] += summary.tokens[name]
        session = session_cost(summary)
        if session is not None:
            cost += session
            known = True
        if not session_is_complete(summary):
            partial = True
    return {
        "sessions": len(summaries),
        "steps": steps,
        "tokens": tokens,
        "cost": round(cost, 6) if known else None,
        "cost_partial": bool(known and partial),
    }


def _dominant_model(summary: SessionSummary) -> str:
    """The model that used the most tokens; ties go to the first id alphabetically."""
    if not summary.models:
        return "(no model)"
    return min(summary.models, key=lambda model: (-_bucket_total(summary.models[model]), model))


def _week_key(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _group_key(summary: SessionSummary, by: str) -> str:
    if by == "day":
        return summary.started.date().isoformat()
    if by == "week":
        return _week_key(summary.started)
    if by == "project":
        return summary.cwd or UNKNOWN_PROJECT
    return summary.agent


def _model_groups(summaries: Sequence[SessionSummary]) -> List[Dict[str, Any]]:
    """`--by model`: one row per model, priced as the receipt prices it.

    A session that used several models contributes its tokens and cost to each of their
    rows, so the totals stay the sum of the rows; its session and step counts stay with the
    model that used most of its tokens, so those columns sum to the total as well."""
    rows: Dict[str, Dict[str, Any]] = {}
    for summary in summaries:
        dominant = _dominant_model(summary)
        buckets = summary.models or {dominant: {}}  # a session with no model keeps its row
        for model, bucket in buckets.items():
            row = rows.setdefault(model, {"sessions": 0, "steps": 0,
                                          "tokens": {name: 0 for name in TOKEN_FIELDS},
                                          "cost": 0.0, "known": False, "partial": False})
            if model == dominant:
                row["sessions"] += 1
                row["steps"] += summary.steps
            for name in TOKEN_FIELDS:
                row["tokens"][name] += int(bucket.get(name) or 0)
            cost = _bucket_cost(model, bucket) if summary.has_usage else None
            if cost is not None:
                row["cost"] += cost
                row["known"] = True
            elif _bucket_total(bucket) and model != pricing.UNATTRIBUTED:
                row["partial"] = True
    return [{"key": key, "sessions": row["sessions"], "steps": row["steps"],
             "tokens": row["tokens"],
             "cost": round(row["cost"], 6) if row["known"] else None,
             "cost_partial": bool(row["known"] and row["partial"])}
            for key, row in sorted(rows.items())]


def _groups(summaries: Sequence[SessionSummary], by: str) -> List[Dict[str, Any]]:
    if by == "model":
        return _model_groups(summaries)
    buckets: Dict[str, List[SessionSummary]] = {}
    for summary in summaries:
        buckets.setdefault(_group_key(summary, by), []).append(summary)
    return [{"key": key, **_block(buckets[key])} for key in sorted(buckets)]


def _risk(summaries: Sequence[SessionSummary], top: int) -> Dict[str, Any]:
    levels = {name: 0 for name in ("low", "medium", "high")}
    codes: Counter = Counter()
    scores = []
    for summary in summaries:
        if summary.level in levels:
            levels[summary.level] += 1
        codes.update(summary.codes)
        scores.append(summary.score)
    return {
        **levels,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "top_codes": _top_pairs(codes, top),
    }


def _quality(summaries: Sequence[SessionSummary], top: int) -> Dict[str, Any]:
    grades: Counter = Counter(summary.grade for summary in summaries if summary.grade)
    kinds: Counter = Counter()
    for summary in summaries:
        kinds.update(summary.rec_kinds)
    return {
        "grades": {grade: int(grades[grade]) for grade in sorted(grades)},
        "top_recommendations": _top_pairs(kinds, top),
    }


def _top_pairs(counts: Counter, top: int) -> List[List[Any]]:
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], str(pair[0])))
    return [[key, int(count)] for key, count in ordered[:max(0, top)]]


def _partial(summaries: Sequence[SessionSummary]) -> Dict[str, Any]:
    models = sorted({model for summary in summaries
                     for model in session_unknown_models(summary)})
    return {"sessions": sum(1 for summary in summaries if not session_is_complete(summary)),
            "unknown_models": models}


def aggregate(summaries: Sequence[SessionSummary], by: str = "agent", top: int = DEFAULT_TOP,
              scope: Optional[Dict[str, Any]] = None, window: Optional[Tuple[datetime, datetime]] = None,
              skipped: Sequence[Dict[str, str]] = (), guard: Optional[Dict[str, int]] = None
              ) -> Dict[str, Any]:
    """The whole report as one JSON-ready dict (`version` 1)."""
    since, until = window if window else (default_since(), local_now())
    return {
        "version": 1,
        "generated": local_now().isoformat(timespec="seconds"),
        "scope": {"project": (scope or {}).get("project"),
                  "agent": (scope or {}).get("agent")},
        "window": {"since": since.isoformat(timespec="seconds"),
                   "until": until.isoformat(timespec="seconds")},
        "totals": _block(summaries),
        "groups": _groups(summaries, by),
        "risk": _risk(summaries, top),
        "quality": _quality(summaries, top),
        "guard": {name: int((guard or {}).get(name) or 0)
                  for name in ("allow", "ask", "deny", "approved", "error")},
        "partial": _partial(summaries),
        "skipped": [{"path": str(entry.get("path")), "reason": str(entry.get("reason"))}
                    for entry in skipped],
    }


def model_blocks(summaries: Sequence[SessionSummary]) -> List[Dict[str, Any]]:
    """Per-model totals for the "top models" line, most expensive first (unpriced last)."""
    blocks: Dict[str, Dict[str, Any]] = {}
    for summary in summaries:
        for model, bucket in summary.models.items():
            block = blocks.setdefault(model, {"key": model, "sessions": 0,
                                              "tokens": {name: 0 for name in TOKEN_FIELDS},
                                              "cost": 0.0, "known": False, "partial": False})
            block["sessions"] += 1
            for name in TOKEN_FIELDS:
                block["tokens"][name] += int(bucket.get(name) or 0)
            cost = _bucket_cost(model, bucket) if summary.has_usage else None
            if cost is not None:
                block["cost"] += cost
                block["known"] = True
            elif _bucket_total(bucket) and model != pricing.UNATTRIBUTED:
                block["partial"] = True
    out = []
    for block in blocks.values():
        out.append({"key": block["key"], "sessions": block["sessions"], "tokens": block["tokens"],
                    "cost": round(block["cost"], 6) if block["known"] else None,
                    "cost_partial": bool(block["known"] and block["partial"])})
    out.sort(key=lambda block: (block["cost"] is None, -(block["cost"] or 0.0),
                                -sum(block["tokens"].values()), block["key"]))
    return out


# ---------------------------------------------------------------- rendering

def _where(agg: Dict[str, Any]) -> str:
    return agg["scope"]["project"] or "any project"


def tokens_text(tokens: Dict[str, int]) -> str:
    return " / ".join(_human(int(tokens.get(name) or 0))
                      for name in ("input", "output", "cache_read"))


def cost_text(cost: Optional[float], partial: bool = False) -> str:
    """A cost cell: `n/a`, `$1.20` or a `>=`-marked lower bound. Never a fake `$0.00`."""
    if cost is None:
        return "n/a"
    text = _usd(cost)
    return f">={text}" if partial else text


def _usd(value: float) -> str:
    if value >= 0.01:
        return f"${value:.2f}"
    if value >= 0.0001:
        return f"${value:.4f}"
    return f"${value:.6f}"


def _human(count: int) -> str:
    if count >= 10_000_000:
        return f"{count / 1_000_000:.0f}M"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 100_000:
        return f"{count / 1000:.0f}K"
    if count >= 1000:
        return f"{count / 1000:.1f}K"
    return str(int(count))


def _cell(block: Dict[str, Any]) -> str:
    text = cost_text(block.get("cost"), bool(block.get("cost_partial")))
    return f"{text} *" if block.get("cost_partial") else text


def _summary_lines(agg: Dict[str, Any], models: Sequence[Dict[str, Any]], top: int
                   ) -> List[Tuple[str, str]]:
    risk = agg["risk"]
    quality = agg["quality"]
    guard = agg["guard"]
    top_models = " | ".join(f"{block['key']} {cost_text(block.get('cost'), bool(block.get('cost_partial')))}"
                            for block in list(models)[:max(0, top)]) or "none"
    codes = ", ".join(f"{code} {count}" for code, count in risk["top_codes"]) or "none"
    grades = " | ".join(f"{grade} {count}" for grade, count in quality["grades"].items()) or "none"
    recommendation = (f"{quality['top_recommendations'][0][0]} "
                      f"({quality['top_recommendations'][0][1]})"
                      if quality["top_recommendations"] else "none")
    return [
        ("top models", top_models),
        ("risk", f"Low {risk['low']} | Medium {risk['medium']} | High {risk['high']}"
                 f"   avg score {risk['avg_score']}   top codes: {codes}"),
        ("quality", f"{grades}   most common recommendation: {recommendation}"),
        ("guard", f"allow {guard['allow']} | ask {guard['ask']} | deny {guard['deny']}"
                  f" | approved {guard['approved']} | errors {guard['error']}"
                  f"   (from {LOG_DIR}/{LOG_FILE})"),
    ]


def _footnotes(agg: Dict[str, Any]) -> List[str]:
    totals = agg["totals"]
    partial = agg["partial"]
    lines = []
    if partial["sessions"]:
        models = ", ".join(partial["unknown_models"]) or "no token data"
        lines.append(f"* partial cost: {partial['sessions']} of {totals['sessions']} session(s) "
                     f"is not fully priced ({models}); amounts marked >= are lower bounds, "
                     f"n/a means no price at all.")
    if agg["skipped"]:
        names = ", ".join(Path(entry["path"]).name for entry in agg["skipped"][:3])
        more = "" if len(agg["skipped"]) <= 3 else f" and {len(agg['skipped']) - 3} more"
        lines.append(f"* skipped {len(agg['skipped'])} unreadable session file(s): {names}{more}")
    return lines


def render_table(agg: Dict[str, Any], by: str = "agent", agents: Optional[int] = None,
                 models: Optional[Sequence[Dict[str, Any]]] = None,
                 label: Optional[str] = None, top: int = DEFAULT_TOP) -> str:
    """The human table: one row per group plus a total row, then the summary lines."""
    totals = agg["totals"]
    agents = len(agg["groups"]) if agents is None else agents
    label = label or f"since {agg['window']['since'][:10]}"
    rows: List[Tuple[str, str, str, str, str]] = []
    for group in agg["groups"]:
        rows.append((group["key"], f"{group['sessions']:,}", f"{group['steps']:,}",
                     tokens_text(group["tokens"]), _cell(group)))
    rows.append(("total", f"{totals['sessions']:,}", f"{totals['steps']:,}",
                 tokens_text(totals["tokens"]), _cell(totals)))
    headers = (by, "sessions", "steps", "tokens (in / out / cache)", "cost")
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(4)]
    cost_width = max(len(headers[4]), *(len(row[4]) for row in rows))
    lines = [f"RunLedger stats - {_where(agg)}  ({label}, "
             f"{totals['sessions']:,} session{'' if totals['sessions'] == 1 else 's'}, "
             f"{agents} agent{'' if agents == 1 else 's'})", ""]
    lines.append(f"{headers[0]:<{widths[0]}}  {headers[1]:>{widths[1]}}  {headers[2]:>{widths[2]}}  "
                 f"{headers[3]:>{widths[3]}}  {headers[4]:>{cost_width}}")
    for row in rows:
        lines.append(f"{row[0]:<{widths[0]}}  {row[1]:>{widths[1]}}  {row[2]:>{widths[2]}}  "
                     f"{row[3]:>{widths[3]}}  {row[4]:>{cost_width}}")
    lines.append("")
    for name, text in _summary_lines(agg, models or [], top):
        lines.append(f"{name:<12} {text}")
    footnotes = _footnotes(agg)
    if footnotes:
        lines.append("")
        lines.extend(footnotes)
    return "\n".join(lines)


def render_md(agg: Dict[str, Any], by: str = "agent", agents: Optional[int] = None,
              models: Optional[Sequence[Dict[str, Any]]] = None,
              label: Optional[str] = None, top: int = DEFAULT_TOP) -> str:
    """The same report as GitHub tables, for a pull request comment."""
    totals = agg["totals"]
    agents = len(agg["groups"]) if agents is None else agents
    label = label or f"since {agg['window']['since'][:10]}"
    sessions = totals["sessions"]
    lines = [f"## RunLedger stats - {_where(agg)}", "",
             f"_{label} · {sessions:,} session{'' if sessions == 1 else 's'} · "
             f"{agents} agent{'' if agents == 1 else 's'}_", "",
             f"| {by} | sessions | steps | tokens (in / out / cache) | cost |",
             "|---| ---: | ---: | ---: | ---: |"]
    for group in agg["groups"]:
        lines.append(f"| {group['key']} | {group['sessions']:,} | {group['steps']:,} "
                     f"| {tokens_text(group['tokens'])} | {_cell(group)} |")
    lines.append(f"| **total** | **{totals['sessions']:,}** | **{totals['steps']:,}** "
                 f"| **{tokens_text(totals['tokens'])}** | **{_cell(totals)}** |")
    lines.append("")
    for name, text in _summary_lines(agg, models or [], top):
        lines.append(f"- {name}: {text}")
    for note in _footnotes(agg):
        lines.append("")
        lines.append(f"- {note}")
    return "\n".join(lines)
