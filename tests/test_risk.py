"""Tests for runledger.risk: Windows-aware paths and temp dirs, Bash and PowerShell
command rules, hardcoded secrets in written content, and the per-project
.runledger.json policy. Every sample secret below is synthetic."""
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from runledger.labels import command_labels, show_reason
from runledger.parser import Run, Step, Usage
from runledger.risk import _outside, assess, assess_step, load_policy

WIN_CWD = "D:\\runledger"
POSIX_CWD = "/home/dev/payments-service"
NO_POLICY = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": []}


def _step(tool, index=1, **inp):
    return Step(index=index, tool=tool, input=inp, tool_use_id=f"t{index}", model=None, timestamp=None)


def _risks(tool, cwd=WIN_CWD, **inp):
    """Rule results for one step, with the policy fixed so the test never reads a file."""
    return assess_step(_step(tool, **inp), cwd, NO_POLICY)


def _reasons(risks):
    return [r.reason for r in risks]


def _shown(tool, command, risks):
    """The reasons a receipt shows: the whole spaced drive path, applied after the rules ran."""
    labels = command_labels(tool, command)
    return [show_reason(r.reason, labels) for r in risks]


def _run(steps, cwd):
    return Run(session_id="s1", path="s1.jsonl", cwd=cwd, git_branch=None, started=None,
               ended=None, prompts=[], steps=steps, final_message="", usage=Usage(), models={})


# ---------------------------------------------------------------- paths and temp dirs

@pytest.mark.parametrize("path", [r"D:\runledger\runledger\risk.py", "D:/runledger/a.py", r"src\x.py"])
def test_windows_path_inside_cwd_is_not_outside(path):
    assert _outside(path, WIN_CWD) is False


def test_windows_paths_compare_case_insensitively():
    assert _outside(r"d:\RUNLEDGER\Tests\x.py", WIN_CWD) is False


def test_windows_other_drive_is_outside():
    assert _outside(r"E:\data\x.txt", WIN_CWD) is True


def test_windows_sibling_folder_with_same_prefix_is_outside():
    assert _outside(r"D:\runledger2\x.py", WIN_CWD) is True


def test_windows_dotdot_escape_is_outside():
    assert _outside(r"..\other\x.py", WIN_CWD) is True


@pytest.mark.parametrize("path", [
    r"C:\Users\bob\AppData\Local\Temp\build.log",
    r"c:\users\BOB\appdata\local\temp\x.txt",
])
def test_windows_appdata_temp_is_not_outside(path):
    assert _outside(path, WIN_CWD) is False


def test_system_tempdir_is_not_outside():
    scratch = os.path.join(tempfile.gettempdir(), "scratch.txt")
    assert _outside(scratch, WIN_CWD) is False
    assert _outside(scratch, POSIX_CWD) is False


@pytest.mark.parametrize("path", ["/tmp/x", "/var/folders/ab/x.txt", "/private/tmp/x"])
def test_posix_temp_dirs_are_not_outside(path):
    assert _outside(path, POSIX_CWD) is False


def test_posix_inside_and_outside():
    assert _outside(POSIX_CWD + "/src/a.py", POSIX_CWD) is False
    assert _outside("/etc/hosts", POSIX_CWD) is True
    assert _outside("/home/dev/payments-service-old/a.py", POSIX_CWD) is True


def test_nothing_is_outside_without_cwd_or_path():
    assert _outside("/etc/hosts", None) is False
    assert _outside("", POSIX_CWD) is False


def test_write_to_windows_outside_path_is_high():
    risks = _risks("Write", file_path=r"E:\data\x.txt", content="x")
    assert [(r.severity, r.code) for r in risks] == [("high", "write_outside")]


def test_write_to_temp_dir_is_not_flagged():
    risks = _risks("Write", file_path=os.path.join(tempfile.gettempdir(), "x.txt"), content="x")
    assert "write_outside" not in {r.code for r in risks}


# ---------------------------------------------------------------- Bash rules

@pytest.mark.parametrize("cmd", [
    "rm -rf build", "rm -fr build", "rm -r -f build", "rm -f -r build",
    "rm --recursive --force build", "rm --force --recursive build", "rm -Rf build",
])
def test_rm_recursive_force_in_any_form_is_high(cmd):
    hits = [r for r in _risks("Bash", command=cmd) if r.reason == "Recursive force delete (rm -rf)"]
    assert hits and hits[0].severity == "high"


@pytest.mark.parametrize("cmd", ["rm file.txt", "rm -f file.txt", "rm -r build", "rm -r build && ls -f"])
def test_rm_without_both_flags_is_not_recursive_force(cmd):
    assert "Recursive force delete (rm -rf)" not in _reasons(_risks("Bash", command=cmd))


@pytest.mark.parametrize("cmd", [
    "git push --force origin main", "git push -f origin main", "git push -fu origin main",
    "git push --force-with-lease origin main", "git push origin +main", "git push origin +HEAD:main",
])
def test_git_force_push_variants_are_high(cmd):
    hits = [r for r in _risks("Bash", command=cmd) if r.reason == "Force-pushed to a git remote"]
    assert hits and hits[0].severity == "high"


@pytest.mark.parametrize("cmd", [
    "git push -u origin main", "git push origin feature-fix", "git push --follow-tags",
])
def test_plain_git_push_is_not_force(cmd):
    reasons = _reasons(_risks("Bash", command=cmd))
    assert "Force-pushed to a git remote" not in reasons
    assert "Pushed to a git remote" in reasons


@pytest.mark.parametrize("cmd", [
    "bash <(curl -fsSL https://example.com/i.sh)",
    'sh -c "$(curl -fsSL https://example.com/i.sh)"',
    'eval "$(wget -qO- https://example.com/i)"',
])
def test_download_straight_into_shell_is_high(cmd):
    reasons = [r for r in _risks("Bash", command=cmd) if r.severity == "high"]
    assert any("straight into a shell" in r.reason for r in reasons)


def test_curl_to_file_is_not_piped_into_shell():
    assert not any("straight into a shell" in r for r in _reasons(
        _risks("Bash", command="curl -fsSL https://example.com/i.sh -o i.sh")))


@pytest.mark.parametrize("cmd", [
    'echo "run sudo apt-get install foo"',
    "printf 'sudo rm everything'",
    "# sudo apt-get update\nls -la",
])
def test_sudo_as_text_in_echo_or_comment_is_not_flagged(cmd):
    assert "Ran a command with sudo" not in _reasons(_risks("Bash", command=cmd))


@pytest.mark.parametrize("cmd", [
    "sudo apt-get update",
    "echo ok && sudo ls",
    'echo "hi" | sudo tee /etc/x',
])
def test_real_sudo_is_still_flagged(cmd):
    hits = [r for r in _risks("Bash", command=cmd) if r.reason == "Ran a command with sudo"]
    assert hits and hits[0].severity == "high"


def test_bash_shell_outside_windows_path_is_high_when_deleting():
    risks = _risks("Bash", command=r"rm -rf C:\Users\bob\docs")
    outside = [r for r in risks if r.code == "shell_outside"]
    assert outside and outside[0].severity == "high"


