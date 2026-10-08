"""SQLite storage for the RunLedger team server.

Runs are scoped to a team. The primary key is (team_id, id), so two teams can
hold the same session id without overwriting each other. API keys live in
api_keys, stored only as SHA-256 hashes (see auth.py); the plaintext is returned
once, by create_team(), create_key() or rotate_key().

Every change to keys, team settings, approvals and pushed runs also writes a row to
audit_log, in the same transaction as the change itself.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..pricing import friendly_model
from .auth import (
    INITIAL_KEY_LABEL,
    generate_key,
    hash_key,
    key_prefix,
    new_key_id,
    validate_label,
    validate_role,
)

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LAST_USED_WRITE_INTERVAL_S = 60

# Team settings added after the first release. Databases created earlier get them
# through _add_missing_columns().
TEAM_SETTING_COLUMNS = (
    ("slack_webhook_url", "TEXT"),
    ("webhook_url", "TEXT"),
    ("approval_ttl_s", "INTEGER NOT NULL DEFAULT 600"),
)

# Run columns added after the first release.
RUN_COLUMNS = (
    ("agent", "TEXT"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    api_key_hash    TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    slack_webhook_url TEXT,
    webhook_url     TEXT,
    approval_ttl_s  INTEGER NOT NULL DEFAULT 600
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
"""

_SUMMARY_COLUMNS = (
    "id, user, project, agent, title, started_at, ended_at, steps, tokens, files_changed, "
    "cost, risk_score, risk_level, created_at, updated_at, (receipt_html IS NOT NULL) AS has_html"
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
            self._migrate_api_keys()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _add_missing_columns(self, table: str, columns: Tuple[Tuple[str, str], ...]) -> None:
        """Upgrade a database created before these columns existed."""
        present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in present:
                with self._conn:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

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
        key = generate_key()
        now = utc_now()
        with self._lock, self._conn:
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
            self._audit(team_id, actor, "key.rotate", key_id,
                        {"label": row["label"], "role": row["role"], "prefix": row["prefix"]})
        return "ok", dict(row, key=new_key)

    def list_audit(self, team_id: int, limit: int = 100, before: Optional[int] = None) -> List[Dict[str, Any]]:
        """Audit events for a team, newest first. `before` returns only events with a smaller id."""
        sql = "SELECT id, at, actor, action, target, details FROM audit_log WHERE team_id = ?"
        params: List[Any] = [team_id]
        if before is not None:
            sql += " AND id < ?"
            params.append(int(before))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
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
                                  receipt_json, receipt_html, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    receipt_json = excluded.receipt_json,
                    receipt_html = excluded.receipt_html,
                    updated_at = excluded.updated_at
                """,
                (
                    run["id"], team_id, run["user"], run["project"], run["agent"], run["title"],
                    run["started_at"], run["ended_at"], models_json,
                    run["steps"], run["tokens"], run["files_changed"], run["cost"],
                    run["risk_score"], run["risk_level"], receipt_json, run["receipt_html"],
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

    def stats(self, team_id: int, since_days: int = 30) -> Dict[str, Any]:
        days = max(1, int(since_days))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TIME_FORMAT)
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
