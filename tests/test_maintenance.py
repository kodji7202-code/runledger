"""Retention and online backups remain independent features of the team server."""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from runledger.cli import main
from runledger.server import maintenance
from runledger.server.app import make_server, receipt_to_run
from runledger.server.db import TIME_FORMAT, Database

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw, expected", [
    ("", None), ("  ", None), ("90", 90), (" 1 ", 1),
])
def test_retention_from_env(raw, expected):
    assert maintenance.retention_from_env({maintenance.RETENTION_ENV: raw}) == expected


def test_retention_is_unset_by_default():
    assert maintenance.retention_from_env({}) is None


@pytest.mark.parametrize("raw", ["0", "ninety", "-5", "1.5"])
def test_retention_from_env_rejects_bad_values(raw):
    with pytest.raises(ValueError, match=maintenance.RETENTION_ENV):
        maintenance.retention_from_env({maintenance.RETENTION_ENV: raw})


def test_serve_rejects_invalid_retention_before_opening_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(maintenance.RETENTION_ENV, "ninety")
    path = tmp_path / "invalid.db"
    assert main(["serve", "--port", "0", "--db", str(path)]) == 1
    assert maintenance.RETENTION_ENV in capsys.readouterr().err
    assert not path.exists()


def test_retention_removes_only_old_history(tmp_path):
    db = Database(str(tmp_path / "retention.db"))
    try:
        team_id, key = db.create_team("kept")
        key_id = db.key_for_token(key)["id"]
        old = (NOW - timedelta(days=100)).strftime(TIME_FORMAT)
        recent = NOW.strftime(TIME_FORMAT)
        for run_id, started in [("old", old), ("new", recent)]:
            db.upsert_run(team_id, receipt_to_run({
                "session_id": run_id, "started": started, "html": "<h1>Receipt</h1>",
                "risk": {"reasons": [
                    {"severity": "high", "code": "example", "reason": "Example", "step": 1},
                ]},
            }), actor="test", key_id=key_id)
        fields = {"session_id": "old", "tool": "Bash", "summary": "example", "risks": [], "cwd": None}
        db.create_approval(team_id, "old-approval", fields, 600)
        db.create_approval(team_id, "new-approval", fields, 600)
        with sqlite3.connect(db.path) as conn:
            conn.execute("UPDATE approvals SET created_at = ? WHERE id = 'old-approval'", (old,))
            conn.execute("UPDATE approvals SET created_at = ? WHERE id = 'new-approval'", (recent,))
            conn.execute("UPDATE audit_log SET at = ?", (old,))
            conn.execute(
                "INSERT INTO budget_alerts VALUES (?, '2026-07', 'team', 50, 5, 10, ?)", (team_id, old),
            )
        token = db.issue_session(team_id, key_id, "admin")
        removed = maintenance.run_maintenance(db, 90, moment=NOW)
        assert removed["runs"] == 1
        assert removed["approvals"] == 1
        assert removed["audit_log"] > 0
        assert removed["budget_alerts"] == 1
        assert [r["id"] for r in db.list_runs(team_id)] == ["new"]
        assert db.get_approval(team_id, "old-approval", 600) is None
        assert db.get_approval(team_id, "new-approval", 600) is not None
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT run_id FROM risks").fetchall() == [("new",)]
        assert db.team_name(team_id) == "kept"
        assert db.key_for_token(key)["id"] == key_id
        assert db.session_key(token)["id"] == key_id
    finally:
        db.close()


def test_retention_is_optional():
    # No database operations are needed when retention is disabled.
    assert maintenance.run_maintenance(None, None, moment=NOW) == {}


def test_maintenance_only_starts_when_needed(tmp_path, monkeypatch):
    monkeypatch.delenv(maintenance.RETENTION_ENV, raising=False)
    server = make_server(str(tmp_path / "off.db"), port=0)
    try:
        assert server.start_maintenance() is False
    finally:
        server.server_close()
        server.db.close()

    monkeypatch.setenv(maintenance.RETENTION_ENV, "90")
    server = make_server(str(tmp_path / "on.db"), port=0)
    ran = threading.Event()
    original = maintenance.run_maintenance

    def recorded(*args, **kwargs):
        result = original(*args, **kwargs)
        ran.set()
        return result

    monkeypatch.setattr(maintenance, "run_maintenance", recorded)
    try:
        assert server.retention_days == 90
        assert server.start_maintenance(interval_s=3600) is True
        assert ran.wait(5)
    finally:
        server.server_close()
        server.db.close()


def test_backup_copies_the_database_and_keeps_the_newest(tmp_path, capsys):
    db_path = tmp_path / "live.db"
    db = Database(str(db_path))
    db.create_team("kept")
    db.close()
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in ("20200101", "20200102", "20200103"):
        (folder / f"runledger-{day}-000000.db").write_bytes(b"old")
    assert main(["backup", "--db", str(db_path), "--dir", str(folder), "--keep", "2"]) == 0
    files = sorted(p.name for p in folder.glob("runledger-*.db"))
    assert len(files) == 2 and files[0] == "runledger-20200103-000000.db"
    copy = Database(str(folder / files[1]))
    try:
        assert copy.team_name(1) == "kept"
    finally:
        copy.close()
    assert "removed 2 older" in capsys.readouterr().out


def test_backup_needs_an_existing_database(tmp_path, capsys):
    assert main(["backup", "--db", str(tmp_path / "missing.db"), "--dir", str(tmp_path / "b")]) == 1
    assert "no database" in capsys.readouterr().err