def test_unquoted_spaced_drive_path_is_read_as_one_path():
    """CORE-SCR-013/R5-001: a folder name with spaces is two words to a shell, so the finding
    names the word the shell reads; a receipt shows the whole path (`E:\\Work\\Client` ->
    `E:\\Work\\Client X\\secret.txt`) so safe mode can mask the folder name (VAL-CORE-034)."""
    command = r"type E:\Work\Client X\secret.txt"
    risks = _risks("Bash", command=command)
    assert _reasons(risks) == [
        r"Shell command referenced a path outside the working folder (E:\Work\Client)"]
    assert _shown("Bash", command, risks) == [
        r"Shell command referenced a path outside the working folder (E:\Work\Client X\secret.txt)"]
    quoted = r'type "E:\Work\Client X\secret.txt"'
    assert _shown("Bash", quoted, _risks("Bash", command=quoted)) == _shown("Bash", command, risks)


def test_spaced_drive_path_does_not_merge_two_paths():
    assert _reasons(_risks("Bash", command=r"copy E:\a\b.txt F:\c\d.txt")) == [
        r"Shell command referenced a path outside the working folder (E:\a\b.txt)",
        r"Shell command referenced a path outside the working folder (F:\c\d.txt)"]


def test_unquoted_spaced_drive_redirect_target_is_one_write_finding():
    """The redirect target is the shell's own word, and the receipt shows the whole path that
    word's folder name belongs to: one finding either way, no fragment and no second finding."""
    command = r"echo hi > E:\Work\Client y\z.txt"
    risks = _risks("Bash", command=command)
    assert [(r.severity, r.code, r.reason) for r in risks] == [
        ("low", "shell_outside", r"Shell command wrote outside the working folder (E:\Work\Client)")]
    assert _shown("Bash", command, risks) == [
        r"Shell command wrote outside the working folder (E:\Work\Client y\z.txt)"]


def test_spaced_drive_redirect_target_with_forward_slashes_is_one_write_finding():
    command = r"echo hi > E:/Work/Client y/z.txt"
    risks = _risks("Bash", command=command)
    outside = [r for r in risks if r.code == "shell_outside"]
    assert len(outside) == 1
    assert outside[0].reason.replace("\\", "/") == \
        "Shell command wrote outside the working folder (E:/Work/Client)"
    assert _shown("Bash", command, risks)[0].replace("\\", "/") == \
        "Shell command wrote outside the working folder (E:/Work/Client y/z.txt)"


def test_unquoted_spaced_drive_read_redirect_names_the_whole_path():
    command = r"cat < E:\Work\Client y\z.txt"
    assert _reasons(_risks("Bash", command=command)) == [
        r"Shell command referenced a path outside the working folder (E:\Work\Client)"]
    assert _shown("Bash", command, _risks("Bash", command=command)) == [
        r"Shell command referenced a path outside the working folder (E:\Work\Client y\z.txt)"]


@pytest.mark.parametrize("command", [
    r"rm C:/repo/a.txt ../outside.txt",
    r"rm C:/repo/a.txt > C:/repo/log ../outside.txt",
    r"rm < C:/repo/input ../outside.txt",
])
def test_spaced_drive_path_does_not_absorb_a_relative_operand(command):
    """CORE-SCR-R2-004: `../outside.txt` is its own argument, not the tail of the drive path
    before it, so deleting it is still a high finding (and the guard asks again)."""
    assert [(r.severity, r.code, r.reason) for r in _risks("Bash", cwd="C:/repo", command=command)] == [
        ("high", "shell_outside",
         r"Shell command referenced a path outside the working folder (../outside.txt)")]


@pytest.mark.parametrize("command", [
    r"rm C:/repo/sub/a.txt nested/../../outside.txt",
    r"rm C:/repo/sub/a.txt > C:/repo/sub/log nested/../../outside.txt",
    r"rm < C:/repo/sub/input nested/../../outside.txt",
])
def test_traversing_operand_after_a_spaced_drive_path_stays_a_high_delete(command):
    """CORE-SCR-R3-001: `nested/../../outside.txt` is an argument of its own that climbs out of
    the project, so the delete keeps a high finding naming it even when the display join reads
    the words as one spaced path (both readings are analysed, most severe wins)."""
    outside = [r for r in _risks("Bash", cwd="C:/repo", command=command)
               if r.code == "shell_outside" and r.severity == "high"]
    assert [r.reason for r in outside] == [
        r"Shell command referenced a path outside the working folder (nested/../../outside.txt)"]


@pytest.mark.parametrize("command", [
    r"rm C:\a b\x nested/../../outside.txt",
    r"rm C:\a b\x > C:\a b\log nested/../../outside.txt",
    r"rm < C:\a b\input nested/../../outside.txt",
])
def test_traversing_operand_is_named_when_the_joined_path_is_itself_outside(command):
    """CORE-SCR-R3-001: the shell reads `C:\\a b\\x` and `nested/../../outside.txt` as two
    arguments, so the escaping operand keeps a finding naming it, next to the finding for the
    fragment (`C:\\a`) that the display label shows as the whole path."""
    reasons = [r.reason for r in _risks("Bash", cwd="C:/repo", command=command)
               if r.code == "shell_outside" and r.severity == "high"]
    assert any(reason.endswith("(nested/../../outside.txt)") for reason in reasons)


def test_spaced_drive_path_with_several_words_is_still_one_path():
    """CORE-SCR-R2-005: a folder name may hold more than one extra word; the label names the
    whole path so safe mode can mask it, while the finding keeps the shell's own word."""
    command = r"type E:\Work\Client X Corp\secret.txt"
    risks = _risks("Bash", command=command)
    assert _reasons(risks) == [
        r"Shell command referenced a path outside the working folder (E:\Work\Client)"]
    assert _shown("Bash", command, risks) == [
        r"Shell command referenced a path outside the working folder (E:\Work\Client X Corp\secret.txt)"]


def test_the_spaced_path_label_shows_what_the_shell_reads_as_two_words():
    """The label step rewrites the path a receipt shows (`E:\\Work\\Client` ->
    `E:\\Work\\Client y\\z.txt`); the findings themselves are the shell's own reading, so the
    codes and severities match the same command with the path quoted (R5-001)."""
    loose_command = r"echo hi > E:\Work\Client y\z.txt"
    quoted_command = r"echo hi > 'E:\Work\Client y\z.txt'"
    loose = _risks("Bash", command=loose_command)
    quoted = _risks("Bash", command=quoted_command)
    assert [(r.severity, r.code) for r in loose] == [(r.severity, r.code) for r in quoted]
    assert _shown("Bash", loose_command, loose) == _reasons(quoted)


def test_the_spaced_path_label_never_adds_a_finding():
    """The label is a display step: the words the shell splits stay two operands, and the
    second one is checked as itself (here it resolves inside the working folder)."""
    command = r"type E:\Work\Client y\z.txt"
    risks = _risks("Bash", command=command)
    assert [(r.severity, r.code, r.reason) for r in risks] == [
        ("low", "shell_outside",
         r"Shell command referenced a path outside the working folder (E:\Work\Client)")]
    assert _shown("Bash", command, risks) == [
        r"Shell command referenced a path outside the working folder (E:\Work\Client y\z.txt)"]


