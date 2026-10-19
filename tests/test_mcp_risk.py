"""MCP verb classification and SQL argument inspection (validation contract, MCP area).

Destructive verbs ask, state-changing verbs are medium, read-only and unknown names keep the
v0.6 low wording with the tool's own leaf name, and the SQL body of an execute-class call
never reaches a reason, a log row or an approval request.
"""
import json
from pathlib import Path

import pytest

from runledger import guard
from runledger.cli import main
from runledger.mcp_risk import mcp_tokens
from runledger.parser import Step
from runledger.risk import assess_step, classify_mcp, mcp_parts

WIN_CWD = "D:\\runledger"
NO_POLICY = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": []}

DESTRUCTIVE = [
    "mcp__supabase__delete_project", "mcp__supabase__delete_branch",
    "mcp__supabase__delete_all_branches", "mcp__supabase__reset_branch",
    "mcp__vercel__delete_env_var", "mcp__x__delete_post", "mcp__github__deleteBranch",
    "mcp__github__delete-branch", "mcp__db__drop_table", "mcp__k8s__destroy_cluster",
    "mcp__s3__purge_bucket", "mcp__ec2__terminate_instances", "mcp__iam__revoke_key",
    "mcp__git__force_push", "mcp__admin__bulk_delete_users",
]
STATE_CHANGING = [
    "mcp__x__create_post", "mcp__vercel__create_deployment", "mcp__vercel__cancel_deployment",
    "mcp__vercel__pause_project", "mcp__supabase__create_branch", "mcp__supabase__restore_project",
    "mcp__github__publish_release", "mcp__slack__send_message", "mcp__x__follow_user",
    "mcp__ci__run_workflow", "mcp__app__deploy",
]
READ_OR_UNKNOWN = [
    "mcp__x__get_me", "mcp__supabase__list_projects", "mcp__supabase__diff_branch",
    "mcp__vercel__validate_credential", "mcp__x__search_recent_posts", "mcp__weird__frobnicate",
]
EDGE_SEVERITIES = {
    "mcp__x__get_delete_policy": "low",
    "mcp__x__list_deleted_items": "low",
    "mcp__x__describe_drop_rules": "low",
    "mcp__x__deleted_items": "low",
    "mcp__x__undelete_items": "low",
    "mcp__x__bulk_delete_users": "high",
    "mcp__x__deleteBranch": "high",
    "mcp__x__delete-branch": "high",
    "mcp__x__delete.branch": "high",
    "mcp__x__createPost": "medium",
    "mcp__x__GET_ME": "low",
}
DROID_BUILTINS = ["Execute", "Read", "Create", "Edit", "LS", "Glob", "Grep", "Task",
                  "FetchUrl", "WebSearch"]

SQL_TOOLS = [("mcp__supabase__execute_query", "query"), ("mcp__db__execute_sql", "sql"),
             ("mcp__db__run_sql", "statement"), ("mcp__pg__query", "query")]
HIGH_SQL = [
    "DROP TABLE users", "drop table if exists users", "DROP DATABASE app",
    "DROP SCHEMA public CASCADE", "TRUNCATE TABLE users", "Truncate users",
    "DELETE FROM users", "delete from users;", "UPDATE users SET admin = true",
    "ALTER TABLE users DROP COLUMN email", "SELECT 1; DROP TABLE t;",
    "DELETE FROM a WHERE x = 1; DELETE FROM b",
]
MEDIUM_SQL = ["INSERT INTO t VALUES (1)", "UPDATE t SET a = 1 WHERE id = 2",
              "DELETE FROM t WHERE id = 1", "CREATE TABLE t (id int)",
              "ALTER TABLE t ADD COLUMN c int", "GRANT SELECT ON t TO r"]
LOW_SQL = ["SELECT * FROM t", "WITH x AS (SELECT 1) SELECT * FROM x", "EXPLAIN SELECT 1",
           "SHOW TABLES", "select 'DROP TABLE x' as s", "/* DROP TABLE x */ SELECT 1",
           "-- TRUNCATE users\nSELECT 1"]
