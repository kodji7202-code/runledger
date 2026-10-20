"""The shared redirect refusal behind the guard log and the checkpoint marker.

A repository can ship `.runledger` or a file in it as a symlink, a Windows junction or another
reparse point, so every RunLedger write there goes through these two checks. Everything here
lives in tmp_path; the symlink cases are skipped where creating a symlink needs a privilege the
runner does not have."""
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from runledger import redirects


def symlink_or_skip(link, target, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    return link


def test_is_redirect_reads_the_path_itself(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "file.txt").write_text("kept\n", encoding="utf-8")

    assert redirects.is_redirect(folder) is False
    assert redirects.is_redirect(folder / "file.txt") is False
    assert redirects.is_redirect(folder / "absent.txt") is False


def test_is_redirect_recognises_a_link_and_a_dangling_link(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("outside stays\n", encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()

    assert redirects.is_redirect(symlink_or_skip(tmp_path / "file-link", target)) is True
    assert redirects.is_redirect(symlink_or_skip(tmp_path / "dangling", tmp_path / "gone")) is True
    assert redirects.is_redirect(symlink_or_skip(tmp_path / "folder-link", folder, True)) is True


def test_unsupported_reparse_point_reads_its_stat_buffer(tmp_path):
    """A junction is recognised from the stat data of the path itself (is_redirect reads a
    symlink from the mode, and this from the Windows attributes)."""
    info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755,
                           st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
    assert redirects.unsupported_reparse_point(tmp_path, info) is True

    plain = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0)
    assert redirects.unsupported_reparse_point(tmp_path, plain) is False

    symlink = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
    assert redirects.unsupported_reparse_point(tmp_path, symlink) is False


def test_open_without_following_refuses_a_link_at_the_last_part(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("outside stays\n", encoding="utf-8")
    link = symlink_or_skip(tmp_path / "file-link", target)

    fd = redirects.open_without_following(link, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    assert fd is None
    assert target.read_text(encoding="utf-8") == "outside stays\n"


def test_open_without_following_refuses_a_dangling_link_without_creating_it(tmp_path):
    missing = tmp_path / "not-created.txt"
    link = symlink_or_skip(tmp_path / "dangling", missing)

    assert redirects.open_without_following(link, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600) is None
    assert not missing.exists()


def test_open_without_following_appends_to_the_file_itself(tmp_path):
    log = tmp_path / "guard.log"
    log.write_text("first\n", encoding="utf-8")

    fd = redirects.open_without_following(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    assert fd is not None
    try:
        os.write(fd, b"second\n")
    finally:
        os.close(fd)
    assert log.read_text(encoding="utf-8") == "first\nsecond\n"


def test_open_without_following_creates_a_missing_file(tmp_path):
    missing = tmp_path / "new.log"

    fd = redirects.open_without_following(missing, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    assert fd is not None
    os.close(fd)
    assert missing.read_text(encoding="utf-8") == ""


def test_open_without_following_reports_an_unopenable_path(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()

    assert redirects.open_without_following(folder, os.O_WRONLY, 0o600) is None
    assert Path(folder).is_dir()
