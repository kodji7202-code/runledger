"""PowerShell write/delete extraction, risk assessment, file_changes, and guard hook."""
import io
import json

import pytest

from runledger import guard
from runledger.parser import Run, Step, Usage
from runledger.ps_writes import ps_deletes, ps_writes, shell_kind
from runledger.receipt import to_dict
from runledger.risk import assess, assess_step
from runledger.summarize import file_changes

NO_POLICY = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": []}


def _risks(tool, cwd=r"C:\proj", **inp):
    return assess_step(Step(1, tool, inp, "t1", None, None), cwd, NO_POLICY)


def _codes(tool, cwd=r"C:\proj", **inp):
    return [(r.severity, r.code) for r in _risks(tool, cwd=cwd, **inp)]


def test_shell_kind_detection():
    assert shell_kind("PowerShell", "dir") == "powershell"
    assert shell_kind("Bash", "Set-Content -Path a.txt -Value 1") == "powershell"
    assert shell_kind("Bash", "echo hi > a.txt") == "posix"
    step_ps = Step(1, "PowerShell", {"command": "sc a.txt 1"}, "t1", None, None)
    step_bash_ps = Step(1, "Bash", {"command": "Out-File -FilePath a.txt"}, "t1", None, None)
    step_bash = Step(1, "Bash", {"command": "ls -la"}, "t1", None, None)
    assert shell_kind(step_ps) == "powershell"
    assert shell_kind(step_bash_ps) == "powershell"
    assert shell_kind(step_bash) == "posix"


def test_quoted_or_commented_cmdlet_does_not_flip_a_bash_step_to_powershell():
    cmd = "echo 'Set-Content is a cmdlet'; touch t.txt; rm r.txt"
    assert shell_kind("Bash", cmd) == "posix"
    step = Step(1, "Bash", {"command": cmd}, "t1", None, None)
    run = Run("s", "p", r"C:\proj", "main", None, None, ["go"], [step], "", Usage(), {})
    assert sorted(file_changes(run)) == ["r.txt", "t.txt"]
    assert shell_kind("Bash", "# Remove-Item old.txt\nrm old.txt") == "posix"
    assert shell_kind("Bash", "grep -n Set-Content notes.md") == "posix"
    assert shell_kind("Bash", 'printf "%s" "Remove-Item"') == "posix"


def test_cmdlet_in_command_position_is_still_powershell():
    assert shell_kind("Bash", "Set-Content -Path a.txt -Value 1") == "powershell"
    assert shell_kind("Bash", "echo hi; Remove-Item -Path a.txt") == "powershell"
    assert shell_kind("Bash", "for f in *.txt; do Remove-Item $f; done") == "powershell"


@pytest.mark.parametrize("tool", ["PowerShell", "Bash"])
def test_powershell_write_risks_match_shell_write_risks(tool):
    assert ("high", "secret_file") in _codes(tool, command="Set-Content -Path .env -Value SECRET=1")
    assert ("medium", "git_internals") in _codes(tool, command="'x' | Out-File .git/hooks/pre-commit")
    assert ("medium", "db_migration") in _codes(tool, command="Set-Content -Path migrations/001_init.sql -Value x")
    assert ("low", "shell_outside") in _codes(
        tool, cwd=r"C:\proj", command=r"Copy-Item src\a.py C:\Users\bob\AppData\Local\Temp\a.py"
    )
    assert ("low", "shell_outside") in _codes(
        tool, cwd=r"C:\proj", command=r"Set-Content C:\Windows\x.txt 1"
    )
    assert ("high", "test_deleted") in _codes(tool, command=r"Remove-Item tests\test_a.py")


