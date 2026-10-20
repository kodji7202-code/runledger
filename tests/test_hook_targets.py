"""Per-agent hook install targets: `guard install|uninstall --agent claude-code|droid|codex|all`
(validation contract VAL-AGENT-001..012).

The hook file each agent reads and the structure it expects come from `research/agent-hooks.md`:
Factory Droid's standalone `hooks.json` is keyed directly by event (no `hooks` wrapper) and Codex
wraps the events in a `hooks` object. The lifecycle payload key sets are the recorded ones
(`tests/fixtures/guard/hook_payloads.json`, droid 0.232.0, codex 0.159.0). Every project folder
and home below is a scratch one: the shared `isolated_settings` fixture in conftest.py redirects
HOME/USERPROFILE, so no real agent file is read or written.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from runledger import guard
from runledger.cli import main as cli_main

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENTS = ("claude-code", "droid", "codex")
NEW_AGENTS = ("droid", "codex")
# The top-level key holding the events, per agent: Codex and Claude Code wrap, Droid does not.
WRAPPER = {"claude-code": "hooks", "droid": None, "codex": "hooks"}
FOLDER = {"claude-code": (".claude", "settings.json"),
          "droid": (".factory", "hooks.json"),
          "codex": (".codex", "hooks.json")}
CHECKPOINTS = ("SessionStart", "UserPromptSubmit")
BAD_SHAPES = {
    "claude-code": ['{not json', "[]", '{"hooks": 5}'],
    "codex": ['{not json', "[]", '{"hooks": 5}'],
    "droid": ['{not json', "[]", '{"PreToolUse": "x"}'],
}


# ---------------------------------------------------------------- helpers

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def container(data, agent):
    """The event mapping of that agent's file: wrapped for Claude Code and Codex, the file
    itself for Droid."""
    key = WRAPPER[agent]
    return data if key is None else data[key]


def events_of(data, agent, event):
    return container(data, agent)[event]


def hook_of(data, agent, event="PreToolUse"):
    return events_of(data, agent, event)[0]["hooks"][0]


def project_file(project, agent):
    return Path(project).joinpath(*FOLDER[agent])


def install(project=None, agent=None, *extra):
    argv = ["guard", "install", *extra]
    if project is not None:
        argv += ["--project", str(project)]
    if agent is not None:
        argv += ["--agent", agent]
    return cli_main(argv)


def uninstall(project=None, agent=None, *extra):
    argv = ["guard", "uninstall", *extra]
    if project is not None:
        argv += ["--project", str(project)]
    if agent is not None:
        argv += ["--agent", agent]
    return cli_main(argv)


def preseed(agent):
    """A hook file with foreign content of every kind the assertions name."""
    foreign = {"matcher": "Execute|Bash", "hooks": [{"type": "command", "command": "echo foreign"}]}
    events = {
        "PreToolUse": [foreign],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo post"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
    }
    if agent == "claude-code":
        return {"model": "claude-haiku-4-5-20251001", "permissions": {"allow": ["Bash(ls)"]}, "hooks": events}
    if agent == "codex":
        return {"description": "workspace hooks", "hooks": events}
    return dict(events, note="droid file, not settings.json")


def assert_written(data, agent):
    """The guard entry, its command, its timeout and the two lifecycle entries."""
    pre = events_of(data, agent, "PreToolUse")
    guard_hooks = [h for group in pre for h in group.get("hooks", []) if guard._owned_command_parts(h.get("command"))]
    assert len(guard_hooks) == 1
    entry = guard_hooks[0]
    assert entry["type"] == "command" and entry["timeout"] == 135
    expected = f'"{sys.executable}" -m runledger guard'
    if agent in NEW_AGENTS:
        expected += f" --agent {agent}"
    assert entry["command"] == expected
    groups = [group for group in pre if entry in group.get("hooks", [])]
    assert isinstance(groups[0].get("matcher", ""), str)
    assert groups[0].get("matcher", "") in ("*", "", ".*")
    for event in CHECKPOINTS:
        entries = [h for group in events_of(data, agent, event) for h in group.get("hooks", [])
                   if h.get("command") == f'"{sys.executable}" -m runledger checkpoint --hook']
        assert len(entries) == 1 and entries[0]["timeout"] == 120


def droid_event(cwd, tool, tool_input, event="PreToolUse", session="droid-session-1"):
    return {"session_id": session, "transcript_path": None, "cwd": str(cwd),
            "permission_mode": "auto-low", "hook_event_name": event,
            "tool_name": tool, "tool_input": tool_input}


def codex_event(cwd, tool, tool_input, event="PreToolUse", session="codex-session-1"):
    payload = {"session_id": session, "transcript_path": None, "cwd": str(cwd),
               "hook_event_name": event, "model": "gpt-6-luna", "permission_mode": "default",
               "tool_name": tool, "tool_use_id": "call_1", "turn_id": "turn_1",
               "tool_input": tool_input}
    return payload


def claude_event(cwd, tool, tool_input, event="PreToolUse", session="claude-session-1"):
    return {"session_id": session, "transcript_path": None, "cwd": str(cwd),
            "hook_event_name": event, "permission_mode": "acceptEdits", "prompt_id": "p1",
            "tool_name": tool, "tool_use_id": "toolu_1", "tool_input": tool_input}


def lifecycle_event(agent, event, cwd, session):
    """The recorded key set of a Droid or Codex lifecycle payload, with the folder replaced."""
    if agent == "droid":
        payload = {"cwd": str(cwd), "hook_event_name": event, "permission_mode": "auto-low",
                   "session_id": session, "source": "startup", "transcript_path": None,
                   "CLAUDE_ENV_FILE": None}
    else:
        payload = {"cwd": str(cwd), "hook_event_name": event, "model": "gpt-6-luna",
                   "permission_mode": "default", "session_id": session, "source": "startup",
                   "transcript_path": None}
    if event == "UserPromptSubmit":
        payload["prompt"] = "SECRET-PROMPT-TEXT"
        if agent == "codex":
            payload["turn_id"] = "turn_1"
    return payload


def git(folder, *args):
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)


def git_repo(folder):
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q")
    git(folder, "config", "core.autocrlf", "false")
    (folder / "tracked.txt").write_text("content\n", encoding="utf-8")
    git(folder, "add", ".")
    return folder


def run_hook_command(command, payload, cwd):
    """Run a stored hook command exactly as the agent would: the string itself, in a shell."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(command, shell=True, cwd=str(cwd), input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=120)


