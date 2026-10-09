"""SQLite storage for the RunLedger team server.

Runs are scoped to a team. The primary key is (team_id, id), so two teams can
hold the same session id without overwriting each other. API keys live in
api_keys, stored only as SHA-256 hashes (see auth.py); the plaintext is returned
once, by create_team(), create_key() or rotate_key().

Every change to keys, team settings, budgets, approvals and pushed runs also writes a row
to audit_log, in the same transaction as the change itself. Dashboard sessions are kept
in the sessions table as SHA-256 hashes of their tokens, so they survive a restart.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..pricing import friendly_model
from .billing import ACTIVE, ENDED, PAST_DUE
from .insights import AI_REVIEW_LIKE, QUALITY_LIKE, RECOMMENDATIONS_LIKE, review_fields
from .auth import (
    INITIAL_KEY_LABEL,
    SESSION_TTL_SECONDS,
    generate_key,
    hash_key,
    key_prefix,
    new_key_id,
    new_session_token,
    validate_label,
    validate_role,
)

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LAST_USED_WRITE_INTERVAL_S = 60
DEFAULT_ALERT_THRESHOLDS = "[50, 80, 100]"

# Team settings added after the first release. Databases created earlier get them
# through _add_missing_columns().
TEAM_SETTING_COLUMNS = (
    ("slack_webhook_url", "TEXT"),
    ("webhook_url", "TEXT"),
    ("approval_ttl_s", "INTEGER NOT NULL DEFAULT 600"),
    ("monthly_budget_usd", "REAL"),
    ("per_user_budget_usd", "REAL"),
    ("alert_thresholds", "TEXT NOT NULL DEFAULT '[50, 80, 100]'"),
)

# Run columns added after the first release.
RUN_COLUMNS = (
    ("agent", "TEXT"),
)

# Columns for the quality score, the AI verdict and the estimated savings of a run. They are
# derived from the receipt (see insights.review_fields). Older databases get them in
# _upgrade_review_columns(), which also fills them from receipts already stored.
REVIEW_COLUMNS = (
    ("quality_score", "INTEGER"),
    ("quality_grade", "TEXT"),
    ("ai_verdict", "TEXT"),
    ("est_savings_usd", "REAL"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    api_key_hash    TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    slack_webhook_url TEXT,
    webhook_url     TEXT,
    approval_ttl_s  INTEGER NOT NULL DEFAULT 600,
    monthly_budget_usd  REAL,
    per_user_budget_usd REAL,
    alert_thresholds    TEXT NOT NULL DEFAULT '[50, 80, 100]'
);
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    team_id      INTEGER NOT NULL REFERENCES teams (id),
    session_id   TEXT NOT NULL,
    tool         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    risks        TEXT NOT NULL DEFAULT '[]',
    cwd          TEXT,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied')),
    decided_by   TEXT,
    decided_at   TEXT,
    reason       TEXT,
    created_at   TEXT NOT NULL,
    created_ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_by_team ON approvals (team_id, status, created_ts);
CREATE TABLE IF NOT EXISTS runs (
    id             TEXT NOT NULL,
    team_id        INTEGER NOT NULL REFERENCES teams (id),
    user           TEXT NOT NULL,
    project        TEXT NOT NULL,
    agent          TEXT,
    title          TEXT,
    started_at     TEXT,
    ended_at       TEXT,
    models         TEXT NOT NULL DEFAULT '{}',
    steps          INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER NOT NULL DEFAULT 0,
    files_changed  INTEGER NOT NULL DEFAULT 0,
    cost           REAL,
    risk_score     INTEGER NOT NULL DEFAULT 0,
    risk_level     TEXT NOT NULL DEFAULT 'low',
    quality_score  INTEGER,
    quality_grade  TEXT,
    ai_verdict     TEXT,
    est_savings_usd REAL,
    receipt_json   TEXT NOT NULL,
    receipt_html   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (team_id, id)
);
CREATE INDEX IF NOT EXISTS runs_by_time ON runs (team_id, started_at);
CREATE TABLE IF NOT EXISTS risks (
    team_id   INTEGER NOT NULL,
    run_id    TEXT NOT NULL,
    severity  TEXT NOT NULL,
    code      TEXT NOT NULL,
    reason    TEXT NOT NULL,
    step      INTEGER,
    FOREIGN KEY (team_id, run_id) REFERENCES runs (team_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS risks_by_run ON risks (team_id, run_id);
CREATE INDEX IF NOT EXISTS risks_by_code ON risks (team_id, code);
CREATE TABLE IF NOT EXISTS api_keys (
    id            TEXT PRIMARY KEY,
    team_id       INTEGER NOT NULL REFERENCES teams (id),
    label         TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    key_hash      TEXT NOT NULL UNIQUE,
    prefix        TEXT,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT
);
CREATE INDEX IF NOT EXISTS api_keys_by_team ON api_keys (team_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id  INTEGER NOT NULL REFERENCES teams (id),
    at       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT,
    details  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_by_team ON audit_log (team_id, id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    team_id     INTEGER NOT NULL REFERENCES teams (id),
    key_id      TEXT NOT NULL REFERENCES api_keys (id),
    role        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_by_key ON sessions (key_id);
-- One row per team, month, scope and threshold: the primary key is what makes an alert fire once.
CREATE TABLE IF NOT EXISTS budget_alerts (
    team_id    INTEGER NOT NULL REFERENCES teams (id),
    month      TEXT NOT NULL,
    scope      TEXT NOT NULL,
    threshold  INTEGER NOT NULL,
    spend_usd  REAL NOT NULL,
    limit_usd  REAL NOT NULL,
    fired_at   TEXT NOT NULL,
    PRIMARY KEY (team_id, month, scope, threshold)
);
-- Hosted plan (see billing.py). A team with a row here is billed; team_id is NULL once the
-- team has been deleted, and email is then cleared too.
CREATE TABLE IF NOT EXISTS billing_subscriptions (
    polar_id             TEXT PRIMARY KEY,
    team_id              INTEGER UNIQUE REFERENCES teams (id),
    customer_id          TEXT,
    email                TEXT,
    product_id           TEXT,
    status               TEXT NOT NULL,
    seats                INTEGER NOT NULL DEFAULT 1,
    current_period_end   TEXT,
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    past_due_since       TEXT,
    ended_at             TEXT,
    source_modified_at   TEXT,
    welcome_sent_at      TEXT,
    last_recovery_at     TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS billing_by_email ON billing_subscriptions (email);
-- Webhook ids already handled, so a redelivered event is applied once.
CREATE TABLE IF NOT EXISTS billing_events (
    webhook_id   TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
"""