def test_powershell_cmdlet_writes_and_deletes_populate_file_changes():
    commands = [
        'Add-Content -Path notes.txt -Value "hello\\nworld"',
        "'line' | Out-File -FilePath out/report.txt",
        "Copy-Item a.txt b.txt",
        "Move-Item tests/test_a.py /tmp/a.py",
        "Remove-Item -Path tests/test_b.py -Force",
        "New-Item -ItemType File -Path src/new.py",
        "New-Item -ItemType Directory -Path src/dir",
        "Rename-Item -Path old.py -NewName new.py",
    ]
    steps = [Step(i + 1, "PowerShell", {"command": c}, f"s{i}", "m", None) for i, c in enumerate(commands)]
    run = Run("s", "p", "/proj", "main", None, None, ["go"], steps, "", Usage(), {})
    score, level, risks = assess(run)
    doc = to_dict(run, score, level, risks)
    files = {f["path"]: f for f in doc["files"]}

    assert files["notes.txt"]["shell"] is True
    assert files["notes.txt"]["deleted"] is False
    assert files["notes.txt"]["added"] >= 1
    assert files["out/report.txt"]["shell"] is True
    assert files["out/report.txt"]["deleted"] is False
    assert files["b.txt"]["shell"] is True
    assert files["b.txt"]["deleted"] is False
    assert "a.txt" not in files
    assert files["tests/test_a.py"]["deleted"] is True
    assert "/tmp/a.py" not in files
    assert files["tests/test_b.py"]["deleted"] is True
    assert files["src/new.py"]["shell"] is True
    assert files["src/new.py"]["deleted"] is False
    assert "src/dir" not in files
    assert files["old.py"]["deleted"] is True
    assert files["new.py"]["deleted"] is False

    reason_pairs = [(r["severity"], r["code"]) for r in doc["risk"]["reasons"]]
    assert ("high", "test_deleted") in reason_pairs
    assert ("low", "temp_copy") in reason_pairs


def test_powershell_aliases_redirections_and_dotnet_writes():
    commands = [
        "sc a.txt 'x'",
        "ac b.txt 'x'",
        "cpi a.txt c.txt",
        "mi c.txt d.txt",
        "ri d.txt",
        "ni e.txt",
        "rni e.txt f.txt",
        "'hi' > g.txt",
        "'more' >> g.txt",
        "cmd > h.txt 2>&1",
        "cmd > $null",
        "cmd 2> $null",
        "Get-Process | Tee-Object -FilePath proc.log",
        "Get-Process | tee proc2.log",
        "Get-Service | Export-Csv -Path svc.csv -NoTypeInformation",
        "Get-Service | epcsv svc2.csv",
        "Set-Location sub; Set-Content f.txt 1",
        "[System.IO.File]::WriteAllText('dotnet.txt', 'z')",
    ]
    steps = [Step(i + 1, "PowerShell", {"command": c}, f"s{i}", "m", None) for i, c in enumerate(commands)]
    run = Run("s", "p", r"C:\proj", "main", None, None, ["go"], steps, "", Usage(), {})
    score, level, risks = assess(run)
    doc = to_dict(run, score, level, risks)
    files = {f["path"]: f for f in doc["files"]}
    for written in ("a.txt", "b.txt", "f.txt", "g.txt", "h.txt", "proc.log", "proc2.log", "svc.csv", "svc2.csv", "sub/f.txt", "dotnet.txt"):
        assert written in files, f"missing {written} in {files}"
        assert files[written]["deleted"] is False
    for deleted in ("c.txt", "d.txt", "e.txt"):
        assert files[deleted]["deleted"] is True
    for bogus in ("1", "&1", "$null"):
        assert bogus not in files


@pytest.mark.parametrize("command, expected", [
    (r"Set-Content -Path E:\Work\Client y\z.txt -Value x", [("E:\\Work\\Client y\\z.txt", False)]),
    (r"Set-Content E:\Work\Client y\z.txt x", [("E:\\Work\\Client y\\z.txt", False)]),
    (r"'x' > E:\Work\Client y\z.txt", [("E:\\Work\\Client y\\z.txt", False)]),
    (r"Copy-Item a.txt E:\Work\Client y\z.txt", [("E:\\Work\\Client y\\z.txt", False)]),
    (r"Remove-Item E:\Work\Client y\z.txt", [("E:\\Work\\Client y\\z.txt", True)]),
])
def test_unquoted_spaced_drive_path_is_one_write_target(command, expected):
    """A PowerShell path argument with spaces is one path, never its first fragment."""
    assert [(w.path, w.deleted) for w in ps_writes(command)] == expected


