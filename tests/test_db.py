"""Tests for the run store: the additive migration of a database written by 0.6.0, the
extracted insight inputs, the window index and the purge of replaced versions.

Each test opens a hand-built 0.6.0 layout (the tables and rows the older server wrote) with
the current Database class and inspects the result through a second connection.
"""
import json
import sqlite3
import threading
import time
from contextlib import closing

from runledger.server.app import receipt_to_run
from runledger.server.db import Database

# The runs table exactly as 0.6.0 created it: no revision, usage_source, cost_partial,
# agent_id or sort_at, and no run_revisions table.
OLD_SCHEMA = """
CREATE TABLE teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    api_key_hash    TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL
);
CREATE TABLE runs (
    id             TEXT NOT NULL,
    team_id        INTEGER NOT NULL,
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
    receipt_html    TEXT,
    pushed_by_key_id TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (team_id, id)
);
CREATE INDEX runs_by_time ON runs (team_id, started_at);
"""

STARTED = "2026-10-05T10:00:00Z"
PUSHED_ONLY = "2026-10-06T08:00:00Z"


def _rows(path, sql, args=()):
    with closing(sqlite3.connect(str(path))) as conn:
        return conn.execute(sql, args).fetchall()


def _columns(path, table):
    return {row[1]: {"notnull": row[3], "default": row[4]} for row in _rows(path, f"PRAGMA table_info({table})")}


def _column_names(path, table):
    return [row[1] for row in _rows(path, f"PRAGMA table_info({table})")]


