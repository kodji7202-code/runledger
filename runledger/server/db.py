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
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

from ..pricing import friendly_model
from .insights import AI_REVIEW_LIKE, QUALITY_LIKE, RECOMMENDATIONS_LIKE, review_fields
from .auth import (
    INITIAL_KEY_LABEL,
    SESSION_CAP_PER_KEY,
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
# A backup copies in small steps and waits between them, so a busy server answers requests
# while it runs (and a push that lands mid-copy only restarts the copy, never the server).
BACKUP_PAGES = 256
BACKUP_SLEEP_S = 0.01
PRIVATE_FILE_MODE = 0o600


def _restrict_file(path: str) -> None:
    """Make a database or backup file readable only by its owner where the OS has modes.
    On Windows, and on filesystems that refuse chmod, this does nothing."""
    try:
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError:
        pass


# Team settings added after the first release. Databases created earlier get them
# through _add_missing_columns().
TEAM_SETTING_COLUMNS = (
    ("slack_webhook_url", "TEXT"),
    ("webhook_url", "TEXT"),
    ("approval_ttl_s", "INTEGER NOT NULL DEFAULT 600"),
    ("monthly_budget_usd", "REAL"),
    ("per_user_budget_usd", "REAL"),
    ("alert_thresholds", "TEXT NOT NULL DEFAULT '[50, 80, 100]'"),
    ("attribution", "TEXT NOT NULL DEFAULT 'client'"),
)

# Run columns added after the first release.
RUN_COLUMNS = (
    ("agent", "TEXT"),
    ("pushed_by_key_id", "TEXT"),
    ("pushed_by", "TEXT"),
)

# Run columns and the version history added with superseding runs and the extracted insight
# inputs: revisions, how the cost was measured, the agent id, the window key and the copies of
# the recommendations and AI assessment the summary counts. Older databases get them (with a
# backfill) in _upgrade_run_store().
RUN_STORE_COLUMNS = (
    ("revision", "INTEGER NOT NULL DEFAULT 1"),
    ("usage_source", "TEXT"),
    ("cost_partial", "INTEGER NOT NULL DEFAULT 0"),
    ("agent_id", "TEXT"),
    ("sort_at", "TEXT NOT NULL DEFAULT ''"),
    ("recommendations", "TEXT"),
    ("ai_assessment", "TEXT"),
)
RUN_STORE_KEYS = ("recommendations", "ai_assessment")
# How many replaced versions of one run are kept. The run itself is always the newest version.
RUN_REVISION_LIMIT = 20

# Approval requester identity was added after the first approval release. It is
# internal provenance: API views expose the attested display value, not the key id.
APPROVAL_IDENTITY_COLUMNS = (
    ("requester_key_id", "TEXT"),
    ("requested_by", "TEXT"),
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
    alert_thresholds    TEXT NOT NULL DEFAULT '[50, 80, 100]',
    attribution         TEXT NOT NULL DEFAULT 'client'
);
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    team_id      INTEGER NOT NULL REFERENCES teams (id),
    session_id   TEXT NOT NULL,
    tool         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    risks        TEXT NOT NULL DEFAULT '[]',
    cwd          TEXT,
    requester_key_id TEXT,
    requested_by TEXT,
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
    agent_id       TEXT,
    title          TEXT,
    started_at     TEXT,
    ended_at       TEXT,
    sort_at        TEXT NOT NULL DEFAULT '',
    models         TEXT NOT NULL DEFAULT '{}',
    steps          INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER NOT NULL DEFAULT 0,
    files_changed  INTEGER NOT NULL DEFAULT 0,
    cost           REAL,
    usage_source   TEXT,
    cost_partial   INTEGER NOT NULL DEFAULT 0,
    risk_score     INTEGER NOT NULL DEFAULT 0,
    risk_level     TEXT NOT NULL DEFAULT 'low',
    quality_score  INTEGER,
    quality_grade  TEXT,
    ai_verdict     TEXT,
    est_savings_usd REAL,
    recommendations TEXT,
    ai_assessment  TEXT,
    revision       INTEGER NOT NULL DEFAULT 1,
    receipt_json   TEXT NOT NULL,
    receipt_html   TEXT,
    pushed_by_key_id TEXT,
    pushed_by      TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (team_id, id)
);
CREATE INDEX IF NOT EXISTS runs_by_time ON runs (team_id, started_at);
-- One row per replaced version of a run. A session pushed again by its own key (or by an admin
-- key) keeps the version it replaced here, newest RUN_REVISION_LIMIT versions only. The purge
-- deletes these rows with their run.
CREATE TABLE IF NOT EXISTS run_revisions (
    team_id      INTEGER NOT NULL,
    run_id       TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    replaced_at  TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_html TEXT,
    risk_score   INTEGER,
    cost         REAL,
    pushed_by_key_id TEXT,
    PRIMARY KEY (team_id, run_id, revision)
);
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
"""

_SUMMARY_COLUMNS = (
    "id, user, project, agent, agent_id, pushed_by, title, started_at, ended_at, steps, tokens, "
    "files_changed, cost, usage_source, cost_partial, risk_score, risk_level, quality_score, "
    "quality_grade, ai_verdict, est_savings_usd, revision, created_at, updated_at, "
    "(receipt_html IS NOT NULL) AS has_html"
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


def _canonical_json(value: Any) -> str:
    """Type-sensitive canonical JSON for immutable receipt equality.

    Comparing decoded Python values directly is unsafe here because bool is a
    subclass of int, so True == 1. JSON serialization preserves that type
    distinction while normalizing object key order and insignificant spacing.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


