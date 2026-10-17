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


def test_backup_same_second_no_collision(tmp_path, capsys):
    from contextlib import closing
    import re
    db_path = tmp_path / "team_a.db"
    db = Database(str(db_path))
    db.create_team("Team A")
    db.close()

    folder = tmp_path / "bkp"
    outs = []
    for _ in range(2):
        assert main(["backup", "--db", str(db_path), "--dir", str(folder)]) == 0
        outs.append(capsys.readouterr().out)

    files = sorted(folder.glob("runledger-team_a-*.db"))
    assert len(files) == 2
    combined_out = "\n".join(outs)
    for f in files:
        assert re.fullmatch(r"runledger-team_a-\d{8}-\d{6}(-\d+)?\.db", f.name)
        assert str(f) in combined_out
        with closing(sqlite3.connect(str(f))) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # Rapid backups with --keep 2 in the same second must never reuse a pruned sequence number.
    keep2_folder = tmp_path / "bkp_keep2"
    for _ in range(5):
        assert main(["backup", "--db", str(db_path), "--dir", str(keep2_folder), "--keep", "2"]) == 0
        capsys.readouterr()
    assert len(list(keep2_folder.glob("runledger-team_a-*.db"))) == 2


def test_backup_prune_only_own_stem(tmp_path, capsys):
    a_db = tmp_path / "a.db"
    b_db = tmp_path / "b.db"
    for p, name in ((a_db, "TeamA"), (b_db, "TeamB")):
        db = Database(str(p))
        db.create_team(name)
        db.close()

    folder = tmp_path / "shared_bkp"
    folder.mkdir()
    legacy = folder / "runledger-20200101-000000.db"
    legacy.write_bytes(b"legacy-backup")

    for _ in range(4):
        assert main(["backup", "--db", str(a_db), "--dir", str(folder), "--keep", "10"]) == 0
        assert main(["backup", "--db", str(b_db), "--dir", str(folder), "--keep", "10"]) == 0
    capsys.readouterr()

    assert main(["backup", "--db", str(a_db), "--dir", str(folder), "--keep", "2"]) == 0
    assert main(["backup", "--db", str(b_db), "--dir", str(folder), "--keep", "2"]) == 0
    capsys.readouterr()

    a_files = sorted(folder.glob("runledger-a-*.db"))
    b_files = sorted(folder.glob("runledger-b-*.db"))
    assert len(a_files) == 2
    assert len(b_files) == 2
    assert legacy.exists()  # legacy file preserved when multiple DB stems share the folder

    single_folder = tmp_path / "single_bkp"
    single_folder.mkdir()
    single_legacy = single_folder / "runledger-20200101-000000.db"
    single_legacy.write_bytes(b"legacy-backup")
    assert main(["backup", "--db", str(a_db), "--dir", str(single_folder), "--keep", "1"]) == 0
    assert not single_legacy.exists()
    assert len(list(single_folder.glob("runledger-a-*.db"))) == 1


def test_failed_backup_leaves_no_partial_file(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "bkp"
    bad_db = tmp_path / "corrupt.db"
    bad_db.write_bytes(b"not-a-sqlite-db")

    assert main(["backup", "--db", str(bad_db), "--dir", str(folder)]) == 1
    assert "error:" in capsys.readouterr().err
    assert list(folder.glob("runledger-*.db")) == []
    assert list(folder.glob("*.tmp")) == []

    good_db = tmp_path / "good.db"
    db = Database(str(good_db))
    db.create_team("Good")
    db.close()

    def broken_backup(self, path):
        tmp = folder / "runledger-good-broken.db.tmp"
        tmp.write_bytes(b"partial")
        try:
            raise sqlite3.OperationalError("simulated disk failure mid-backup")
        finally:
            tmp.unlink(missing_ok=True)

    # Also test Database.backup_to directly when _conn.backup fails mid-copy
    live = Database(str(good_db))
    try:
        class FailingConn:
            def __init__(self, real):
                self._real = real

            def backup(self, target):
                target.execute("CREATE TABLE partial_data (x INT)")
                raise sqlite3.OperationalError("simulated mid-copy failure")

            def close(self):
                self._real.close()

        real_conn = live._conn
        live._conn = FailingConn(real_conn)
        dest = folder / "runledger-good-20261016-000000.db"
        with pytest.raises(sqlite3.OperationalError, match="mid-copy"):
            live.backup_to(str(dest))
        live._conn = real_conn
    finally:
        live.close()

    assert list(folder.glob("runledger-*.db")) == []
    assert list(folder.glob("*.tmp")) == []


def test_backup_stems_keep_distinct_database_names():
    """Databases that differ in characters the stem cannot carry stay in separate namespaces."""
    from runledger.cli import _backup_stem

    stems = {name: _backup_stem(name) for name in (
        "team a.db", "team_a.db", "team%20a.db", "a.db", "A.db", "team (1).db", "team-1.db",
        "José.db", "team+two.db",
    )}
    assert len(set(stems.values())) == len(stems)
    assert stems["a.db"] == "a"
    assert stems["team_a.db"] == "team_a"


def test_backup_retention_keeps_similar_stems_apart(tmp_path, capsys):
    spaced_db = tmp_path / "team a.db"
    plain_db = tmp_path / "team_a.db"
    for path, name in ((spaced_db, "Spaced"), (plain_db, "Plain")):
        db = Database(str(path))
        db.create_team(name)
        db.close()

    folder = tmp_path / "shared"
    for _ in range(2):
        assert main(["backup", "--db", str(spaced_db), "--dir", str(folder), "--keep", "1"]) == 0
        assert main(["backup", "--db", str(plain_db), "--dir", str(folder), "--keep", "1"]) == 0
    capsys.readouterr()

    files = sorted(folder.glob("runledger-*.db"))
    assert len(files) == 2
    teams = []
    for path in files:
        copy = Database(str(path))
        try:
            teams.append(copy.team_name(1))
        finally:
            copy.close()
    assert sorted(teams) == ["Plain", "Spaced"]


def test_concurrent_backups_never_share_a_target(tmp_path, monkeypatch):
    from contextlib import closing

    db_path = tmp_path / "team.db"
    db = Database(str(db_path))
    db.create_team("Concurrent")
    db.close()
    folder = tmp_path / "bkp"

    barrier = threading.Barrier(2, timeout=15)
    real_backup_to = Database.backup_to

    def slow_backup_to(self, path):
        barrier.wait()
        return real_backup_to(self, path)

    monkeypatch.setattr(Database, "backup_to", slow_backup_to)
    codes = []

    def worker():
        codes.append(main(["backup", "--db", str(db_path), "--dir", str(folder)]))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert codes == [0, 0]
    files = sorted(folder.glob("runledger-*.db"))
    assert len(files) == 2
    assert list(folder.glob("*.tmp")) == []
    for path in files:
        with closing(sqlite3.connect(str(path))) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 1

