"""User and project rules for the guard and for receipts (validation contract VAL-RULES-019..028).

The user's own file may add `command_rules` (regex, matched case-insensitively against the
quote/heredoc-blanked command view, or against the raw command with `raw: true`), `tool_rules`
(fnmatch globs on the tool name) and extra MCP verbs. A rule hit is enforced even when the user
replaced the default deny list, and no exclusion can remove it. A project file may only add,
and receipts see the project's rules only.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from runledger import guard
from runledger.cli import main

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT

USER_RULES = {
    "command_rules": {
        "deny": [r"\bmyfancytool\s+wipe\b"],
        "ask": [{"pattern": r"\bkubectl\s+exec\b", "reason": "shell into prod"},
                {"pattern": "secretword", "raw": True, "reason": "raw match"}],
    }
}


def bash_event(cwd, command, tool="Bash", session="sess-rules"):
    return {"session_id": session, "cwd": str(cwd), "tool_name": tool,
            "tool_input": {"command": command}, "tool_use_id": "toolu-1"}


def tool_event(cwd, tool, tool_input=None, session="sess-rules"):
    return {"session_id": session, "cwd": str(cwd), "tool_name": tool,
            "tool_input": tool_input if tool_input is not None else {}, "tool_use_id": "toolu-1"}


def write_user_config(home, data):
    folder = Path(home) / ".runledger"
    folder.mkdir(exist_ok=True)
    (folder / "config.json").write_text(json.dumps(data), encoding="utf-8")


def write_project(cwd, data):
    (Path(cwd) / ".runledger.json").write_text(json.dumps(data), encoding="utf-8")


def decide(cwd, command, tool="Bash"):
    return guard.decide(bash_event(cwd, command, tool), guard.read_policy(str(cwd)))


def decide_tool(cwd, tool, tool_input=None):
    return guard.decide(tool_event(cwd, tool, tool_input), guard.read_policy(str(cwd)))


def handle(cwd, command, tool="Bash"):
    return guard.handle(json.dumps(bash_event(cwd, command, tool)))


def handle_tool(cwd, tool, tool_input=None):
    return guard.handle(json.dumps(tool_event(cwd, tool, tool_input)))


def log_records(cwd):
    path = Path(cwd) / ".runledger" / "guard.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def warning_texts(cwd):
    return " | ".join(r["reason"] for r in log_records(cwd) if r["decision"] == "warning")


def permission(out):
    return out["hookSpecificOutput"]["permissionDecision"]


def rule_risks(decision, code):
    return [r for r in decision["risks"] if r["code"] == code]


# ---------------------------------------------------------------- VAL-RULES-019

def test_a_user_command_rule_denies_and_names_itself(tmp_path, isolated_settings):
    write_user_config(isolated_settings, USER_RULES)
    d = decide(tmp_path, "myfancytool wipe all")
    assert d["decision"] == "deny" and d["codes"] == ["rule_deny"]
    risk = rule_risks(d, "rule_deny")[0]
    assert risk["severity"] == "high"
    assert "deny rule" in risk["reason"] and "myfancytool" in risk["reason"]
    assert decide(tmp_path, "myfancytool list")["decision"] == "allow"


def test_a_command_rule_matches_case_insensitively(tmp_path, isolated_settings):
    write_user_config(isolated_settings, USER_RULES)
    assert decide(tmp_path, "MYFANCYTOOL WIPE --all")["decision"] == "deny"


def test_an_object_ask_rule_carries_its_reason(tmp_path, isolated_settings):
    write_user_config(isolated_settings, USER_RULES)
    d = decide(tmp_path, "kubectl exec -it pod -- sh")
    assert d["decision"] == "ask" and d["codes"] == ["rule_ask"]
    risk = rule_risks(d, "rule_ask")[0]
    assert risk["severity"] == "medium" and "shell into prod" in risk["reason"]
    assert "ask rule" in risk["reason"]
    assert permission(guard.hook_output(d)) == "ask"


def test_the_hook_prints_the_rule_decision_and_logs_the_code(tmp_path, isolated_settings):
    write_user_config(isolated_settings, USER_RULES)
    assert permission(handle(tmp_path, "myfancytool wipe all")) == "deny"
    assert permission(handle(tmp_path, "kubectl exec -it pod -- sh")) == "ask"
    rows = [(r["decision"], r["codes"]) for r in log_records(tmp_path)]
    assert rows == [("deny", ["rule_deny"]), ("ask", ["rule_ask"])]


def test_rules_apply_to_powershell_commands_too(tmp_path, isolated_settings):
    write_user_config(isolated_settings, USER_RULES)
    assert decide(tmp_path, "myfancytool wipe all", tool="PowerShell")["decision"] == "deny"


def test_a_rule_reads_the_blanked_view_unless_raw(tmp_path, isolated_settings):
    write_user_config(isolated_settings, USER_RULES)
    assert decide(tmp_path, 'echo "myfancytool wipe"')["decision"] == "allow"
    assert decide(tmp_path, 'git commit -m "myfancytool wipe"')["decision"] == "allow"
    assert decide(tmp_path, "echo secretword")["decision"] == "ask"


# ---------------------------------------------------------------- VAL-RULES-020

ODD_RULES = {
    "command_rules": {"deny": ["(unclosed", r"\bgoodrule\b", 42, None, {"nopattern": 1}],
                      "ask": "not-a-list"},
    "tool_rules": {"deny": ["[", " ", 7]},
    "mcp_verbs": {"destructive": "nuke", "write": [1, None]},
}


def test_oddly_typed_rule_entries_are_dropped_safely(tmp_path, isolated_settings):
    write_user_config(isolated_settings, ODD_RULES)
    d = decide(tmp_path, "goodrule x")
    assert d["decision"] == "deny" and d["codes"] == ["rule_deny"]
    assert decide(tmp_path, "ls")["decision"] == "allow"
    assert decide(tmp_path, "git status")["decision"] == "allow"
    assert decide(tmp_path, 'echo "[ "')["decision"] == "allow"
    assert decide_tool(tmp_path, "Edit", {"file_path": str(tmp_path / "a.py")})["decision"] == "allow"


def test_a_user_invalid_regex_is_dropped_with_a_warning(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"command_rules": {"deny": ["(unclosed"]}})
    d = decide(tmp_path, "ls")
    assert d["decision"] == "allow"
    assert any("(unclosed" in w and "command_rules.deny" in w and "Dropped" in w for w in d["warnings"])


def test_a_project_invalid_regex_is_dropped_with_a_warning(tmp_path):
    write_project(tmp_path, {"command_rules": {"deny": ["(unclosed", r"\bgoodrule\b"]}})
    assert permission(handle(tmp_path, "goodrule x")) == "deny"
    assert "Dropped invalid command_rules.deny pattern" in warning_texts(tmp_path)
    assert "(unclosed" in warning_texts(tmp_path)
    assert json.loads((tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8")
                      .splitlines()[0])["decision"] == "warning"


# ---------------------------------------------------------------- GE-005: a rule regex cannot stall

CATASTROPHIC_RULE = r"(?:a{1,32}){1,32}b"
CATASTROPHIC = {"command_rules": {"deny": [{"pattern": CATASTROPHIC_RULE, "raw": True,
                                            "reason": "nested repeat"},
                                           r"\bgoodrule\b"]}}


def test_a_catastrophic_user_regex_is_dropped_with_a_warning(tmp_path, isolated_settings):
    write_user_config(isolated_settings, CATASTROPHIC)
    d = decide(tmp_path, "echo aaa!")
    assert d["decision"] == "allow"
    assert any(CATASTROPHIC_RULE in w and "command_rules.deny" in w and "Dropped" in w
               for w in d["warnings"]), d["warnings"]
    assert decide(tmp_path, "goodrule x")["decision"] == "deny"


def test_a_catastrophic_project_regex_is_dropped_with_a_warning(tmp_path):
    write_project(tmp_path, CATASTROPHIC)
    assert permission(handle(tmp_path, "goodrule x")) == "deny"
    assert "Dropped invalid command_rules.deny pattern" in warning_texts(tmp_path)
    assert CATASTROPHIC_RULE in warning_texts(tmp_path)


def test_a_catastrophic_project_regex_never_reaches_a_receipt_policy(tmp_path):
    from runledger.risk import load_policy

    write_project(tmp_path, CATASTROPHIC)
    rules = load_policy(str(tmp_path))["command_rules"]
    assert [r["label"] for r in rules["deny"]] == [r"\bgoodrule\b"]


def test_the_hook_cannot_be_stalled_by_a_command_rule(tmp_path, isolated_settings):
    """The 40-character probe would take hours if the pattern were accepted (GE-005)."""
    write_user_config(isolated_settings, CATASTROPHIC)
    assert handle(tmp_path, "echo aaa!") is None
    assert permission(handle(tmp_path, "goodrule x")) == "deny"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "runledger", "guard"],
                          input=json.dumps(bash_event(tmp_path, "echo " + "a" * 40 + "!")).encode("utf-8"),
                          capture_output=True, cwd=str(tmp_path), env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"" and b"Traceback" not in proc.stderr
    assert time.monotonic() - start < 15.0


def test_oddly_typed_rules_do_not_break_the_hook_process(tmp_path, isolated_settings):
    write_user_config(isolated_settings, ODD_RULES)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-m", "runledger", "guard"],
                          input=json.dumps(bash_event(tmp_path, "goodrule x")).encode("utf-8"),
                          capture_output=True, cwd=str(tmp_path), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = subprocess.run([sys.executable, "-m", "runledger", "guard"],
                             input=json.dumps(bash_event(tmp_path, "ls")).encode("utf-8"),
                             capture_output=True, cwd=str(tmp_path), env=env, timeout=60)
    assert allowed.returncode == 0 and allowed.stdout == b"" and b"Traceback" not in allowed.stderr


# ---------------------------------------------------------------- VAL-RULES-021

def test_a_rule_hit_survives_a_user_replaced_deny_list(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {
        "guard": {"deny": ["secret_in_content"]},
        "command_rules": {"deny": [r"\bmyfancytool\s+wipe\b"]}})
    assert permission(handle(tmp_path, "myfancytool wipe")) == "deny"
    # rm -rf is no longer on the deny list, and it is high, so it asks instead.
    assert decide(tmp_path, "rm -rf ./x")["decision"] == "ask"


@pytest.mark.parametrize("exclusion", ["!rule_deny", "!severity:high"])
def test_a_user_exclusion_cannot_remove_a_rule_hit(tmp_path, isolated_settings, exclusion):
    write_user_config(isolated_settings, {
        "guard": {"deny": ["secret_in_content", exclusion]},
        "command_rules": {"deny": [r"\bmyfancytool\s+wipe\b"]}})
    assert decide(tmp_path, "myfancytool wipe")["decision"] == "deny"


def test_a_project_exclusion_cannot_remove_a_rule_hit(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"command_rules": {"deny": [r"\bmyfancytool\s+wipe\b"]}})
    write_project(tmp_path, {"guard": {"deny": ["!rule_deny", "!severity:high"]}})
    assert permission(handle(tmp_path, "myfancytool wipe")) == "deny"
    assert "exclusion" in warning_texts(tmp_path)


# ---------------------------------------------------------------- VAL-RULES-022

TOOL_RULES = {"tool_rules": {"deny": ["mcp__supabase__execute_query", "Edit"],
                             "ask": ["mcp__vercel__*"]}}


@pytest.mark.parametrize("tool,expected", [
    ("mcp__supabase__execute_query", "deny"),
    ("Edit", "deny"),
    ("mcp__vercel__get_project", "ask"),
    ("mcp__vercel__delete_project", "ask"),
    ("mcp__Vercel__get_project", "allow"),
    ("Write", "allow"),
])
def test_tool_rules_match_the_whole_tool_name(tmp_path, isolated_settings, tool, expected):
    write_user_config(isolated_settings, TOOL_RULES)
    tool_input = {"query": "SELECT 1"} if "execute_query" in tool else {"file_path": str(tmp_path / "a.py")}
    d = decide_tool(tmp_path, tool, tool_input)
    assert d["decision"] == expected, d["risks"]
    if expected == "deny":
        assert "deny rule" in rule_risks(d, "rule_deny")[0]["reason"]
    elif expected == "ask":
        assert "ask rule" in rule_risks(d, "rule_ask")[0]["reason"]
    else:
        assert rule_risks(d, "rule_deny") == [] and rule_risks(d, "rule_ask") == []


def test_a_tool_on_both_lists_is_denied(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"tool_rules": {
        "deny": ["mcp__vercel__delete_project"], "ask": ["mcp__vercel__*"]}})
    d = decide_tool(tmp_path, "mcp__vercel__delete_project")
    assert d["decision"] == "deny"
    assert rule_risks(d, "rule_deny") and rule_risks(d, "rule_ask")


def test_the_hook_prints_the_tool_rule_decision(tmp_path, isolated_settings):
    write_user_config(isolated_settings, TOOL_RULES)
    assert permission(handle_tool(tmp_path, "Edit", {"file_path": str(tmp_path / "a.py")})) == "deny"
    assert permission(handle_tool(tmp_path, "mcp__vercel__get_project")) == "ask"
    codes = [r["codes"] for r in log_records(tmp_path)]
    assert codes == [["rule_deny"], ["mcp", "rule_ask"]]


# ---------------------------------------------------------------- VAL-RULES-023

MCP_VERB_RULES = {"mcp_verbs": {"destructive": ["nuke"], "write": ["sync"], "read": ["peek"]}}
MCP_VERB_CASES = {
    "mcp__foo__nuke_cluster": ("high", "ask"),
    "mcp__foo__sync_data": ("medium", "allow"),
    "mcp__foo__peek_queue": ("low", "allow"),
    "mcp__foo__peek_nuke_cluster": ("low", "allow"),
    "mcp__foo__sync_nuke": ("high", "ask"),
    "mcp__foo__nukeCluster": ("high", "ask"),
}


@pytest.mark.parametrize("tool,expected", sorted(MCP_VERB_CASES.items()))
def test_user_mcp_verbs_extend_the_vocabulary(tmp_path, isolated_settings, tool, expected):
    write_user_config(isolated_settings, MCP_VERB_RULES)
    severity, decision = expected
    d = decide_tool(tmp_path, tool)
    assert [r["severity"] for r in d["risks"] if r["code"] == "mcp"] == [severity], d["risks"]
    assert d["decision"] == decision


@pytest.mark.parametrize("tool", sorted(MCP_VERB_CASES))
def test_without_the_file_the_same_names_stay_low(tmp_path, tool):
    d = decide_tool(tmp_path, tool)
    assert [(r["severity"], r["code"]) for r in d["risks"]] == [("low", "mcp")]
    assert d["decision"] == "allow"


# ---------------------------------------------------------------- VAL-MCP-009 / VAL-MCP-010

DESTRUCTIVE_MCP = "mcp__supabase__delete_project"
MEDIUM_MCP = "mcp__vercel__create_deployment"
LOW_MCP = "mcp__x__get_me"


def test_a_user_deny_entry_blocks_the_destructive_mcp_class(tmp_path, isolated_settings):
    event = tool_event(tmp_path, DESTRUCTIVE_MCP)
    assert guard.decide(event, {})["decision"] == "ask"          # the default, with no files
    write_user_config(isolated_settings, {"guard": {"deny": ["secret_in_content", "command:high",
                                                             "mcp:high"]}})
    assert permission(guard.handle(json.dumps(event))) == "deny"
    assert guard.decide(tool_event(tmp_path, MEDIUM_MCP), {})["decision"] == "allow"


def test_a_user_ask_entry_covers_every_mcp_class(tmp_path, isolated_settings):
    events = [tool_event(tmp_path, tool) for tool in (DESTRUCTIVE_MCP, MEDIUM_MCP, LOW_MCP)]
    assert [guard.decide(e, {})["decision"] for e in events] == ["ask", "allow", "allow"]
    write_user_config(isolated_settings, {"guard": {"ask": ["severity:high", "mcp"]}})
    for event in events:
        d = guard.decide(event, {})
        assert d["decision"] == "ask" and permission(guard.hook_output(d)) == "ask", d["tool"]


def test_a_user_severity_override_or_ignore_softens_a_destructive_mcp_call(tmp_path, isolated_settings):
    event = tool_event(tmp_path, DESTRUCTIVE_MCP)
    write_user_config(isolated_settings, {"severity_overrides": {"mcp": "low"}})
    d = guard.decide(event, {})
    assert d["decision"] == "allow" and [r["severity"] for r in d["risks"]] == ["low"]
    assert guard.hook_output(d) is None
    write_user_config(isolated_settings, {"ignore": ["destructive mcp"]})
    d = guard.decide(event, {})
    assert d["decision"] == "allow" and d["risks"] == [] and guard.hook_output(d) is None


def test_a_project_cannot_soften_a_destructive_mcp_call(tmp_path):
    write_project(tmp_path, {"severity_overrides": {"mcp": "low"}, "ignore": ["destructive mcp"],
                             "guard": {"ask": ["!mcp"]}})
    event = tool_event(tmp_path, DESTRUCTIVE_MCP)
    d = guard.decide(event, guard.read_policy(str(tmp_path)))
    assert d["decision"] == "ask" and [r["severity"] for r in d["risks"]] == ["high"]
    assert permission(guard.handle(json.dumps(event))) == "ask"
    texts = " | ".join(d["warnings"])
    for needle in ("severity_overrides for mcp", "ignore entry", "exclusion"):
        assert needle in texts, texts


def test_a_project_may_raise_a_medium_mcp_call(tmp_path):
    write_project(tmp_path, {"severity_overrides": {"mcp": "high"}})
    d = guard.decide(tool_event(tmp_path, MEDIUM_MCP), guard.read_policy(str(tmp_path)))
    assert d["decision"] == "ask" and [r["severity"] for r in d["risks"]] == ["high"]


# ---------------------------------------------------------------- VAL-RULES-024 / 025

def test_a_project_can_add_rules_and_verbs(tmp_path):
    write_project(tmp_path, {"command_rules": {"deny": [r"\bprojtool\s+wipe\b"]},
                             "tool_rules": {"deny": ["mcp__proj__danger"]},
                             "mcp_verbs": {"destructive": ["frob"], "write": ["tickle"]}})
    assert permission(handle(tmp_path, "projtool wipe")) == "deny"
    assert permission(handle_tool(tmp_path, "mcp__proj__danger")) == "deny"
    frob = decide_tool(tmp_path, "mcp__x__frob_it")
    assert [r["severity"] for r in frob["risks"] if r["code"] == "mcp"] == ["high"]
    assert frob["decision"] == "ask"
    tickle = decide_tool(tmp_path, "mcp__x__tickle_it")
    assert [r["severity"] for r in tickle["risks"] if r["code"] == "mcp"] == ["medium"]
    assert tickle["decision"] == "allow"


def test_user_and_project_rules_both_apply(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"command_rules": {"deny": [r"\busertool\s+wipe\b"]}})
    write_project(tmp_path, {"command_rules": {"deny": [r"\bprojtool\s+wipe\b"]}})
    assert decide(tmp_path, "usertool wipe")["decision"] == "deny"
    assert decide(tmp_path, "projtool wipe")["decision"] == "deny"


def test_a_project_cannot_remove_a_user_or_builtin_rule(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"command_rules": {"deny": [r"\bmyfancytool\s+wipe\b"]}})
    write_project(tmp_path, {"guard": {"deny": ["!command:high"], "ask": ["!severity:high"]},
                             "command_rules": {"deny": []}, "tool_rules": {}, "mcp_verbs": {}})
    assert decide(tmp_path, "rm -rf ./x")["decision"] == "deny"
    assert decide(tmp_path, "myfancytool wipe")["decision"] == "deny"
    d = decide_tool(tmp_path, "mcp__supabase__delete_project")
    assert d["decision"] == "ask" and [r["severity"] for r in d["risks"]] == ["high"]
    assert "exclusion" in warning_texts(tmp_path) or any("exclusion" in w for w in d["warnings"])


def test_project_loosening_rule_keys_are_warned_and_ignored(tmp_path, isolated_settings):
    write_user_config(isolated_settings, {"command_rules": {"deny": [r"\bmyfancytool\s+wipe\b"]}})
    write_project(tmp_path, {"mcp_verbs": {"read": ["delete"]},
                             "severity_overrides": {"rule_deny": "low", "mcp": "low"},
                             "guard": {"mode": "monitor"},
                             "ignore": ["command"]})
    assert permission(handle(tmp_path, "rm -rf ./x")) == "deny"
    assert permission(handle(tmp_path, "myfancytool wipe")) == "deny"
    assert permission(handle_tool(tmp_path, "mcp__x__delete_post")) == "ask"
    texts = warning_texts(tmp_path)
    for needle in ("mcp_verbs.read", "mode monitor", "severity_overrides for rule_deny",
                   "severity_overrides for mcp", "ignore entry"):
        assert needle in texts, texts
    d = decide_tool(tmp_path, "mcp__x__delete_post")
    assert d["mode"] == "enforce" and [r["severity"] for r in d["risks"]] == ["high"]


# ---------------------------------------------------------------- VAL-RULES-027

def test_the_new_rule_keys_do_not_change_the_trust_model(tmp_path, isolated_settings):
    """A project rule is enforced by the live guard, but a project still cannot choose the
    approval server, and the user's monitor mode still records instead of blocking."""
    write_user_config(isolated_settings, {"guard": {"mode": "monitor"}})
    write_project(tmp_path, {"guard": {"approval": {"server": "https://attacker.example.test",
                                                    "api_key_env": "ATTACKER_KEY"}},
                             "command_rules": {"deny": [r"\bmyfancytool\s+wipe\b"]}})
    d = guard.decide(bash_event(tmp_path, "myfancytool wipe"), guard.read_policy(str(tmp_path)))
    assert d["decision"] == "allow" and d["approval"] is None
    assert d["mode"] == "monitor" and d["policy_decision"] == "deny"
    texts = " | ".join(d["warnings"])
    assert "approval.server" in texts and "approval.api_key_env" in texts
    assert guard.handle(json.dumps(bash_event(tmp_path, "myfancytool wipe"))) is None
    rows = [r for r in log_records(tmp_path) if r["decision"] == "allow"]
    assert rows[-1]["policy_decision"] == "deny" and rows[-1]["codes"] == ["rule_deny"]