# ------------------------------------------------ round 4: the shell's own words decide

def test_an_explicit_rm_executable_still_sees_the_escaping_operand():
    """CORE-SCR-R4-001: `C:/repo/bin/rm` is the executable and `sub/echo` a plain argument, so
    `../outside.txt` is a delete operand of its own: the outside delete is high again."""
    assert [(r.severity, r.code, r.reason) for r in
            _risks("Bash", cwd="C:/repo", command="C:/repo/bin/rm sub/echo ../outside.txt")] == [
        ("high", "shell_outside",
         "Shell command referenced a path outside the working folder (../outside.txt)")]


@pytest.mark.parametrize("command, codes, named", [
    ("rm C:/elsewhere/tests/test_a.py other/file.txt",
     [("high", "test_deleted"), ("high", "shell_outside")], "tests/test_a.py"),
    ("rm C:/elsewhere/key.pem other/file.txt",
     [("high", "secret_file"), ("high", "shell_outside")], "key.pem"),
])
def test_a_delete_beside_a_spaced_path_keeps_its_own_finding(command, codes, named):
    """CORE-SCR-R4-002: a finding is kept for the code and the target it names, never dropped
    because another finding for the same command is equally severe. The path the finding shows
    is the display label; the finding itself comes from the shell's own word."""
    risks = _risks("Bash", cwd="C:/repo", command=command)
    assert [(r.severity, r.code) for r in risks] == codes
    assert any(named in r.reason for r in risks if r.code == "shell_outside")


def test_a_redirect_beside_a_spaced_operand_is_still_a_migration():
    """CORE-SCR-R4-003: the real redirect target gets the write-specific checks, so writing a
    migration through the shell stays a medium db_migration."""
    assert [(r.severity, r.code, r.reason) for r in _risks(
        "Bash", cwd="C:/repo", command="echo hi > C:/repo/migrations/001_init.sql other/file.txt")] == [
        ("medium", "db_migration", "Wrote a database migration (migrations/001_init.sql)")]


def test_multiline_ansi_c_string_keeps_the_next_command_visible():
    """CORE-SCR-R2-002: a `$'...'` string may contain literal newlines, so the `rm -rf` after
    the closing quote is still a high command finding (the guard denies it)."""
    risks = _risks("Bash", cwd="C:/repo", command="echo $'first\nsecond'\nrm -rf ./build")
    assert [(r.severity, r.code, r.reason) for r in risks] == [
        ("high", "command", "Recursive force delete (rm -rf)")]


def test_git_dash_c_rm_resolves_delete_operand_against_git_working_folder():
    risks = _risks("Bash", cwd=POSIX_CWD, command="git -C /opt/other-repo rm victim.txt")
    outside = [r for r in risks if r.code == "shell_outside" and r.severity == "high"]
    assert len(outside) == 1
    assert "/opt/other-repo/victim.txt" in outside[0].reason


def test_git_dash_c_rm_inside_project_is_not_outside():
    risks = _risks("Bash", cwd=POSIX_CWD,
                   command=f"git -C {POSIX_CWD}/subdir rm victim.txt")
    assert not any(r.code == "shell_outside" for r in risks)


def test_find_delete_marks_outside_search_root_as_high():
    risks = _risks("Bash", cwd=POSIX_CWD, command="find /opt/other-tree -delete")
    outside = [r for r in risks if r.code == "shell_outside"]
    assert [(r.severity, r.code) for r in outside] == [("high", "shell_outside")]


def test_find_delete_inside_project_is_not_outside():
    assert not any(r.code == "shell_outside" for r in
                   _risks("Bash", cwd=POSIX_CWD, command="find ./build -delete"))


# ---------------------------------------------------------------- PowerShell tool

def test_powershell_remove_item_recurse_force_is_high():
    hits = [r for r in _risks("PowerShell", command=r"Remove-Item -Recurse -Force .\build")
            if r.reason == "Recursive force delete (Remove-Item -Recurse -Force)"]
    assert hits and hits[0].severity == "high"


def test_powershell_remove_item_flags_in_any_order():
    reasons = _reasons(_risks("PowerShell", command="Remove-Item -Force -Recurse build"))
    assert "Recursive force delete (Remove-Item -Recurse -Force)" in reasons


@pytest.mark.parametrize("cmd", ["Remove-Item -Recurse build", "Remove-Item build"])
def test_powershell_remove_item_without_both_flags_not_flagged(cmd):
    assert not any("Recursive force" in r for r in _reasons(_risks("PowerShell", command=cmd)))


@pytest.mark.parametrize("cmd", [
    "iwr https://example.com/i.ps1 -UseBasicParsing | iex",
    "irm https://example.com/i.ps1 | iex",
    "Invoke-RestMethod https://example.com/i.ps1 | Invoke-Expression",
])
def test_powershell_download_piped_to_iex_is_high(cmd):
    hits = [r for r in _risks("PowerShell", command=cmd)
            if r.reason == "Piped a downloaded script into PowerShell (iex)"]
    assert hits and hits[0].severity == "high"


def test_powershell_iex_on_web_request_is_high():
    reasons = [r for r in _risks("PowerShell", command="iex (irm https://example.com/x.ps1)")
               if r.severity == "high"]
    assert any("iex on a web request" in r.reason for r in reasons)


def test_powershell_download_to_file_is_not_piped_to_iex():
    reasons = _reasons(_risks("PowerShell", command="Invoke-WebRequest https://example.com/x.zip -OutFile x.zip"))
    assert not any("iex" in r for r in reasons)


def test_powershell_start_process_runas_is_high():
    hits = [r for r in _risks("PowerShell", command="Start-Process powershell -Verb RunAs")
            if "RunAs" in r.reason]
    assert hits and hits[0].severity == "high"
    assert not any("RunAs" in r for r in _reasons(_risks("PowerShell", command="Start-Process notepad.exe")))


def test_powershell_set_execution_policy_is_medium():
    hits = [r for r in _risks("PowerShell", command="Set-ExecutionPolicy Bypass -Scope Process")
            if "execution policy" in r.reason]
    assert hits and hits[0].severity == "medium"


@pytest.mark.parametrize("cmd", [
    "Write-Output $env:GITHUB_TOKEN",
    "Get-ChildItem env:",
    "echo $env:API_KEY",
])
def test_powershell_printed_secret_env_is_medium(cmd):
    hits = [r for r in _risks("PowerShell", command=cmd) if "environment" in r.reason]
    assert hits and hits[0].severity == "medium"


def test_powershell_assigning_env_var_is_not_printing():
    assert not any("environment" in r for r in _reasons(_risks("PowerShell", command='$env:GITHUB_TOKEN = "abc"')))


def test_powershell_also_gets_shared_shell_rules():
    assert "Force-pushed to a git remote" in _reasons(_risks("PowerShell", command="git push --force origin main"))