# ---------------------------------------------------------------- VAL-AGENT-001

def test_default_install_is_claude_code_only_and_unchanged(tmp_path, capsys):
    p1, p2 = tmp_path / "p1", tmp_path / "p2"
    p1.mkdir()
    p2.mkdir()

    assert install(p1) == 0
    out = capsys.readouterr().out
    assert install(p2, "claude-code") == 0
    capsys.readouterr()

    first, second = project_file(p1, "claude-code"), project_file(p2, "claude-code")
    assert first.read_bytes() == second.read_bytes()
    assert sorted(p.name for p in p1.iterdir()) == [".claude"]
    assert not (p1 / ".factory").exists() and not (p1 / ".codex").exists()
    assert_written(read_json(first), "claude-code")
    assert "Installed" in out and str(first) in out
    assert install(p1) == 0
    again = capsys.readouterr().out
    assert "already up to date" in again and "Nothing changed" in again


def test_install_help_lists_the_agents_and_the_scopes(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main(["guard", "install", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for needle in ("--agent {claude-code,droid,codex,all}", "--project", "--global"):
        assert needle in out, needle
    with pytest.raises(SystemExit) as exc:
        cli_main(["guard", "uninstall", "--help"])
    assert exc.value.code == 0
    assert "--agent {claude-code,droid,codex,all}" in capsys.readouterr().out


# ---------------------------------------------------------------- VAL-AGENT-002 / 003

@pytest.mark.parametrize("agent", AGENTS)
def test_project_install_writes_that_agents_own_file(tmp_path, agent, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()

    assert install(proj, agent) == 0
    out = capsys.readouterr().out

    target = project_file(proj, agent)
    assert target.exists() and str(target) in out
    assert sorted(p.name for p in proj.iterdir()) == [FOLDER[agent][0]]
    data = read_json(target)
    assert set(container(data, agent)) >= {"PreToolUse", "SessionStart", "UserPromptSubmit"}
    if agent == "droid":
        assert "hooks" not in data
        assert not (proj / ".factory" / "settings.json").exists()
    else:
        assert set(data) == {"hooks"}
    if agent == "codex":
        assert not (proj / ".codex" / "config.toml").exists()
        matcher = events_of(data, "codex", "PreToolUse")[0]["matcher"]
        for name in ("Bash", "apply_patch", "mcp__fs__read_file"):
            assert re.search(matcher, name)
    assert_written(data, agent)


def test_droid_install_message_says_how_to_load_the_hooks(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "droid") == 0
    out = capsys.readouterr().out
    assert "/hooks" in out and "restart" in out.lower()
    assert "verif" not in out.lower()


def test_codex_install_message_says_trust_and_ask_to_deny(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0
    out = capsys.readouterr().out
    assert "/hooks" in out and "trust" in out.lower()
    assert "guard.codex_ask" in out
    assert "deny" in out.lower()
    assert "verif" not in out.lower()


# ---------------------------------------------------------------- VAL-AGENT-004

@pytest.mark.parametrize("agent", AGENTS)
def test_the_installed_command_uses_the_absolute_interpreter_and_denies(tmp_path, agent, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, agent) == 0
    capsys.readouterr()

    stored = hook_of(read_json(project_file(proj, agent)), agent)["command"]
    interpreter = subprocess.run([sys.executable, "-c", "import sys;print(sys.executable)"],
                                 capture_output=True, text=True).stdout.strip()
    assert stored == f'"{interpreter}" -m runledger guard' + (f" --agent {agent}" if agent in NEW_AGENTS else "")
    assert Path(interpreter).exists()

    event = {"claude-code": claude_event, "droid": droid_event, "codex": codex_event}[agent]
    tool = "Execute" if agent == "droid" else "Bash"
    res = run_hook_command(stored, event(proj, tool, {"command": "rm -rf x"}), proj)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("agent", AGENTS)
def test_portable_writes_the_bare_command(tmp_path, agent, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, agent, "--portable") == 0
    capsys.readouterr()
    stored = hook_of(read_json(project_file(proj, agent)), agent)["command"]
    assert stored == "runledger guard" + (f" --agent {agent}" if agent in NEW_AGENTS else "")
    for event in CHECKPOINTS:
        assert hook_of(read_json(project_file(proj, agent)), agent, event)["command"] == "runledger checkpoint --hook"


def test_the_installed_command_survives_a_spaced_interpreter_path(tmp_path, capsys):
    """A project folder is not the only path that can hold a space: the stored interpreter can
    too (the `--portable` form aside, which never names one)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0
    capsys.readouterr()
    stored = hook_of(read_json(project_file(proj, "codex")), "codex")["command"]
    assert stored.startswith('"') and '" -m runledger guard --agent codex' in stored
    assert guard._owned_command_parts(stored) is not None


# ---------------------------------------------------------------- VAL-AGENT-035 (Codex)

def test_codex_entries_carry_the_windows_command_it_can_actually_run(tmp_path, capsys):
    """Codex runs hook commands itself, and on Windows the `"C:\\...\\python.exe" -m runledger
    guard` form never starts: the quotes are read as part of the program name (measured with
    codex 0.163.0 by pointing the hook at a recorder script - the same path unquoted fires, the
    quoted one is silent). Every Codex entry therefore also stores the `cmd /c` copy of the same
    command, in the `commandWindows` field Codex reads on Windows."""
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0
    capsys.readouterr()

    data = read_json(project_file(proj, "codex"))
    guard_hook = hook_of(data, "codex")
    assert guard_hook["commandWindows"] == f'cmd /c "{guard_hook["command"]}"'
    assert sys.executable in guard_hook["commandWindows"]
    for event in CHECKPOINTS:
        hook = hook_of(data, "codex", event)
        assert hook["commandWindows"] == f'cmd /c "{hook["command"]}"'


def test_portable_codex_entries_carry_the_windows_command_too(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex", "--portable") == 0
    capsys.readouterr()

    data = read_json(project_file(proj, "codex"))
    assert hook_of(data, "codex")["commandWindows"] == 'cmd /c "runledger guard --agent codex"'
    for event in CHECKPOINTS:
        assert hook_of(data, "codex", event)["commandWindows"] == 'cmd /c "runledger checkpoint --hook"'


@pytest.mark.parametrize("agent", ("claude-code", "droid"))
def test_other_agents_keep_the_single_command(tmp_path, agent, capsys):
    """Droid's and Claude Code's own hook runners start the quoted interpreter path as written,
    so their files stay as they were."""
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, agent) == 0
    capsys.readouterr()

    data = read_json(project_file(proj, agent))
    assert "commandWindows" not in hook_of(data, agent)
    for event in CHECKPOINTS:
        assert "commandWindows" not in hook_of(data, agent, event)


def test_a_codex_entry_without_the_windows_command_is_repaired_in_place(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0
    capsys.readouterr()
    target = project_file(proj, "codex")
    data = read_json(target)
    del hook_of(data, "codex")["commandWindows"]
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")

    assert install(proj, "codex") == 0
    capsys.readouterr()
    data = read_json(target)
    assert hook_of(data, "codex")["commandWindows"] == f'cmd /c "{hook_of(data, "codex")["command"]}"'

    before = sha(target)
    assert install(proj, "codex") == 0
    out = capsys.readouterr().out
    assert "already up to date" in out and "Nothing changed" in out
    assert sha(target) == before


def test_uninstall_removes_a_codex_entry_with_the_windows_command(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "codex") == 0
    capsys.readouterr()

    assert uninstall(proj, "codex") == 0
    capsys.readouterr()
    text = project_file(proj, "codex").read_text(encoding="utf-8")
    assert "commandWindows" not in text and "runledger" not in text
    assert read_json(project_file(proj, "codex")) == {}


# ---------------------------------------------------------------- VAL-AGENT-005

@pytest.mark.parametrize("agent", AGENTS)
def test_install_is_idempotent_and_backs_up_once(tmp_path, agent, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = project_file(proj, agent)
    target.parent.mkdir(parents=True)
    original = preseed(agent)
    text = json.dumps(original, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")
    backup = target.with_name(target.name + ".bak")

    assert install(proj, agent) == 0
    capsys.readouterr()
    assert backup.read_text(encoding="utf-8") == text
    first, backup_first = sha(target), sha(backup)

    assert install(proj, agent) == 0
    out = capsys.readouterr().out
    assert "already up to date" in out and "Nothing changed" in out
    assert sha(target) == first and sha(backup) == backup_first
    assert sorted(p.name for p in target.parent.iterdir()) == sorted([target.name, backup.name])


@pytest.mark.parametrize("agent", AGENTS)
def test_a_hand_edited_timeout_is_corrected_in_place(tmp_path, agent, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, agent) == 0
    capsys.readouterr()
    target = project_file(proj, agent)
    data = read_json(target)
    hook_of(data, agent)["timeout"] = 10
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")

    assert install(proj, agent) == 0
    capsys.readouterr()
    data = read_json(target)
    assert hook_of(data, agent)["timeout"] == 135
    entries = [h for group in events_of(data, agent, "PreToolUse") for h in group.get("hooks", [])]
    assert sum(guard._owned_command_parts(h.get("command")) is not None for h in entries) == 1


def test_installing_one_agent_leaves_the_others_alone(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "claude-code") == 0
    capsys.readouterr()
    before = sha(project_file(proj, "claude-code"))
    for agent in NEW_AGENTS:
        assert install(proj, agent) == 0
        capsys.readouterr()
        assert sha(project_file(proj, "claude-code")) == before


# ---------------------------------------------------------------- VAL-AGENT-006

@pytest.mark.parametrize("agent", AGENTS)
def test_install_preserves_foreign_content_and_uninstall_restores_it(tmp_path, agent, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = project_file(proj, agent)
    target.parent.mkdir(parents=True)
    original = preseed(agent)
    target.write_text(json.dumps(original, indent=2), encoding="utf-8")

    assert install(proj, agent) == 0
    capsys.readouterr()
    installed = read_json(target)
    foreign = container(original, agent)["PreToolUse"][0]
    assert foreign in events_of(installed, agent, "PreToolUse")
    assert events_of(installed, agent, "PostToolUse") == container(original, agent)["PostToolUse"]
    assert events_of(installed, agent, "Stop") == container(original, agent)["Stop"]
    for key, value in original.items():
        if key not in ("hooks",) and key not in container(original, agent):
            assert installed[key] == value

    assert uninstall(proj, agent) == 0
    capsys.readouterr()
    assert read_json(target) == original
    assert uninstall(proj, agent) == 0
    out = capsys.readouterr().out
    assert "Nothing changed" in out
    assert read_json(target) == original


def test_uninstall_without_a_hook_file_creates_nothing(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    policy = proj / ".runledger.json"
    policy.write_text(json.dumps({"ask": ["severity:high"]}), encoding="utf-8")
    (proj / ".runledger").mkdir()
    log = proj / ".runledger" / "guard.log"
    log.write_text("", encoding="utf-8")

    for agent in AGENTS:
        assert uninstall(proj, agent) == 0
    assert "Nothing changed" in capsys.readouterr().out
    assert sorted(p.name for p in proj.iterdir()) == [".runledger", ".runledger.json"]
    assert json.loads(policy.read_text(encoding="utf-8")) == {"ask": ["severity:high"]}
    assert log.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------- VAL-AGENT-007

@pytest.mark.parametrize("agent", AGENTS)
def test_malformed_files_are_refused_untouched(tmp_path, agent, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = project_file(proj, agent)
    target.parent.mkdir(parents=True)

    for bad in BAD_SHAPES[agent]:
        target.write_text(bad, encoding="utf-8")
        before = sha(target)

        assert install(proj, agent) == 1
        err = capsys.readouterr().err
        assert str(target) in err and "Nothing was changed" in err
        assert sha(target) == before

        assert uninstall(proj, agent) == 1
        err = capsys.readouterr().err
        assert str(target) in err and "Nothing was changed" in err
        assert sha(target) == before

        assert not target.with_name(target.name + ".bak").exists()
        assert not target.with_name(target.name + ".tmp").exists()


@pytest.mark.parametrize("agent", AGENTS)
def test_an_empty_or_bom_file_is_accepted(tmp_path, agent, capsys):
    empty = tmp_path / "empty"
    (empty / FOLDER[agent][0]).mkdir(parents=True)
    target = project_file(empty, agent)
    target.write_bytes(b"")

    assert install(empty, agent) == 0
    capsys.readouterr()
    assert_written(read_json(target), agent)

    bom = tmp_path / "bom"
    (bom / FOLDER[agent][0]).mkdir(parents=True)
    bom_target = project_file(bom, agent)
    bom_target.write_bytes(b"\xef\xbb\xbf" + json.dumps(preseed(agent)).encode("utf-8"))
    assert install(bom, agent) == 0
    capsys.readouterr()
    assert_written(read_json(bom_target), agent)


# ---------------------------------------------------------------- VAL-AGENT-008

def test_an_unknown_agent_is_rejected_before_anything_is_written(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    for argv in (["guard", "install", "--agent", "bogus", "--project", str(proj)],
                 ["guard", "uninstall", "--agent", "bogus", "--project", str(proj)]):
        with pytest.raises(SystemExit) as exc:
            cli_main(argv)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        for needle in ("claude-code", "droid", "codex", "all"):
            assert needle in err
    assert list(proj.iterdir()) == []


def test_guard_test_reads_each_agents_payload_and_writes_no_log(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()

    assert cli_main(["guard", "test", "--agent", "droid",
                     json.dumps(droid_event(proj, "Execute", {"command": "rm -rf x"}))]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"]["decision"] == "deny"
    assert out["hook_output"]["hookSpecificOutput"]["permissionDecision"] == "deny"

    assert cli_main(["guard", "test", "--agent", "codex",
                     json.dumps(codex_event(proj, "Bash", {"command": "rm -rf x"}))]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hook_output"]["hookSpecificOutput"]["permissionDecision"] == "deny"

    assert cli_main(["guard", "test", json.dumps(claude_event(proj, "Bash", {"command": "rm -rf x"}))]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hook_output"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not (proj / ".runledger").exists()


# ---------------------------------------------------------------- VAL-AGENT-009

@pytest.mark.parametrize("agent,expected", [
    ("claude-code", (".claude", "settings.json")),
    ("droid", (".factory", "hooks.json")),
    ("codex", (".codex", "hooks.json")),
])
def test_global_install_resolves_the_agents_home(tmp_path, agent, expected, capsys):
    home = Path(os.environ["HOME"])
    assert cli_main(["guard", "install", "--global", "--agent", agent]) == 0
    capsys.readouterr()
    target = home.joinpath(*expected)
    assert target.exists()
    assert_written(read_json(target), agent)

    assert cli_main(["guard", "uninstall", "--global", "--agent", agent]) == 0
    capsys.readouterr()
    assert read_json(target) == {}


def test_global_install_follows_each_config_directory_variable(tmp_path, monkeypatch, capsys):
    home = Path(os.environ["HOME"])

    claude_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    assert cli_main(["guard", "install", "--global", "--agent", "claude-code"]) == 0
    capsys.readouterr()
    assert (claude_dir / "settings.json").exists()
    assert not (home / ".claude" / "settings.json").exists()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")

    override = tmp_path / "factory-home"
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", str(override))
    assert cli_main(["guard", "install", "--global", "--agent", "droid"]) == 0
    capsys.readouterr()
    assert (override / ".factory" / "hooks.json").exists()

    factory_dir = tmp_path / "factory-dir"
    monkeypatch.setenv("FACTORY_HOME", str(factory_dir))
    assert cli_main(["guard", "install", "--global", "--agent", "droid"]) == 0
    capsys.readouterr()
    assert (factory_dir / "hooks.json").exists()          # FACTORY_HOME wins over the override

    codex_dir = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    assert cli_main(["guard", "install", "--global", "--agent", "codex"]) == 0
    capsys.readouterr()
    assert (codex_dir / "hooks.json").exists()
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.delenv("FACTORY_HOME")
    monkeypatch.delenv("FACTORY_HOME_OVERRIDE")
    assert not (home / ".codex" / "hooks.json").exists()


def test_global_uninstall_mirrors_the_environment(monkeypatch, capsys):
    home = Path(os.environ["HOME"])
    monkeypatch.setenv("CODEX_HOME", str(home / "codex-home"))
    assert cli_main(["guard", "install", "--global", "--agent", "codex"]) == 0
    capsys.readouterr()
    target = home / "codex-home" / "hooks.json"
    assert read_json(target)["hooks"]["PreToolUse"]
    assert cli_main(["guard", "uninstall", "--global", "--agent", "codex"]) == 0
    capsys.readouterr()
    assert read_json(target) == {}


# ---------------------------------------------------------------- VAL-AGENT-010

def test_agent_all_installs_only_where_the_agent_home_exists(tmp_path, capsys):
    home = Path(os.environ["HOME"])
    (home / ".claude").mkdir()
    (home / ".factory").mkdir()

    assert cli_main(["guard", "install", "--global", "--agent", "all"]) == 0
    out = capsys.readouterr().out
    assert (home / ".claude" / "settings.json").exists()
    assert (home / ".factory" / "hooks.json").exists()
    assert not (home / ".codex").exists()
    assert "Codex" in out and "skipped" in out.lower() and "CODEX_HOME" in out

    first = sha(home / ".factory" / "hooks.json")
    assert cli_main(["guard", "install", "--global", "--agent", "all"]) == 0
    again = capsys.readouterr().out
    assert "already up to date" in again
    assert sha(home / ".factory" / "hooks.json") == first


def test_agent_all_installs_every_existing_home_and_uninstalls_them(tmp_path, capsys):
    home = Path(os.environ["HOME"])
    for folder in (".claude", ".factory", ".codex"):
        (home / folder).mkdir()

    assert cli_main(["guard", "install", "--global", "--agent", "all"]) == 0
    out = capsys.readouterr().out
    assert "skipped" not in out.lower()
    for agent in AGENTS:
        assert_written(read_json(home.joinpath(*FOLDER[agent])), agent)

    assert cli_main(["guard", "uninstall", "--global", "--agent", "all"]) == 0
    capsys.readouterr()
    for agent in AGENTS:
        assert read_json(home.joinpath(*FOLDER[agent])) == {}


def test_agent_all_global_without_any_agent_home_still_installs_claude(tmp_path, capsys):
    home = Path(os.environ["HOME"])
    assert cli_main(["guard", "install", "--global", "--agent", "all"]) == 0
    out = capsys.readouterr().out
    assert (home / ".claude" / "settings.json").exists()
    assert not (home / ".factory").exists() and not (home / ".codex").exists()
    assert "Droid" in out and "Codex" in out and out.lower().count("skipped") == 2

    assert cli_main(["guard", "uninstall", "--global", "--agent", "all"]) == 0
    capsys.readouterr()
    assert sorted(p.name for p in home.iterdir()) == [".claude"]


def test_agent_all_in_a_project_installs_all_three(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert install(proj, "all") == 0
    capsys.readouterr()
    for agent in AGENTS:
        assert_written(read_json(project_file(proj, agent)), agent)
    assert sorted(p.name for p in proj.iterdir()) == [".claude", ".codex", ".factory"]


# ---------------------------------------------------------------- VAL-AGENT-012

@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_lifecycle_checkpoint_hook_is_silent_and_creates_a_checkpoint(tmp_path, agent, capsys):
    repo = git_repo(tmp_path / "repo")
    assert install(repo, agent) == 0
    capsys.readouterr()
    data = read_json(project_file(repo, agent))
    stored = hook_of(data, agent, "SessionStart")["command"]
    assert stored == f'"{sys.executable}" -m runledger checkpoint --hook'

    for index, event in enumerate(CHECKPOINTS):
        res = run_hook_command(stored, lifecycle_event(agent, event, repo, f"{agent}-session-{index}"),
                               repo)
        assert res.returncode == 0
        assert res.stdout == "" and res.stderr == ""

    assert cli_main(["checkpoints", "--cwd", str(repo)]) == 0
    listing = capsys.readouterr().out
    assert "'SessionStart'" in listing and "'UserPromptSubmit'" in listing
    refs = git(repo, "for-each-ref", "--format=%(contents)", "refs/runledger/checkpoints/").stdout
    assert refs.count("RunLedger checkpoint") == 2
    assert "SECRET-PROMPT-TEXT" not in refs


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_lifecycle_checkpoint_hook_is_silent_outside_a_git_repository(tmp_path, agent, capsys):
    proj = tmp_path / "plain"
    proj.mkdir()
    assert install(proj, agent) == 0
    capsys.readouterr()
    stored = hook_of(read_json(project_file(proj, agent)), agent, "SessionStart")["command"]

    for event in CHECKPOINTS:
        res = run_hook_command(stored, lifecycle_event(agent, event, proj, f"{agent}-session-{event}"), proj)
        assert res.returncode == 0
        assert res.stdout == "" and res.stderr == ""
    assert git(proj, "for-each-ref", "refs/runledger/checkpoints/").stdout == ""