UNREADABLE_SQL = [None, 123, "", {"nested": "DROP TABLE t"}]


def _step(tool, index=1, **inp):
    return Step(index=index, tool=tool, input=inp, tool_use_id=f"t{index}", model=None, timestamp=None)


def _mcp_risks(tool, cwd=WIN_CWD, **inp):
    return [r for r in assess_step(_step(tool, **inp), cwd, NO_POLICY) if r.code == "mcp"]


def _mcp(tool, **inp):
    hits = _mcp_risks(tool, **inp)
    assert len(hits) == 1, [(r.severity, r.reason) for r in hits]
    return hits[0]


def _leaf(tool):
    return tool.split("__", 2)[-1]


# ---------------------------------------------------------------- verb classes

@pytest.mark.parametrize("tool", DESTRUCTIVE)
def test_destructive_mcp_tools_are_high(tool):
    risk = _mcp(tool)
    server, _, leaf = tool[5:].partition("__")
    assert risk.severity == "high"
    assert risk.reason.startswith("Called a destructive MCP tool (")
    assert server in risk.reason and leaf in risk.reason


@pytest.mark.parametrize("tool", STATE_CHANGING)
def test_state_changing_mcp_tools_are_medium(tool):
    risk = _mcp(tool)
    server, _, leaf = tool[5:].partition("__")
    assert risk.severity == "medium"
    assert risk.reason.startswith("Called a state-changing MCP tool (")
    assert server in risk.reason and leaf in risk.reason


@pytest.mark.parametrize("tool", READ_OR_UNKNOWN)
def test_read_and_unknown_mcp_tools_keep_the_original_reason(tool):
    risk = _mcp(tool)
    assert risk.severity == "low"
    assert risk.reason == f"Called an external MCP tool ({_leaf(tool)})"


@pytest.mark.parametrize("tool,severity", sorted(EDGE_SEVERITIES.items()))
def test_verb_matching_edge_cases(tool, severity):
    assert _mcp(tool).severity == severity


@pytest.mark.parametrize("leaf,expected", [
    ("deleteBranch", ["delete", "branch"]),
    ("delete-branch", ["delete", "branch"]),
    ("delete.branch", ["delete", "branch"]),
    ("delete_all_branches", ["delete", "all", "branches"]),
    ("GET_ME", ["get", "me"]),
    ("createPost", ["create", "post"]),
    ("", []),
])
def test_leaf_names_split_on_case_and_separators(leaf, expected):
    assert mcp_tokens(leaf) == expected


def test_classify_mcp_is_a_pure_function():
    assert classify_mcp("mcp__supabase__delete_project", {}, NO_POLICY)[0] == "high"
    assert classify_mcp("supabase__delete_project", None, None)[0] == "high"


# ---------------------------------------------------------------- Droid connector names

@pytest.mark.parametrize("tool,severity,label", [
    ("supabase__delete_project", "high", "Called a destructive MCP tool (supabase: delete_project)"),
    ("vercel__create_deployment", "medium", "Called a state-changing MCP tool (vercel: create_deployment)"),
    ("x__get_me", "low", "Called an external MCP tool (get_me)"),
])
def test_droid_connector_names_use_the_same_classifier(tool, severity, label):
    risk = _mcp(tool)
    assert (risk.severity, risk.reason) == (severity, label)


def test_prefixed_and_connector_names_agree():
    prefixed = _mcp("mcp__supabase__delete_project")
    connector = _mcp("supabase__delete_project")
    assert (prefixed.severity, prefixed.reason) == (connector.severity, connector.reason)


@pytest.mark.parametrize("tool", DROID_BUILTINS)
def test_agent_builtin_tools_are_not_mcp(tool):
    assert _mcp_risks(tool) == []


def test_mcp_parts_reads_both_name_shapes():
    assert mcp_parts("mcp__supabase__delete_project") == ("supabase", "delete_project")
    assert mcp_parts("supabase__delete_project") == ("supabase", "delete_project")
    assert mcp_parts("mcp__x__y__z") == ("x", "y__z")
    assert mcp_parts("Execute") is None
    assert mcp_parts("Read") is None
    assert mcp_parts("____") is None