def _tables(path):
    return {row[0] for row in _rows(path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


def old_database(path):
    """A database with two runs as 0.6.0 stored them: one with a start time, one without."""
    receipt_with_start = {
        "session_id": "with-start",
        "user": "dev@example.com",
        "project": "payments-service",
        "totals": {"steps": 4, "tokens": 1000, "cost_usd": 0.1},
        "quality": {"score": 80, "grade": "B", "signals": []},
        "recommendations": [{"kind": "model_downgrade", "title": "Use Haiku", "est_savings_usd": 0.4}],
        "ai_review": {"verdict": "needs_review", "risk_assessments": [
            {"step": 1, "code": "rm_rf", "assessment": "false_positive"},
            {"step": 2, "code": "secret_file", "assessment": "confirmed"},
        ]},
    }
    receipt_pushed_only = {
        "session_id": "pushed-only",
        "user": "dev@example.com",
        "project": "payments-service",
        "totals": {"steps": 2, "tokens": 500, "cost_usd": 0.05},
    }
    columns = ("id, team_id, user, project, agent, title, started_at, ended_at, models, steps, tokens, "
               "files_changed, cost, risk_score, risk_level, quality_score, quality_grade, ai_verdict, "
               "est_savings_usd, receipt_json, receipt_html, pushed_by_key_id, created_at, updated_at")
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            "INSERT INTO teams (id, name, api_key_hash, created_at) VALUES (1, 'alpha', 'hash', ?)", (STARTED,)
        )
        conn.execute(
            f"INSERT INTO runs ({columns}) VALUES ('with-start', 1, 'dev@example.com', 'payments-service', "
            "'Claude Code', 'Fix the bug', ?, '2026-10-05T10:05:00Z', '{}', 4, 1000, 1, 0.1, 20, 'low', 80, 'B', "
            "'needs_review', 0.4, ?, '<html>old</html>', 'key-1', ?, ?)",
            (STARTED, json.dumps(receipt_with_start), STARTED, STARTED),
        )
        conn.execute(
            f"INSERT INTO runs ({columns}) VALUES ('pushed-only', 1, 'dev@example.com', 'payments-service', "
            "NULL, NULL, NULL, NULL, '{}', 2, 500, 0, 0.05, 10, 'low', NULL, NULL, NULL, NULL, ?, NULL, 'key-1', "
            "?, ?)",
            (json.dumps(receipt_pushed_only), PUSHED_ONLY, PUSHED_ONLY),
        )
        conn.commit()


def test_the_migration_adds_the_new_columns_table_and_index(tmp_path):
    path = tmp_path / "old.db"
    old_database(path)
    tables_before = _tables(path)

    with closing(Database(str(path))):
        pass

    columns = _columns(path, "runs")
    assert {"revision", "usage_source", "cost_partial", "agent_id", "sort_at"} <= set(columns)
    assert columns["revision"]["notnull"] == 1 and str(columns["revision"]["default"]) == "1"
    assert columns["cost_partial"]["notnull"] == 1 and str(columns["cost_partial"]["default"]) == "0"
    assert columns["sort_at"]["notnull"] == 1

    assert "run_revisions" in _tables(path)
    assert set(_column_names(path, "run_revisions")) == {
        "team_id", "run_id", "revision", "replaced_at", "receipt_json", "receipt_html",
        "risk_score", "cost", "pushed_by_key_id",
    }
    primary_key = [row[1] for row in _rows(path, "PRAGMA table_info(run_revisions)") if row[5] > 0]
    assert primary_key == ["team_id", "run_id", "revision"]
    assert [row[2] for row in _rows(path, "PRAGMA index_info(runs_by_sort_at)")] == ["team_id", "sort_at"]

    rows = _rows(path, "SELECT id, revision, cost_partial, usage_source, agent_id, sort_at, started_at, "
                       "created_at FROM runs ORDER BY id")
    assert [row[0] for row in rows] == ["pushed-only", "with-start"]
    for _id, revision, cost_partial, usage_source, agent_id, sort_at, started, created in rows:
        assert revision == 1 and cost_partial == 0
        assert usage_source is None and agent_id is None
        assert sort_at == (started or created)
    assert _rows(path, "SELECT COUNT(*) FROM runs WHERE sort_at IS NULL OR sort_at = ''")[0][0] == 0

    # No pre-existing table was dropped.
    assert tables_before <= _tables(path)


def test_the_migration_backfills_the_extracted_insight_inputs(tmp_path):
    path = tmp_path / "old.db"
    old_database(path)

    with closing(Database(str(path))):
        pass

    extracted = dict(
        (row[0], (row[1], row[2])) for row in _rows(path, "SELECT id, recommendations, ai_assessment FROM runs")
    )
    assert json.loads(extracted["with-start"][0]) == [
        {"kind": "model_downgrade", "title": "Use Haiku", "est_savings_usd": 0.4},
    ]
    assert json.loads(extracted["with-start"][1]) == {"assessed": 2, "false_positive": 1}
    # A run with neither key has nothing to extract.
    assert extracted["pushed-only"] == (None, None)


def test_the_migration_is_idempotent_and_a_fresh_database_matches(tmp_path):
    path = tmp_path / "old.db"
    old_database(path)

    with closing(Database(str(path))):
        pass
    after_first = _rows(path, "SELECT id, sort_at, revision FROM runs ORDER BY id")
    with closing(Database(str(path))):
        pass
    assert _rows(path, "SELECT id, sort_at, revision FROM runs ORDER BY id") == after_first

    fresh = tmp_path / "fresh.db"
    with closing(Database(str(fresh))):
        pass
    assert set(_column_names(fresh, "runs")) == set(_column_names(path, "runs"))
    assert set(_column_names(fresh, "run_revisions")) == set(_column_names(path, "run_revisions"))


def test_window_queries_use_the_sort_at_index(tmp_path):
    path = tmp_path / "fresh.db"
    with closing(Database(str(path))):
        pass

    plan = "\n".join(
        row[3] for row in _rows(path, "EXPLAIN QUERY PLAN SELECT id FROM runs "
                                     "WHERE team_id = 1 AND sort_at >= '2026-01-01'")
    )
    assert "runs_by_sort_at" in plan


def _stored_run(run_id, *, steps=4, ended="2026-10-15T10:05:00Z", started="2026-10-15T10:00:00Z",
                cost=0.1, risk=20, html=None):
    """A receipt as the push endpoint flattens it, without going through the HTTP layer."""
    payload = {
        "session_id": run_id,
        "user": "dev@example.com",
        "project": "payments-service",
        "agent": "Claude Code",
        "agent_id": "claude-code",
        "started": started,
        "ended": ended,
        "totals": {"steps": steps, "tokens": 1000, "cost_usd": cost, "usage_source": "messages"},
        "models": {"claude-sonnet-4-5-20250929": {"tokens": 1000, "cost_usd": cost}},
        "risk": {"score": risk, "reasons": []},
        "html": html if html is not None else f"<html>{run_id} v{steps}</html>",
    }
    return receipt_to_run(payload)


def test_a_superseded_run_keeps_its_replaced_versions_and_purge_removes_them(tmp_path):
    path = tmp_path / "store.db"
    with closing(Database(str(path))) as db:
        team_id, key = db.create_team("alpha")
        key_id = db.key_for_token(key)["id"]
        for run_id, ended, started in [
            ("old", "2026-08-01T10:05:00Z", "2026-08-01T10:00:00Z"),
            ("young", "2026-10-15T10:05:00Z", "2026-10-15T10:00:00Z"),
        ]:
            for version in (1, 2, 3):
                db.upsert_run(
                    team_id,
                    _stored_run(run_id, steps=version, ended=ended, started=started),
                    actor="test", key_id=key_id, role="member",
                )
        assert {run["id"]: run["revision"] for run in db.list_runs(team_id, limit=10)} == {"old": 3, "young": 3}
        assert _rows(path, "SELECT COUNT(*) FROM run_revisions")[0][0] == 4

        removed = db.purge_before("2026-09-01T00:00:00Z")

    assert removed["run_revisions"] == 2
    assert _rows(path, "SELECT run_id FROM run_revisions ORDER BY run_id") == [("young",), ("young",)]
    assert _rows(path, "SELECT id FROM runs") == [("young",)]


def test_reports_read_on_their_own_connection_while_a_writer_holds_the_store(tmp_path):
    """A report does not wait for the lock a writer holds, and never sees its uncommitted rows."""
    path = tmp_path / "reports.db"
    with closing(Database(str(path))) as db:
        team_id, _ = db.create_team("alpha")
        db.upsert_run(team_id, _stored_run("one"))
        started = threading.Event()
        release = threading.Event()

        def writer():
            with db._lock:
                db._conn.execute(
                    "INSERT INTO audit_log (team_id, at, actor, action, target, details) "
                    "VALUES (?, '2026-10-15T12:00:00Z', 'test', 'run.push', 'one', '{}')",
                    (team_id,),
                )
                started.set()
                release.wait(3)
                db._conn.rollback()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        assert started.wait(5)
        began = time.monotonic()
        stats = db.stats(team_id)
        events = db.audit_events(team_id)
        elapsed = time.monotonic() - began
        release.set()
        thread.join(5)

        assert elapsed < 1.0
        assert stats["totals"]["runs"] == 1
        # Only the committed key.create row is there: the writer's uncommitted row is invisible.
        assert [event["action"] for event in events] == ["key.create"]


def test_session_cap_per_key(tmp_path):
    """Signing in 25 times leaves the 20 newest sessions of that key and no more."""
    path = tmp_path / "sessions.db"
    with closing(Database(str(path))) as db:
        team_id, admin = db.create_team("alpha")
        other_team_id, other_admin = db.create_team("beta")
        key_id = db.key_for_token(admin)["id"]
        other_key_id = db.key_for_token(other_admin)["id"]
        other_token = db.issue_session(other_team_id, other_key_id, "admin")

        tokens = [db.issue_session(team_id, key_id, "admin") for _ in range(25)]
        assert len(_rows(path, "SELECT token_hash FROM sessions WHERE key_id = ?", (key_id,))) == 20
        assert len(_rows(path, "SELECT token_hash FROM sessions WHERE key_id = ?", (other_key_id,))) == 1
        for token in tokens[:5]:
            assert db.session_key(token) is None
        for token in tokens[5:]:
            assert db.session_key(token) is not None
        assert db.session_key(other_token) is not None
