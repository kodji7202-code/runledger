"""Keys, roles, sessions and the failed-sign-in limiter for the RunLedger team server.

A team has any number of API keys. Each key has one role:

  admin   everything, including key management, team settings and the audit log
  member  push runs, request and decide approvals, read everything
  viewer  read only: every GET endpoint except the admin ones

A key looks like "rl_" followed by 40 URL-safe characters. The server keeps only the
SHA-256 hash of the key, plus its first 8 characters (the prefix) for display.
Rotating a key replaces the hash and revoking it sets revoked_at; both take effect
at once.

The dashboard signs in with a key once and keeps an opaque session token in a cookie.
The session carries the role of that key and ends when the key is revoked or rotated.

Rejected API keys and failed sign-ins are counted per client address, in memory. More
than 20 within five minutes and that address gets 429 until enough of them age out.
Stale dashboard cookies are not counted (see app._Handler._identify).

Standard library only.
"""
from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Mapping, Optional, Tuple

ROLES = ("admin", "member", "viewer")
_RANK = {"viewer": 0, "member": 1, "admin": 2}

KEY_PREFIX = "rl_"
PREFIX_LENGTH = 8
MAX_LABEL = 100
INITIAL_KEY_LABEL = "initial"

MAX_FAILURES = 20
FAILURE_WINDOW_S = 300.0
MAX_TRACKED_ADDRESSES = 10000

SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 1000


class InvalidKey(ValueError):
    """A key request failed validation. The message is safe to send to the client."""


def generate_key() -> str:
    # 30 random bytes encode to exactly 40 URL-safe characters.
    return KEY_PREFIX + secrets.token_urlsafe(30)


def new_key_id() -> str:
    return secrets.token_urlsafe(12)


def key_prefix(key: str) -> str:
    return key[:PREFIX_LENGTH]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def allows(role: str, minimum: str) -> bool:
    """True when `role` is `minimum` or stronger (viewer < member < admin)."""
    return _RANK.get(role, -1) >= _RANK[minimum]


def validate_label(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidKey("'label' is required.")
    label = value.strip()
    if len(label) > MAX_LABEL:
        raise InvalidKey(f"'label' must be at most {MAX_LABEL} characters.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
        raise InvalidKey("'label' must not contain control characters.")
    return label


def validate_role(value: Any) -> str:
    if value not in ROLES:
        raise InvalidKey("'role' must be one of admin, member, viewer.")
    return value


@dataclass(frozen=True)
class Identity:
    """Who is making a request. Built from the key row on every request, so a revoked
    key or a changed label takes effect at once."""

    team_id: int
    team_name: str
    key_id: str
    label: str
    role: str
    prefix: Optional[str]
    via: str  # "key" for an API key, "session" for a dashboard cookie

    def actor(self) -> str:
        """Who did it, as written to the audit log. Never the key itself."""
        if self.via == "session":
            return f"dashboard:{self.label}"
        return f"{self.label} ({self.prefix})" if self.prefix else self.label


def identity_from_row(row: Mapping[str, Any], via: str) -> Identity:
    return Identity(
        team_id=row["team_id"],
        team_name=row["team_name"],
        key_id=row["id"],
        label=row["label"],
        role=row["role"],
        prefix=row["prefix"],
        via=via,
    )


class FailureLimiter:
    """Counts failed credentials per client address. Once an address has more than
    max_failures inside the window, its requests get 429 until enough failures age out."""

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window_s: float = FAILURE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: Dict[str, Deque[float]] = {}

    def _recent(self, address: str, now: float) -> Optional[Deque[float]]:
        """The failures of `address` still inside the window (caller holds the lock)."""
        times = self._failures.get(address)
        if times is None:
            return None
        while times and now - times[0] >= self.window_s:
            times.popleft()
        if not times:
            del self._failures[address]
            return None
        return times

    def blocked_for(self, address: str) -> int:
        """Seconds until this address may try again, or 0 when it may."""
        with self._lock:
            now = self._clock()
            times = self._recent(address, now)
            if times is None or len(times) <= self.max_failures:
                return 0
            # Requests are allowed again once this many of the oldest failures have expired.
            excess = len(times) - self.max_failures
            return max(1, math.ceil(times[excess - 1] + self.window_s - now))

    def record_failure(self, address: str) -> None:
        with self._lock:
            now = self._clock()
            times = self._recent(address, now)
            if times is None:
                if len(self._failures) >= MAX_TRACKED_ADDRESSES:
                    for other in list(self._failures):
                        self._recent(other, now)
                times = self._failures.setdefault(address, deque())
            times.append(now)


class SessionStore:
    """Dashboard sessions: random opaque tokens kept in memory, so no key sits in a cookie.
    A server restart signs everyone out."""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        ttl_s: float = SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_sessions = max_sessions
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, Tuple[Dict[str, Any], float]]" = OrderedDict()

    def issue(self, data: Dict[str, Any]) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[token] = (dict(data), self._clock() + self.ttl_s)
            while len(self._items) > self.max_sessions:
                self._items.popitem(last=False)
        return token

    def get(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            data, expires = item
            if expires < self._clock():
                del self._items[token]
                return None
            return dict(data)

    def drop(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._items.pop(token, None)