_SUMMARY_COLUMNS = (
    "id, user, project, agent, title, started_at, ended_at, steps, tokens, files_changed, "
    "cost, risk_score, risk_level, quality_score, quality_grade, ai_verdict, est_savings_usd, "
    "created_at, updated_at, (receipt_html IS NOT NULL) AS has_html"
)

_KEY_COLUMNS = "id, label, role, prefix, created_at, last_used_at, revoked_at"

_KEY_JOIN = (
    "SELECT k.id, k.team_id, t.name AS team_name, k.label, k.role, k.prefix, k.key_hash, "
    "k.created_at, k.last_used_at, k.revoked_at FROM api_keys k JOIN teams t ON t.id = k.team_id"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def _contains(text: str) -> str:
    """LIKE pattern for a substring match, with the LIKE wildcards escaped."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_APPROVAL_COLUMNS = (
    "id, session_id, tool, summary, risks, cwd, status, decided_by, decided_at, reason, "
    "created_at, created_ts"
)


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(TIME_FORMAT)


def _approval_view(data: Dict[str, Any], ttl_s: int, now: float) -> Dict[str, Any]:
    """The approval as the API reports it. A pending approval older than the team's TTL reads as expired."""
    status = data["status"]
    if status == "pending" and now - data["created_ts"] > ttl_s:
        status = "expired"
    return {
        "id": data["id"],
        "status": status,
        "decided_by": data["decided_by"],
        "decided_at": data["decided_at"],
        "reason": data["reason"],
        "session_id": data["session_id"],
        "tool": data["tool"],
        "summary": data["summary"],
        "risks": data["risks"],
        "cwd": data["cwd"],
        "created_at": data["created_at"],
        "expires_at": _stamp(data["created_ts"] + ttl_s),
    }


def _approval_row_view(row: sqlite3.Row, ttl_s: int, now: float) -> Dict[str, Any]:
    data = dict(row)
    data["risks"] = json.loads(data["risks"] or "[]")
    return _approval_view(data, ttl_s, now)


def _summary(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "user": row["user"],
        "project": row["project"],
        "agent": row["agent"],
        "title": row["title"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "steps": row["steps"],
        "tokens": row["tokens"],
        "files_changed": row["files_changed"],
        "cost_usd": row["cost"],
        "risk_score": row["risk_score"],
        "risk_level": row["risk_level"],
        "quality_score": row["quality_score"],
        "quality_grade": row["quality_grade"],
        "ai_verdict": row["ai_verdict"],
        "est_savings_usd": row["est_savings_usd"],
        "has_html": bool(row["has_html"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class Database:
    """One SQLite connection shared by all request threads, guarded by a lock."""

    def __init__(self, path: str = "runledger.db") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass  # some filesystems refuse WAL; the default journal still works
            self._conn.executescript(SCHEMA)
            self._add_missing_columns("teams", TEAM_SETTING_COLUMNS)
            self._add_missing_columns("runs", RUN_COLUMNS)
            self._upgrade_review_columns()
            self._migrate_api_keys()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def backup_to(self, path: str) -> None:
        """A consistent copy of the database at `path`, taken while the server keeps running."""
        target = sqlite3.connect(str(path))
        try:
            with self._lock:
                self._conn.backup(target)
        finally:
            target.close()

    def _add_missing_columns(self, table: str, columns: Tuple[Tuple[str, str], ...]) -> None:
        """Upgrade a database created before these columns existed."""
        present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in present:
                with self._conn:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _upgrade_review_columns(self) -> None:
        """Add the review columns to a runs table from before them. A receipt stored by an older
        server kept any quality, recommendations or AI review keys the client sent, so each of
        those receipts is filled in from its receipt_json and rewritten with the validated keys.
        The ALTERs and the backfill share one transaction, so a failure leaves the old layout."""
        present = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)")}
        missing = [(name, ddl) for name, ddl in REVIEW_COLUMNS if name not in present]
        if not missing:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for name, ddl in missing:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")
            candidates = self._conn.execute(
                "SELECT team_id, id, receipt_json FROM runs WHERE receipt_json LIKE ? "
                "OR receipt_json LIKE ? OR receipt_json LIKE ?",
                (QUALITY_LIKE, AI_REVIEW_LIKE, RECOMMENDATIONS_LIKE),
            ).fetchall()
            for row in candidates:
                try:
                    receipt = json.loads(row["receipt_json"])
                except ValueError:
                    continue
                if not isinstance(receipt, dict):
                    continue
                fields = review_fields(receipt)
                receipt.update({k: fields[k] for k in ("quality", "recommendations", "ai_review")})
                self._conn.execute(
                    "UPDATE runs SET quality_score = ?, quality_grade = ?, ai_verdict = ?, "
                    "est_savings_usd = ?, receipt_json = ? WHERE team_id = ? AND id = ?",
                    (fields["quality_score"], fields["quality_grade"], fields["ai_verdict"],
                     fields["est_savings_usd"], json.dumps(receipt, ensure_ascii=False),
                     row["team_id"], row["id"]),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _migrate_api_keys(self) -> None:
        """Before roles, each team had one API key, kept in teams.api_key_hash. That key
        becomes an admin key labelled "initial". Only teams with no key rows are touched,
        so a key that was rotated or revoked is never brought back. The plaintext of an
        old key was never stored, so its prefix stays NULL."""
        legacy = self._conn.execute(
            "SELECT id, api_key_hash, created_at FROM teams "
            "WHERE NOT EXISTS (SELECT 1 FROM api_keys k WHERE k.team_id = teams.id) ORDER BY id"
        ).fetchall()
        if not legacy:
            return
        with self._conn:
            for row in legacy:
                self._conn.execute(
                    "INSERT INTO api_keys (id, team_id, label, role, key_hash, prefix, created_at) "
                    "VALUES (?, ?, ?, 'admin', ?, NULL, ?)",
                    (new_key_id(), row["id"], INITIAL_KEY_LABEL, row["api_key_hash"], row["created_at"]),
                )

    def _audit(self, team_id: int, actor: str, action: str, target: Optional[str], details: Dict[str, Any]) -> None:
        """Write one audit row. The caller holds the lock and the transaction. Details must
        never carry a key or a webhook URL."""
        self._conn.execute(
            "INSERT INTO audit_log (team_id, at, actor, action, target, details) VALUES (?, ?, ?, ?, ?, ?)",
            (team_id, utc_now(), actor, action, target, json.dumps(details, sort_keys=True)),
        )

    def record_audit(
        self, team_id: int, actor: str, action: str, target: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock, self._conn:
            self._audit(team_id, actor, action, target, details or {})

    # Teams and keys

    def create_team(self, name: str, actor: str = "cli") -> Tuple[int, str]:
        """Create a team and its first admin key ("initial"). Returns (team_id, key).
        The key is not stored, only its hash."""
        name = (name or "").strip()
        if not name or len(name) > 100:
            raise ValueError("Team name must be 1-100 characters.")
        with self._lock, self._conn:
            return self._create_team_locked(name, actor)

    def _create_team_locked(self, name: str, actor: str) -> Tuple[int, str]:
        """create_team() inside the caller's lock and transaction."""
        key = generate_key()
        now = utc_now()
        try:
            cur = self._conn.execute(
                # The legacy column keeps the hash of the first key so the NOT NULL and UNIQUE
                # constraints hold. Authentication reads api_keys only.
                "INSERT INTO teams (name, api_key_hash, created_at) VALUES (?, ?, ?)",
                (name, hash_key(key), now),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"A team named {name!r} already exists.") from None
        team_id = int(cur.lastrowid)
        key_id = self._insert_key(team_id, INITIAL_KEY_LABEL, "admin", key, now)
        self._audit(team_id, actor, "key.create", key_id,
                    {"label": INITIAL_KEY_LABEL, "role": "admin", "prefix": key_prefix(key)})
        return team_id, key

    def _insert_key(self, team_id: int, label: str, role: str, key: str, created_at: str) -> str:
        key_id = new_key_id()
        self._conn.execute(
            "INSERT INTO api_keys (id, team_id, label, role, key_hash, prefix, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key_id, team_id, label, role, hash_key(key), key_prefix(key), created_at),
        )
        return key_id

    def team_name(self, team_id: int) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
        return row["name"] if row else None

    def key_team_id(self, key_id: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute("SELECT team_id FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return int(row["team_id"]) if row else None

    def team_for_key(self, key: Optional[str]) -> Optional[Dict[str, Any]]:
        """{"id", "name"} of the team an active key belongs to, or None."""
        row = self.key_for_token(key)
        return {"id": row["team_id"], "name": row["team_name"]} if row else None

    def key_for_token(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """The active (not revoked) key whose secret is `token`, or None. Records the use,
        at most once per LAST_USED_WRITE_INTERVAL_S."""
        if not token or len(token) > 512:
            return None
        with self._lock, self._conn:
            row = self._conn.execute(_KEY_JOIN + " WHERE k.key_hash = ? AND k.revoked_at IS NULL",
                                     (hash_key(token),)).fetchone()
            if row is None:
                return None
            self._touch_key(row["id"])
        return dict(row)

    def active_key(self, key_id: str) -> Optional[Dict[str, Any]]:
        """The active key with this id (used to check a dashboard session), or None."""
        with self._lock, self._conn:
            row = self._conn.execute(_KEY_JOIN + " WHERE k.id = ? AND k.revoked_at IS NULL", (key_id,)).fetchone()
            if row is None:
                return None
            self._touch_key(row["id"])
        return dict(row)

    def _touch_key(self, key_id: str) -> None:
        now = datetime.now(timezone.utc)
        stale_before = (now - timedelta(seconds=LAST_USED_WRITE_INTERVAL_S)).strftime(TIME_FORMAT)
        self._conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR last_used_at <= ?)",
            (now.strftime(TIME_FORMAT), key_id, stale_before),
        )

    # Dashboard sessions

    def issue_session(self, team_id: int, key_id: str, role: str, ttl_s: int = SESSION_TTL_SECONDS) -> str:
        """Start a dashboard session for a key. Returns the plaintext token (for the cookie);
        only its hash is stored. Expired sessions are purged here, so the table stays small."""
        token = new_session_token()
        now = datetime.now(timezone.utc)
        now_text = now.strftime(TIME_FORMAT)
        expires = (now + timedelta(seconds=ttl_s)).strftime(TIME_FORMAT)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_text,))
            self._conn.execute(
                "INSERT INTO sessions (token_hash, team_id, key_id, role, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (hash_key(token), team_id, key_id, role, now_text, expires),
            )
        return token

    def session_key(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """The active key behind a dashboard session token, or None. A session that has
        expired, or whose key was revoked or rotated, is deleted here."""
        if not token or len(token) > 512:
            return None
        token_hash = hash_key(token)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT key_id, team_id, expires_at FROM sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= datetime.now(timezone.utc).strftime(TIME_FORMAT):
                self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            key = self._conn.execute(
                _KEY_JOIN + " WHERE k.id = ? AND k.team_id = ? AND k.revoked_at IS NULL",
                (row["key_id"], row["team_id"]),
            ).fetchone()
            if key is None:
                self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            self._touch_key(key["id"])
        return dict(key)


    def list_keys(self, team_id: int) -> List[Dict[str, Any]]:
        """The team's keys, oldest first. Never includes a secret or a hash."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE team_id = ? ORDER BY created_at, rowid",
                (team_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _key_row(self, team_id: int, key_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE id = ? AND team_id = ?", (key_id, team_id)
        ).fetchone()
        return dict(row) if row else None

    def create_key(self, team_id: int, label: Any, role: Any, actor: str) -> Dict[str, Any]:
        """Create a key. The returned dict includes "key", the only time the plaintext is available.
        Raises InvalidKey for a bad label or role, ValueError for an unknown team."""
        label = validate_label(label)
        role = validate_role(role)
        key = generate_key()
        now = utc_now()
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone() is None:
                raise ValueError(f"No team with id {team_id}.")
            key_id = self._insert_key(team_id, label, role, key, now)
            prefix = key_prefix(key)
            self._audit(team_id, actor, "key.create", key_id, {"label": label, "role": role, "prefix": prefix})
        return {"id": key_id, "label": label, "role": role, "prefix": prefix, "created_at": now, "key": key}

    def revoke_key(self, team_id: int, key_id: str, actor: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Revoke a key. Returns (outcome, view): "ok", "missing", "already_revoked", or
        "last_admin" (the team's only active admin key cannot be revoked)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND team_id = ? AND revoked_at IS NULL "
                "AND (role != 'admin' OR EXISTS (SELECT 1 FROM api_keys o WHERE o.team_id = api_keys.team_id "
                "AND o.role = 'admin' AND o.revoked_at IS NULL AND o.id != api_keys.id))",
                (utc_now(), key_id, team_id),
            )
            row = self._key_row(team_id, key_id)
            if cur.rowcount == 1 and row is not None:
                self._conn.execute("DELETE FROM sessions WHERE key_id = ?", (key_id,))
                self._audit(team_id, actor, "key.revoke", key_id,
                            {"label": row["label"], "role": row["role"], "prefix": row["prefix"]})
                return "ok", row
            if row is None:
                return "missing", None
            if row["revoked_at"] is not None:
                return "already_revoked", row
            return "last_admin", row

    def rotate_key(self, team_id: int, key_id: str, actor: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Replace a key's secret. The id, label and role stay; the old secret stops working at once.
        Returns (outcome, view) where view includes the new "key". Outcomes: "ok", "missing", "revoked"."""
        new_key = generate_key()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE api_keys SET key_hash = ?, prefix = ? WHERE id = ? AND team_id = ? AND revoked_at IS NULL",
                (hash_key(new_key), key_prefix(new_key), key_id, team_id),
            )
            row = self._key_row(team_id, key_id)
            if row is None:
                return "missing", None
            if cur.rowcount != 1:
                return "revoked", row
            # The old secret is gone, so every dashboard session it started ends with it.
            self._conn.execute("DELETE FROM sessions WHERE key_id = ?", (key_id,))
            self._audit(team_id, actor, "key.rotate", key_id,
                        {"label": row["label"], "role": row["role"], "prefix": row["prefix"]})
        return "ok", dict(row, key=new_key)

    def list_audit(
        self, team_id: int, limit: int = 100, before: Optional[int] = None, action: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Audit events for a team, newest first. `before` returns only events with a smaller id.
        `action` keeps events whose action starts with it ("key." matches key.create and key.revoke)."""
        return self.audit_events(team_id, limit=max(1, min(int(limit), 500)), before=before, action=action)

    def audit_events(
        self,
        team_id: int,
        limit: Optional[int] = None,
        before: Optional[int] = None,
        action: Optional[str] = None,
        since: Optional[str] = None,
        ascending: bool = False,
    ) -> List[Dict[str, Any]]:
        """The general form of list_audit. `since` is a TIME_FORMAT text bound on the event time.
        limit=None returns every match (used by the export)."""
        sql = "SELECT id, at, actor, action, target, details FROM audit_log WHERE team_id = ?"
        params: List[Any] = [team_id]
        if since is not None:
            sql += " AND at >= ?"
            params.append(since)
        if action:
            # substr() compares the literal prefix: no LIKE wildcards to escape.
            sql += " AND substr(action, 1, ?) = ?"
            params += [len(action), action]
        if before is not None:
            sql += " AND id < ?"
            params.append(int(before))
        sql += " ORDER BY id " + ("ASC" if ascending else "DESC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {"id": r["id"], "at": r["at"], "actor": r["actor"], "action": r["action"],
             "target": r["target"], "details": json.loads(r["details"] or "{}")}
            for r in rows
        ]

    # Runs

    def upsert_run(self, team_id: int, run: Dict[str, Any], actor: Optional[str] = None) -> None:
        """Insert or replace one run (re-pushing a session updates it). With an actor, the push is audited."""
        now = utc_now()
        models_json = json.dumps(run["models"], sort_keys=True)
        receipt_json = json.dumps(run["receipt"], ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (id, team_id, user, project, agent, title, started_at, ended_at, models,
                                  steps, tokens, files_changed, cost, risk_score, risk_level,
                                  quality_score, quality_grade, ai_verdict, est_savings_usd,
                                  receipt_json, receipt_html, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (team_id, id) DO UPDATE SET
                    user = excluded.user,
                    project = excluded.project,
                    agent = excluded.agent,
                    title = excluded.title,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    models = excluded.models,
                    steps = excluded.steps,
                    tokens = excluded.tokens,
                    files_changed = excluded.files_changed,
                    cost = excluded.cost,
                    risk_score = excluded.risk_score,
                    risk_level = excluded.risk_level,
                    quality_score = excluded.quality_score,
                    quality_grade = excluded.quality_grade,
                    ai_verdict = excluded.ai_verdict,
                    est_savings_usd = excluded.est_savings_usd,
                    receipt_json = excluded.receipt_json,
                    receipt_html = excluded.receipt_html,
                    updated_at = excluded.updated_at
                """,
                (
                    run["id"], team_id, run["user"], run["project"], run["agent"], run["title"],
                    run["started_at"], run["ended_at"], models_json,
                    run["steps"], run["tokens"], run["files_changed"], run["cost"],
                    run["risk_score"], run["risk_level"],
                    run["quality_score"], run["quality_grade"], run["ai_verdict"], run["est_savings_usd"],
                    receipt_json, run["receipt_html"],
                    now, now,
                ),
            )
            self._conn.execute(
                "DELETE FROM risks WHERE team_id = ? AND run_id = ?", (team_id, run["id"])
            )
            self._conn.executemany(
                "INSERT INTO risks (team_id, run_id, severity, code, reason, step) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (team_id, run["id"], r["severity"], r["code"], r["reason"], r["step"])
                    for r in run["risks"]
                ],
            )
            if actor is not None:
                self._audit(team_id, actor, "run.push", run["id"],
                            {"agent": run["agent"], "project": run["project"]})

    def list_runs(
        self,
        team_id: int,
        user: Optional[str] = None,
        project: Optional[str] = None,
        min_risk: Optional[int] = None,
        limit: int = 100,
        agent: Optional[str] = None,
        min_quality: Optional[int] = None,
        max_quality: Optional[int] = None,
        ai_verdict: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = ["team_id = ?"]
        params: List[Any] = [team_id]
        if user:  # case-insensitive substring match, so "bob" finds "bob@example.com"
            clauses.append("user LIKE ? ESCAPE '\\'")
            params.append(_contains(user))
        if project:
            clauses.append("project LIKE ? ESCAPE '\\'")
            params.append(_contains(project))
        if agent:  # "claude" finds "Claude Code"
            clauses.append("agent LIKE ? ESCAPE '\\'")
            params.append(_contains(agent))
        if min_risk is not None:
            clauses.append("risk_score >= ?")
            params.append(int(min_risk))
        # A run with no quality score matches neither bound.
        if min_quality is not None:
            clauses.append("quality_score >= ?")
            params.append(int(min_quality))
        if max_quality is not None:
            clauses.append("quality_score <= ?")
            params.append(int(max_quality))
        if ai_verdict:
            clauses.append("ai_verdict = ?")
            params.append(ai_verdict)
        params.append(max(1, min(int(limit), 500)))
        sql = (
            f"SELECT {_SUMMARY_COLUMNS} FROM runs WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(started_at, created_at) DESC, created_at DESC, id LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_summary(r) for r in rows]

    def get_run(self, team_id: int, run_id: str) -> Optional[Dict[str, Any]]:
        """One run with its receipt (parsed) and risk reasons. The HTML is fetched separately."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SUMMARY_COLUMNS}, receipt_json FROM runs WHERE team_id = ? AND id = ?",
                (team_id, run_id),
            ).fetchone()
            if row is None:
                return None
            risks = self._conn.execute(
                "SELECT severity, code, reason, step FROM risks WHERE team_id = ? AND run_id = ? ORDER BY rowid",
                (team_id, run_id),
            ).fetchall()
        run = _summary(row)
        run["receipt"] = json.loads(row["receipt_json"])
        run["risks"] = [dict(r) for r in risks]
        return run

    def get_receipt_html(self, team_id: int, run_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT receipt_html FROM runs WHERE team_id = ? AND id = ?", (team_id, run_id)
            ).fetchone()
        return row["receipt_html"] if row else None

    # Statistics

    def stats(self, team_id: int, since_days: int = 30, now: Optional[datetime] = None) -> Dict[str, Any]:
        days = max(1, int(since_days))
        moment = now or datetime.now(timezone.utc)
        since = (moment - timedelta(days=days)).strftime(TIME_FORMAT)
        window = "team_id = ? AND COALESCE(started_at, created_at) >= ?"
        args = (team_id, since)
        with self._lock:
            totals = self._conn.execute(
                "SELECT COUNT(*) AS runs, COALESCE(SUM(cost), 0) AS cost_usd, "
                "COALESCE(SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END), 0) AS unpriced_runs, "
                "AVG(risk_score) AS avg_risk, "
                "COALESCE(SUM(CASE WHEN risk_level = 'high' THEN 1 ELSE 0 END), 0) AS high_risk_runs, "
                "COALESCE(SUM(steps), 0) AS steps, COALESCE(SUM(tokens), 0) AS tokens, "
                "COALESCE(SUM(files_changed), 0) AS files_changed "
                f"FROM runs WHERE {window}",
                args,
            ).fetchone()
            by_user = self._group("user", window, args)
            by_project = self._group("project", window, args)
            by_agent = self._group("COALESCE(agent, 'unknown')", window, args)
            model_rows = self._conn.execute(
                f"SELECT models FROM runs WHERE {window}", args
            ).fetchall()
            top = self._conn.execute(
                "SELECT r.code AS code, COUNT(*) AS occurrences, COUNT(DISTINCT r.run_id) AS runs "
                "FROM risks r JOIN runs u ON u.team_id = r.team_id AND u.id = r.run_id "
                "WHERE r.team_id = ? AND COALESCE(u.started_at, u.created_at) >= ? "
                "GROUP BY r.code ORDER BY occurrences DESC, r.code LIMIT 10",
                args,
            ).fetchall()

        models: Dict[str, Dict[str, Any]] = {}
        for row in model_rows:
            for name, info in json.loads(row["models"] or "{}").items():
                label = friendly_model(name)
                slot = models.setdefault(label, {"model": label, "runs": 0, "tokens": 0, "cost_usd": 0.0})
                slot["runs"] += 1
                slot["tokens"] += int(info.get("tokens") or 0)
                if info.get("cost_usd") is not None:
                    slot["cost_usd"] += float(info["cost_usd"])
        by_model = sorted(models.values(), key=lambda m: (-m["cost_usd"], -m["tokens"], m["model"]))

        return {
            "days": days,
            "since": since,
            "totals": {
                "runs": totals["runs"],
                "cost_usd": round(totals["cost_usd"] or 0.0, 6),
                "unpriced_runs": totals["unpriced_runs"],
                "avg_risk": None if totals["avg_risk"] is None else round(totals["avg_risk"], 1),
                "high_risk_runs": totals["high_risk_runs"],
                "steps": totals["steps"],
                "tokens": totals["tokens"],
                "files_changed": totals["files_changed"],
            },
            "by_user": [{"user": r["name"], "runs": r["runs"], "cost_usd": round(r["cost_usd"], 6)} for r in by_user],
            "by_project": [{"project": r["name"], "runs": r["runs"], "cost_usd": round(r["cost_usd"], 6)} for r in by_project],
            "by_agent": [{"agent": r["name"], "runs": r["runs"], "cost_usd": round(r["cost_usd"], 6)} for r in by_agent],
            "by_model": [
                {"model": m["model"], "runs": m["runs"], "tokens": m["tokens"], "cost_usd": round(m["cost_usd"], 6)}
                for m in by_model[:20]
            ],
            "top_risk_codes": [
                {"code": r["code"], "occurrences": r["occurrences"], "runs": r["runs"]} for r in top
            ],
        }

    # Exports and the compliance report (read only)

    def export_runs(self, team_id: int, since: str) -> List[Dict[str, Any]]:
        """Every run that started (or was pushed) at or after `since`, oldest first, with its
        models. Never the receipt JSON or the HTML."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, user, project, agent, models, started_at, created_at, steps, tokens, "
                "files_changed, cost, risk_score, risk_level, title, quality_score, quality_grade, "
                "ai_verdict, est_savings_usd FROM runs "
                "WHERE team_id = ? AND COALESCE(started_at, created_at) >= ? "
                "ORDER BY COALESCE(started_at, created_at), id",
                (team_id, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def insight_parts(self, team_id: int, since: str) -> Dict[str, Any]:
        """The raw rows behind the Insights summary (see insights.compute), for runs that started
        (or were pushed) at or after `since`. Aggregates are computed in SQL. Only the receipts that
        can hold recommendations or an AI review are read back as JSON."""
        window = "team_id = ? AND COALESCE(started_at, created_at) >= ?"
        args = (team_id, since)
        with self._lock:
            quality = self._conn.execute(
                f"SELECT COUNT(quality_score) AS scored, AVG(quality_score) AS avg FROM runs WHERE {window}",
                args,
            ).fetchone()
            grades = self._conn.execute(
                f"SELECT quality_grade AS grade, COUNT(*) AS n FROM runs "
                f"WHERE {window} AND quality_grade IS NOT NULL GROUP BY quality_grade",
                args,
            ).fetchall()
            trend = self._conn.execute(
                "SELECT substr(COALESCE(started_at, created_at), 1, 10) AS day, AVG(quality_score) AS avg "
                f"FROM runs WHERE {window} AND quality_score IS NOT NULL GROUP BY day ORDER BY day",
                args,
            ).fetchall()
            verdicts = self._conn.execute(
                f"SELECT ai_verdict AS verdict, COUNT(*) AS n FROM runs "
                f"WHERE {window} AND ai_verdict IS NOT NULL GROUP BY ai_verdict",
                args,
            ).fetchall()
            savings = self._conn.execute(
                f"SELECT COALESCE(SUM(est_savings_usd), 0) AS total FROM runs WHERE {window}", args,
            ).fetchone()
            agents = self._conn.execute(
                "SELECT COALESCE(agent, 'unknown') AS name, COUNT(*) AS runs, AVG(quality_score) AS avg_quality, "
                "AVG(risk_score) AS avg_risk, COALESCE(SUM(cost), 0) AS cost_usd "
                f"FROM runs WHERE {window} GROUP BY name ORDER BY cost_usd DESC, runs DESC, name",
                args,
            ).fetchall()
            model_rows = self._conn.execute(
                f"SELECT models, quality_score FROM runs WHERE {window}", args,
            ).fetchall()
            review_rows = self._conn.execute(
                f"SELECT receipt_json FROM runs WHERE {window} AND (ai_verdict IS NOT NULL OR receipt_json LIKE ?)",
                args + (RECOMMENDATIONS_LIKE,),
            ).fetchall()
        return {
            "scored": quality["scored"],
            "quality_avg": quality["avg"],
            "grades": [dict(r) for r in grades],
            "trend": [(r["day"], r["avg"]) for r in trend],
            "verdicts": [dict(r) for r in verdicts],
            "est_savings_usd": savings["total"],
            "agents": [dict(r) for r in agents],
            "model_rows": [dict(r) for r in model_rows],
            "review_receipts": [r["receipt_json"] for r in review_rows],
        }

    def risk_level_counts(self, team_id: int, since: str) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT risk_level, COUNT(*) AS n FROM runs WHERE team_id = ? "
                "AND COALESCE(started_at, created_at) >= ? GROUP BY risk_level",
                (team_id, since),
            ).fetchall()
        counts = {"low": 0, "medium": 0, "high": 0}
        for r in rows:
            counts[r["risk_level"]] = counts.get(r["risk_level"], 0) + int(r["n"])
        return counts

    def high_risk_runs(self, team_id: int, since: str, limit: int) -> List[Dict[str, Any]]:
        """Up to `limit` high-risk runs since `since`, riskiest first."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SUMMARY_COLUMNS} FROM runs WHERE team_id = ? AND risk_level = 'high' "
                "AND COALESCE(started_at, created_at) >= ? "
                "ORDER BY risk_score DESC, COALESCE(started_at, created_at) DESC, id LIMIT ?",
                (team_id, since, int(limit)),
            ).fetchall()
        return [_summary(r) for r in rows]

    def decided_approvals(self, team_id: int, since: str, limit: int) -> List[Dict[str, Any]]:
        """Approvals decided at or after `since`, newest decision first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tool, status, decided_by, decided_at FROM approvals "
                "WHERE team_id = ? AND decided_at IS NOT NULL AND decided_at >= ? "
                "ORDER BY decided_at DESC, id LIMIT ?",
                (team_id, since, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # Budgets

    def budget_settings(self, team_id: int) -> Dict[str, Any]:
        """The team's budget: monthly_usd, per_user_monthly_usd (each a number or None) and
        alert_thresholds (a list of whole percentages)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT monthly_budget_usd, per_user_budget_usd, alert_thresholds FROM teams WHERE id = ?",
                (team_id,),
            ).fetchone()
        return {
            "monthly_usd": row["monthly_budget_usd"],
            "per_user_monthly_usd": row["per_user_budget_usd"],
            "alert_thresholds": json.loads(row["alert_thresholds"] or DEFAULT_ALERT_THRESHOLDS),
        }

    def update_budget_settings(self, team_id: int, values: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Replace the team's budget. The change and its audit row are one transaction."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE teams SET monthly_budget_usd = ?, per_user_budget_usd = ?, alert_thresholds = ? WHERE id = ?",
                (values["monthly_usd"], values["per_user_monthly_usd"], json.dumps(values["alert_thresholds"]), team_id),
            )
            self._audit(team_id, actor, "budget.update", "budget", {
                "monthly_usd": values["monthly_usd"],
                "per_user_monthly_usd": values["per_user_monthly_usd"],
                "alert_thresholds": values["alert_thresholds"],
            })
        return self.budget_settings(team_id)

    def month_spend(self, team_id: int, start: str, end: str) -> Tuple[float, List[Tuple[str, float]]]:
        """Total run cost for runs whose start (or push time, when there is no start) is in [start, end),
        and the same per developer. Runs without a cost count as zero."""
        window = "team_id = ? AND COALESCE(started_at, created_at) >= ? AND COALESCE(started_at, created_at) < ?"
        args = (team_id, start, end)
        with self._lock:
            total = self._conn.execute(f"SELECT COALESCE(SUM(cost), 0) FROM runs WHERE {window}", args).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT user AS name, COALESCE(SUM(cost), 0) AS spend FROM runs WHERE {window} GROUP BY user",
                args,
            ).fetchall()
        return float(total or 0.0), [(r["name"], float(r["spend"])) for r in rows]

    def record_budget_alerts(self, team_id: int, month: str, candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Record the candidate alerts that have not fired this month. Each insert is conditional
        on the unique key, so a threshold fires once per month even when pushes race. Every new
        alert gets its audit row in the same transaction. Returns the candidates that were new."""
        fired: List[Dict[str, Any]] = []
        now_text = utc_now()
        with self._lock, self._conn:
            for item in candidates:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO budget_alerts (team_id, month, scope, threshold, spend_usd, limit_usd, fired_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (team_id, month, item["scope"], item["threshold"], item["spend_usd"], item["limit_usd"], now_text),
                )
                if cur.rowcount == 1:
                    self._audit(team_id, "system", "budget.alert", item["scope"], {
                        "month": month, "threshold": item["threshold"], "spend_usd": item["spend_usd"],
                        "limit_usd": item["limit_usd"], "pct": item["pct"],
                    })
                    fired.append(item)
        return fired

    def _group(self, column: str, window: str, args: Tuple[Any, ...]) -> List[sqlite3.Row]:
        # column is always a fixed expression from this module ("user", "project", ...)
        return self._conn.execute(
            f"SELECT {column} AS name, COUNT(*) AS runs, COALESCE(SUM(cost), 0) AS cost_usd "
            f"FROM runs WHERE {window} GROUP BY {column} "
            "ORDER BY cost_usd DESC, runs DESC, name LIMIT 20",
            args,
        ).fetchall()

    # Team approval settings

    def approval_settings(self, team_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT slack_webhook_url, webhook_url, approval_ttl_s FROM teams WHERE id = ?", (team_id,)
            ).fetchone()
        return {
            "slack_webhook_url": row["slack_webhook_url"],
            "webhook_url": row["webhook_url"],
            "approval_ttl_s": int(row["approval_ttl_s"]),
        }

    def approval_ttl_s(self, team_id: int) -> int:
        return self.approval_settings(team_id)["approval_ttl_s"]

    def update_approval_settings(
        self, team_id: int, changes: Dict[str, Any], actor: Optional[str] = None
    ) -> Dict[str, Any]:
        """Apply validated settings. Only the three known column names can reach the SQL text.
        The audit entry names the fields that changed, never their values (the URLs are secrets)."""
        columns = [c for c in ("slack_webhook_url", "webhook_url", "approval_ttl_s") if c in changes]
        if columns:
            sql = "UPDATE teams SET " + ", ".join(f"{c} = ?" for c in columns) + " WHERE id = ?"
            with self._lock, self._conn:
                self._conn.execute(sql, [changes[c] for c in columns] + [team_id])
                if actor is not None:
                    details: Dict[str, Any] = {"fields": sorted(columns)}
                    if "approval_ttl_s" in changes:
                        details["approval_ttl_s"] = changes["approval_ttl_s"]
                    self._audit(team_id, actor, "team.settings", "settings", details)
        return self.approval_settings(team_id)

    # Approvals

    def create_approval(
        self, team_id: int, approval_id: str, approval: Dict[str, Any], ttl_s: int
    ) -> Dict[str, Any]:
        """Store a new pending approval. Returns its API view (the same shape GET returns)."""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals (id, team_id, session_id, tool, summary, risks, cwd, status, "
                "created_at, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    approval_id, team_id, approval["session_id"], approval["tool"], approval["summary"],
                    json.dumps(approval["risks"], ensure_ascii=False), approval["cwd"], _stamp(now), now,
                ),
            )
        data = {
            "id": approval_id, "status": "pending", "decided_by": None, "decided_at": None,
            "reason": None, "session_id": approval["session_id"], "tool": approval["tool"],
            "summary": approval["summary"], "risks": approval["risks"], "cwd": approval["cwd"],
            "created_at": _stamp(now), "created_ts": now,
        }
        return _approval_view(data, ttl_s, now)

    def get_approval(self, team_id: int, approval_id: str, ttl_s: int) -> Optional[Dict[str, Any]]:
        """One approval of this team, or None. Another team's approval reads as missing."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE id = ? AND team_id = ?",
                (approval_id, team_id),
            ).fetchone()
        return _approval_row_view(row, ttl_s, time.time()) if row else None

    def list_approvals(
        self, team_id: int, status: Optional[str], ttl_s: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Newest first. "pending" and "expired" are split by the TTL cutoff, not by a stored status."""
        now = time.time()
        cutoff = now - ttl_s
        clauses = ["team_id = ?"]
        params: List[Any] = [team_id]
        if status == "pending":
            clauses += ["status = 'pending'", "created_ts >= ?"]
            params.append(cutoff)
        elif status == "expired":
            clauses += ["status = 'pending'", "created_ts < ?"]
            params.append(cutoff)
        elif status:
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, min(int(limit), 200)))
        sql = (
            f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE {' AND '.join(clauses)} "
            "ORDER BY created_ts DESC, id LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_approval_row_view(r, ttl_s, now) for r in rows]

    def decide_approval(
        self,
        team_id: int,
        approval_id: str,
        status: str,
        decided_by: str,
        reason: Optional[str],
        ttl_s: int,
        actor: Optional[str] = None,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Record approved/denied on a pending, unexpired approval, atomically. With an actor,
        the decision is audited in the same transaction.

        Returns (outcome, view): "decided", "missing" (no such approval for this team),
        "expired", or "conflict" (already decided)."""
        now = time.time()
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, reason = ? "
                    "WHERE id = ? AND team_id = ? AND status = 'pending' AND created_ts >= ?",
                    (status, decided_by, _stamp(now), reason, approval_id, team_id, now - ttl_s),
                )
                decided = cur.rowcount == 1
                if decided and actor is not None:
                    self._audit(team_id, actor, "approval.decide", approval_id, {"status": status})
            view = self.get_approval(team_id, approval_id, ttl_s)
        if view is None:
            return "missing", None
        if decided:
            return "decided", view
        if view["status"] == "expired":
            return "expired", view
        return "conflict", view

    # Billing (see billing.py)

    def billing_event_seen(self, webhook_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM billing_events WHERE webhook_id = ?", (webhook_id,)).fetchone()
        return row is not None

    def record_billing_event(self, webhook_id: str, event_type: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO billing_events (webhook_id, type, received_at) VALUES (?, ?, ?)",
                (webhook_id, str(event_type)[:100], utc_now()),
            )

    def apply_subscription(self, sub: Dict[str, Any], actor: str = "billing:polar") -> Dict[str, Any]:
        """Store a subscription from a webhook (see billing.parse_subscription) and create its team
        the first time it is active. Returns the stored row plus:

          stale            the event was older than what is stored, or late for a deleted team: ignored
          previous_status  the status before this event (None for a new subscription)
          created_key      the new team's admin key, when this event created the team
          team_name        the team's name, or None

        Polar may deliver events out of order, so an event whose modified_at is older than the one
        stored is ignored, and "incomplete" never replaces a later status."""
        now = utc_now()
        status = sub["status"]
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM billing_subscriptions WHERE polar_id = ?", (sub["id"],)
            ).fetchone()
            previous = row["status"] if row else None
            extra: Dict[str, Any] = {"stale": False, "previous_status": previous, "created_key": None}
            if row is not None:
                older = bool(sub["modified_at"] and row["source_modified_at"]
                             and sub["modified_at"] < row["source_modified_at"])
                regress = status == "incomplete" and previous != "incomplete"
                deleted = row["team_id"] is None and row["ended_at"] is not None and status not in ACTIVE
                if older or regress or deleted:
                    extra["stale"] = True
                    return self._billing_result(row, extra)
            past_due_since = ((row["past_due_since"] if row else None) or now) if status in PAST_DUE else None
            ended_at = ((row["ended_at"] if row else None) or sub["ended_at"] or now) if status in ENDED else None
            team_id = row["team_id"] if row else None
            if team_id is None and status in ACTIVE:
                team_id, key = self._create_team_locked(self._free_team_name(sub["team_name"]), actor)
                extra["created_key"] = key
                self._audit(team_id, actor, "billing.provision", sub["id"], {"seats": sub["seats"]})
            values = (
                team_id, sub["customer_id"], sub["email"], sub["product_id"], status, sub["seats"],
                sub["current_period_end"], int(sub["cancel_at_period_end"]), past_due_since, ended_at,
                sub["modified_at"], now,
            )
            if row is None:
                self._conn.execute(
                    "INSERT INTO billing_subscriptions (team_id, customer_id, email, product_id, status, seats, "
                    "current_period_end, cancel_at_period_end, past_due_since, ended_at, source_modified_at, "
                    "updated_at, created_at, polar_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values + (now, sub["id"]),
                )
            else:
                self._conn.execute(
                    "UPDATE billing_subscriptions SET team_id = ?, customer_id = ?, email = ?, product_id = ?, "
                    "status = ?, seats = ?, current_period_end = ?, cancel_at_period_end = ?, past_due_since = ?, "
                    "ended_at = ?, source_modified_at = ?, updated_at = ? WHERE polar_id = ?",
                    values + (sub["id"],),
                )
            changed = row is None or (previous, row["seats"], bool(row["cancel_at_period_end"])) != (
                status, sub["seats"], bool(sub["cancel_at_period_end"]))
            if team_id is not None and changed:
                self._audit(team_id, actor, "billing.subscription", sub["id"], {
                    "status": status, "seats": sub["seats"],
                    "cancel_at_period_end": bool(sub["cancel_at_period_end"]),
                })
            stored = self._conn.execute(
                "SELECT * FROM billing_subscriptions WHERE polar_id = ?", (sub["id"],)
            ).fetchone()
            return self._billing_result(stored, extra)

    def _billing_result(self, row: sqlite3.Row, extra: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(row)
        result.update(extra)
        result["team_name"] = None
        if row["team_id"] is not None:
            name = self._conn.execute("SELECT name FROM teams WHERE id = ?", (row["team_id"],)).fetchone()
            result["team_name"] = name["name"] if name else None
        return result

    def _free_team_name(self, base: str) -> str:
        """`base`, or "base 2", "base 3"... when a team already has that name."""
        name, n = base, 2
        while self._conn.execute("SELECT 1 FROM teams WHERE name = ?", (name,)).fetchone() is not None:
            name = f"{base} {n}"
            n += 1
        return name

    def welcome_key(self, team_id: int, actor: str = "billing:polar") -> str:
        """A fresh secret for the team's first admin key, for a welcome email that has to be sent
        again (the plaintext of the earlier one is gone). Falls back to a new admin key "owner"."""
        new_key = generate_key()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id, label FROM api_keys WHERE team_id = ? AND role = 'admin' AND label = ? "
                "AND revoked_at IS NULL ORDER BY created_at, rowid LIMIT 1",
                (team_id, INITIAL_KEY_LABEL),
            ).fetchone()
            if row is None:
                key_id = self._insert_key(team_id, "owner", "admin", new_key, utc_now())
                self._audit(team_id, actor, "key.create", key_id,
                            {"label": "owner", "role": "admin", "prefix": key_prefix(new_key)})
                return new_key
            self._conn.execute("UPDATE api_keys SET key_hash = ?, prefix = ? WHERE id = ?",
                               (hash_key(new_key), key_prefix(new_key), row["id"]))
            self._conn.execute("DELETE FROM sessions WHERE key_id = ?", (row["id"],))
            self._audit(team_id, actor, "key.rotate", row["id"],
                        {"label": row["label"], "role": "admin", "prefix": key_prefix(new_key)})
        return new_key

    def mark_welcome_sent(self, polar_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE billing_subscriptions SET welcome_sent_at = ? WHERE polar_id = ?",
                               (utc_now(), polar_id))

    def billing_for_team(self, team_id: int) -> Optional[Dict[str, Any]]:
        """The subscription of a billed team, or None for a team created with `runledger team create`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM billing_subscriptions WHERE team_id = ?", (team_id,)
            ).fetchone()
        return dict(row) if row else None

    def seat_users(self, team_id: int, since: str) -> List[str]:
        """The developers who pushed a run at or after `since` (the seats in use)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT user FROM runs WHERE team_id = ? AND updated_at >= ? ORDER BY user",
                (team_id, since),
            ).fetchall()
        return [r["user"] for r in rows]

    def subscriptions_for_email(self, email: str) -> List[Dict[str, Any]]:
        """Subscriptions with a team (not yet deleted) for this customer email."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, t.name AS team_name FROM billing_subscriptions s JOIN teams t ON t.id = s.team_id "
                "WHERE s.email = ? ORDER BY s.created_at",
                (email,),
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_recovery(self, polar_id: str, cutoff: str) -> bool:
        """Record a key recovery for this subscription unless one happened after `cutoff`."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE billing_subscriptions SET last_recovery_at = ? WHERE polar_id = ? "
                "AND (last_recovery_at IS NULL OR last_recovery_at < ?)",
                (utc_now(), polar_id, cutoff),
            )
        return cur.rowcount == 1

    def list_subscriptions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, t.name AS team_name FROM billing_subscriptions s "
                "LEFT JOIN teams t ON t.id = s.team_id ORDER BY s.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def teams_due_for_deletion(self, ended_before: str) -> List[Dict[str, Any]]:
        """Billed teams whose subscription ended before `ended_before`."""
        placeholders = ", ".join("?" for _ in ENDED)
        with self._lock:
            rows = self._conn.execute(
                "SELECT polar_id, team_id FROM billing_subscriptions WHERE team_id IS NOT NULL "
                f"AND status IN ({placeholders}) AND ended_at IS NOT NULL AND ended_at < ?",
                tuple(ENDED) + (ended_before,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_team(self, team_id: int) -> None:
        """Delete a team and everything it owns. Its subscription row stays, for the billing
        history, without the team and without the customer's email."""
        with self._lock, self._conn:
            for table in ("sessions", "risks", "runs", "approvals", "audit_log", "budget_alerts", "api_keys"):
                self._conn.execute(f"DELETE FROM {table} WHERE team_id = ?", (team_id,))
            self._conn.execute(
                "UPDATE billing_subscriptions SET team_id = NULL, email = NULL, updated_at = ? WHERE team_id = ?",
                (utc_now(), team_id),
            )
            self._conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))

    def purge_before(self, cutoff: str) -> Dict[str, int]:
        """Delete, for every team, runs that started (or were pushed) before `cutoff`, and approvals,
        audit events and budget alerts older than it. Returns the number of rows removed per table."""
        counts: Dict[str, int] = {}
        with self._lock, self._conn:
            old_runs = "SELECT team_id, id FROM runs WHERE COALESCE(started_at, created_at) < ?"
            self._conn.execute(
                f"DELETE FROM risks WHERE (team_id, run_id) IN ({old_runs})", (cutoff,)
            )
            counts["runs"] = self._conn.execute(
                "DELETE FROM runs WHERE COALESCE(started_at, created_at) < ?", (cutoff,)
            ).rowcount
            counts["approvals"] = self._conn.execute(
                "DELETE FROM approvals WHERE created_at < ?", (cutoff,)
            ).rowcount
            counts["audit_log"] = self._conn.execute(
                "DELETE FROM audit_log WHERE at < ?", (cutoff,)
            ).rowcount
            counts["budget_alerts"] = self._conn.execute(
                "DELETE FROM budget_alerts WHERE fired_at < ?", (cutoff,)
            ).rowcount
            self._conn.execute("DELETE FROM billing_events WHERE received_at < ?", (cutoff,))
        return counts
