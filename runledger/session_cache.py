"""Shared header and list-summary cache in ~/.runledger/cache/sessions.json.

Used by session discovery (Codex, Droid) and `runledger list` so unchanged
transcripts do not need to be re-opened on every invocation. Corrupt or
unreadable cache files are ignored and overwritten atomically.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

CACHE_VERSION = 1


def cache_path() -> Path:
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / ".runledger" / "cache" / "sessions.json"


def _read_bytes(path: Path) -> bytes:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        chunks = []
        while True:
            buf = os.read(fd, 65536)
            if not buf:
                break
            chunks.append(buf)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".sessions.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def load() -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """Return `(entries_by_path, needs_rewrite)`.

    `needs_rewrite` is True when a cache file existed on disk but was corrupt
    or had an incompatible structure, so callers can rewrite a clean file.
    """
    path = cache_path()
    try:
        st = os.stat(str(path))
    except OSError:
        return {}, False
    if st.st_size <= 0:
        return {}, True
    try:
        raw = _read_bytes(path).decode("utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        return {}, True
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}, True
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}, True
    clean: Dict[str, Dict[str, Any]] = {}
    for k, v in entries.items():
        if isinstance(k, str) and isinstance(v, dict):
            clean[k] = v
    return clean, False


def save(entries: Dict[str, Dict[str, Any]]) -> None:
    """Persist `entries` atomically to `~/.runledger/cache/sessions.json`."""
    path = cache_path()
    payload = json.dumps({"version": CACHE_VERSION, "entries": entries}, separators=(",", ":")).encode("utf-8")
    try:
        _write_bytes_atomic(path, payload)
    except OSError:
        pass
