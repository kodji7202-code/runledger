"""Spaced drive path labels are presentation only (CORE-SCR-R5-001..003).

`E:\\Work\\Client X\\secret.txt` is one path to a reader and two words to a shell. Every
decision RunLedger takes - findings and their de-duplication, severities, user policies and
ignore rules, guard rules and decisions, containment, file keys, deletion state, counts and Git
lookups - reads the shell's own words. The label that shows the whole path is applied when
output is rendered (`runledger.labels`), never before it."""
import contextlib
import json
from pathlib import Path

import pytest

from runledger import cli, guard, paths, risk, summarize
from runledger.labels import command_labels, show_risks, step_label_map
from runledger.parser import Run, Step, Usage

REPO = "C:/repo"
NO_POLICY = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": []}
IGNORE_Y = {"ignore": ["y/z.txt"], "severity_overrides": {}, "extra_secret_paths": []}

# Every reproducer the round 2-5 reviews reported, plus spaced and quoted drive paths in both
# shells. The third field says whether the unquoted spaced path rule fires for the command.
CORPUS = [
    ("Bash", r"cat E:/Work/Client y/z.txt; rm 'E:/Work/Client y/z.txt'", True),   # R5-001
    ("Bash", r"rm E:/Work/Client y/z.txt 'E:/Work/Client y/z.txt'", True),        # R5-001
    ("Bash", r"rm E:/Work/Client y/z.txt", True),                                 # R5-002
    ("Bash", r"type E:\Work\Client X\secret.txt", True),
    ("Bash", r"type E:\Work\Client X Corp\secret.txt", True),                     # R2-005
    ("Bash", r'cat "C:\Users\John  Smith\Documents\x.txt"', False),               # R2-006
    ("Bash", r'type "E:\Work\Client X\secret.txt"', False),
    ("Bash", r"echo hi > E:\Work\Client y\z.txt", True),
    ("Bash", r"echo hi > 'E:\Work\Client y\z.txt'", False),
    ("Bash", r"rm C:/repo/a.txt ../outside.txt", False),                          # R2-004
    ("Bash", r"rm C:/repo/a.txt > C:/repo/log ../outside.txt", False),
    ("Bash", r"rm C:/repo/a.txt < C:/repo/input ../outside.txt", False),
    ("Bash", r"rm C:/repo/sub/a.txt nested/../../outside.txt", True),             # R3-001
    ("Bash", r"rm C:/repo/sub/a.txt > C:/repo/sub/log nested/../../outside.txt", True),
    ("Bash", r"rm < C:/repo/sub/input nested/../../outside.txt", True),
    ("Bash", r"C:/repo/bin/rm sub/echo ../outside.txt", True),                    # R4-001
    ("Bash", r"rm C:/elsewhere/tests/test_a.py other/file.txt", True),            # R4-002
    ("Bash", r"rm C:/elsewhere/key.pem other/file.txt", True),                    # R4-002
    ("Bash", r"echo hi > C:/repo/migrations/001_init.sql other/file.txt", True),  # R4-003
    ("Bash", "echo <<EOF $'first\nsecond'\npayload\nEOF\nrm -rf ./build", False),  # R3-002
    ("Bash", "echo $'first\nsecond'\nrm -rf ./build", False),                     # R2-002
    ("PowerShell", r"Set-Content -Path E:\Work\Client y\z.txt -Value x", True),
    ("PowerShell", r"Remove-Item 'E:\Work\Client y\z.txt'", False),
    ("PowerShell", r"Remove-Item E:\Work\Client y\z.txt -Recurse -Force", True),
    ("PowerShell", r"Set-Content -Path C:\repo\My Folder\x.txt -Value x", True),
]


def _step(tool, command, index=1):
    return Step(index=index, tool=tool, input={"command": command}, tool_use_id="t%d" % index,
                model=None, timestamp=None)


def _run(steps, cwd=REPO):
    return Run(session_id="s1", path="s1.jsonl", cwd=cwd, git_branch=None, started=None,
               ended=None, prompts=[], steps=steps, final_message="", usage=Usage(), models={})


def _findings(risks):
    return [(r.severity, r.code, r.reason) for r in risks]