def test_existing_junit_disabled_annotation_is_not_reported_as_new_skip():
    risks = _risks("Edit", cwd=POSIX_CWD, file_path=POSIX_CWD + "/tests/FooTest.java",
                   old_string="@Disabled\nvoid testThing() { assertEquals(1, 1); }",
                   new_string="@Disabled\nvoid testThing() { assertEquals(2, 2); }")
    assert "test_skipped" not in {r.code for r in risks}


def test_new_junit_disabled_annotation_is_reported():
    risks = _risks("Edit", cwd=POSIX_CWD, file_path=POSIX_CWD + "/tests/FooTest.java",
                   old_string="void testThing() { assertEquals(1, 1); }",
                   new_string="@Disabled\nvoid testThing() { assertEquals(1, 1); }")
    assert "test_skipped" in {r.code for r in risks}


def test_powershell_windows_path_argument_is_checked_outside_cwd():
    risks = _risks("PowerShell", command=r"Remove-Item -Recurse C:\Users\bob\docs")
    outside = [r for r in risks if r.code == "shell_outside"]
    assert outside and outside[0].severity == "high"
    assert "C:\\Users\\bob\\docs" in outside[0].reason


# ---------------------------------------------------------------- hardcoded secrets in content

SECRET_SAMPLES = [
    ("Anthropic API key", "key = 'sk-ant-x1Y2z3A4b5C6d7E8f9G0h1'"),
    ("OpenAI API key", "OPENAI = 'sk-proj-a1B2c3D4e5F6g7H8i9J0k1L2'"),
    ("AWS access key", 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'),
    ("GitHub token", "gh = 'ghp_" + "a" * 36 + "'"),
    ("Slack token", "slack = 'xoxb-1234567890-abcdefghij'"),
    ("private key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAsynthetic\n-----END RSA PRIVATE KEY-----"),
    ("password", 'password = "hunter2hunter2!"'),
    ("api key", 'API_KEY: "abcd1234efgh5678"'),
]


@pytest.mark.parametrize("kind,content", SECRET_SAMPLES)
def test_write_with_hardcoded_secret_is_high(kind, content):
    hits = [r for r in _risks("Write", file_path="config.py", content=content)
            if r.code == "secret_in_content"]
    assert [r.reason for r in hits] == [f"Wrote a hardcoded {kind} into config.py"]
    assert hits[0].severity == "high"


@pytest.mark.parametrize("kind,content", SECRET_SAMPLES)
def test_secret_value_never_appears_in_reason(kind, content):
    for r in _risks("Write", file_path="config.py", content=content):
        assert "x1Y2z3A4b5C6d7E8f9G0h1" not in r.reason
        assert "hunter2" not in r.reason
        assert "AKIAIOSFODNN7EXAMPLE" not in r.reason
        assert "MIIEpAIBAAKCAQEAsynthetic" not in r.reason
        assert "abcd1234efgh5678" not in r.reason


def test_secret_in_edit_new_string_is_detected():
    hits = _risks("Edit", file_path=r"D:\runledger\config.py",
                  old_string="x = 1", new_string="AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'")
    assert "Wrote a hardcoded AWS access key into config.py" in _reasons(hits)


def test_secret_in_multiedit_edits_is_detected():
    hits = _risks("MultiEdit", file_path="config.py", edits=[
        {"old_string": "a", "new_string": "ok = True"},
        {"old_string": "b", "new_string": "gh = 'ghp_" + "b" * 36 + "'"},
    ])
    assert "Wrote a hardcoded GitHub token into config.py" in _reasons(hits)


def test_secret_in_notebook_edit_source_is_detected():
    hits = _risks("NotebookEdit", notebook_path="nb.ipynb",
                  new_source="token = 'xoxb-1234567890-abcdefghij'")
    assert "Wrote a hardcoded Slack token into nb.ipynb" in _reasons(hits)


@pytest.mark.parametrize("content", [
    'password = os.environ["PASSWORD"]',
    'token = "<your-token-here>"',
    'api_key = "${API_KEY_VALUE_HERE}"',
    'password = "short"',
    "slug = 'task-abcdefghijklmnopqrstuvwxyz'",
    "print('hello world')",
])
def test_placeholders_short_values_and_lookalikes_are_not_flagged(content):
    assert "secret_in_content" not in {r.code for r in _risks("Write", file_path="a.py", content=content)}


def test_secret_rule_only_applies_to_written_content():
    assert "secret_in_content" not in {r.code for r in _risks(
        "Bash", command="echo AKIAIOSFODNN7EXAMPLE")}


def test_windows_secret_file_path_is_detected():
    reasons = _reasons(_risks("Read", file_path=r"C:\Users\bob\.ssh\id_ed25519"))
    assert "Read a secrets file (id_ed25519)" in reasons


def test_windows_env_example_is_not_a_secret():
    assert "secret_file" not in {r.code for r in _risks("Read", file_path=r"D:\runledger\.env.example")}


# ---------------------------------------------------------------- .runledger.json policy

def _write_policy(tmp_path, data, raw=None, bom=False):
    body = raw if raw is not None else json.dumps(data)
    payload = body.encode("utf-8")
    if bom:
        payload = b"\xef\xbb\xbf" + payload
    (tmp_path / ".runledger.json").write_bytes(payload)


DEFAULTS = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": []}


def test_load_policy_missing_file_gives_defaults(tmp_path):
    assert load_policy(str(tmp_path)) == DEFAULTS


def test_load_policy_without_cwd_gives_defaults():
    assert load_policy(None) == DEFAULTS


@pytest.mark.parametrize("raw", ["{not json", "[1, 2, 3]", '"just a string"', ""])
def test_load_policy_invalid_or_non_object_gives_defaults(tmp_path, raw):
    _write_policy(tmp_path, None, raw=raw)
    assert load_policy(str(tmp_path)) == DEFAULTS


def test_load_policy_drops_invalid_entries(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user_config.json"
    user_cfg.write_text(json.dumps({
        "ignore": ["command", 5, "", "   "],
        "severity_overrides": {"command": "critical", "mcp": "LOW", "rm": 3},
        "extra_secret_paths": ["(", r"\.vault$"],
    }), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(user_cfg))
    policy = load_policy(str(tmp_path))
    assert policy["ignore"] == ["command"]
    assert policy["severity_overrides"] == {"mcp": "low"}
    assert policy["extra_secret_paths"] == [r"\.vault$"]


def test_load_policy_accepts_utf8_bom(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user_config.json"
    user_cfg.write_bytes(json.dumps({"ignore": ["mcp"]}).encode("utf-8-sig"))
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(user_cfg))
    assert load_policy(str(tmp_path))["ignore"] == ["mcp"]


def test_policy_ignore_removes_matching_risks(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user_config.json"
    user_cfg.write_text(json.dumps({"ignore": ["mcp"]}), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(user_cfg))
    steps = [_step("mcp__github__create_issue", 1, title="x"), _step("Bash", 2, command="sudo ls")]
    _, _, risks = assess(_run(steps, str(tmp_path)))
    codes = {r.code for r in risks}
    assert "mcp" not in codes
    assert "command" in codes


def test_policy_ignore_matches_reason_text_case_insensitively():
    policy = {"ignore": ["READ OUTSIDE"], "severity_overrides": {}, "extra_secret_paths": []}
    risks = assess_step(_step("Read", file_path="/etc/hosts"), POSIX_CWD, policy)
    assert "read_outside" not in {r.code for r in risks}


def test_policy_severity_override_changes_level_and_score(tmp_path, monkeypatch):
    steps = [_step("Bash", 1, command="sudo ls")]
    base_score, _, _ = assess(_run(steps, str(tmp_path)))
    user_cfg = tmp_path / "user_config.json"
    user_cfg.write_text(json.dumps({"severity_overrides": {"command": "low"}}), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(user_cfg))
    score, _, risks = assess(_run(steps, str(tmp_path)))
    assert [(r.severity, r.reason) for r in risks] == [("low", "Ran a command with sudo")]
    assert base_score == 30 and score == 5


def test_policy_extra_secret_paths_are_secret_files():
    policy = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": [r"\.vault$"]}
    hits = assess_step(_step("Read", file_path="config/app.vault"), POSIX_CWD, policy)
    assert "secret_file" in {r.code for r in hits}
    misses = assess_step(_step("Read", file_path="config/app.txt"), POSIX_CWD, policy)
    assert "secret_file" not in {r.code for r in misses}


def test_invalid_policy_file_never_breaks_assess(tmp_path):
    _write_policy(tmp_path, None, raw="{broken")
    score, level, risks = assess(_run([_step("Bash", 1, command="sudo ls")], str(tmp_path)))
    assert isinstance(score, int) and level in ("Low", "Medium", "High")
    assert [r.severity for r in risks] == ["high"]


# ---------------------------------------------------------------- Git Bash / MSYS, devices, switches

@pytest.mark.parametrize("path", ["/d/runledger/src/a.py", "/mnt/d/runledger/a.py", "/cygdrive/d/runledger/a.py"])
def test_msys_drive_paths_inside_windows_cwd_are_not_outside(path):
    assert _outside(path, WIN_CWD) is False


@pytest.mark.parametrize("path", ["/c/Users/claud/.claude/CLAUDE.md", "/mnt/c/Users/x.txt", "/d/runledger2/x.py"])
def test_msys_drive_paths_outside_windows_cwd_are_outside(path):
    assert _outside(path, WIN_CWD) is True


@pytest.mark.parametrize("path", ["/dev/null", "/dev/stdout", "/dev/stderr", "NUL", "nul", "/proc/self/status"])
def test_device_paths_are_never_outside(path):
    assert _outside(path, WIN_CWD) is False
    assert _outside(path, POSIX_CWD) is False


def test_windows_switches_are_not_paths():
    assert _reasons(_risks("Bash", command="cmd /c dir /S /Q")) == []
    assert _reasons(_risks("Bash", command="robocopy D:\\runledger\\a D:\\runledger\\b /MIR /MT:8")) == []


def test_url_and_sed_expression_are_not_paths():
    assert _reasons(_risks("Bash", command="curl -o out.txt https://example.com/a/b/c")) == []
    assert _reasons(_risks("Bash", command="sed -i 's#/old/path#/new/path#g' file.txt")) == []


def test_option_value_path_after_equals_is_checked():
    risks = _risks("Bash", command="python -m runledger push x.jsonl --db=/c/Users/bob/keep.db")
    assert [(r.severity, r.code) for r in risks] == [("low", "shell_outside")]


def test_heredoc_body_and_echo_text_are_not_analysed():
    assert _reasons(_risks("Bash", command="cat <<'EOF' > notes.txt\nrm -rf /\n.env\nEOF")) == []
    assert _reasons(_risks("Bash", command='echo "cat .env && rm tests/test_x.py"')) == []


WS_CWD = "/workspace/proj"


@pytest.mark.parametrize("cmd", [
    "rg '/workspace|/home' src",
    "rg -n --hidden '/workspace/[a-z]+|/home/[^/]+' .",
    'grep -rnE "/home/[a-z]+|/workspace/" .',
    "egrep '/opt|/srv' notes.txt",
    "grep -C 3 /home/dev src",
    "grep -rl -- '/home/dev' .",
    "grep -r --include='*.py' '/usr/lib' .",
    "grep -e /home/dev -e /workspace/other src",
    "rg -e /etc/hosts src",
    "rg --regexp=/home/dev .",
    "rg -g '!/home/**' needle",
    "ag '/opt/x|/workspace/y' .",
    "git grep -e '/home/dev' -- src",
    "git grep -nE '/home|/workspace' -- src",
    "cat log.txt | grep '/var/log|/tmp'",
    "find . -name '*.py' | xargs grep -l '/home|/opt'",
    "sed -n 's#/home/dev#/tmp#p' file.txt",
    "sed -e '/\\/home\\//d' file.txt",
    "awk '/\\/home\\//{print}' log.txt",
    "awk -F/ -v root=/home '{print $2}' paths.txt",
    "perl -pe 's|/home/a|/b|' f.txt",
    "find . -path '/home/*'",
    "Select-String -Pattern '/home|/workspace' -Path notes.txt",
])
def test_search_patterns_are_not_paths(cmd):
    assert not any(r.code == "shell_outside" for r in _risks("Bash", cwd=WS_CWD, command=cmd))


@pytest.mark.parametrize("cmd, shown", [
    ("cat /etc/passwd", "/etc/passwd"),
    ("cp x /outside", "/outside"),
    ("grep root /etc/passwd", "/etc/passwd"),
    ("rg -n '/home|/workspace' /etc", "/etc"),
    ("grep -e /home/dev /etc/hosts", "/etc/hosts"),
    ("grep -f /etc/patterns src", "/etc/patterns"),
    ("git grep -e foo -- /opt/other", "/opt/other"),
    ("sed -e 's/a/b/' /etc/hosts", "/etc/hosts"),
    ("awk -F: '{print $1}' /etc/passwd", "/etc/passwd"),
    ("awk -f /opt/prog.awk in.txt", "/opt/prog.awk"),
])
def test_files_in_path_positions_of_search_commands_are_still_outside(cmd, shown):
    outside = [r for r in _risks("Bash", cwd=WS_CWD, command=cmd) if r.code == "shell_outside"]
    assert outside and shown in outside[0].reason


def test_grep_pattern_text_is_not_a_dangerous_command():
    assert _reasons(_risks("Bash", cwd=WS_CWD, command="grep -rn 'rm -rf /' .")) == []


def test_awk_script_is_still_read_by_dangerous_command_rules():
    risks = _risks("Bash", cwd=WS_CWD, command="awk 'BEGIN{system(\"sudo reboot\")}'")
    assert "Ran a command with sudo" in _reasons(risks)


def test_env_var_in_temp_is_not_outside(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    assert _reasons(_risks("Bash", command='rm -f "$TEMP/e2e.db"')) == []


# ---------------------------------------------------------------- real Windows session commands

CASES_FILE = Path(__file__).parent / "fixtures" / "real_windows_commands.txt"


def _load_cases():
    cases = {}
    text = CASES_FILE.read_text(encoding="utf-8")
    for block in re.split(r"^---[ \t]*$", text, flags=re.M):
        lines = block.strip("\n").split("\n")
        name, tool = None, "Bash"
        while lines and re.match(r"^#\s*(case|tool):", lines[0]):
            key, value = re.match(r"^#\s*(case|tool):\s*(.+)$", lines[0]).groups()
            if key == "case":
                name = value.strip()
            else:
                tool = value.strip()
            lines.pop(0)
        if name:
            cases[name] = (tool, "\n".join(lines).strip("\n"))
    return cases


# (severity, code) of every finding, sorted. Each expectation was checked by hand against the
# real command in tests/fixtures/real_windows_commands.txt.
EXPECTED = {
    "cd_inside_cwd": [],
    "heredoc_claude_md": [("low", "shell_outside")],
    "taskkill_image_name": [("medium", "command")],
    "taskkill_by_pid": [],
    "env_var_temp_db": [],
    "rm_test_source": [("high", "test_deleted")],
    "rm_test_fixture": [("low", "test_deleted")],
    "rm_fixture_in_compound_with_push": [("low", "test_deleted")],
    "git_commit_text_mentions_rm": [],
    "git_commit_heredoc_message": [],
    "rm_rf_root": [("high", "command")],
    "curl_pipe_sh": [("high", "command")],
    "sudo_real": [("high", "command")],
    "sudo_in_echo_is_text": [],
    "mnt_drive_outside": [("low", "shell_outside")],
    "cygdrive_inside": [],
    "devices_are_not_paths": [],
    "msys_option_value_in_temp": [],
    "python_c_text_is_not_code": [],
    "python_heredoc_body_is_not_code": [],
    "echo_printed_token": [("medium", "command")],
    "cat_env_file": [("high", "secret_file")],
    "force_push": [("high", "command"), ("medium", "command")],
    "kill_and_rm_with_unknown_vars": [],
    "rm_rf_temp_dir": [("high", "command")],
    "cd_switch_and_clone": [],
    "robocopy_switches": [("low", "shell_outside")],
    "sed_expression": [],
    "ps_remove_recurse_force": [("high", "command")],
    "ps_remove_recurse_outside": [("high", "shell_outside")],
    "ps_printed_env_secret": [("medium", "command")],
    "ps_download_to_file": [],
    "ps_remove_test_file": [("high", "test_deleted")],
}
CASES = _load_cases()


def test_every_fixture_case_has_an_expectation():
    assert set(CASES) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_windows_command_findings(name):
    tool, command = CASES[name]
    outcome = sorted((r.severity, r.code) for r in _risks(tool, command=command))
    assert outcome == EXPECTED[name]


def test_taskkill_by_image_name_reason_is_explicit():
    hits = _risks("Bash", command="taskkill //F //IM python.exe")
    assert [r.reason for r in hits] == ["Killed all processes by image name (taskkill /IM)"]


def test_heredoc_claude_md_reason_names_the_file():
    hits = _risks("Bash", command="cat >> /c/Users/claud/.claude/CLAUDE.md <<'EOF'\nrm -rf /\nEOF")
    assert [r.reason for r in hits] == [
        "Shell command wrote outside the working folder (/c/Users/claud/.claude/CLAUDE.md)"]


# ---------------------------------------------------------------- Delete tool

@pytest.mark.parametrize("path, expected", [
    ("tests/test_payments.py", [("high", "test_deleted")]),
    ("tests/old.spec.ts", [("high", "test_deleted")]),
    ("tests/fixtures/data.json", [("low", "test_deleted")]),
    ("src/app.py", []),
    (".env", [("high", "secret_file")]),
    (r"E:\data\x.txt", [("high", "write_outside")]),
    (r"D:\runledger\src\x.py", []),
])
def test_delete_tool_is_checked_like_rm(path, expected):
    assert sorted((r.severity, r.code) for r in _risks("Delete", file_path=path)) == expected


def test_delete_tool_reason_names_the_test_file():
    hits = _risks("Delete", file_path="tests/test_payments.py")
    assert _reasons(hits) == ["Deleted a test file (test_payments.py)"]


# ---------------------------------------------------------------- .git folder

@pytest.mark.parametrize("tool, inp", [
    ("Write", {"file_path": ".git/config", "content": "x"}),
    ("Edit", {"file_path": r"D:\runledger\.git\hooks\pre-commit", "old_string": "a", "new_string": "b"}),
    ("MultiEdit", {"file_path": ".git/HEAD", "edits": [{"old_string": "a", "new_string": "b"}]}),
    ("Delete", {"file_path": ".git/index"}),
])
def test_writes_inside_git_folder_are_medium(tool, inp):
    hits = [(r.severity, r.code) for r in _risks(tool, **inp)]
    assert ("medium", "git_internals") in hits


@pytest.mark.parametrize("tool, inp", [
    ("Write", {"file_path": ".github/workflows/ci.yml", "content": "x"}),
    ("Write", {"file_path": ".gitignore", "content": "x"}),
    ("Read", {"file_path": ".git/config"}),
])
def test_git_folder_rule_is_for_writes_only(tool, inp):
    assert "git_internals" not in {r.code for r in _risks(tool, **inp)}


# ---------------------------------------------------------------- workdir

def test_workdir_outside_working_folder_is_medium():
    hits = _risks("Bash", command="ls", workdir=r"E:\other")
    assert [(r.severity, r.code, r.reason) for r in hits] == [
        ("medium", "shell_outside", "Ran a command outside the working folder (E:\\other)")]


def test_workdir_inside_working_folder_is_quiet():
    assert _reasons(_risks("Bash", command="cat x/y.md", workdir=r"D:\runledger\sub")) == []


def test_relative_argument_resolves_against_workdir():
    hits = _risks("Bash", command="rm tests/test_payments.py", workdir=r"E:\proj")
    codes = sorted((r.severity, r.code) for r in hits)
    assert ("high", "test_deleted") in codes and ("high", "shell_outside") in codes
    outside = [r for r in hits if r.reason.startswith("Shell command referenced")]
    assert [r.severity for r in outside] == ["high"]
    assert r"E:\proj\tests\test_payments.py" in outside[0].reason


def test_relative_read_resolves_against_workdir():
    hits = [r for r in _risks("Bash", command="cat notes/readme.md", workdir=r"E:\proj")
            if r.reason.startswith("Shell command referenced")]
    assert [r.severity for r in hits] == ["low"]


def test_relative_path_without_workdir_is_inside_cwd():
    assert _reasons(_risks("Bash", command="cat notes/readme.md")) == []


def test_relative_workdir_is_ignored():
    assert _reasons(_risks("Bash", command="cat notes/readme.md", workdir="sub")) == []


# ---------------------------------------------------------------- scoring caps

def _steps(tool, n, **inp):
    return [_step(tool, index=i + 1, **inp) for i in range(n)]


def test_low_findings_cap_at_ten_points(tmp_path):
    steps = [_step(f"mcp__github__call_{i}", i + 1) for i in range(50)]
    score, _, _ = assess(_run(steps, str(tmp_path)))
    assert score == 10


def test_medium_findings_cap_at_forty_five_points(tmp_path):
    score, _, _ = assess(_run(_steps("Bash", 30, command="git push origin main"), str(tmp_path)))
    assert score == 45


def test_high_findings_are_not_capped_below_one_hundred(tmp_path):
    score, _, _ = assess(_run(_steps("Bash", 4, command="sudo ls"), str(tmp_path)))
    assert score == 60  # 30 for the first sudo, then 10 for each repeat


# ---------------------------------------------------------------- secret paths, ReDoS validator & trust model (VAL-CORE-014, 016..018, 024..026)

@pytest.mark.parametrize("secret_path", [
    "main.tfstate",
    "terraform.tfstate.backup",
    "prod.tfvars",
    "keystore.jks",
    "app.keystore",
    "AuthKey_ABC1234567.p8",
    "cert.p12",
    "cert.pfx",
    ".htpasswd",
    ".vault-token",
    ".config/gh/hosts.yml",
    ".azure/accessTokens.json",
    ".git/config",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    ".envrc",
    "/app/.envrc",
    "~/.ssh/config",
    "~/.ssh/authorized_keys",
    "~/.ssh/deploy_ed25519",
    "credentials",
    "config/credentials",
    "~/.aws/credentials",
])
def test_extended_secret_paths_are_detected(secret_path):
    from runledger import secrets

    assert secrets.is_secret_path(secret_path) is True
    hits = assess_step(_step("Read", file_path=secret_path), POSIX_CWD)
    assert any(r.code == "secret_file" and r.severity == "high" for r in hits)


@pytest.mark.parametrize("public_path", [
    "~/.ssh/id_rsa.pub",
    "id_ed25519.pub",
    "deploy_key.pub",
    "src/public.py",
    "src/credentials.py",
    "src/credentials/utils.py",
    "docs/secrets.md",
    "README.md",
    "package.json",
])
def test_public_key_and_normal_paths_are_not_secret(public_path):
    from runledger import secrets

    assert secrets.is_secret_path(public_path) is False
    hits = assess_step(_step("Read", file_path=public_path), POSIX_CWD)
    assert not any(r.code == "secret_file" for r in hits)


@pytest.mark.parametrize("bad_pattern", [
    "(a+)+$",
    "(a|aa)+$",
    "(?>(a+)+$)",
    "(?>(a|aa)+$)",
    "((?>a+))+$",
    "((?>a|aa))+$",
    "(a++)+",
    "(a+)*+",
    "(a*)*",
    "(x+x+)+y",
    r"(\w+\.)+com",
    "(a+){2,}",
    r"(a+)\1",
    "a" * 201,
])
def test_safe_user_regex_rejects_redos_and_overlong(bad_pattern):
    from runledger import secrets

    assert secrets.safe_user_regex(bad_pattern) is None


@pytest.mark.parametrize("good_pattern,matching_sample,non_matching_sample", [
    (r"\.vault$", "config/app.vault", "config/app.txt"),
    (r"(^|/)vault\.txt$", "dir/vault.txt", "dir/other.txt"),
    (r"(^|/)config/prod\.ya?ml$", "config/prod.yaml", "config/dev.yaml"),
    (r"^(?:src|lib)/secrets/.*$", "src/secrets/key.json", "test/secrets/key.json"),
    (r"[a-z]+\d*\.pem$", "cert12.pem", "cert12.txt"),
    ("glob:*.SecretConfig", "app.SecretConfig", "app.Config"),
])
def test_safe_user_regex_accepts_valid_and_glob_patterns(good_pattern, matching_sample, non_matching_sample):
    from runledger import secrets

    compiled = secrets.safe_user_regex(good_pattern)
    assert compiled is not None
    assert compiled.search(matching_sample) is not None
    assert compiled.search(non_matching_sample) is None


def test_extra_secret_paths_truncates_long_path_and_caps_count(tmp_path):
    from runledger import secrets

    long_path = "a/" * 5000 + "file.vault"
    assert len(long_path) > 10000
    assert secrets.is_secret_path(long_path, extra=[r"\.vault$"]) is False
    short_path = "a/" * 40 + "file.vault"
    assert len(short_path) < 200
    assert secrets.is_secret_path(short_path, extra=[r"\.vault$"]) is True

    _write_policy(tmp_path, {
        "extra_secret_paths": [f"pat{i}\\.secret$" for i in range(30)],
    })
    policy = load_policy(str(tmp_path))
    assert len(policy["extra_secret_paths"]) == 20


def test_project_config_cannot_ignore_or_lower_risks_and_emits_note(tmp_path):
    from runledger.receipt import render

    _write_policy(tmp_path, {
        "ignore": ["destructive_delete", "git_force_push", "secret_file"],
        "severity_overrides": {"destructive_delete": "low", "secret_file": "low"},
    })
    steps = [
        _step("Bash", 1, command="rm -rf dist"),
        _step("Bash", 2, command="git push --force"),
        _step("Read", 3, file_path=".env"),
    ]
    run = _run(steps, str(tmp_path))
    score, level, risks = assess(run)
    assert score == 100 and level == "High"
    high_reasons = [r.reason for r in risks if r.severity == "high"]
    assert len(high_reasons) == 3
    assert any("rm" in r or "deletion" in r for r in high_reasons)
    assert any("Force-pushed" in r for r in high_reasons)
    assert any(".env" in r for r in high_reasons)

    expected_note = "Project config tried to ignore/lower 3 risk kind(s) (not applied; set in user config to enable)."
    assert expected_note in run.notes
    for fmt in ("md", "html", "json"):
        rendered = render(run, score, level, risks, fmt)
        assert expected_note in rendered


def test_project_config_can_raise_severities_and_add_extra_secret_paths(tmp_path):
    _write_policy(tmp_path, {
        "severity_overrides": {"git_push": "high", "mcp": "medium"},
        "extra_secret_paths": [r"\.vault$"],
    })
    steps = [
        _step("mcp__github__create_issue", 1, title="x"),
        _step("Bash", 2, command="git push origin main"),
        _step("Read", 3, file_path="secrets.vault"),
    ]
    run = _run(steps, str(tmp_path))
    _, _, risks = assess(run)
    by_step = {r.step: r for r in risks}
    assert by_step[1].code == "mcp" and by_step[1].severity == "medium"
    assert by_step[2].severity == "high"
    assert by_step[3].code == "secret_file" and by_step[3].severity == "high"
    assert run.notes == []


def test_project_severity_override_cannot_lower_a_high_finding(tmp_path):
    # `rm` of a path outside the folder is a high shell_outside; the project's medium is refused.
    _write_policy(tmp_path, {"severity_overrides": {"shell_outside": "medium"}})
    run = _run([_step("Bash", 1, command=r"rm C:\other\important.txt")], str(tmp_path))
    _, _, risks = assess(run)
    assert [(r.severity, r.code) for r in risks] == [("high", "shell_outside")]
    assert run.notes == [
        "Project config tried to ignore/lower 1 risk kind(s) (not applied; set in user config to enable)."
    ]


def test_project_severity_override_still_raises_low_findings(tmp_path):
    _write_policy(tmp_path, {"severity_overrides": {"shell_outside": "high"}})
    run = _run([_step("Bash", 1, command=r"echo x > C:\Windows\win.ini")], str(tmp_path))
    _, _, risks = assess(run)
    assert risks and {r.code for r in risks} == {"shell_outside"}
    assert {r.severity for r in risks} == {"high"}
    assert run.notes == []


def test_user_severity_override_outranks_the_project(tmp_path, monkeypatch):
    _write_policy(tmp_path, {"severity_overrides": {"shell_outside": "high"}})
    user_cfg = tmp_path / "user_cfg.json"
    user_cfg.write_text(json.dumps({"severity_overrides": {"shell_outside": "low"}}), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(user_cfg))
    run = _run([_step("Bash", 1, command=r"rm C:\other\important.txt")], str(tmp_path))
    _, _, risks = assess(run)
    assert [(r.severity, r.code) for r in risks] == [("low", "shell_outside")]
    assert run.notes == []


def test_user_config_can_ignore_and_lower_severities(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user_cfg.json"
    user_cfg.write_text(json.dumps({
        "ignore": ["network"],
        "severity_overrides": {"outside_repo": "low"},
    }), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(user_cfg))
    steps = [
        _step("WebFetch", 1, url="https://example.com"),
        _step("Bash", 2, command="curl https://example.com"),
        _step("Write", 3, file_path="/etc/hosts", content="127.0.0.1 localhost"),
    ]
    run = _run(steps, str(tmp_path))
    _, _, risks = assess(run)
    assert not any(r.code == "network" or "Network call" in r.reason for r in risks)
    outside = [r for r in risks if r.code in ("outside_workspace", "write_outside", "outside_repo")]
    assert outside and all(r.severity == "low" for r in outside)
    assert run.notes == []


# ---------------------------------------------------------------- UNC, \\?\, symlinks/junctions (VAL-CORE-027..031)

def test_unc_paths_in_risk_and_guard():
    from runledger import guard

    cwd = r"\\srv\share\proj"
    for inside_path in (r"\\srv\share\proj\a.py", "//srv/share/proj/a.py", r"\\SRV\Share\Proj\b.py"):
        risks = _risks("Write", cwd=cwd, file_path=inside_path, content="x")
        assert "write_outside" not in {r.code for r in risks}
        ev = {"session_id": "s1", "cwd": cwd, "hook_event_name": "PreToolUse",
              "tool_name": "Write", "tool_input": {"file_path": inside_path, "content": "x"}}
        assert guard.decide(ev, {})["decision"] == "allow"

    for outside_path in (r"\\srv\share2\x.py", r"\\srv\share\other\x.py"):
        risks = _risks("Write", cwd=cwd, file_path=outside_path, content="x")
        assert any(r.code == "write_outside" and r.severity == "high" for r in risks)
        ev = {"session_id": "s1", "cwd": cwd, "hook_event_name": "PreToolUse",
              "tool_name": "Write", "tool_input": {"file_path": outside_path, "content": "x"}}
        assert guard.decide(ev, {})["decision"] == "ask"


def test_long_path_prefix_in_risk():
    cwd = r"C:\proj"
    assert any(r.code == "write_outside" for r in _risks("Write", cwd=cwd, file_path=r"\\?\C:\Windows\x.txt", content="x"))
    assert not any(r.code == "write_outside" for r in _risks("Write", cwd=cwd, file_path=r"\\?\C:\proj\a.py", content="x"))
    assert any(r.code == "write_outside" for r in _risks("Write", cwd=r"\\srv\share\proj", file_path=r"\\?\UNC\srv\share\x", content="x"))
    assert any(r.code == "write_outside" for r in _risks("Write", cwd=cwd, file_path=r"C:\proj\..\Windows\x", content="x"))
    assert not any(r.code == "write_outside" for r in _risks("Write", cwd=cwd, file_path=r"\\.\PhysicalDrive0", content="x"))
    assert not any(r.code == "write_outside" for r in _risks("Write", cwd=cwd, file_path=r"\\?\GLOBALROOT\Device\HarddiskVolume1", content="x"))


def test_symlink_and_junction_escapes_are_flagged(tmp_path):
    import subprocess
    from runledger import guard

    proj = tmp_path / "proj"
    proj.mkdir()
    sub = proj / "sub"
    sub.mkdir()
    outside_dir = Path(r"C:\Windows\Temp") if os.name == "nt" and Path(r"C:\Windows\Temp").exists() else (tmp_path / "outside")
    if not outside_dir.exists():
        outside_dir.mkdir()

    escape_link = proj / "link"
    in_link = proj / "inlink"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(escape_link), str(outside_dir)], check=True, capture_output=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(in_link), str(sub)], check=True, capture_output=True)
    else:
        os.symlink(str(outside_dir), str(escape_link))
        os.symlink(str(sub), str(in_link))

    try:
        evil_file = str(escape_link / "evil.txt")
        risks = _risks("Write", cwd=str(proj), file_path=evil_file, content="x")
        assert any(r.code == "write_outside" and r.severity == "high" for r in risks)

        ev = {"session_id": "s1", "cwd": str(proj), "hook_event_name": "PreToolUse",
              "tool_name": "Write", "tool_input": {"file_path": evil_file, "content": "x"}}
        assert guard.decide(ev, {})["decision"] == "ask"

        bash_risks = _risks("Bash", cwd=str(proj), command="echo hi > link/evil.txt")
        assert any(r.code in ("shell_outside", "write_outside") for r in bash_risks)

        ok_file = str(in_link / "ok.txt")
        ok_risks = _risks("Write", cwd=str(proj), file_path=ok_file, content="x")
        assert not any(r.code in ("write_outside", "shell_outside") for r in ok_risks)
    finally:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "rmdir", str(escape_link)], check=False, capture_output=True)
            subprocess.run(["cmd", "/c", "rmdir", str(in_link)], check=False, capture_output=True)