@pytest.mark.parametrize("tool", ["mcp__onlyserver", "mcp__", "mcp__a__", "mcp____x"])
def test_malformed_mcp_names_stay_low_without_a_crash(tool):
    hits = _mcp_risks(tool)
    assert all(r.severity == "low" for r in hits)


# ---------------------------------------------------------------- SQL arguments

@pytest.mark.parametrize("tool,key", SQL_TOOLS)
@pytest.mark.parametrize("sql", HIGH_SQL)
def test_destructive_sql_is_high(tool, key, sql):
    risk = _mcp(tool, **{key: sql})
    server, _, leaf = tool[5:].partition("__")
    assert risk.severity == "high"
    assert risk.reason.startswith("Ran SQL that ")
    assert f"({server}: {leaf})" in risk.reason
    assert sql not in risk.reason  # the body itself is never copied out


@pytest.mark.parametrize("tool,key", SQL_TOOLS)
@pytest.mark.parametrize("sql", MEDIUM_SQL)
def test_data_changing_sql_is_medium(tool, key, sql):
    risk = _mcp(tool, **{key: sql})
    assert risk.severity == "medium"
    assert risk.reason.startswith("Ran SQL that changes data (")
    assert sql not in risk.reason


@pytest.mark.parametrize("tool,key", SQL_TOOLS)
@pytest.mark.parametrize("sql", LOW_SQL)
def test_read_only_sql_stays_low(tool, key, sql):
    risk = _mcp(tool, **{key: sql})
    assert risk.severity == "low"
    assert risk.reason == f"Called an external MCP tool ({_leaf(tool)})"


@pytest.mark.parametrize("tool,key", [("mcp__supabase__execute_query", "query"),
                                      ("mcp__db__execute_sql", "sql"),
                                      ("mcp__supabase__execute_query", "query")])
@pytest.mark.parametrize("given", UNREADABLE_SQL)
def test_unreadable_sql_on_an_execute_tool_is_medium(tool, key, given):
    risk = _mcp(tool, **({key: given} if given is not None else {}))
    assert risk.severity == "medium"
    assert risk.reason.startswith("Ran SQL the guard could not read (")


def test_read_verb_first_token_does_not_beat_the_sql_verdict():
    assert _mcp("mcp__pg__query", query="SELECT 1").severity == "low"
    assert _mcp("mcp__pg__query", query="DROP TABLE t").severity == "high"


def test_a_non_sql_tool_with_a_query_argument_is_not_escalated():
    risk = _mcp("mcp__x__search_recent_posts", query="release notes")
    assert (risk.severity, risk.reason) == ("low", "Called an external MCP tool (search_recent_posts)")


# ---------------------------------------------------------------- privacy and size

def test_sql_body_is_absent_from_reasons_and_log_rows(tmp_path):
    event = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__supabase__execute_query",
             "tool_use_id": "t1", "tool_input": {"query": "DROP TABLE zz_marker_9f3a",
                                                 "api_key": "SECRET-ARG-MARKER"}}
    out = guard.handle(json.dumps(event))
    log = (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8")
    assert json.loads(log.splitlines()[0])["codes"] == ["mcp"]
    for text in (json.dumps(out), log):
        assert "zz_marker_9f3a" not in text and "SECRET-ARG-MARKER" not in text


def test_the_approval_request_never_carries_the_sql_body(tmp_path):
    seen = []
    event = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__supabase__execute_query",
             "tool_use_id": "t1", "tool_input": {"query": "DELETE FROM zz_marker_9f3a"}}
    decision = guard.decide(event, policy={},
                            approver=lambda request, cfg: seen.append(request) or "denied")
    assert decision["policy_decision"] == "ask"
    assert len(seen) == 1
    body = json.dumps(seen[0])
    assert "zz_marker_9f3a" not in body
    assert seen[0]["tool"] == "mcp__supabase__execute_query"
    assert seen[0]["session_id"] == "s" and seen[0]["risks"]