def test_spaced_drive_redirect_target_is_one_write_finding():
    risks = _risks("PowerShell", command=r"'x' > E:\Work\Client y\z.txt")
    assert [(r.severity, r.code, r.reason) for r in risks] == [
        ("low", "shell_outside", "Shell command wrote outside the working folder (E:\\Work\\Client y\\z.txt)")]


@pytest.mark.parametrize("command", [
    "Set-Content $target 'x'",
    r"Out-File -FilePath $env:TEMP\x.txt",
    r"Remove-Item $dir\*",
    "Remove-Item *.tmp",
    "Copy-Item a?.txt dst\\",
    "'x' > $null",
    "Set-Content (Join-Path $a $b) x",
    "Remove-Item -Path .env -WhatIf",
    "[System.IO.File]::WriteAllText($p, 'x')",
])
def test_powershell_variable_and_wildcard_targets_are_skipped(command):
    assert ps_writes(command) == []
    assert ps_deletes(command) == []
    step = Step(1, "PowerShell", {"command": command}, "s1", "m", None)
    run = Run("s", "p", r"C:\proj", "main", None, None, ["go"], [step], "", Usage(), {})
    assert file_changes(run) == {}
    codes = {r.code for r in assess_step(step, r"C:\proj", NO_POLICY)}
    assert not (codes & {"secret_file", "test_deleted", "db_write", "db_migration", "git_internals", "write_outside", "shell_outside"})


def test_powershell_cmdlets_inside_bash_step_get_powershell_analysis():
    commands = [
        "Set-Content -Path .env -Value 'A=1'",
        "Out-File -FilePath out.txt",
        r"Remove-Item tests\test_x.py",
    ]
    steps = [Step(i + 1, "Bash", {"command": c}, f"s{i}", "m", None) for i, c in enumerate(commands)]
    run = Run("s", "p", r"C:\proj", "main", None, None, ["go"], steps, "", Usage(), {})
    score, level, risks = assess(run)
    doc = to_dict(run, score, level, risks)
    files = {f["path"]: f for f in doc["files"]}
    assert "out.txt" in files and not files["out.txt"]["deleted"]
    assert "tests/test_x.py" in files and files["tests/test_x.py"]["deleted"]
    codes = {r["code"] for r in doc["risk"]["reasons"]}
    assert "secret_file" in codes
    assert "test_deleted" in codes


def test_guard_evaluates_powershell_tool_events(tmp_path):
    ev_secret = {
        "session_id": "s-ps",
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
        "tool_name": "PowerShell",
        "tool_input": {"command": "Set-Content -Path .env -Value x"},
    }
    out_secret = guard.handle(json.dumps(ev_secret))
    assert out_secret is not None
    assert out_secret["hookSpecificOutput"]["permissionDecision"] == "ask"
    log_lines = [
        json.loads(line)
        for line in (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any("secret_file" in rec.get("codes", []) for rec in log_lines)

    ev_rm = {
        "session_id": "s-ps",
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
        "tool_name": "PowerShell",
        "tool_input": {"command": "rm -rf /"},
    }
    ev_rm_bash = dict(ev_rm, tool_name="Bash")
    out_rm = guard.handle(json.dumps(ev_rm))
    out_rm_bash = guard.handle(json.dumps(ev_rm_bash))
    assert out_rm is not None
    assert out_rm["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out_rm == out_rm_bash

    ev_benign = {
        "session_id": "s-ps",
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
        "tool_name": "PowerShell",
        "tool_input": {"command": "Get-ChildItem"},
    }
    assert guard.handle(json.dumps(ev_benign)) is None