def test_foreign_host_paths_use_lexical_comparison():
    cwd = "/home/dev/proj"
    assert not any(r.code == "write_outside" for r in _risks("Write", cwd=cwd, file_path="/home/dev/proj/src/a.py", content="x"))
    assert any(r.code == "write_outside" for r in _risks("Write", cwd=cwd, file_path="/etc/passwd", content="x"))


def test_unreachable_unc_share_never_hangs_realpath():
    import time
    from runledger import guard

    cwd = r"\\192.0.2.1\share\proj"
    t0 = time.monotonic()
    in_risks = assess_step(_step("Write", file_path=r"\\192.0.2.1\share\proj\a.py", content="x"), cwd=cwd)
    out_risks = assess_step(_step("Write", file_path=r"\\192.0.2.1\share\other\a.py", content="x"), cwd=cwd)
    dec = guard.decide({"session_id": "s1", "cwd": cwd, "hook_event_name": "PreToolUse",
                        "tool_name": "Write", "tool_input": {"file_path": r"\\192.0.2.1\share\other\a.py", "content": "x"}})
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0
    assert not any(r.code == "write_outside" for r in in_risks)
    assert any(r.code == "write_outside" for r in out_risks)
    assert dec["decision"] == "ask"
    assert "write_outside" in dec["codes"]


