"""The committed conformance matrix is a release gate, not only documentation."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_committed_conformance_matrix():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_conformance.py"), "--json"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["failed"] == 0
    assert report["summary"]["passed"] == report["summary"]["cases"] >= 7
    names = [case["name"] for case in report["cases"]]
    assert len(names) == len(set(names))
    assert {"claude", "codex", "droid", "droid_shell", "aider", "native", "guard"} <= set(names)


def test_custom_prices_and_user_config_do_not_change_the_matrix(tmp_path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"sonnet:4.5": {"input": 1000, "output": 1000, "cache_read": 1000}}),
                      encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"ignore": ["secret_file", "test_deleted", "command"]}), encoding="utf-8")
    env = dict(os.environ, RUNLEDGER_PRICES=str(prices), RUNLEDGER_CONFIG=str(config))

    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_conformance.py")],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_conformance_failures_have_a_nonzero_exit_and_name_the_case(tmp_path):
    broken = tmp_path / "tree"
    shutil.copytree(
        ROOT, broken,
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"),
    )
    manifest = json.loads((broken / "tests" / "fixtures" / "conformance.json").read_text(encoding="utf-8"))
    manifest["cases"][0]["expect"]["risk_score"] += 1
    (broken / "tests" / "fixtures" / "conformance.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(broken / "tools" / "check_conformance.py")],
        cwd=str(broken), capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 1
    assert "FAIL claude" in result.stdout
    assert "risk_score" in result.stdout
