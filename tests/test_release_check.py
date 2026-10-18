"""Tests for tools/check_release.py, the tag checker the release workflow runs first.

Each test builds a throwaway repository tree, changes one version reference and checks the
exit code, so a drift between the tag, pyproject.toml, runledger/__init__.py and the CHANGELOG
fails the release before anything is built or published.
"""
import os
import subprocess
import sys
from pathlib import Path

from runledger import __version__

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_release.py"

PYPROJECT = """[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "runledger-ai"
version = "0.6.0"
"""

INIT = '"""RunLedger."""\n__version__ = "0.6.0"\n'

CHANGELOG = """# Changelog

## [Unreleased]

- pending change

## [0.6.0] - 2026-10-15

### Fixed

- the 0.6.0 fix
- a partial cost renders as "\u2265 $1", like the real changelog

## [0.5.2] - 2026-10-15

- the 0.5.2 fix
"""


def _repo(tmp_path, **overrides):
    (tmp_path / "runledger").mkdir()
    (tmp_path / "pyproject.toml").write_text(overrides.get("pyproject", PYPROJECT), encoding="utf-8")
    (tmp_path / "runledger" / "__init__.py").write_text(overrides.get("init", INIT), encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(overrides.get("changelog", CHANGELOG), encoding="utf-8")
    return tmp_path


def _run(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def test_match_passes(tmp_path):
    result = _run(_repo(tmp_path), "--tag", "v0.6.0")
    assert result.returncode == 0, result.stderr
    assert "0.6.0" in result.stdout


def test_tag_without_the_v_prefix_is_accepted(tmp_path):
    assert _run(_repo(tmp_path), "--tag", "0.6.0").returncode == 0


def test_mismatch_fails(tmp_path):
    result = _run(_repo(tmp_path), "--tag", "v9.9.9")
    assert result.returncode == 1
    assert "pyproject.toml" in result.stderr
    assert "__init__.py" in result.stderr
    assert "CHANGELOG.md" in result.stderr


def test_init_version_mismatch_fails(tmp_path):
    result = _run(_repo(tmp_path, init='__version__ = "0.5.0"\n'), "--tag", "v0.6.0")
    assert result.returncode == 1
    assert "runledger/__init__.py" in result.stderr
    assert "pyproject.toml" not in result.stderr


def test_changelog_heading_missing_fails(tmp_path):
    changelog = CHANGELOG.replace("## [0.6.0] - 2026-10-15", "## [0.6.0-beta] - 2026-10-15")
    result = _run(_repo(tmp_path, changelog=changelog), "--tag", "v0.6.0")
    assert result.returncode == 1
    assert "CHANGELOG.md" in result.stderr


def test_missing_tag_is_a_usage_error(tmp_path):
    env = dict(os.environ)
    env.pop("GITHUB_REF_NAME", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(_repo(tmp_path))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=env,
    )
    assert result.returncode == 2
    assert "tag" in result.stderr


def test_notes_print_only_that_section(tmp_path):
    result = _run(_repo(tmp_path), "--tag", "v0.6.0", "--notes")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("## [0.6.0]")
    assert "the 0.6.0 fix" in result.stdout
    assert "the 0.5.2 fix" not in result.stdout
    assert "pending change" not in result.stdout


def test_notes_fail_for_a_mismatched_tag(tmp_path):
    result = _run(_repo(tmp_path), "--tag", "v9.9.9", "--notes")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_notes_for_the_current_version_of_this_repository():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v" + __version__, "--notes"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("## [%s]" % __version__)


def test_notes_survive_a_legacy_console_encoding(tmp_path):
    # Windows consoles default to a legacy code page, and a changelog holds characters such as
    # U+2265 that it cannot encode; the notes must still be written, in full.
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(_repo(tmp_path)), "--tag", "v0.6.0", "--notes"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("## [0.6.0]")
    assert "the 0.6.0 fix" in result.stdout
    assert "\u2265" in result.stdout
