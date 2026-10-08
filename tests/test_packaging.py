"""Packaging checks: the built wheel has the files and metadata it needs, pyproject.toml
says what the release depends on, and the CI and release workflows cover the matrix.

The wheel is built with pip. When pip cannot reach the package index (offline, so the
build backend cannot be downloaded), the wheel tests are skipped with the reason. Any
other build failure fails the test. Standard library only, so it runs on Python 3.9.
"""
import re
import shutil
import subprocess
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

# pip output that means "no network", not "the package is broken".
OFFLINE_MARKERS = (
    "No matching distribution found",
    "Could not find a version that satisfies",
    "Failed to establish a new connection",
    "Max retries exceeded",
    "Temporary failure in name resolution",
    "getaddrinfo failed",
)


def _section(header):
    """Text of one TOML table: from its header line to the next table header."""
    start = PYPROJECT.index(header + "\n")
    end = PYPROJECT.find("\n[", start + len(header))
    return PYPROJECT[start:] if end == -1 else PYPROJECT[start:end]


def _toml_string(section, key):
    match = re.search(r'^%s\s*=\s*"([^"]*)"' % re.escape(key), section, re.M)
    assert match, f"{key} is not set in {section.splitlines()[0]}"
    return match.group(1)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    # Build from a copy of the sources, so the build leaves nothing in the repository.
    src = tmp_path_factory.mktemp("src")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(str(ROOT / name), str(src / name))
    shutil.copytree(str(ROOT / "runledger"), str(src / "runledger"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(src), "--no-deps", "-w", str(out)],
        cwd=str(src),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    if proc.returncode != 0:
        if any(marker in proc.stdout for marker in OFFLINE_MARKERS):
            pytest.skip("pip cannot download the build backend (offline?); wheel checks skipped")
        pytest.fail("pip wheel failed:\n" + "\n".join(proc.stdout.splitlines()[-30:]))
    built = sorted(out.glob("runledger-*.whl"))
    assert len(built) == 1, built
    return built[0]


def _wheel_names(wheel_path):
    with zipfile.ZipFile(str(wheel_path)) as zf:
        return set(zf.namelist())


def _wheel_text(wheel_path, name):
    with zipfile.ZipFile(str(wheel_path)) as zf:
        return zf.read(name).decode("utf-8")


def test_wheel_contains_modules_and_package_data(wheel):
    names = _wheel_names(wheel)
    for required in (
        "runledger/cli.py",
        "runledger/server/app.py",
        "runledger/adapters/codex.py",
        "runledger/prices.json",
    ):
        assert required in names, f"{required} is missing from {wheel.name}"


def test_wheel_declares_the_console_script_and_the_license(wheel):
    names = _wheel_names(wheel)
    dist_info = {n.split("/")[0] for n in names if ".dist-info/" in n}
    assert len(dist_info) == 1, dist_info
    info = dist_info.pop()
    entry_points = _wheel_text(wheel, info + "/entry_points.txt")
    assert "runledger = runledger.cli:main" in entry_points
    assert any(n.startswith(info + "/") and n.endswith("/LICENSE") for n in names)


def test_wheel_metadata_matches_pyproject(wheel):
    project = _section("[project]")
    meta_name = next(n for n in _wheel_names(wheel) if n.endswith(".dist-info/METADATA"))
    meta = Parser().parsestr(_wheel_text(wheel, meta_name))
    assert meta["Name"] == _toml_string(project, "name")
    assert meta["Version"] == _toml_string(project, "version")
    assert meta["Requires-Python"] == ">=3.9"
    assert meta["License-Expression"] == "Apache-2.0"
    assert "RunLedger <hello@runledger.site>" in (meta["Author-email"] or "")
    assert "Homepage, https://runledger.site" in meta.get_all("Project-URL", [])
    assert "claude-code" in (meta["Keywords"] or "")
    assert meta.get_all("Requires-Dist") is None, "the package must not depend on anything"


def test_pyproject_project_table():
    project = _section("[project]")
    assert _toml_string(project, "name") == "runledger"
    assert _toml_string(project, "license") == "Apache-2.0"
    assert _toml_string(project, "requires-python") == ">=3.9"
    assert _toml_string(project, "readme") == "README.md"
    assert 'license-files = ["LICENSE"]' in project
    assert 'authors = [{ name = "RunLedger", email = "hello@runledger.site" }]' in project
    assert "keywords = [" in project
    assert "Programming Language :: Python :: 3.9" in project
    assert "dependencies = []" in project
    # setuptools rejects License classifiers next to a license expression.
    assert "License ::" not in project


def test_pyproject_urls_scripts_and_packages():
    assert 'Homepage = "https://runledger.site"' in _section("[project.urls]")
    assert 'runledger = "runledger.cli:main"' in _section("[project.scripts]")
    assert 'packages = ["runledger", "runledger.server", "runledger.adapters"]' in _section("[tool.setuptools]")
    assert 'runledger = ["prices.json"]' in _section("[tool.setuptools.package-data]")


def test_ci_matrix_has_every_os_and_python():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "os: [ubuntu-latest, windows-latest, macos-latest]" in ci
    assert 'python: ["3.9", "3.12", "3.13"]' in ci
    assert "python -m compileall -q runledger" in ci
    assert "/health" in ci


def test_release_workflow_uses_trusted_publishing_on_version_tags():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert '- "v*"' in release
    assert "id-token: write" in release
    assert "pypa/gh-action-pypi-publish@release/v1" in release


def test_container_runs_as_non_root_on_port_8787():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "pip install --no-cache-dir ." in dockerfile
    assert "USER runledger" in dockerfile
    assert "EXPOSE 8787" in dockerfile
    assert 'ENTRYPOINT ["runledger"]' in dockerfile


def test_compose_publishes_ports_only_through_caddy():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    runledger_service, caddy_service = compose.split("\n  caddy:\n", 1)
    assert "ports:" not in runledger_service
    assert "ports:" in caddy_service
    assert "reverse_proxy runledger:8787" in (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")


def test_license_file_is_apache_2_0():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "Copyright 2026 RunLedger" in text