def _event(cwd, tool, command):
    return {"session_id": "s1", "cwd": cwd, "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"command": command}}


def _observe(commands, cwd=REPO):
    """Everything the label pass must never be able to change."""
    steps = [_step(tool, command, i + 1) for i, (tool, command) in enumerate(commands)]
    run = _run(steps, cwd)
    score, level, risks = risk.assess(run)
    changes = summarize.file_changes(run)
    ignored = [_findings(risk.assess_step(_step(tool, command), cwd, IGNORE_Y))
               for tool, command in commands]
    decision = guard.decide(_event(cwd, commands[0][0], commands[0][1]), NO_POLICY)
    return {
        "score": score, "level": level,
        "findings": _findings(risks),
        "steps": [_findings(s.risks) for s in steps],
        "files": sorted((p, c.deleted, c.shell, c.created) for p, c in changes.items()),
        "counts": len(changes),
        "ignored": ignored,
        # The decision and the findings it was taken from; the message text it prints is
        # rendered output and shows the whole path on purpose (see the rendering tests below).
        "guard": (decision["decision"], decision["policy_decision"], sorted(decision["codes"]),
                  sorted((r["severity"], r["code"]) for r in decision["risks"])),
    }


def _no_labels(monkeypatch):
    monkeypatch.setattr(paths, "spaced_drive_labels", lambda text: {})


def _write_user_config(home, data):
    folder = Path(home) / ".runledger"
    folder.mkdir(exist_ok=True)
    (folder / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _session(tmp_path, steps, cwd=None, prompts=()):
    path = tmp_path / "s.runledger.json"
    path.write_text(json.dumps({
        "runledger_format": 1,
        "agent": "demo",
        "session_id": "label-demo",
        "cwd": cwd if cwd is not None else str(tmp_path),
        "prompts": list(prompts),
        "steps": steps,
    }), encoding="utf-8")
    return path


def _receipt(capsys, *args):
    rc = cli.main(["receipt", *args])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    return captured.out, captured.err


# ---------------------------------------------------------------- the invariance corpus

@pytest.mark.parametrize("tool, command, label_fires", CORPUS)
def test_decisions_do_not_depend_on_the_label_pass(monkeypatch, tool, command, label_fires):
    """R5-001..003: findings, guard decisions, user-policy results, file keys, deletion state
    and counts are identical whether the whole-path label rule is active or disabled."""
    shown = _observe([(tool, command)])
    assert bool(command_labels(tool, command)) is label_fires
    with monkeypatch.context() as m:
        _no_labels(m)
        plain = _observe([(tool, command)])
    assert plain == shown


@pytest.mark.parametrize("commands", [
    [("Bash", r"rm C:/repo/My Folder/x.txt"), ("Bash", r"touch 'C:/repo/My Folder/x.txt'")],
    [("Bash", r"touch C:/repo/My Folder/x.txt"), ("Bash", r"touch C:/repo/My Another/y.txt")],
    [("Bash", r"touch C:/repo/My Folder/../../outside.txt")],
    [("PowerShell", r"Set-Content -Path C:\repo\My Folder\x.txt -Value x")],
    [("Bash", r"touch C:/repo/tests/My Folder/test_a.py")],
])
def test_file_identity_does_not_depend_on_the_label_pass(monkeypatch, commands):
    shown = _observe(commands)
    with monkeypatch.context() as m:
        _no_labels(m)
        plain = _observe(commands)
    assert plain == shown


# ---------------------------------------------------------------- R5-001: identity and severity

def test_a_high_delete_keeps_its_finding_next_to_the_spaced_read():
    """R5-001: the low finding for the shell's own first word and the high finding for the
    separately quoted path are two different real targets; de-duplication keeps both."""
    command = r"cat E:/Work/Client y/z.txt; rm 'E:/Work/Client y/z.txt'"
    _, _, risks = risk.assess(_run([_step("Bash", command, 1)]))
    assert _findings(risks) == [
        ("low", "shell_outside",
         r"Shell command referenced a path outside the working folder (E:/Work/Client)"),
        ("high", "shell_outside",
         r"Shell command referenced a path outside the working folder (E:/Work/Client y/z.txt)"),
    ]


def test_two_high_deletes_of_different_targets_both_survive():
    command = r"rm E:/Work/Client y/z.txt 'E:/Work/Client y/z.txt'"
    _, _, risks = risk.assess(_run([_step("Bash", command, 1)]))
    assert [r.reason for r in risks if r.code == "shell_outside"] == [
        r"Shell command referenced a path outside the working folder (E:/Work/Client)",
        r"Shell command referenced a path outside the working folder (E:/Work/Client y/z.txt)",
    ]


def test_the_default_guard_asks_for_the_spaced_delete_with_a_read_in_front():
    command = r"cat E:/Work/Client y/z.txt; rm 'E:/Work/Client y/z.txt'"
    d = guard.decide(_event(REPO, "Bash", command), NO_POLICY)
    assert d["decision"] == "ask"
    assert sorted((r["severity"], r["code"]) for r in d["risks"]) == [
        ("high", "shell_outside"), ("low", "shell_outside")]


# ---------------------------------------------------------------- R5-002: policies see raw reasons

def test_a_user_ignore_rule_reads_the_shells_own_reason(isolated_settings):
    """R5-002: `y/z.txt` is an operand of its own; it must not make the user's ignore rule
    swallow the high outside-delete finding for the unrelated target `E:/Work/Client`."""
    _write_user_config(isolated_settings, {"ignore": ["y/z.txt"]})
    step = _step("Bash", r"rm E:/Work/Client y/z.txt")
    assert _findings(risk.assess_step(step, REPO)) == [
        ("high", "shell_outside",
         r"Shell command referenced a path outside the working folder (E:/Work/Client)")]


def test_a_user_ignore_rule_cannot_turn_the_guard_ask_into_an_allow(isolated_settings):
    _write_user_config(isolated_settings, {"ignore": ["y/z.txt"]})
    d = guard.decide(_event(REPO, "Bash", r"rm E:/Work/Client y/z.txt"), {})
    assert d["policy_decision"] == "ask" and d["decision"] == "ask"


def test_a_user_ignore_rule_cannot_hide_the_delete_in_a_receipt(tmp_path, capsys, isolated_settings):
    _write_user_config(isolated_settings, {"ignore": ["y/z.txt"]})
    path = _session(tmp_path, [{"tool": "Bash", "input": {"command": r"rm E:/Work/Client y/z.txt"}}])
    out, _ = _receipt(capsys, str(path), "--format", "json", "-o", "-", "--no-safe")
    outside = [r for r in json.loads(out)["risk"]["reasons"] if r["code"] == "shell_outside"]
    assert [(r["severity"], r["reason"]) for r in outside] == [
        ("high", r"Shell command referenced a path outside the working folder "
                 r"(E:/Work/Client y/z.txt)")]


def test_a_guard_reason_rule_matches_the_shells_own_reason(isolated_settings):
    """R5-002: the reason rule matches the raw reason (the shell's word), so the closing
    parenthesis of `(E:/Work/Client)` still matches and the delete stays denied."""
    _write_user_config(isolated_settings, {"guard": {"deny": ["reason:(e:/work/client)"], "ask": []}})
    d = guard.decide(_event(REPO, "Bash", r"rm E:/Work/Client y/z.txt"), {})
    assert d["policy_decision"] == "deny" and d["decision"] == "deny"


# ---------------------------------------------------------------- R5-003: raw file keys

def test_file_keys_keep_every_real_target_and_its_deletion_state():
    """R5-003: `rm C:/repo/My Folder/x.txt` really deletes `My` and `Folder/x.txt`; the label
    must not merge `My` into the unrelated quoted `My Folder/x.txt` or clear its deleted flag."""
    steps = [_step("Bash", r"rm C:/repo/My Folder/x.txt", 1),
             _step("Bash", r"touch 'C:/repo/My Folder/x.txt'", 2)]
    changes = summarize.file_changes(_run(steps))
    assert sorted((p, c.deleted) for p, c in changes.items()) == [
        ("Folder/x.txt", True), ("My", True), ("My Folder/x.txt", False)]


def test_file_counts_follow_the_shells_own_words():
    steps = [_step("Bash", r"touch C:/repo/My Folder/x.txt", 1),
             _step("Bash", r"touch C:/repo/My Another/y.txt", 2)]
    changes = summarize.file_changes(_run(steps))
    assert sorted(changes) == ["Another/y.txt", "Folder/x.txt", "My"]


def test_a_traversing_operand_does_not_hide_the_in_project_file():
    changes = summarize.file_changes(_run([_step("Bash", r"touch C:/repo/My Folder/../../outside.txt")]))
    assert sorted(changes) == ["../outside.txt", "My"]


def test_git_reconciliation_looks_up_the_shells_own_keys(monkeypatch, tmp_path):
    """R5-003: the keys that select Git line counts and test diffs are the same raw keys the
    receipt counts, so metadata can never be attached to another file."""
    seen = {}
    monkeypatch.setattr(cli, "repo_context", contextlib.nullcontext)
    monkeypatch.setattr(cli, "current_branch", lambda cwd: "main")
    monkeypatch.setattr(cli, "session_base", lambda cwd, ts: None)
    monkeypatch.setattr(cli, "working_changes",
                        lambda cwd, shell_paths, base: seen.setdefault("shell", list(shell_paths)) or {})
    monkeypatch.setattr(cli, "file_diffs",
                        lambda cwd, test_paths, base: seen.setdefault("tests", list(test_paths)) or {})
    steps = [_step("Bash", r"touch C:/repo/My Folder/x.txt", 1),
             _step("Bash", r"touch C:/repo/tests/My Folder/test_a.py", 2)]
    run = _run(steps)
    cli.reconcile_git(run)
    assert seen["shell"] == ["My", "Folder/x.txt", "tests/My", "Folder/test_a.py"]
    assert "tests/My" in seen["tests"]
    assert not [p for p in seen["shell"] + seen["tests"] if "My Folder" in p]


# ---------------------------------------------------------------- rendering only

def test_the_whole_path_is_shown_when_output_is_rendered():
    """The label is a copy step: it rewrites what a receipt shows and leaves the finding alone."""
    run = _run([_step("Bash", r"echo hi > E:\Work\Client y\z.txt")])
    _, _, risks = risk.assess(run)
    assert _findings(risks) == [
        ("low", "shell_outside", r"Shell command wrote outside the working folder (E:\Work\Client)")]
    shown = show_risks(risks, step_label_map(run))
    assert _findings(shown) == [
        ("low", "shell_outside",
         r"Shell command wrote outside the working folder (E:\Work\Client y\z.txt)")]
    assert _findings(risks) == [
        ("low", "shell_outside", r"Shell command wrote outside the working folder (E:\Work\Client)")]


def test_a_receipt_shows_the_whole_path_and_the_label_rule_is_what_shows_it(tmp_path, capsys,
                                                                          monkeypatch):
    path = _session(tmp_path, [{"tool": "Bash", "input": {"command": r"echo hi > E:\Work\Client y\z.txt"}}])
    out, _ = _receipt(capsys, str(path), "--format", "json", "-o", "-", "--no-safe")
    assert [r["reason"] for r in json.loads(out)["risk"]["reasons"]] == [
        r"Shell command wrote outside the working folder (E:\Work\Client y\z.txt)"]
    with monkeypatch.context() as m:
        _no_labels(m)
        plain, _ = _receipt(capsys, str(path), "--format", "json", "-o", "-", "--no-safe")
    assert [r["reason"] for r in json.loads(plain)["risk"]["reasons"]] == [
        r"Shell command wrote outside the working folder (E:\Work\Client)"]


def test_the_guard_message_shows_the_whole_path_without_changing_the_decision():
    d = guard.decide(_event(REPO, "Bash", r"rm E:\Work\Client y\z.txt"), NO_POLICY)
    assert d["decision"] == "ask"
    assert r"(E:\Work\Client y\z.txt)" in d["reason"]
    assert [(r["severity"], r["code"], r["reason"]) for r in d["risks"]] == [
        ("high", "shell_outside",
         r"Shell command referenced a path outside the working folder (E:\Work\Client y\z.txt)")]