def test_huge_sql_argument_is_capped_and_fast(tmp_path):
    import time
    payload = "SELECT 1;" + "x = 'aaaaaaaa', " * 150000  # ~2 MB
    assert len(payload) > 2 * 1024 * 1024
    event = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__supabase__execute_query",
             "tool_use_id": "t1", "tool_input": {"query": payload}}
    start = time.monotonic()
    guard.handle(json.dumps(event))
    elapsed = time.monotonic() - start
    row = (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8").splitlines()[0]
    assert elapsed < 5.0
    assert len(row.encode("utf-8")) < 2048


# ---------------------------------------------------------------- robustness

@pytest.mark.parametrize("tool_input", [None, "just a string", ["a"], 5])
def test_odd_tool_input_types_do_not_crash(tmp_path, capsys, tool_input):
    event = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__x__delete_post",
             "tool_input": tool_input}
    rc = main(["guard", "test", json.dumps(event)])
    captured = capsys.readouterr()
    assert rc == 0 and "Traceback" not in captured.err


def test_missing_tool_input_and_odd_names_do_not_crash(tmp_path, capsys):
    for name in ["mcp__x__delete_post", "mcp__x__delete_post", "mcp__" + "a" * 10000,
                 "mcp__srv__șterge_tot"]:
        event = {"session_id": "s", "cwd": str(tmp_path), "tool_name": name}
        assert main(["guard", "test", json.dumps(event)]) == 0
        assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("tool", [None, 5, ["mcp__x__delete_post"]])
def test_non_string_tool_names_are_not_mcp(tool):
    assert mcp_parts(tool) is None
    assert _mcp_risks(tool) == []


@pytest.mark.parametrize("value", [["DROP TABLE t"], {"text": "DROP TABLE t"}, 5, None, True])
def test_non_string_sql_arguments_do_not_crash(value):
    risks = _mcp_risks("mcp__supabase__execute_query", query=value)
    assert len(risks) == 1 and risks[0].severity in ("low", "medium", "high")


# ---------------------------------------------------------------- the hook itself

def test_destructive_mcp_call_asks_and_logs_its_code(tmp_path):
    event = {"session_id": "sess-mcp", "cwd": str(tmp_path), "tool_name": "mcp__supabase__delete_project",
             "tool_use_id": "t1", "tool_input": {}}
    decision = guard.decide(event, policy={})
    assert decision["policy_decision"] == "ask" and decision["decision"] == "ask"
    assert [(r["code"], r["severity"]) for r in decision["risks"]] == [("mcp", "high")]
    out = guard.hook_output(decision)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert out["hookSpecificOutput"]["permissionDecisionReason"]
    assert guard.handle(json.dumps(event)) == out
    row = json.loads((tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8").splitlines()[0])
    assert row["decision"] == "ask" and row["codes"] == ["mcp"]


def test_state_changing_mcp_call_prints_nothing(tmp_path):
    event = {"session_id": "sess-mcp", "cwd": str(tmp_path), "tool_name": "mcp__x__create_post",
             "tool_use_id": "t1", "tool_input": {"text": "hi"}}
    assert guard.handle(json.dumps(event)) is None
    row = json.loads((tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8").splitlines()[0])
    assert row["decision"] == "allow" and row["codes"] == ["mcp"]


def test_guard_test_cli_shows_the_decision(tmp_path, capsys):
    event = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__supabase__delete_branch",
             "tool_input": {}}
    assert main(["guard", "test", json.dumps(event)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["decision"]["decision"] == "ask"
    assert printed["hook_output"]["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_log_of_a_destructive_call_stays_small(tmp_path):
    event = {"session_id": "s", "cwd": str(tmp_path), "tool_name": "mcp__s3__purge_bucket",
             "tool_use_id": "t1", "tool_input": {"bucket": "SECRET-ARG-MARKER"}}
    guard.handle(json.dumps(event))
    row = (tmp_path / ".runledger" / "guard.log").read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(row)["summary"] == "" and "SECRET-ARG-MARKER" not in row
    assert len(row.encode("utf-8")) < 1024