_APPROVAL_COLUMNS = (
    "id, session_id, tool, summary, risks, cwd, requester_key_id, requested_by, "
    "status, decided_by, decided_at, reason, "
    "created_at, created_ts"
)


def _extraction_json(receipt: Dict[str, Any]) -> Tuple[str, str]:
    """The two extracted insight inputs of a receipt, as the runs columns store them: the
    recommendation entries the summary counts, and the assessed/false-positive pair of its AI
    review. Both are always written, so a report never has to read receipt_json."""
    fields = review_fields(receipt)
    return (
        json.dumps(fields["recommendation_counts"], ensure_ascii=False),
        json.dumps(fields["assessment_counts"]),
    )


def _supersedes(existing: sqlite3.Row, run: Dict[str, Any], key_id: Optional[str], role: Optional[str]) -> bool:
    """Whether a new push may replace the stored version of a run: the same key pushed it (or an
    admin key), and the new receipt is not a regression (an older end time, or fewer steps).

    A run stored before key attribution has no pushing key at all (the migration leaves the
    column NULL), so no member key can be the one that pushed it: only an admin may replace it."""
    if role != "admin" and existing["pushed_by_key_id"] != key_id:
        return False
    old_ended, new_ended = existing["ended_at"], run["ended_at"]
    if old_ended is not None and new_ended is not None and new_ended < old_ended:
        return False
    return int(run["steps"]) >= int(existing["steps"] or 0)


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
        "requested_by": data.get("requested_by"),
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
        "agent_id": row["agent_id"],
        "pushed_by": row["pushed_by"],
        "title": row["title"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "steps": row["steps"],
        "tokens": row["tokens"],
        "files_changed": row["files_changed"],
        "cost_usd": row["cost"],
        "usage_source": row["usage_source"],
        "cost_partial": bool(row["cost_partial"]),
        "risk_score": row["risk_score"],
        "risk_level": row["risk_level"],
        "quality_score": row["quality_score"],
        "quality_grade": row["quality_grade"],
        "ai_verdict": row["ai_verdict"],
        "est_savings_usd": row["est_savings_usd"],
        "revision": row["revision"],
        "has_html": bool(row["has_html"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _group_row(row: sqlite3.Row, name_key: str) -> Dict[str, Any]:
    """One breakdown row. A group with no priced run has a null cost, never a fake zero."""
    return {
        name_key: row["name"],
        "runs": row["runs"],
        "cost_usd": None if row["cost_usd"] is None else round(row["cost_usd"], 6),
        "priced_runs": row["priced_runs"],
        "unpriced_runs": row["unpriced_runs"],
        "cost_is_lower_bound": bool(row["unpriced_runs"]),
    }


def _in_memory(path: str) -> bool:
    """Whether a second connection would be a different, empty database."""
    return path == ":memory:" or ":memory:" in path


def _read_only_uri(path: str) -> str:
    """A `file:` URI for a second, read-only connection to the same database file. SQLite
    percent-decodes the path, so a Windows drive letter, a space or a non-ASCII name works."""
    text = path.replace("\\", "/") if os.sep == "\\" else path
    return "file:" + quote(text, safe="/:") + "?mode=ro"


class Database:
    """One SQLite connection shared by all request threads, guarded by a lock, and a second,
    read-only connection for reports and exports: a long report never holds the lock the writers
    need, and a writer never holds up a report. An in-memory database keeps the single
    connection, because a second connection to ":memory:" would be a different database."""

    def __init__(self, path: str = "runledger.db") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._read_lock = threading.Lock()
        self._read_conn: Optional[sqlite3.Connection] = None
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        _restrict_file(self.path)
        try:
            with self._lock:
                self._conn.execute("PRAGMA foreign_keys = ON")
                try:
                    self._conn.execute("PRAGMA journal_mode = WAL")
                except sqlite3.DatabaseError:
                    pass  # some filesystems refuse WAL; the default journal still works
                self._conn.executescript(SCHEMA)
                self._add_missing_columns("teams", TEAM_SETTING_COLUMNS)
                self._add_missing_columns("runs", RUN_COLUMNS)
                self._add_missing_columns("approvals", APPROVAL_IDENTITY_COLUMNS)
                self._upgrade_run_store()
                self._upgrade_review_columns()
                self._migrate_api_keys()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock, self._read_lock:
            if self._read_conn is not None:
                self._read_conn.close()
                self._read_conn = None
            self._conn.close()

    def _read_connection(self) -> Optional[sqlite3.Connection]:
        """The second, read-only connection, opened on first use. None when there is no second
        connection to open: an in-memory database, or a file this process cannot reopen."""
        if _in_memory(self.path):
            return None
        with self._read_lock:
            if self._read_conn is None:
                try:
                    self._read_conn = sqlite3.connect(
                        _read_only_uri(self.path), uri=True, check_same_thread=False, timeout=10
                    )
                    self._read_conn.row_factory = sqlite3.Row
                except sqlite3.Error:
                    self._read_conn = None  # fall back to the shared connection, never fail a read
            return self._read_conn

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """Run a read-only query. On the second connection the report never waits for a writer
        (and never sees an uncommitted one); without one it uses the shared connection."""
        conn = self._read_connection()
        if conn is None:
            with self._lock:
                yield self._conn
            return
        with self._read_lock:
            yield conn

    def _backup_source(self) -> sqlite3.Connection:
        """The connection a backup copies from: its own connection to the same file, so the copy
        never holds the lock the writers and reports need. An in-memory database has no second
        connection and copies through the shared one."""
        if _in_memory(self.path):
            return self._conn
        return sqlite3.connect(self.path, check_same_thread=False, timeout=10)

    def backup_to(self, path: str) -> None:
        """A consistent copy of the database at `path`, taken while the server keeps running.
        The copy reads through its own connection in small steps, so pushes and reports go
        through while it runs."""
        dest = Path(path)
        tmp = dest.with_name(f"{dest.name}.tmp")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        target = sqlite3.connect(str(tmp))
        source: Optional[sqlite3.Connection] = None
        ok = False
        try:
            source = self._backup_source()
            source.backup(target, pages=BACKUP_PAGES, sleep=BACKUP_SLEEP_S)
            ok = True
        finally:
            target.close()
            if source is not None and source is not self._conn:
                source.close()
            if not ok:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        try:
            os.replace(str(tmp), str(dest))
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        _restrict_file(str(dest))

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

    def _upgrade_run_store(self) -> None:
        """Add the version and window columns, the version table and the window index to a
        database from before them, and fill them in: `sort_at` from the window key the queries
        used, and the extracted recommendations and AI assessment from the receipts already
        stored. The ALTERs, the backfill and the index share one transaction, so a failure
        leaves the old layout."""
        present = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)")}
        added = {name for name, _ in RUN_STORE_COLUMNS if name not in present}
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for name, ddl in RUN_STORE_COLUMNS:
                if name in added:
                    self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")
            self._conn.execute(
                "UPDATE runs SET sort_at = COALESCE(started_at, created_at) WHERE sort_at IS NULL OR sort_at = ''"
            )
            if added & set(RUN_STORE_KEYS):
                self._backfill_extractions()
            self._conn.execute("CREATE INDEX IF NOT EXISTS runs_by_sort_at ON runs (team_id, sort_at)")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _backfill_extractions(self) -> None:
        """Fill the extracted recommendation and AI assessment columns from the receipts that
        hold those keys. Runs with neither key have nothing to extract and keep them empty."""
        rows = self._conn.execute(
            "SELECT team_id, id, receipt_json FROM runs WHERE recommendations IS NULL "
            "AND (receipt_json LIKE ? OR receipt_json LIKE ?)",
            (RECOMMENDATIONS_LIKE, AI_REVIEW_LIKE),
        ).fetchall()
        for row in rows:
            try:
                receipt = json.loads(row["receipt_json"])
            except ValueError:
                continue
            if not isinstance(receipt, dict):
                continue
            self._conn.execute(
                "UPDATE runs SET recommendations = ?, ai_assessment = ? WHERE team_id = ? AND id = ?",
                _extraction_json(receipt) + (row["team_id"], row["id"]),
            )

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
        only its hash is stored. Expired sessions are purged here, and a key keeps only its
        SESSION_CAP_PER_KEY newest sessions, so the table stays small."""
        token = new_session_token()
        now = datetime.now(timezone.utc)
        now_text = now.strftime(TIME_FORMAT)
        expires = (now + timedelta(seconds=ttl_s)).strftime(TIME_FORMAT)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_text,))
            self._conn.execute(
                "DELETE FROM sessions WHERE token_hash IN ("
                "SELECT token_hash FROM sessions WHERE key_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (key_id, SESSION_CAP_PER_KEY - 1),
            )
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
        with self._reading() as conn:
            rows = conn.execute(
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
        with self._reading() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {"id": r["id"], "at": r["at"], "actor": r["actor"], "action": r["action"],
             "target": r["target"], "details": json.loads(r["details"] or "{}")}
            for r in rows
        ]

    # Runs

    def upsert_run(
        self,
        team_id: int,
        run: Dict[str, Any],
        actor: Optional[str] = None,
        *,
        key_id: Optional[str] = None,
        role: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store one run, or supersede the version already stored for its session id.

        An identical re-push is idempotent. A different receipt for the same team/session id
        replaces the stored row when the same key pushed it (or the pusher is an admin) and the
        new version is not a regression, keeping the replaced version in run_revisions and
        pruning that history to RUN_REVISION_LIMIT versions. Anything else is a conflict. The
        check, the insert and the update share this lock and one transaction, so concurrent
        pushes cannot replace one another.

        Returns a result dict whose outcome is inserted, duplicate, updated or conflict.
        """
        now = utc_now()
        models_json = json.dumps(run["models"], sort_keys=True)
        receipt_json = json.dumps(run["receipt"], ensure_ascii=False)
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT receipt_json, receipt_html, risk_score, risk_level, cost, steps, ended_at, "
                "revision, pushed_by_key_id, created_at FROM runs WHERE team_id = ? AND id = ?",
                (team_id, run["id"]),
            ).fetchone()
            if existing is not None:
                try:
                    same_receipt = _canonical_json(json.loads(existing["receipt_json"])) == _canonical_json(run["receipt"])
                except (TypeError, ValueError):
                    same_receipt = False
                if same_receipt and existing["receipt_html"] == run["receipt_html"]:
                    return {
                        "outcome": "duplicate",
                        "risk_score": existing["risk_score"],
                        "risk_level": existing["risk_level"],
                        "revision": int(existing["revision"] or 1),
                    }
                if not _supersedes(existing, run, key_id, role):
                    return {"outcome": "conflict"}
                revision = int(existing["revision"] or 1) + 1
                self._conn.execute(
                    "INSERT INTO run_revisions (team_id, run_id, revision, replaced_at, receipt_json, "
                    "receipt_html, risk_score, cost, pushed_by_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (team_id, run["id"], revision - 1, now, existing["receipt_json"],
                     existing["receipt_html"], existing["risk_score"], existing["cost"],
                     existing["pushed_by_key_id"]),
                )
                self._conn.execute(
                    "DELETE FROM run_revisions WHERE team_id = ? AND run_id = ? AND revision <= ?",
                    (team_id, run["id"], revision - RUN_REVISION_LIMIT - 1),
                )
                self._conn.execute(
                    "UPDATE runs SET user = ?, project = ?, agent = ?, agent_id = ?, title = ?, "
                    "started_at = ?, ended_at = ?, sort_at = ?, models = ?, steps = ?, tokens = ?, "
                    "files_changed = ?, cost = ?, usage_source = ?, cost_partial = ?, risk_score = ?, "
                    "risk_level = ?, quality_score = ?, quality_grade = ?, ai_verdict = ?, "
                    "est_savings_usd = ?, recommendations = ?, ai_assessment = ?, receipt_json = ?, "
                    "receipt_html = ?, pushed_by_key_id = ?, pushed_by = ?, revision = ?, updated_at = ? "
                    "WHERE team_id = ? AND id = ?",
                    (
                        run["user"], run["project"], run["agent"], run["agent_id"], run["title"],
                        run["started_at"], run["ended_at"], run["started_at"] or existing["created_at"],
                        models_json, run["steps"], run["tokens"], run["files_changed"], run["cost"],
                        run["usage_source"], run["cost_partial"], run["risk_score"], run["risk_level"],
                        run["quality_score"], run["quality_grade"], run["ai_verdict"],
                        run["est_savings_usd"], run["recommendations"], run["ai_assessment"],
                        receipt_json, run["receipt_html"], key_id, run.get("pushed_by"), revision, now,
                        team_id, run["id"],
                    ),
                )
                self._conn.execute("DELETE FROM risks WHERE team_id = ? AND run_id = ?", (team_id, run["id"]))
                self._conn.executemany(
                    "INSERT INTO risks (team_id, run_id, severity, code, reason, step) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (team_id, run["id"], r["severity"], r["code"], r["reason"], r["step"])
                        for r in run["risks"]
                    ],
                )
                if actor is not None:
                    self._audit(team_id, actor, "run.supersede", run["id"],
                                {"revision": revision, "replaced_revision": revision - 1,
                                 "agent": run["agent"], "project": run["project"]})
                return {
                    "outcome": "updated",
                    "revision": revision,
                    "risk_score": run["risk_score"],
                    "risk_level": run["risk_level"],
                }

            self._conn.execute(
                """
                INSERT INTO runs (id, team_id, user, project, agent, agent_id, title, started_at, ended_at, sort_at, models,
                                  steps, tokens, files_changed, cost, usage_source, cost_partial,
                                  risk_score, risk_level,
                                  quality_score, quality_grade, ai_verdict, est_savings_usd, recommendations,
                                  ai_assessment, revision, receipt_json, receipt_html, pushed_by_key_id,
                                  pushed_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["id"], team_id, run["user"], run["project"], run["agent"], run["agent_id"],
                    run["title"], run["started_at"], run["ended_at"], run["started_at"] or now, models_json,
                    run["steps"], run["tokens"], run["files_changed"], run["cost"], run["usage_source"],
                    run["cost_partial"], run["risk_score"], run["risk_level"],
                    run["quality_score"], run["quality_grade"], run["ai_verdict"], run["est_savings_usd"],
                    run["recommendations"], run["ai_assessment"],
                    receipt_json, run["receipt_html"], key_id, run.get("pushed_by"),
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
        return {
            "outcome": "inserted",
            "risk_score": run["risk_score"],
            "risk_level": run["risk_level"],
            "revision": 1,
        }

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
            "ORDER BY sort_at DESC, created_at DESC, id LIMIT ?"
        )
        with self._reading() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_summary(r) for r in rows]

    def get_run(self, team_id: int, run_id: str) -> Optional[Dict[str, Any]]:
        """One run with its receipt (parsed) and risk reasons. The HTML is fetched separately."""
        with self._reading() as conn:
            row = conn.execute(
                f"SELECT {_SUMMARY_COLUMNS}, receipt_json FROM runs WHERE team_id = ? AND id = ?",
                (team_id, run_id),
            ).fetchone()
            if row is None:
                return None
            risks = conn.execute(
                "SELECT severity, code, reason, step FROM risks WHERE team_id = ? AND run_id = ? ORDER BY rowid",
                (team_id, run_id),
            ).fetchall()
        run = _summary(row)
        run["receipt"] = json.loads(row["receipt_json"])
        run["risks"] = [dict(r) for r in risks]
        return run

    def get_receipt_html(self, team_id: int, run_id: str) -> Optional[str]:
        with self._reading() as conn:
            row = conn.execute(
                "SELECT receipt_html FROM runs WHERE team_id = ? AND id = ?", (team_id, run_id)
            ).fetchone()
        return row["receipt_html"] if row else None

    # Statistics

    def stats(self, team_id: int, since_days: int = 30, now: Optional[datetime] = None) -> Dict[str, Any]:
        days = max(1, int(since_days))
        moment = now or datetime.now(timezone.utc)
        since = (moment - timedelta(days=days)).strftime(TIME_FORMAT)
        window = "team_id = ? AND sort_at >= ?"
        args = (team_id, since)
        with self._reading() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS runs, SUM(cost) AS cost_usd, "
                "COALESCE(SUM(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END), 0) AS priced_runs, "
                "COALESCE(SUM(CASE WHEN cost IS NULL OR cost_partial THEN 1 ELSE 0 END), 0) AS unpriced_runs, "
                "COALESCE(SUM(CASE WHEN cost_partial THEN 1 ELSE 0 END), 0) AS partial_runs, "
                "COALESCE(SUM(CASE WHEN usage_source = 'missing' THEN 1 ELSE 0 END), 0) AS missing_usage_runs, "
                "AVG(risk_score) AS avg_risk, "
                "COALESCE(SUM(CASE WHEN risk_level = 'high' THEN 1 ELSE 0 END), 0) AS high_risk_runs, "
                "COALESCE(SUM(steps), 0) AS steps, COALESCE(SUM(tokens), 0) AS tokens, "
                "COALESCE(SUM(files_changed), 0) AS files_changed "
                f"FROM runs WHERE {window}",
                args,
            ).fetchone()
            by_user = self._group(conn, "user", window, args)
            by_project = self._group(conn, "project", window, args)
            by_agent = self._group(conn, "COALESCE(agent, 'unknown')", window, args)
            model_rows = conn.execute(
                f"SELECT models FROM runs WHERE {window}", args
            ).fetchall()
            top = conn.execute(
                "SELECT r.code AS code, COUNT(*) AS occurrences, COUNT(DISTINCT r.run_id) AS runs "
                "FROM risks r JOIN runs u ON u.team_id = r.team_id AND u.id = r.run_id "
                "WHERE r.team_id = ? AND u.sort_at >= ? "
                "GROUP BY r.code ORDER BY occurrences DESC, r.code LIMIT 10",
                args,
            ).fetchall()

        models: Dict[str, Dict[str, Any]] = {}
        for row in model_rows:
            for name, info in json.loads(row["models"] or "{}").items():
                label = friendly_model(name)
                slot = models.setdefault(
                    label, {"model": label, "runs": 0, "tokens": 0, "cost_usd": None, "priced_runs": 0},
                )
                slot["runs"] += 1
                slot["tokens"] += int(info.get("tokens") or 0)
                if info.get("cost_usd") is not None:
                    slot["priced_runs"] += 1
                    slot["cost_usd"] = round((slot["cost_usd"] or 0.0) + float(info["cost_usd"]), 6)
        by_model = sorted(
            models.values(),
            key=lambda m: (m["cost_usd"] is None, -(m["cost_usd"] or 0.0), -m["tokens"], m["model"]),
        )

        return {
            "days": days,
            "since": since,
            "totals": {
                "runs": totals["runs"],
                "cost_usd": None if totals["cost_usd"] is None else round(totals["cost_usd"], 6),
                "priced_runs": totals["priced_runs"],
                "unpriced_runs": totals["unpriced_runs"],
                "partial_runs": totals["partial_runs"],
                "missing_usage_runs": totals["missing_usage_runs"],
                "cost_is_lower_bound": bool(totals["unpriced_runs"]),
                "avg_risk": None if totals["avg_risk"] is None else round(totals["avg_risk"], 1),
                "high_risk_runs": totals["high_risk_runs"],
                "steps": totals["steps"],
                "tokens": totals["tokens"],
                "files_changed": totals["files_changed"],
            },
            "by_user": [_group_row(r, "user") for r in by_user],
            "by_project": [_group_row(r, "project") for r in by_project],
            "by_agent": [_group_row(r, "agent") for r in by_agent],
            "by_model": [
                {
                    "model": m["model"], "runs": m["runs"], "tokens": m["tokens"],
                    "cost_usd": m["cost_usd"], "priced_runs": m["priced_runs"],
                }
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
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT id, user, project, agent, models, started_at, created_at, steps, tokens, "
                "files_changed, cost, risk_score, risk_level, title, quality_score, quality_grade, "
                "ai_verdict, est_savings_usd FROM runs "
                "WHERE team_id = ? AND sort_at >= ? "
                "ORDER BY sort_at, id",
                (team_id, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def insight_parts(self, team_id: int, since: str) -> Dict[str, Any]:
        """The raw rows behind the Insights summary (see insights.compute), for runs that started
        (or were pushed) at or after `since`. Aggregates are computed in SQL. The recommendations
        and the AI assessments come from the copies the server extracted at push time; only the
        receipts stored before those columns existed are read back as JSON."""
        window = "team_id = ? AND sort_at >= ?"
        args = (team_id, since)
        with self._reading() as conn:
            quality = conn.execute(
                f"SELECT COUNT(quality_score) AS scored, AVG(quality_score) AS avg FROM runs WHERE {window}",
                args,
            ).fetchone()
            grades = conn.execute(
                f"SELECT quality_grade AS grade, COUNT(*) AS n FROM runs "
                f"WHERE {window} AND quality_grade IS NOT NULL GROUP BY quality_grade",
                args,
            ).fetchall()
            trend = conn.execute(
                "SELECT substr(sort_at, 1, 10) AS day, AVG(quality_score) AS avg "
                f"FROM runs WHERE {window} AND quality_score IS NOT NULL GROUP BY day ORDER BY day",
                args,
            ).fetchall()
            verdicts = conn.execute(
                f"SELECT ai_verdict AS verdict, COUNT(*) AS n FROM runs "
                f"WHERE {window} AND ai_verdict IS NOT NULL GROUP BY ai_verdict",
                args,
            ).fetchall()
            savings = conn.execute(
                f"SELECT COALESCE(SUM(est_savings_usd), 0) AS total FROM runs WHERE {window}", args,
            ).fetchone()
            agents = conn.execute(
                "SELECT COALESCE(agent, 'unknown') AS name, COUNT(*) AS runs, AVG(quality_score) AS avg_quality, "
                "AVG(risk_score) AS avg_risk, SUM(cost) AS cost_usd, COUNT(cost) AS priced_runs, "
                "COALESCE(SUM(CASE WHEN cost_partial THEN 1 ELSE 0 END), 0) AS partial_runs "
                f"FROM runs WHERE {window} GROUP BY name "
                "ORDER BY cost_usd IS NULL, cost_usd DESC, runs DESC, name",
                args,
            ).fetchall()
            model_rows = conn.execute(
                f"SELECT models, quality_score FROM runs WHERE {window}", args,
            ).fetchall()
            recommendation_rows = conn.execute(
                f"SELECT recommendations FROM runs WHERE {window} AND recommendations IS NOT NULL", args,
            ).fetchall()
            assessment_rows = conn.execute(
                f"SELECT ai_assessment FROM runs WHERE {window} AND ai_assessment IS NOT NULL", args,
            ).fetchall()
            legacy_receipts = conn.execute(
                f"SELECT receipt_json FROM runs WHERE {window} AND recommendations IS NULL "
                "AND (receipt_json LIKE ? OR receipt_json LIKE ?)",
                args + (RECOMMENDATIONS_LIKE, AI_REVIEW_LIKE),
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
            "recommendation_rows": [dict(r) for r in recommendation_rows],
            "assessment_rows": [dict(r) for r in assessment_rows],
            "legacy_receipts": [r["receipt_json"] for r in legacy_receipts],
        }

    def risk_level_counts(self, team_id: int, since: str) -> Dict[str, int]:
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT risk_level, COUNT(*) AS n FROM runs WHERE team_id = ? "
                "AND sort_at >= ? GROUP BY risk_level",
                (team_id, since),
            ).fetchall()
        counts = {"low": 0, "medium": 0, "high": 0}
        for r in rows:
            counts[r["risk_level"]] = counts.get(r["risk_level"], 0) + int(r["n"])
        return counts

    def high_risk_runs(self, team_id: int, since: str, limit: int) -> List[Dict[str, Any]]:
        """Up to `limit` high-risk runs since `since`, riskiest first."""
        with self._reading() as conn:
            rows = conn.execute(
                f"SELECT {_SUMMARY_COLUMNS} FROM runs WHERE team_id = ? AND risk_level = 'high' "
                "AND sort_at >= ? "
                "ORDER BY risk_score DESC, sort_at DESC, id LIMIT ?",
                (team_id, since, int(limit)),
            ).fetchall()
        return [_summary(r) for r in rows]

    def decided_approvals(self, team_id: int, since: str, limit: int) -> List[Dict[str, Any]]:
        """Approvals decided at or after `since`, newest decision first."""
        with self._reading() as conn:
            rows = conn.execute(
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

    def month_spend(self, team_id: int, start: str, end: str) -> Dict[str, Any]:
        """What the runs whose start (or push time, when there is no start) falls in [start, end)
        cost, for the team and per developer. `spend_usd` adds up the priced runs only and is null
        when the window has none; `runs - priced_runs` counts the runs with no cost at all, and
        `partial_runs` the runs whose cost is a lower bound."""
        window = "team_id = ? AND sort_at >= ? AND sort_at < ?"
        args = (team_id, start, end)
        with self._reading() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS runs, SUM(cost) AS spend_usd, COUNT(cost) AS priced_runs, "
                "COALESCE(SUM(CASE WHEN cost_partial THEN 1 ELSE 0 END), 0) AS partial_runs "
                f"FROM runs WHERE {window}",
                args,
            ).fetchone()
            rows = conn.execute(
                "SELECT user AS name, COUNT(*) AS runs, SUM(cost) AS spend_usd, COUNT(cost) AS priced_runs, "
                "COALESCE(SUM(CASE WHEN cost_partial THEN 1 ELSE 0 END), 0) AS partial_runs "
                f"FROM runs WHERE {window} GROUP BY user "
                "ORDER BY spend_usd IS NULL, spend_usd DESC, runs DESC, name",
                args,
            ).fetchall()
        return {
            "runs": total["runs"],
            "spend_usd": None if total["spend_usd"] is None else float(total["spend_usd"]),
            "priced_runs": total["priced_runs"],
            "partial_runs": total["partial_runs"],
            "users": [
                {
                    "user": r["name"],
                    "runs": r["runs"],
                    "spend_usd": None if r["spend_usd"] is None else float(r["spend_usd"]),
                    "priced_runs": r["priced_runs"],
                    "partial_runs": r["partial_runs"],
                }
                for r in rows
            ],
        }

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
                        "runs": item.get("runs", 0), "unpriced_runs": item.get("unpriced_runs", 0),
                        "partial_runs": item.get("partial_runs", 0),
                        "cost_is_lower_bound": bool(item.get("lower_bound")),
                    })
                    fired.append(item)
        return fired

    def _group(self, conn: sqlite3.Connection, column: str, window: str, args: Tuple[Any, ...]) -> List[sqlite3.Row]:
        # column is always a fixed expression from this module ("user", "project", ...)
        return conn.execute(
            f"SELECT {column} AS name, COUNT(*) AS runs, SUM(cost) AS cost_usd, "
            "COALESCE(SUM(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END), 0) AS priced_runs, "
            "COALESCE(SUM(CASE WHEN cost IS NULL OR cost_partial THEN 1 ELSE 0 END), 0) AS unpriced_runs "
            f"FROM runs WHERE {window} GROUP BY {column} "
            "ORDER BY cost_usd IS NULL, cost_usd DESC, runs DESC, name LIMIT 20",
            args,
        ).fetchall()

    # Team approval settings

    def approval_settings(self, team_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT slack_webhook_url, webhook_url, approval_ttl_s, attribution FROM teams WHERE id = ?",
                (team_id,),
            ).fetchone()
        return {
            "slack_webhook_url": row["slack_webhook_url"],
            "webhook_url": row["webhook_url"],
            "approval_ttl_s": int(row["approval_ttl_s"]),
            "attribution": row["attribution"] or "client",
        }

    def team_attribution(self, team_id: int) -> str:
        """How a pushed run's displayed user is decided: "client" (the default) or "key"."""
        with self._lock:
            row = self._conn.execute("SELECT attribution FROM teams WHERE id = ?", (team_id,)).fetchone()
        return (row["attribution"] if row else None) or "client"

    def approval_ttl_s(self, team_id: int) -> int:
        return self.approval_settings(team_id)["approval_ttl_s"]

    def update_approval_settings(
        self, team_id: int, changes: Dict[str, Any], actor: Optional[str] = None
    ) -> Dict[str, Any]:
        """Apply validated settings. Only the known column names can reach the SQL text.
        The audit entry names the fields that changed; the webhook URLs are never written to it."""
        columns = [c for c in ("slack_webhook_url", "webhook_url", "approval_ttl_s", "attribution")
                   if c in changes]
        if columns:
            sql = "UPDATE teams SET " + ", ".join(f"{c} = ?" for c in columns) + " WHERE id = ?"
            with self._lock, self._conn:
                self._conn.execute(sql, [changes[c] for c in columns] + [team_id])
                if actor is not None:
                    details: Dict[str, Any] = {"fields": sorted(columns)}
                    if "approval_ttl_s" in changes:
                        details["approval_ttl_s"] = changes["approval_ttl_s"]
                    if "attribution" in changes:
                        details["attribution"] = changes["attribution"]
                    self._audit(team_id, actor, "team.settings", "settings", details)
        return self.approval_settings(team_id)

    # Approvals

    def create_approval(
        self,
        team_id: int,
        approval_id: str,
        approval: Dict[str, Any],
        ttl_s: int,
        *,
        requester_key_id: Optional[str] = None,
        requested_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store a new pending approval. Returns its API view (the same shape GET returns)."""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals (id, team_id, session_id, tool, summary, risks, cwd, "
                "requester_key_id, requested_by, status, created_at, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    approval_id, team_id, approval["session_id"], approval["tool"], approval["summary"],
                    json.dumps(approval["risks"], ensure_ascii=False), approval["cwd"],
                    requester_key_id, requested_by, _stamp(now), now,
                ),
            )
        data = {
            "id": approval_id, "status": "pending", "decided_by": None, "decided_at": None,
            "reason": None, "session_id": approval["session_id"], "tool": approval["tool"],
            "summary": approval["summary"], "risks": approval["risks"], "cwd": approval["cwd"],
            "requester_key_id": requester_key_id, "requested_by": requested_by,
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
        with self._reading() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_approval_row_view(r, ttl_s, now) for r in rows]

    def decide_approval(
        self,
        team_id: int,
        approval_id: str,
        status: str,
        decided_by: str,
        reason: Optional[str],
        ttl_s: int,
        approver_key_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Record approved/denied on a pending, unexpired approval, atomically. With an actor,
        the decision is audited in the same transaction.

        Returns (outcome, view): decided, missing (no such approval for this team),
        self, legacy_unverifiable, expired, or conflict (already decided)."""
        now = time.time()
        with self._lock:
            with self._conn:
                requester = self._conn.execute(
                    "SELECT requester_key_id, status, created_ts FROM approvals WHERE id = ? AND team_id = ?",
                    (approval_id, team_id),
                ).fetchone()
                if requester is None:
                    return "missing", None
                if requester["status"] != "pending":
                    return "conflict", self.get_approval(team_id, approval_id, ttl_s)
                if requester["created_ts"] < now - ttl_s:
                    return "expired", self.get_approval(team_id, approval_id, ttl_s)
                if requester["requester_key_id"] is None:
                    return "legacy_unverifiable", self.get_approval(team_id, approval_id, ttl_s)
                if (
                    approver_key_id is not None
                    and requester["requester_key_id"] == approver_key_id
                ):
                    return "self", self.get_approval(team_id, approval_id, ttl_s)
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

    # Retention

    def purge_before(
        self, cutoff: Optional[str], audit_cutoff: Optional[str] = None
    ) -> Dict[str, int]:
        """Delete the run history older than `cutoff` (None keeps it): the runs that started (or
        were pushed) before it, the versions kept of those runs, and the approvals of that age.
        Budget alerts go by calendar month, never by fire time, so an alert of the current month
        can never be forgotten and fire again. Audit events live on their own clock: they are
        deleted against `audit_cutoff` only, which None keeps forever. Returns the number of rows
        removed per table."""
        counts: Dict[str, int] = {}
        with self._lock, self._conn:
            if cutoff:
                old_runs = "SELECT team_id, id FROM runs WHERE sort_at < ?"
                self._conn.execute(
                    f"DELETE FROM risks WHERE (team_id, run_id) IN ({old_runs})", (cutoff,)
                )
                counts["run_revisions"] = self._conn.execute(
                    f"DELETE FROM run_revisions WHERE (team_id, run_id) IN ({old_runs})", (cutoff,)
                ).rowcount
                counts["runs"] = self._conn.execute(
                    "DELETE FROM runs WHERE sort_at < ?", (cutoff,)
                ).rowcount
                counts["approvals"] = self._conn.execute(
                    "DELETE FROM approvals WHERE created_at < ?", (cutoff,)
                ).rowcount
                counts["budget_alerts"] = self._conn.execute(
                    "DELETE FROM budget_alerts WHERE month < ?", (cutoff[:7],)
                ).rowcount
            if audit_cutoff:
                counts["audit_log"] = self._conn.execute(
                    "DELETE FROM audit_log WHERE at < ?", (audit_cutoff,)
                ).rowcount
        return counts
