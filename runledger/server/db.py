"""SQLite storage for the RunLedger team server.

Runs are scoped to a team. The primary key is (team_id, id), so two teams can
hold the same session id without overwriting each other. API keys are stored
only as SHA-256 hashes; the plaintext is returned once, by create_team().
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..pricing import friendly_model

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Team settings added after the first release. Databases created earlier get them
# through _add_missing_team_columns().
TEAM_SETTING_COLUMNS = (
    ("slack_webhook_url", "TEXT"),
    ("webhook_url", "TEXT"),
    ("approval_ttl_s", "INTEGER NOT NULL DEFAULT 600"),
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
"""

_SUMMARY_COLUMNS = (
    "id, user, project, title, started_at, ended_at, steps, tokens, files_changed, "
    "cost, risk_score, risk_level, created_at, updated_at, (receipt_html IS NOT NULL) AS has_html"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


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
            self._add_missing_team_columns()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _add_missing_team_columns(self) -> None:
        """Upgrade a database created before the approval settings existed."""
        present = {row["name"] for row in self._conn.execute("PRAGMA table_info(teams)")}
        for name, ddl in TEAM_SETTING_COLUMNS:
            if name not in present:
                with self._conn:
                    self._conn.execute(f"ALTER TABLE teams ADD COLUMN {name} {ddl}")

    # Teams and keys

    def create_team(self, name: str) -> Tuple[int, str]:
        """Create a team. Returns (team_id, api_key). The key is not stored, only its hash."""
        name = (name or "").strip()
        if not name or len(name) > 100:
            raise ValueError("Team name must be 1-100 characters.")
        key = "rl_" + secrets.token_urlsafe(32)
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO teams (name, api_key_hash, created_at) VALUES (?, ?, ?)",
                    (name, hash_key(key), utc_now()),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"A team named {name!r} already exists.") from None
            return int(cur.lastrowid), key

    def team_for_key(self, key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key or len(key) > 512:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name FROM teams WHERE api_key_hash = ?", (hash_key(key),)
            ).fetchone()
        return {"id": row["id"], "name": row["name"]} if row else None

    # Runs

    def upsert_run(self, team_id: int, run: Dict[str, Any]) -> None:
        """Insert or replace one run (re-pushing a session updates it)."""
        now = utc_now()
        models_json = json.dumps(run["models"], sort_keys=True)
        receipt_json = json.dumps(run["receipt"], ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (id, team_id, user, project, title, started_at, ended_at, models,
                                  steps, tokens, files_changed, cost, risk_score, risk_level,
                                  receipt_json, receipt_html, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (team_id, id) DO UPDATE SET
                    user = excluded.user,
                    project = excluded.project,
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
                    run["id"], team_id, run["user"], run["project"], run["title"],
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

    def list_runs(
        self,
        team_id: int,
        user: Optional[str] = None,
        project: Optional[str] = None,
        min_risk: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses = ["team_id = ?"]
        params: List[Any] = [team_id]
        if user:  # case-insensitive substring match, so "bob" finds "bob@example.com"
            clauses.append("user LIKE ? ESCAPE '\\'")
            params.append(_contains(user))
        if project:
            clauses.append("project LIKE ? ESCAPE '\\'")
            params.append(_contains(project))
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
            "by_model": [
                {"model": m["model"], "runs": m["runs"], "tokens": m["tokens"], "cost_usd": round(m["cost_usd"], 6)}
                for m in by_model[:20]
            ],
            "top_risk_codes": [
                {"code": r["code"], "occurrences": r["occurrences"], "runs": r["runs"]} for r in top
            ],
        }

    def _group(self, column: str, window: str, args: Tuple[Any, ...]) -> List[sqlite3.Row]:
        # column is always a fixed identifier from this module ("user" or "project")
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

    def update_approval_settings(self, team_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Apply validated settings. Only the three known column names can reach the SQL text."""
        columns = [c for c in ("slack_webhook_url", "webhook_url", "approval_ttl_s") if c in changes]
        if columns:
            sql = "UPDATE teams SET " + ", ".join(f"{c} = ?" for c in columns) + " WHERE id = ?"
            with self._lock, self._conn:
                self._conn.execute(sql, [changes[c] for c in columns] + [team_id])
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
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Record approved/denied on a pending, unexpired approval, atomically.

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
            view = self.get_approval(team_id, approval_id, ttl_s)
        if view is None:
            return "missing", None
        if decided:
            return "decided", view
        if view["status"] == "expired":
            return "expired", view
        return "conflict", view
