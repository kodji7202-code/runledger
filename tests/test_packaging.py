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

from runledger import __version__

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


def _workflow_jobs(text):
    """{job name: job text} for a workflow file, split on two-space indentation."""
    lines = text.splitlines()
    if "jobs:" not in lines:
        return {}
    lines = lines[lines.index("jobs:") + 1:]
    jobs = {}
    name = None
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            if name is not None:
                jobs[name] = "\n".join(lines[start:index])
            name = line.strip()[:-1]
            start = index
        elif name is not None and re.match(r"^[^\s#]", line):
            jobs[name] = "\n".join(lines[start:index])
            name = None
    if name is not None:
        jobs[name] = "\n".join(lines[start:])
    return jobs


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
    built = sorted(out.glob("runledger_ai-*.whl"))
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
        "runledger/rollback.py",
        "runledger/server/app.py",
        "runledger/adapters/codex.py",
        "runledger/adapters/droid.py",
        "runledger/prices.json",
        "runledger/receipt.schema.json",
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
    assert meta["Summary"] == _toml_string(project, "description")
    for keyword in ("claude-code", "factory-droid", "droid", "codex", "aider", "ai-agents", "guardrails"):
        assert keyword in (meta["Keywords"] or ""), f"{keyword} is missing from the wheel metadata"
    assert meta.get_all("Requires-Dist") is None, "the package must not depend on anything"


def test_pyproject_project_table():
    project = _section("[project]")
    assert _toml_string(project, "name") == "runledger-ai"
    assert _toml_string(project, "license") == "Apache-2.0"
    assert _toml_string(project, "requires-python") == ">=3.9"
    assert _toml_string(project, "readme") == "README.md"
    assert 'license-files = ["LICENSE"]' in project
    assert 'authors = [{ name = "RunLedger", email = "hello@runledger.site" }]' in project
    assert "keywords = [" in project
    assert "Programming Language :: Python :: 3.9" in project
    assert "dependencies = []" in project
    assert __version__ == _toml_string(project, "version")
    # setuptools rejects License classifiers next to a license expression.
    assert "License ::" not in project


def test_pyproject_urls_scripts_and_packages():
    assert 'Homepage = "https://runledger.site"' in _section("[project.urls]")
    assert 'runledger = "runledger.cli:main"' in _section("[project.scripts]")
    # Packages are discovered, not hand-listed: a new subpackage must ship without a second edit.
    assert 'include = ["runledger*"]' in _section("[tool.setuptools.packages.find]")
    assert "packages = [" not in PYPROJECT, "packages must be discovered, not hand-listed"
    assert 'runledger = ["prices.json", "receipt.schema.json"]' in _section("[tool.setuptools.package-data]")


def test_description_mentions_multiple_agents():
    project = _section("[project]")
    assert _toml_string(project, "description") == (
        "Audit, cost and real-time guard for AI coding agents: receipts, risk score, rollback. "
        "Claude Code, Codex CLI, Factory Droid, Aider."
    )
    keywords = re.search(r"^keywords\s*=\s*\[(.*?)\]", project, re.M)
    assert keywords, "keywords are not set"
    names = re.findall(r'"([^"]+)"', keywords.group(1))
    for keyword in ("factory-droid", "droid", "codex", "aider", "ai-agents", "guardrails"):
        assert keyword in names, f"{keyword} is missing from keywords"
    assert "Programming Language :: Python :: 3.14" not in project


def test_every_package_directory_has_an_init():
    missing = []
    for path in sorted((ROOT / "runledger").rglob("*")):
        if not path.is_dir() or "__pycache__" in path.parts:
            continue
        if any(child.suffix == ".py" for child in path.iterdir() if child.is_file()):
            if not (path / "__init__.py").is_file():
                missing.append(str(path.relative_to(ROOT)))
    assert missing == [], f"package directories without __init__.py: {missing}"


def test_gitignore_covers_artifacts():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    patterns = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    duplicates = sorted({p for p in patterns if patterns.count(p) > 1})
    assert duplicates == [], f"duplicate .gitignore entries: {duplicates}"
    for pattern in ("dist/", "build/", ".release-smoke/", "*.db", "*.db-wal", "*.db-shm", ".env",
                    "*.log", "runledger/guard.log", ".venv/", ".pytest_cache/", "*.egg-info/",
                    "__pycache__/", ".runledger/", "deploy/.env"):
        assert pattern in patterns, f"{pattern} is missing from .gitignore"


