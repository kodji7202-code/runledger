"""CLI tests for the file permissions of the team database and its backups.

POSIX only: the chmod calls must be a no-op on Windows, where mode bits do not exist.
"""
import os
import stat

import pytest

from runledger.cli import main

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_database_file_mode(tmp_path):
    db = tmp_path / "team.db"
    assert main(["team", "create", "demo", "--db", str(db)]) == 0
    assert db.is_file()
    assert _mode(db) == 0o600


def test_backup_file_mode(tmp_path):
    db = tmp_path / "team.db"
    assert main(["team", "create", "demo", "--db", str(db)]) == 0
    folder = tmp_path / "backups"
    assert main(["backup", "--db", str(db), "--dir", str(folder)]) == 0
    copies = list(folder.glob("*.db"))
    assert len(copies) == 1
    assert _mode(copies[0]) == 0o600
