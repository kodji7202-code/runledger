"""The autouse fixture in conftest.py is what keeps the suite away from the developer's real
agent configs. This machine has FACTORY_HOME_OVERRIDE pointing at the real home, so a variable
that stops being cleared here would silently make every Droid/Codex test read real data.
"""
import os
from pathlib import Path

# Mirrors tests/conftest.py ISOLATED_ENV_VARS: the list is pinned here on purpose, so removing
# one of them fails this test instead of quietly changing what other tests read.
ISOLATED_ENV_VARS = (
    "RUNLEDGER_SERVER",
    "RUNLEDGER_API_KEY",
    "RUNLEDGER_CONFIG",
    "RUNLEDGER_PUSH_SAFE",
    "RUNLEDGER_RETENTION_DAYS",
    "RUNLEDGER_AUDIT_RETENTION_DAYS",
    "FACTORY_HOME",
    "FACTORY_HOME_OVERRIDE",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "FACTORY_PROJECT_DIR",
    "DROID_PROJECT_DIR",
    "CLAUDE_PROJECT_DIR",
)


def test_every_agent_config_and_server_variable_is_cleared():
    leaked = [name for name in ISOLATED_ENV_VARS if name in os.environ]
    assert leaked == []


def test_home_and_userprofile_point_at_a_temporary_folder(tmp_path):
    home = Path(os.environ["HOME"])
    assert os.environ["USERPROFILE"] == str(home)
    assert home == tmp_path / "isolated-home"
    assert home.is_dir()


def test_the_isolated_home_holds_no_agent_config(tmp_path):
    home = Path(os.environ["HOME"])
    for name in (".claude", ".factory", ".codex", ".runledger"):
        assert not (home / name).exists(), f"{name} exists under the isolated HOME"