# ---------------------------------------------------------------- VAL-RULES-026

MODEL = "claude-sonnet-4-5-20250929"


def _claude_session(tmp_path, calls, session_id="rules-session"):
    rows = [{"cwd": str(tmp_path), "sessionId": session_id, "gitBranch": "main", "version": "2.1.0",
             "timestamp": "2026-10-19T10:00:00.000Z", "type": "user",
             "message": {"role": "user", "content": "Run the tools."}}]
    for i, (name, inp) in enumerate(calls):
        rows.append({
            "cwd": str(tmp_path), "sessionId": session_id, "gitBranch": "main", "version": "2.1.0",
            "timestamp": f"2026-10-19T10:00:{i + 1:02d}.000Z", "type": "assistant",
            "message": {"id": f"msg_{i}", "role": "assistant", "model": MODEL,
                        "content": [{"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}],
                        "usage": {"input_tokens": 10, "output_tokens": 5,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _receipt_reasons(capsys, path):
    assert main(["receipt", str(path), "--format", "json", "-o", "-"]) == 0
    return json.loads(capsys.readouterr().out)["risk"]["reasons"]


def test_a_receipt_flags_the_project_rules(tmp_path, capsys):
    write_project(tmp_path, {"command_rules": {"deny": [r"\bmyfancytool\s+wipe\b"]},
                             "tool_rules": {"deny": ["mcp__supabase__delete_project"]}})
    path = _claude_session(tmp_path, [("Bash", {"command": "myfancytool wipe all"}),
                                      ("mcp__supabase__delete_project", {})])
    hits = [r for r in _receipt_reasons(capsys, path) if r["code"] == "rule_deny"]
    assert [(r["severity"]) for r in hits] == ["high", "high"]
    reasons = " | ".join(r["reason"] for r in hits)
    assert "myfancytool" in reasons and "mcp__supabase__delete_project" in reasons


def test_a_receipt_does_not_show_user_rules(tmp_path, isolated_settings, capsys):
    write_user_config(isolated_settings, {"command_rules": {"deny": [r"\bmyfancytool\s+wipe\b"]},
                                          "tool_rules": {"deny": ["mcp__supabase__delete_project"]}})
    path = _claude_session(tmp_path, [("Bash", {"command": "myfancytool wipe all"}),
                                      ("mcp__supabase__delete_project", {})])
    assert [r for r in _receipt_reasons(capsys, path) if r["code"].startswith("rule_")] == []
    # The live guard still enforces the user's rules.
    assert decide(tmp_path, "myfancytool wipe all")["decision"] == "deny"
    assert decide_tool(tmp_path, "mcp__supabase__delete_project")["decision"] == "deny"


def test_the_guard_doc_states_the_receipt_limitation():
    text = (ROOT / "docs" / "guard.md").read_text(encoding="utf-8")
    assert "receipt" in text
    assert re.search(r"receipt.{0,200}project", text, re.S | re.I), "docs/guard.md must say receipts see project rules"


# ---------------------------------------------------------------- VAL-RULES-028 / VAL-MCP-013

RULE_FAMILIES = [
    "git branch -D", "git branch --delete --force", "git push --delete", "git push origin :branch",
    "git checkout .", "git restore .", "git stash drop", "git stash clear", "find ... -delete",
    "find ... -exec rm", "dd of=", "docker volume prune",
    "docker system|builder|image|container|network prune", "aws s3 rm", "aws s3 rb",
    "gh repo delete", "gh release|issue|gist|label|secret|variable|cache|run|workflow",
    "helm uninstall", "DELETE FROM", "rsync --delete", "reg delete", "rd /s /q", "rmdir /s /q",
    "Remove-Item -Recurse",
]

RULE_KEYS = ["command_rules", "tool_rules", "mcp_verbs", "rule_deny", "rule_ask", "fnmatch",
             "raw", "heredoc"]


@pytest.mark.parametrize("phrase", RULE_FAMILIES + RULE_KEYS)
def test_the_guard_doc_names_every_new_rule_and_key(phrase):
    text = (ROOT / "docs" / "guard.md").read_text(encoding="utf-8")
    assert phrase in text, phrase


def _json_blocks(text):
    return re.findall(r"```json\n(.*?)```", text, re.S)


def test_every_json_example_in_the_guard_doc_parses():
    blocks = _json_blocks((ROOT / "docs" / "guard.md").read_text(encoding="utf-8"))
    assert len(blocks) >= 3
    for block in blocks:
        assert isinstance(json.loads(block), dict)


def _documented_config(marker_a, marker_b):
    blocks = [b for b in _json_blocks((ROOT / "docs" / "guard.md").read_text(encoding="utf-8"))
              if marker_a in b and marker_b in b]
    assert len(blocks) == 1, blocks
    return json.loads(blocks[0])


def test_the_documented_user_rule_example_behaves_as_documented(tmp_path, isolated_settings):
    config = _documented_config("obliterate", "heroku")
    config.pop("server", None)  # the example's server must never be contacted by a test
    write_user_config(isolated_settings, config)
    assert permission(handle(tmp_path, "heroku apps:destroy my-app")) == "deny"
    assert permission(handle_tool(tmp_path, "mcp__supabase__execute_query",
                                  {"query": "SELECT 1"})) == "deny"
    d = decide_tool(tmp_path, "mcp__foo__obliterate_everything")
    assert [r["severity"] for r in d["risks"] if r["code"] == "mcp"] == ["high"]
    assert d["decision"] == "ask"


def test_the_documented_project_rule_example_behaves_as_documented(tmp_path):
    config = _documented_config("npm", "vercel")
    write_project(tmp_path, config)
    assert permission(handle(tmp_path, "npm run release")) == "deny"
    assert permission(handle_tool(tmp_path, "mcp__vercel__get_project")) == "ask"
    assert permission(handle_tool(tmp_path, "mcp__supabase__delete_project")) == "deny"
    d = decide_tool(tmp_path, "mcp__foo__obliterate_everything")
    assert [r["severity"] for r in d["risks"] if r["code"] == "mcp"] == ["high"]