def test_version_references_are_consistent():
    version = __version__
    minor = ".".join(version.split(".")[:2]) + ".x"
    assert _toml_string(_section("[project]"), "version") == version
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{version}]" in changelog, f"CHANGELOG.md has no '## [{version}]' heading"
    assert f"Version {version}" in (ROOT / "README.md").read_text(encoding="utf-8")
    for relative in ("README.md", "docs/github.md", "examples/workflows/runledger.yml"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert f"runledger@v{version}" in text, f"{relative} does not use the {version} action tag"
    api = (ROOT / "docs" / "api.md").read_text(encoding="utf-8")
    assert f'"version": "{version}"' in api
    assert f'"runledger_version": "{version}"' in api
    table = (ROOT / "SECURITY.md").read_text(encoding="utf-8").splitlines()
    start = table.index("| Version | Security fixes |")
    first_row = table[start + 2]
    assert minor in first_row and "Yes" in first_row, f"first supported-versions row must be {minor}"


def test_ci_matrix_has_every_os_and_python():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "os: [ubuntu-latest, windows-latest, macos-latest]" in ci
    assert 'python: ["3.9", "3.10", "3.11", "3.12", "3.13"]' in ci
    assert "python -m compileall -q runledger" in ci
    assert "/health" in ci
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    assert "python-version: ${{ inputs.python-version }}" in action


def test_ci_has_timeouts_and_least_privilege():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "on:\n  push:\n    branches: [main]\n  pull_request:\n  workflow_dispatch:" in ci
    assert "permissions:\n  contents: read\n" in ci
    assert "cancel-in-progress: true" in ci
    assert ci.count("uses: actions/setup-python@v5") == ci.count("cache: pip")
    jobs = _workflow_jobs(ci)
    assert jobs, "no jobs found in ci.yml"
    for name, body in jobs.items():
        assert re.search(r"^    timeout-minutes: \d+$", body, re.M), f"{name} has no timeout-minutes"


def test_release_workflow_uses_trusted_publishing_on_version_tags():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert '- "v*"' in release
    assert "python -m pytest -q" in release
    assert "python -m venv .release-smoke" in release
    assert ".release-smoke/bin/runledger --version" in release
    assert "id-token: write" in release
    # Only the third-party publish action is pinned by commit; first-party actions stay on tags.
    assert re.search(r"pypa/gh-action-pypi-publish@[0-9a-f]{40} # release/v1", release)
    assert "pypa/gh-action-pypi-publish@release/v1" not in release
    for action in ("actions/checkout@v4", "actions/setup-python@v5", "actions/upload-artifact@v4",
                   "actions/download-artifact@v4"):
        assert action in release
    assert "v0.3.0" not in release


def test_release_workflow_verifies_tag_against_version():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in release
    assert 'tools/check_release.py --tag "$GITHUB_REF_NAME"' in release
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in release
    assert release.index("tools/check_release.py") < release.index("python -m pytest -q")
    assert (ROOT / "tools" / "check_release.py").is_file()


def test_release_workflow_creates_the_release_with_least_privilege():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "permissions:\n  contents: read\n" in release
    assert release.count("contents: write") == 1
    jobs = _workflow_jobs(release)
    assert [name for name, body in jobs.items() if "contents: write" in body] == ["release"]
    assert "needs: publish" in jobs["release"]
    assert 'gh release create "$GITHUB_REF_NAME" --notes-file' in jobs["release"]
    assert "environment:" in jobs["publish"] and "id-token: write" in jobs["publish"]
    for name, body in jobs.items():
        assert re.search(r"^    timeout-minutes: \d+$", body, re.M), f"{name} has no timeout-minutes"


def test_dependabot_config_exists():
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "version: 2" in text
    for ecosystem in ("github-actions", "docker", "pip"):
        assert f"package-ecosystem: {ecosystem}" in text
    assert text.count("directory: /") == 3
    assert text.count("interval: weekly") == 3


def test_container_runs_as_non_root_on_port_8787():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG PYTHON_BASE_IMAGE=python:3.12-slim" in dockerfile
    assert "FROM ${PYTHON_BASE_IMAGE}" in dockerfile
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert '--build-arg PYTHON_BASE_IMAGE="$base"' in ci
    for mirror in ("public.ecr.aws/docker/library/python:3.12-slim",
                   "mirror.gcr.io/library/python:3.12-slim"):
        assert mirror in ci
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
