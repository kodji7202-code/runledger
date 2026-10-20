"""`runledger stats`: one summary over the local sessions, its formats, the cost honesty
rules and the parse cache. Every test runs against an isolated HOME, so the machine's real
sessions and agent configs are never read."""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import ISOLATED_ENV_VARS
from runledger import adapters, gitinfo, pricing, stats
from runledger.cli import build, main
from runledger.parser import encode_project_path

MODEL = "claude-sonnet-4-5-20250929"
HAIKU = "claude-haiku-5-5"          # the priced model with a long-context tier
CODEX_MODEL = "gpt-5.2-codex"
UNPRICED = "mystery-model-9"


@pytest.fixture
def isolated_settings(monkeypatch):
    """The shared HOME/USERPROFILE isolation, with a short home.

    Claude encodes the project path into its session folder name and Droid encodes the
    working folder, so a home under the pytest tmp path grows both names. With a longer
    `--basetemp` (for example `$env:TEMP\\rlt-scr-local`, which pytest-xdist extends with
    `popen-gwN`) the fixtures then exceeded the Windows path limit and their setup failed
    with WinError 206. A short `%TEMP%\\rlsXXXXXX` home keeps every fixture path well under
    it; `tests/test_isolation.py` pins the shared fixture's own location."""
    home = Path(tempfile.mkdtemp(prefix="rls", dir=tempfile.gettempdir()))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ISOLATED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield home
    shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------- fixture writers

def _stamp(local_dt: datetime) -> str:
    """A session timestamp for a local time, written the way the agents write it (UTC)."""
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _started(days_ago: float) -> datetime:
    """Local noon `days_ago` days back: the local calendar day is what the tests expect."""
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    return day - timedelta(days=days_ago)


def _write(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def claude_session(home: Path, project: Path, session_id: str, started: datetime, *,
                   calls=(("Read", {"file_path": "a.py"}),), model: str = MODEL,
                   usage=(1000, 200, 0, 500), prompt: str = "Please change a.py"):
    """A Claude Code transcript: one user prompt and one assistant message per call."""
    folder = home / ".claude" / "projects" / encode_project_path(str(project))
    rows = [{"cwd": str(project), "sessionId": session_id, "version": "2.1.0", "gitBranch": "main",
             "timestamp": _stamp(started), "type": "user",
             "message": {"role": "user", "content": prompt}}]
    for i, (tool, tool_input) in enumerate(calls):
        rows.append({
            "cwd": str(project), "sessionId": session_id, "version": "2.1.0", "gitBranch": "main",
            "timestamp": _stamp(started + timedelta(seconds=i + 1)), "type": "assistant",
            "message": {"id": f"msg_{i}", "role": "assistant", "model": model,
                        "content": [{"type": "tool_use", "id": f"toolu_{i}", "name": tool,
                                     "input": tool_input}],
                        "usage": {"input_tokens": usage[0], "output_tokens": usage[1],
                                  "cache_creation_input_tokens": usage[2],
                                  "cache_read_input_tokens": usage[3]}}})
    return _write(folder / f"{session_id}.jsonl", rows)


def codex_session(home: Path, project: Path, session_id: str, started: datetime, *,
                  model: str = CODEX_MODEL, command: str = "pytest -q",
                  tokens=(500, 250, 400)):
    """A Codex rollout with one shell call and one cumulative token count."""
    rows = [
        {"timestamp": _stamp(started), "type": "session_meta",
         "payload": {"id": session_id, "cwd": str(project)}},
        {"timestamp": _stamp(started + timedelta(seconds=1)), "type": "turn_context",
         "payload": {"model": model, "cwd": str(project)}},
        {"timestamp": _stamp(started + timedelta(seconds=2)), "type": "response_item",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": json.dumps({"command": command}), "call_id": "c1"}},
        {"timestamp": _stamp(started + timedelta(seconds=3)), "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"input_tokens": tokens[0] + tokens[2],
                                   "cached_input_tokens": tokens[2],
                                   "output_tokens": tokens[1]},
             "last_token_usage": None}}},
    ]
    folder = home / ".codex" / "sessions" / "2026" / "01" / "02"
    return _write(folder / f"rollout-{session_id}.jsonl", rows)


def droid_session(home: Path, project: Path, session_id: str, started: datetime, *,
                  model: str = MODEL, command: str = "pytest -q", usage=(100, 40, 0, 20),
                  message_usage: bool = True):
    """A Factory Droid transcript with per-message usage and its settings file. With
    `message_usage=False` the messages carry no usage, so the settings file's cumulative
    `tokenUsage` is the only source (the sidecar a cache record must depend on)."""
    call = {"type": "message", "id": "call", "timestamp": _stamp(started + timedelta(seconds=1)),
            "message": {"role": "assistant", "modelId": model, "content": [
                {"type": "tool_use", "id": "call-1", "name": "Execute",
                 "input": {"command": command}}]}}
    if message_usage:
        call["message"]["usage"] = {"input_tokens": usage[0], "output_tokens": usage[1],
                                    "cache_creation_input_tokens": usage[2],
                                    "cache_read_input_tokens": usage[3]}
    rows = [
        {"type": "session_start", "id": session_id, "cwd": str(project), "version": 1},
        {"type": "message", "id": "request", "timestamp": _stamp(started),
         "message": {"role": "user", "content": [{"type": "text", "text": "Run the tests."}]}},
        call,
        {"type": "message", "id": "answer", "timestamp": _stamp(started + timedelta(seconds=2)),
         "message": {"role": "assistant", "openaiPhase": "final_answer",
                     "content": [{"type": "text", "text": "Tests passed."}]}},
    ]
    path = _write(_droid_folder(home, project) / f"{session_id}.jsonl", rows)
    droid_settings(path, model, usage)
    return path


def _droid_folder(home: Path, project: Path) -> Path:
    return home / ".factory" / "sessions" / str(project).replace("\\", "-").replace("/", "-")


def droid_settings(path: Path, model: str, usage) -> Path:
    """Write the sibling `.settings.json` beside a Droid transcript (rewriting it is how a
    test edits only the sidecar)."""
    settings = path.parent / f"{path.stem}.settings.json"
    settings.write_text(json.dumps({"model": model, "tokenUsage": {
        "inputTokens": usage[0], "outputTokens": usage[1]}}), encoding="utf-8")
    return settings


def droid_relative_session(home: Path, project: Path, session_id: str, started: datetime, *,
                           model: str = MODEL) -> Path:
    """A `droid exec --cwd .` transcript: the header records `.` and the only tool path is
    absolute, in a session folder name that decodes to nothing. The working folder can only
    come from the tool paths, which is the recovery that used to ask Git."""
    rows = [
        {"type": "session_start", "id": session_id, "cwd": ".", "version": 1},
        {"type": "message", "id": "request", "timestamp": _stamp(started),
         "message": {"role": "user", "content": [{"type": "text", "text": "Read the file."}]}},
        {"type": "message", "id": "call", "timestamp": _stamp(started + timedelta(seconds=1)),
         "message": {"role": "assistant", "modelId": model, "content": [
             {"type": "tool_use", "id": "call-1", "name": "Read",
              "input": {"file_path": str(project / "a.py")}}],
             "usage": {"input_tokens": 100, "output_tokens": 20}}},
    ]
    return _write(home / ".factory" / "sessions" / "relative" / f"{session_id}.jsonl", rows)


def claude_mixed_session(home: Path, project: Path, session_id: str, started: datetime,
                         messages):
    """A Claude transcript whose assistant messages name different models, one per
    `(model, usage)` pair: a session that used several models."""
    folder = home / ".claude" / "projects" / encode_project_path(str(project))
    rows = [{"cwd": str(project), "sessionId": session_id, "version": "2.1.0", "gitBranch": "main",
             "timestamp": _stamp(started), "type": "user",
             "message": {"role": "user", "content": "Please change a.py"}}]
    for i, (model, usage) in enumerate(messages):
        rows.append({
            "cwd": str(project), "sessionId": session_id, "version": "2.1.0", "gitBranch": "main",
            "timestamp": _stamp(started + timedelta(seconds=i + 1)), "type": "assistant",
            "message": {"id": f"msg_{i}", "role": "assistant", "model": model,
                        "content": [{"type": "tool_use", "id": f"toolu_{i}", "name": "Read",
                                     "input": {"file_path": f"f{i}.py"}}],
                        "usage": {"input_tokens": usage[0], "output_tokens": usage[1],
                                  "cache_creation_input_tokens": usage[2],
                                  "cache_read_input_tokens": usage[3]}}})
    return _write(folder / f"{session_id}.jsonl", rows)


def claude_subagent(parent: Path, name: str, started: datetime, messages) -> Path:
    """A sub-agent transcript of a Claude session: `<project>/<session>/subagents/<name>.jsonl`,
    one assistant message per `(model, usage)` pair, merged into the parent's usage."""
    folder = parent.parent / parent.stem / "subagents"
    rows = [{"cwd": str(parent.parent), "sessionId": parent.stem, "version": "2.1.0",
             "timestamp": _stamp(started + timedelta(seconds=i + 1)), "type": "assistant",
             "message": {"id": f"{name}-{i}", "role": "assistant", "model": model,
                         "content": [{"type": "text", "text": "Looking into it."}],
                         "usage": {"input_tokens": usage[0], "output_tokens": usage[1],
                                   "cache_creation_input_tokens": usage[2],
                                   "cache_read_input_tokens": usage[3]}}}
            for i, (model, usage) in enumerate(messages)]
    return _write(folder / f"{name}.jsonl", rows)


def guard_log(project: Path, rows) -> Path:
    path = project / ".runledger" / "guard.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def guard_record(when: datetime, decision: str, *, approval=None, session_id="s-1"):
    return {"ts": when.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "session_id": session_id, "tool": "Bash", "agent": "claude-code", "mode": "enforce",
            "tool_use_id": "", "decision": decision, "policy_decision": decision, "codes": [],
            "approval": approval, "summary": "pytest -q", "reason": "No deny or ask rule matched."}


@pytest.fixture
def fixture_a(isolated_settings):
    """The contract's "Fixture set A": P1 holds two Claude sessions (one 20 days old) and a
    60-day-old Codex session; P2 holds a Droid session."""
    home = isolated_settings
    p1 = home / "projects" / "p1"
    p2 = home / "projects" / "p2"
    p1.mkdir(parents=True)
    p2.mkdir(parents=True)
    paths = {
        "claude-today": claude_session(home, p1, "claude-today", _started(1),
                                       calls=(("Read", {"file_path": "a.py"}),
                                              ("Edit", {"file_path": "a.py", "old_string": "1",
                                                        "new_string": "2"}))),
        "claude-old": claude_session(home, p1, "claude-old", _started(20)),
        "codex-far": codex_session(home, p1, "codex-far", _started(60)),
        "droid-today": droid_session(home, p2, "droid-today", _started(1)),
    }
    return {"home": home, "p1": p1, "p2": p2, "paths": paths}


def _run(capsys, *args, cwd=None, monkeypatch=None):
    if cwd is not None and monkeypatch is not None:
        monkeypatch.chdir(cwd)
    rc = main(list(args))
    out, err = capsys.readouterr()
    return rc, out, err


def _json(capsys, *args, cwd=None, monkeypatch=None):
    rc, out, err = _run(capsys, *args, cwd=cwd, monkeypatch=monkeypatch)
    assert rc == 0, err
    return json.loads(out), err


def _without_generated(data: dict) -> dict:
    return {key: value for key, value in data.items() if key != "generated"}


def _row(table: str, key: str) -> str:
    for line in table.splitlines():
        if line.startswith(key) and "  " in line:
            return line
    raise AssertionError(f"no {key!r} row in:\n{table}")


def _parse_calls(monkeypatch) -> list:
    """Every session file `adapters.detect` is asked for, in order."""
    calls = []
    real = adapters.detect

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(adapters, "detect", counting)
    return calls


# ---------------------------------------------------------------- window parsing

def test_parse_since_units_and_dates():
    now = datetime(2026, 10, 19, 12, 0, 0)
    assert stats.parse_since("7d", now) == now - timedelta(days=7)
    assert stats.parse_since("4w", now) == now - timedelta(days=28)
    assert stats.parse_since("1m", now) == now - timedelta(days=30)
    assert stats.parse_since("2026-05-01", now) == datetime(2026, 5, 1)


@pytest.mark.parametrize("bad", ["banana", "7x", "2026-13-45", "", "  ", "7", "d7", "1y", "-3d"])
def test_parse_since_rejects_bad_values(bad):
    with pytest.raises(stats.SinceError) as exc:
        stats.parse_since(bad, datetime(2026, 10, 19))
    message = str(exc.value)
    assert "7d" in message and "4w" in message and "YYYY-MM-DD" in message


def test_parse_until_a_date_covers_the_whole_day():
    until = stats.parse_until("2026-05-01", datetime(2026, 10, 19))
    assert until.date().isoformat() == "2026-05-01"
    assert (until.hour, until.minute) == (23, 59)


def test_window_label_reads_naturally():
    now = datetime(2026, 10, 19, 12, 0, 0)
    assert stats.window_label(None, None, now - timedelta(days=30), now) == "last 30 days"
    assert stats.window_label("7d", None, now - timedelta(days=7), now) == "last 7 days"
    assert stats.window_label("2w", None, now - timedelta(days=14), now) == "last 2 weeks"
    assert stats.window_label("2026-09-24", None, datetime(2026, 9, 24), now) == "since 2026-09-24"
    assert (stats.window_label(None, "2026-10-09", now - timedelta(days=30),
                               datetime(2026, 10, 9, 23, 59, 59))
            == "last 30 days until 2026-10-09")


# ---------------------------------------------------------------- the command surface

def test_stats_help_lists_every_option(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["stats", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--project", "--all", "--agent", "--since", "--until", "--by", "--top",
                 "--format", "--no-cache", "--rebuild-cache", "--limit"):
        assert flag in out, flag
    assert "table" in out and "json" in out and "md" in out


def test_root_help_lists_stats(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "stats" in capsys.readouterr().out


def test_no_sessions_exits_one_like_list(capsys):
    rc, out, err = _run(capsys, "stats", "--all")
    assert rc == 1
    assert out == ""
    assert err.count("\n") == 1 and "No sessions found" in err and "Traceback" not in err
    assert main(["list", "--all"]) == 1
    capsys.readouterr()


def test_sessions_only_outside_the_window_hint_at_since(capsys, isolated_settings):
    project = isolated_settings / "old-project"
    project.mkdir()
    claude_session(isolated_settings, project, "old", _started(90))
    rc, out, err = _run(capsys, "stats", "--all")
    assert rc == 1
    assert out == ""
    assert "No sessions found" in err and "--since" in err and err.count("\n") == 1


def test_default_window_and_project_scope(capsys, fixture_a, monkeypatch):
    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert data["totals"]["sessions"] == 3  # the 60-day-old Codex session is out of the window
    assert data["scope"] == {"project": None, "agent": None}
    since = datetime.fromisoformat(data["window"]["since"])
    assert abs((datetime.now() - since) - timedelta(days=30)) < timedelta(days=1)

    data, _ = _json(capsys, "stats", "--format", "json", cwd=fixture_a["p1"], monkeypatch=monkeypatch)
    assert data["totals"]["sessions"] == 2
    assert data["scope"]["project"] == str(fixture_a["p1"])


def test_since_filters_by_session_start(capsys, fixture_a):
    data, _ = _json(capsys, "stats", "--all", "--since", "7d", "--format", "json")
    assert data["totals"]["sessions"] == 2  # the 20-day and 60-day sessions are excluded
    data, _ = _json(capsys, "stats", "--all", "--since", "30d", "--format", "json")
    assert data["totals"]["sessions"] == 3
    four_weeks, _ = _json(capsys, "stats", "--all", "--since", "4w", "--format", "json")
    twenty_eight, _ = _json(capsys, "stats", "--all", "--since", "28d", "--format", "json")
    assert four_weeks == twenty_eight
    one_month, _ = _json(capsys, "stats", "--all", "--since", "1m", "--format", "json")
    thirty, _ = _json(capsys, "stats", "--all", "--since", "30d", "--format", "json")
    assert one_month == thirty


def test_since_accepts_a_date_and_until_bounds_the_window(capsys, fixture_a):
    since = _started(25).date().isoformat()
    data, _ = _json(capsys, "stats", "--all", "--since", since, "--format", "json")
    assert data["totals"]["sessions"] == 3
    assert data["window"]["since"].startswith(since)

    until = _started(10).date().isoformat()
    data, _ = _json(capsys, "stats", "--all", "--since", since, "--until", until, "--format", "json")
    assert data["totals"]["sessions"] == 1  # only the 20-day-old Claude session
    assert data["window"]["until"].startswith(until)

    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    rc, out, err = _run(capsys, "stats", "--all", "--since", tomorrow)
    assert rc == 1 and out == "" and "No sessions found" in err


@pytest.mark.parametrize("bad", ["banana", "7x", "2026-13-45", ""])
def test_invalid_since_is_a_usage_error(capsys, bad):
    with pytest.raises(SystemExit) as exc:
        main(["stats", "--all", "--since", bad])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert bad in captured.err or repr(bad) in captured.err
    assert "7d" in captured.err and "4w" in captured.err and "YYYY-MM-DD" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("bad", ["banana", "2026-13-45"])
def test_invalid_until_is_a_usage_error(capsys, bad):
    with pytest.raises(SystemExit) as exc:
        main(["stats", "--all", "--until", bad])
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""


def test_agent_filter(capsys, fixture_a):
    data, _ = _json(capsys, "stats", "--all", "--agent", "codex", "--since", "90d",
                    "--format", "json")
    assert data["scope"]["agent"] == "codex"
    assert data["totals"]["sessions"] == 1
    assert [g["key"] for g in data["groups"]] == ["codex"]

    data, _ = _json(capsys, "stats", "--all", "--agent", "claude-code", "--since", "30d",
                    "--format", "json")
    assert data["totals"]["sessions"] == 2
    assert [g["key"] for g in data["groups"]] == ["claude-code"]

    data, _ = _json(capsys, "stats", "--all", "--agent", "droid", "--format", "json")
    assert data["totals"]["sessions"] == 1
    assert [g["key"] for g in data["groups"]] == ["droid"]

    rc, out, err = _run(capsys, "stats", "--all", "--agent", "aider")
    assert rc == 1 and out == "" and "No Aider sessions found" in err
    rc, out, err = _run(capsys, "stats", "--all", "--agent", "native")
    assert rc == 1 and out == ""

    with pytest.raises(SystemExit) as exc:
        main(["stats", "--all", "--agent", "foo"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    for choice in ("claude-code", "codex", "droid", "aider", "native"):
        assert choice in err


def test_project_filter(capsys, fixture_a):
    data, _ = _json(capsys, "stats", "--project", str(fixture_a["p1"]), "--format", "json")
    assert data["scope"]["project"] == str(fixture_a["p1"])
    assert data["totals"]["sessions"] == 2
    data, _ = _json(capsys, "stats", "--project", str(fixture_a["p2"]), "--format", "json")
    assert data["totals"]["sessions"] == 1
    rc, out, err = _run(capsys, "stats", "--project", str(fixture_a["home"] / "nowhere"))
    assert rc == 1 and out == "" and "No sessions found" in err


def test_agents_are_aggregated_together(capsys, fixture_a):
    data, _ = _json(capsys, "stats", "--all", "--since", "7d", "--format", "json")
    assert [g["key"] for g in data["groups"]] == ["claude-code", "droid"]
    totals = data["totals"]
    assert totals["sessions"] == sum(g["sessions"] for g in data["groups"]) == 2
    for field in ("input", "output", "cache_read", "cache_write"):
        assert totals["tokens"][field] == sum(g["tokens"][field] for g in data["groups"])
    assert totals["tokens"]["input"] == 2 * 1000 + 100
    assert totals["tokens"]["output"] == 2 * 200 + 40
    assert totals["tokens"]["cache_read"] == 2 * 500 + 20
    assert totals["steps"] == sum(g["steps"] for g in data["groups"])

    table = _run(capsys, "stats", "--all", "--since", "7d")[1]
    header = table.splitlines()[0]
    assert "2 agents" in header and "2 sessions" in header
    assert _row(table, "claude-code") and _row(table, "total")


def test_totals_cost_matches_the_price_table(capsys, fixture_a):
    data, _ = _json(capsys, "stats", "--all", "--agent", "claude-code", "--since", "30d",
                    "--format", "json")
    rate = pricing.price_for(MODEL)
    assert rate, "the fixture model must be priced"
    unit = 1000 * rate[0] + 200 * rate[1] + 500 * rate[2]
    assert data["totals"]["tokens"]["cache_write"] == 0
    assert data["totals"]["cost"] == pytest.approx(2 * 2 * unit / 1_000_000, abs=0.01)
    assert data["totals"]["cost_partial"] is False
    assert data["partial"] == {"sessions": 0, "unknown_models": []}
    table = _run(capsys, "stats", "--all", "--agent", "claude-code")[1]
    cost_cell = _row(table, "total").split()[-1]
    assert cost_cell.startswith("$") and ">=" not in cost_cell and "n/a" not in cost_cell


def test_grouping_by_model_day_week_and_project(capsys, fixture_a):
    data, _ = _json(capsys, "stats", "--all", "--since", "90d", "--by", "model", "--format", "json")
    assert sorted(g["key"] for g in data["groups"]) == sorted([MODEL, CODEX_MODEL])
    assert sum(g["sessions"] for g in data["groups"]) == data["totals"]["sessions"] == 4
    assert sum(g["tokens"]["input"] for g in data["groups"]) == data["totals"]["tokens"]["input"]

    data, _ = _json(capsys, "stats", "--all", "--by", "day", "--since", "30d", "--format", "json")
    keys = [g["key"] for g in data["groups"]]
    assert keys == sorted(keys) and len(keys) == 2  # the two 1-day-old sessions share a day
    assert all(len(key) == 10 and key[4] == "-" for key in keys)
    assert sum(g["sessions"] for g in data["groups"]) == 3

    data, _ = _json(capsys, "stats", "--all", "--by", "week", "--since", "30d", "--format", "json")
    keys = [g["key"] for g in data["groups"]]
    assert keys == sorted(keys) and len(keys) == 2
    assert all("-W" in key for key in keys)
    assert sum(g["sessions"] for g in data["groups"]) == 3

    data, _ = _json(capsys, "stats", "--all", "--by", "project", "--since", "30d", "--format", "json")
    assert sorted(g["key"] for g in data["groups"]) == sorted([str(fixture_a["p1"]),
                                                               str(fixture_a["p2"])])
    assert sum(g["sessions"] for g in data["groups"]) == 3

    with pytest.raises(SystemExit) as exc:
        main(["stats", "--all", "--by", "nonsense"])
    assert exc.value.code == 2
    capsys.readouterr()

    assert "top models" in _run(capsys, "stats", "--all")[1]


def test_risk_distribution_and_top_codes(capsys, isolated_settings):
    project = isolated_settings / "risky"
    project.mkdir()
    danger = claude_session(isolated_settings, project, "danger", _started(1),
                            calls=(("Bash", {"command": "rm -rf /"}),
                                   ("Bash", {"command": "git push --force origin main"})))
    claude_session(isolated_settings, project, "mcp", _started(1),
                   calls=(("mcp__x__get_me", {}),))
    claude_session(isolated_settings, project, "clean", _started(1))
    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    risk = data["risk"]
    assert risk["low"] + risk["medium"] + risk["high"] == data["totals"]["sessions"] == 3
    assert risk["high"] >= 1 and risk["low"] >= 1
    assert isinstance(risk["avg_score"], (int, float)) and risk["avg_score"] > 0
    assert risk["top_codes"] and all(len(pair) == 2 for pair in risk["top_codes"])
    counts = [count for _, count in risk["top_codes"]]
    assert counts == sorted(counts, reverse=True)
    assert len(risk["top_codes"]) <= 5

    limited, _ = _json(capsys, "stats", "--all", "--top", "1", "--format", "json")
    assert len(limited["risk"]["top_codes"]) == 1
    assert len(limited["quality"]["top_recommendations"]) <= 1

    # the same session through `receipt` reports the same score, level and codes
    run, score, level, risks, _ = build(str(danger))
    summary = stats.summarize(danger)
    assert (summary.score, summary.level) == (score, level.lower())
    assert set(summary.codes) == {r.code for r in risks}
    for code, count in summary.codes.items():
        assert count == sum(1 for r in risks if r.code == code)


def test_quality_grades_and_recommendations(capsys, isolated_settings):
    project = isolated_settings / "quality"
    project.mkdir()
    claude_session(isolated_settings, project, "tests-first", _started(1), calls=(
        ("Edit", {"file_path": "a.py", "old_string": "1", "new_string": "2"}),
        ("Edit", {"file_path": "tests/test_a.py", "old_string": "1", "new_string": "2"}),
        ("Bash", {"command": "pytest -q"}),))
    plain = claude_session(isolated_settings, project, "no-tests", _started(1), model=UNPRICED,
                           calls=(("Edit", {"file_path": "b.py", "old_string": "1",
                                            "new_string": "2"}),))
    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    quality = data["quality"]
    assert sum(quality["grades"].values()) == data["totals"]["sessions"] == 2
    assert len(quality["grades"]) >= 2
    assert quality["top_recommendations"]
    counts = [count for _, count in quality["top_recommendations"]]
    assert counts == sorted(counts, reverse=True)

    run, _, _, risks, _ = build(str(plain))
    assert stats.summarize(plain).grade == run.quality["grade"]
    assert run.quality["grade"] in quality["grades"]


def test_output_formats(capsys, fixture_a):
    table, _ = _run(capsys, "stats", "--all")[1:]
    assert "RunLedger stats" in table and "\x1b[" not in table

    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert data["version"] == 1
    for key in ("generated", "scope", "window", "totals", "groups", "risk", "quality",
                "guard", "partial", "skipped"):
        assert key in data

    md = _run(capsys, "stats", "--all", "--format", "md")[1]
    md_lines = [line for line in md.splitlines() if line.startswith("|")]
    assert len(md_lines) >= 3
    assert any("---" in line and line.startswith("|") for line in md_lines)
    assert str(data["totals"]["sessions"]) in md
    assert data["totals"]["cost"] is not None and "n/a" not in md and ">=" not in md

    with pytest.raises(SystemExit) as exc:
        main(["stats", "--all", "--format", "xml"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_json_stdout_stays_clean_with_progress_and_truncation(capsys, isolated_settings):
    project = isolated_settings / "many"
    project.mkdir()
    for i in range(30):
        claude_session(isolated_settings, project, f"session-{i}", _started(1 + (i % 20)))

    rc, out, err = _run(capsys, "stats", "--all", "--format", "json")
    assert rc == 0
    data = json.loads(out)
    assert data["totals"]["sessions"] == 30
    assert "parsing" in err and "{" not in err

    rc, out, err = _run(capsys, "stats", "--all", "--limit", "4", "--format", "json")
    assert rc == 0
    assert json.loads(out)["totals"]["sessions"] == 4
    assert "limit" in err.lower()


def test_unknown_cost_is_never_a_fake_zero(capsys, isolated_settings):
    project = isolated_settings / "unpriced"
    project.mkdir()
    claude_session(isolated_settings, project, "claude", _started(1), model=UNPRICED)
    codex_session(isolated_settings, project, "codex", _started(1), model=UNPRICED)
    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert data["totals"]["cost"] is None
    assert all(group["cost"] is None for group in data["groups"])
    assert data["partial"]["unknown_models"] == [UNPRICED]
    assert data["partial"]["sessions"] == 2

    table = _run(capsys, "stats", "--all")[1]
    assert "n/a" in _row(table, "total") and UNPRICED in table
    assert "$0.00" not in table and "$0.0000" not in table

    md = _run(capsys, "stats", "--all", "--format", "md")[1]
    assert "n/a" in md and UNPRICED in md and "$0.00" not in md


def test_partial_cost_is_a_lower_bound(capsys, isolated_settings):
    project = isolated_settings / "mixed"
    project.mkdir()
    claude_session(isolated_settings, project, "claude", _started(1))
    codex_session(isolated_settings, project, "codex-priced", _started(1), model=MODEL)
    codex_session(isolated_settings, project, "codex-unpriced", _started(1), model=UNPRICED)
    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert data["totals"]["cost_partial"] is True
    assert data["partial"]["sessions"] == 1
    assert data["partial"]["unknown_models"] == [UNPRICED]

    table = _run(capsys, "stats", "--all")[1]
    codex_row = _row(table, "codex")
    assert ">=" in codex_row and "n/a" not in codex_row
    assert ">=" in _row(table, "total")
    assert ">=" not in _row(table, "claude-code")
    assert UNPRICED in table

    md = _run(capsys, "stats", "--all", "--format", "md")[1]
    assert ">=" in md and UNPRICED in md


def test_guard_decision_totals(capsys, fixture_a):
    guard_log(fixture_a["p1"], [
        guard_record(_started(1), "allow"),
        guard_record(_started(1), "allow"),
        guard_record(_started(1), "allow"),
        guard_record(_started(1), "ask", approval="approved"),
        guard_record(_started(1), "ask"),
        guard_record(_started(1), "deny"),
        guard_record(_started(40), "deny"),
    ])
    data, _ = _json(capsys, "stats", "--all", "--since", "30d", "--format", "json")
    assert data["guard"] == {"allow": 3, "ask": 2, "deny": 1, "approved": 1, "error": 0}
    guard_line = _row(_run(capsys, "stats", "--all", "--since", "30d")[1], "guard")
    assert "allow 3" in guard_line and "ask 2" in guard_line and "deny 1" in guard_line

    guard_line = _row(_run(capsys, "stats", "--project", str(fixture_a["p2"]))[1], "guard")
    assert "allow 0" in guard_line and "deny 0" in guard_line


def test_stats_runs_no_git(capsys, fixture_a, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("stats must not run git")

    for name in ("toplevel", "current_branch", "session_base", "working_changes", "file_diffs"):
        monkeypatch.setattr(gitinfo, name, boom)
    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert data["totals"]["sessions"] == 3


def test_discovery_does_not_walk_up_to_git_for_aider(capsys, isolated_settings, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("stats must not run git")

    monkeypatch.setattr(gitinfo, "toplevel", boom)
    project = isolated_settings / "no-aider"
    project.mkdir()
    assert stats.discover(str(project), None) == []
    history = project / ".aider.chat.history.md"
    history.write_text("# aider chat started at 2026-10-18 10:00:00\n", encoding="utf-8")
    assert stats.discover(str(project), None) == [history]


def test_stats_is_read_only(capsys, fixture_a):
    def snapshot(root: Path):
        out = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                stat = path.stat()
                out[str(path)] = (stat.st_size, stat.st_mtime_ns, path.read_bytes()[:2048])
        return out

    agents = snapshot(fixture_a["home"] / ".claude") | snapshot(fixture_a["home"] / ".codex")
    project = snapshot(fixture_a["p1"])
    cwd = fixture_a["home"] / "cwd"
    cwd.mkdir()
    before_cwd = snapshot(cwd)

    assert main(["stats", "--all"]) == 0
    capsys.readouterr()
    assert snapshot(fixture_a["home"] / ".claude") | snapshot(fixture_a["home"] / ".codex") == agents
    assert snapshot(fixture_a["p1"]) == project
    assert snapshot(cwd) == before_cwd
    written = [str(p) for p in (fixture_a["home"] / ".runledger").rglob("*") if p.is_file()]
    assert written and all(".runledger" in path and "cache" in path for path in written)


def test_a_corrupt_session_is_skipped(capsys, fixture_a):
    folder = fixture_a["home"] / ".claude" / "projects" / encode_project_path(str(fixture_a["p1"]))
    (folder / "broken.jsonl").write_text("not json at all\n{truncated\n", encoding="utf-8")
    data, _ = _json(capsys, "stats", "--all", "--since", "30d", "--format", "json")
    assert data["totals"]["sessions"] == 3
    assert len(data["skipped"]) == 1
    skipped = data["skipped"][0]
    assert skipped["path"].endswith("broken.jsonl") and skipped["reason"]
    table = _run(capsys, "stats", "--all", "--since", "30d")[1]
    assert "broken.jsonl" in table or "skipped" in table


def test_an_empty_file_is_skipped_but_a_step_less_session_is_counted(capsys, fixture_a):
    folder = fixture_a["home"] / ".claude" / "projects" / encode_project_path(str(fixture_a["p1"]))
    (folder / "empty.jsonl").write_text("", encoding="utf-8")
    data, _ = _json(capsys, "stats", "--all", "--since", "30d", "--format", "json")
    assert data["totals"]["sessions"] == 3
    assert [Path(entry["path"]).name for entry in data["skipped"]] == ["empty.jsonl"]

    # a session file that parses but holds no step is still a session (as `list` reports it)
    (folder / "header-only.jsonl").write_text(
        json.dumps({"cwd": str(fixture_a["p1"]), "sessionId": "header-only",
                    "timestamp": _stamp(_started(1))}) + "\n", encoding="utf-8")
    data, _ = _json(capsys, "stats", "--all", "--since", "30d", "--format", "json")
    assert data["totals"]["sessions"] == 4
    assert [Path(entry["path"]).name for entry in data["skipped"]] == ["empty.jsonl"]


def test_limit_truncates_to_the_newest_sessions(capsys, isolated_settings):
    project = isolated_settings / "limit"
    project.mkdir()
    for i in range(1, 6):
        claude_session(isolated_settings, project, f"s{i}", _started(i))
    data, err = _json(capsys, "stats", "--all", "--limit", "3", "--format", "json")
    assert data["totals"]["sessions"] == 3
    assert len(data["groups"]) == 1
    assert "limit" in err.lower()
    plain, err = _json(capsys, "stats", "--all", "--format", "json")
    assert plain["totals"]["sessions"] == 5 and "limit" not in err.lower()


# ---------------------------------------------------------------- the parse cache

def test_cache_is_written_and_a_second_run_reuses_it(capsys, fixture_a, monkeypatch):
    cache_file = fixture_a["home"] / ".runledger" / "cache" / "stats-v1.json"
    calls = _parse_calls(monkeypatch)
    first, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert cache_file.is_file()
    assert len(calls) >= 3
    calls.clear()
    second, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert calls == []
    assert _without_generated(second) == _without_generated(first)
    assert json.loads(cache_file.read_text(encoding="utf-8"))["version"] == 1


def test_the_window_is_resolved_to_the_minute(capsys, fixture_a):
    """Two runs in the same minute report the same window, so a cached report and an
    uncached one are comparable (and a session started seconds ago is inside it)."""
    first, _ = _json(capsys, "stats", "--all", "--format", "json")
    second, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert first["window"] == second["window"]
    assert _without_generated(first) == _without_generated(second)
    until = datetime.fromisoformat(first["window"]["until"])
    assert datetime.now() <= until <= datetime.now() + timedelta(minutes=1)
    since = datetime.fromisoformat(first["window"]["since"])
    assert (until - since) == timedelta(days=30, minutes=1)


def test_no_cache_reads_and_writes_nothing(capsys, fixture_a, monkeypatch):
    cache_file = fixture_a["home"] / ".runledger" / "cache" / "stats-v1.json"
    calls = _parse_calls(monkeypatch)
    data, _ = _json(capsys, "stats", "--all", "--no-cache", "--format", "json")
    assert not cache_file.exists()
    assert data["totals"]["sessions"] == 3
    calls.clear()
    assert _json(capsys, "stats", "--all", "--format", "json")[0]["totals"] == data["totals"]
    assert len(calls) >= 3  # --no-cache left nothing behind, so the next run parses


def test_rebuild_cache_parses_everything_again(capsys, fixture_a, monkeypatch):
    cache_file = fixture_a["home"] / ".runledger" / "cache" / "stats-v1.json"
    calls = _parse_calls(monkeypatch)
    first, _ = _json(capsys, "stats", "--all", "--format", "json")
    calls.clear()
    again, _ = _json(capsys, "stats", "--all", "--rebuild-cache", "--format", "json")
    assert len(calls) >= 3
    assert _without_generated(again) == _without_generated(first)
    assert json.loads(cache_file.read_text(encoding="utf-8"))["version"] == 1
    calls.clear()
    assert _json(capsys, "stats", "--all", "--format", "json")[0]["totals"] == first["totals"]
    assert calls == []


def test_cache_follows_size_and_mtime(capsys, fixture_a, monkeypatch):
    path = fixture_a["paths"]["claude-today"]
    first, _ = _json(capsys, "stats", "--all", "--format", "json")
    baseline = first["totals"]["tokens"]["input"]

    extra = {"cwd": str(fixture_a["p1"]), "sessionId": "claude-today", "version": "2.1.0",
             "timestamp": _stamp(_started(1) + timedelta(seconds=9)), "type": "assistant",
             "message": {"id": "msg_x", "role": "assistant", "model": MODEL,
                         "content": [{"type": "tool_use", "id": "toolu_x", "name": "Read",
                                      "input": {"file_path": "b.py"}}],
                         "usage": {"input_tokens": 7000, "output_tokens": 300,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0}}}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(extra) + "\n")
    grown, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert grown["totals"]["tokens"]["input"] == baseline + 7000

    original = path.read_bytes()
    changed = original.replace(b'"input_tokens": 7000', b'"input_tokens": 8000')
    assert len(changed) == len(original)
    path.write_bytes(changed)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    bumped, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert bumped["totals"]["tokens"]["input"] == baseline + 8000


def test_cache_is_reused_while_path_mtime_and_size_are_unchanged(capsys, fixture_a, monkeypatch):
    path = fixture_a["paths"]["claude-today"]
    first, _ = _json(capsys, "stats", "--all", "--format", "json")
    stat = path.stat()
    original = path.read_bytes()
    replacement = original.replace(b'"input_tokens": 1000', b'"input_tokens": 9000')
    assert len(replacement) == len(original)
    path.write_bytes(replacement)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    calls = _parse_calls(monkeypatch)
    unchanged, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert calls == []
    assert unchanged["totals"] == first["totals"]

    (fixture_a["home"] / ".runledger" / "cache" / "stats-v1.json").unlink()
    fresh, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert fresh["totals"]["tokens"]["input"] == first["totals"]["tokens"]["input"] + 2 * 8000
    assert (fixture_a["home"] / ".runledger" / "cache" / "stats-v1.json").is_file()


def test_cache_holds_no_prompts_or_commands(capsys, isolated_settings):
    project = isolated_settings / "private"
    project.mkdir()
    claude_session(isolated_settings, project, "private", _started(1),
                   prompt="PROMPT-MARKER-8f21",
                   calls=(("Bash", {"command": "echo COMMAND-MARKER-4c19"}),))
    assert main(["stats", "--all"]) == 0
    capsys.readouterr()
    cache = (isolated_settings / ".runledger" / "cache" / "stats-v1.json").read_text(encoding="utf-8")
    assert "PROMPT-MARKER-8f21" not in cache
    assert "COMMAND-MARKER-4c19" not in cache
    assert "private" in cache  # session ids, model ids and cwds are what it keeps


def test_price_change_recomputes_cost_from_cached_tokens(capsys, fixture_a, monkeypatch, tmp_path):
    calls = _parse_calls(monkeypatch)
    first, _ = _json(capsys, "stats", "--all", "--agent", "claude-code", "--format", "json")
    calls.clear()
    rate = pricing.price_for(MODEL)
    override = tmp_path / "prices.json"
    override.write_text(json.dumps({"sonnet:4.5": {"input": rate[0] * 2, "output": rate[1] * 2,
                                                   "cache_read": rate[2] * 2}}), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_PRICES", str(override))
    monkeypatch.setattr(pricing, "PRICES", pricing.load_prices(str(override)))
    doubled, _ = _json(capsys, "stats", "--all", "--agent", "claude-code", "--format", "json")
    assert calls == []  # the cache still holds the tokens
    assert doubled["totals"]["tokens"] == first["totals"]["tokens"]
    assert doubled["totals"]["cost"] == pytest.approx(first["totals"]["cost"] * 2, rel=0.02)

    monkeypatch.setattr(pricing, "PRICES", {})
    unknown, _ = _json(capsys, "stats", "--all", "--agent", "claude-code", "--format", "json")
    assert unknown["totals"]["cost"] is None
    assert unknown["partial"]["unknown_models"] == [MODEL]
    assert calls == []


def test_corrupt_or_unknown_cache_is_ignored_and_rewritten(capsys, fixture_a):
    cache_file = fixture_a["home"] / ".runledger" / "cache" / "stats-v1.json"
    reference, _ = _json(capsys, "stats", "--all", "--format", "json")
    for bad in ("{not json", json.dumps({"version": 999, "entries": {"x": {}}}), ""):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(bad, encoding="utf-8")
        data, _ = _json(capsys, "stats", "--all", "--format", "json")
        assert _without_generated(data) == _without_generated(reference)
        rewritten = json.loads(cache_file.read_text(encoding="utf-8"))
        assert rewritten["version"] == 1 and rewritten["entries"]


def test_project_policy_change_invalidates_cached_risk(capsys, isolated_settings):
    project = isolated_settings / "policy"
    project.mkdir()
    session = claude_session(isolated_settings, project, "vault", _started(1),
                             calls=(("Read", {"file_path": "vault-notes.txt"}),))
    before, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert before["risk"]["low"] == 1
    assert "secret_file" not in [code for code, _ in before["risk"]["top_codes"]]

    (project / ".runledger.json").write_text(
        json.dumps({"extra_secret_paths": ["vault-notes\\.txt$"]}), encoding="utf-8")
    after, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert after["risk"]["low"] == 0
    assert "secret_file" in [code for code, _ in after["risk"]["top_codes"]]
    assert stats.summarize(session).score == after["risk"]["avg_score"]


# ---------------------------------------------------------------- the documentation

def test_docs_stats_md_matches_the_command(capsys):
    doc = (Path(__file__).resolve().parent.parent / "docs" / "stats.md")
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")

    with pytest.raises(SystemExit):
        main(["stats", "--help"])
    help_text = capsys.readouterr().out
    for flag in ("--project", "--all", "--agent", "--since", "--until", "--by", "--top",
                 "--format", "--no-cache", "--rebuild-cache", "--limit"):
        assert flag in text and flag in help_text
    for default in ("--since 30d", "--top 5", "--limit 500"):
        assert default in text
    for needed in ("n/a", ">=", "cache", "stats-v1.json", "exit", "table", "json", "md"):
        assert needed in text
    assert "read-only" in text.lower() or "read only" in text.lower()


# ---------------------------------------------------------------- round-1 fixes

def _no_process_spy(monkeypatch) -> list:
    """Every way RunLedger could start a child process, made fatal: `stats` is a pure local
    reader, so no `git`, no shell and no helper may be spawned by any of its paths."""
    started = []

    def boom(*args, **kwargs):
        started.append(str(args[0]) if args else "?")
        raise AssertionError("stats must not start a process")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        if hasattr(subprocess, name):
            monkeypatch.setattr(subprocess, name, boom)
    for name in ("system", "popen", "spawnv", "spawnl", "execv"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, boom)
    return started


def test_request_tiered_cost_matches_the_receipt_cold_and_warm(capsys, isolated_settings,
                                                               monkeypatch):
    """Two individually short 60000-token Haiku requests cost $0.012, the same figure
    `runledger receipt` reports: each request stays under the long-context threshold, so
    pricing the session's aggregate 120000 tokens as one request would report $0.06."""
    project = isolated_settings / "tiered"
    project.mkdir()
    session = claude_session(isolated_settings, project, "tiered", _started(1), model=HAIKU,
                             usage=(60000, 0, 0, 0),
                             calls=(("Read", {"file_path": "a.py"}),
                                    ("Read", {"file_path": "b.py"})))
    assert pricing.has_long_context(HAIKU), "the fixture model needs a long-context tier"
    run, _, _, _, _ = build(str(session))
    assert run.cost == pytest.approx(0.012, abs=1e-9)

    cold, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert cold["totals"]["tokens"]["input"] == 120000
    assert cold["totals"]["cost"] == pytest.approx(0.012, abs=1e-9)
    assert cold["totals"]["cost_partial"] is False

    calls = _parse_calls(monkeypatch)
    warm, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert calls == []  # the second run came from the cache
    assert warm["totals"] == cold["totals"]


def test_stats_starts_no_process_for_a_relative_droid_cwd(capsys, isolated_settings, monkeypatch):
    started = _no_process_spy(monkeypatch)
    project = isolated_settings / "relative"
    (project / ".git").mkdir(parents=True)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = droid_relative_session(isolated_settings, project, "relative", _started(1))

    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert started == []
    assert data["totals"]["sessions"] == 1
    assert [g["key"] for g in data["groups"]] == ["droid"]

    run = adapters.get("droid").parse(session, git_root=False)
    assert (run.cwd_source, os.path.normcase(run.cwd or "")) == (
        "session", os.path.normcase(str(project)))
    assert os.path.normcase(stats.summarize(session).cwd or "") == os.path.normcase(str(project))
    by_project, _ = _json(capsys, "stats", "--all", "--by", "project", "--format", "json")
    assert [os.path.normcase(g["key"]) for g in by_project["groups"]] == \
        [os.path.normcase(str(project))]


def test_stats_starts_no_process_for_explicit_aider_discovery(capsys, isolated_settings,
                                                              monkeypatch):
    started = _no_process_spy(monkeypatch)
    project = isolated_settings / "aider-project"
    sub = project / "src"
    sub.mkdir(parents=True)

    rc, out, err = _run(capsys, "stats", "--agent", "aider", "--project", str(sub))
    assert started == []
    assert rc == 1 and out == "" and "No Aider sessions found" in err

    history = project / ".aider.chat.history.md"
    history.write_text("# aider chat started at 2026-10-18 10:00:00\n", encoding="utf-8")
    assert started == []
    assert stats.discover(str(project), "aider") == [history]
    assert stats.discover(str(sub), "aider") == []  # no walk up to the Git top level


def test_grouping_by_model_splits_a_mixed_model_session(capsys, isolated_settings, monkeypatch):
    """A session that used two models contributes to both rows: 2000 Sonnet tokens and
    1000 Haiku tokens are two groups, each priced as its own model, and the totals are
    still the sum of the rows."""
    project = isolated_settings / "mixed-models"
    project.mkdir()
    session = claude_mixed_session(isolated_settings, project, "mixed", _started(1),
                                   [(MODEL, (2000, 0, 0, 0)), (HAIKU, (1000, 0, 0, 0))])
    run, _, _, _, _ = build(str(session))
    per_model = {model: pricing.cost_of(usage, model) for model, usage in run.models.items()}
    assert set(per_model) == {MODEL, HAIKU}

    data, _ = _json(capsys, "stats", "--all", "--by", "model", "--format", "json")
    groups = {group["key"]: group for group in data["groups"]}
    assert sorted(groups) == sorted([MODEL, HAIKU])
    assert groups[MODEL]["tokens"]["input"] == 2000
    assert groups[HAIKU]["tokens"]["input"] == 1000
    assert groups[MODEL]["cost"] == pytest.approx(per_model[MODEL], abs=1e-9)
    assert groups[HAIKU]["cost"] == pytest.approx(per_model[HAIKU], abs=1e-9)
    assert data["totals"]["cost"] == pytest.approx(sum(per_model.values()), abs=1e-9)
    assert sum(group["cost"] for group in data["groups"]) == pytest.approx(
        data["totals"]["cost"], abs=1e-9)
    assert sum(group["tokens"]["input"] for group in data["groups"]) == \
        data["totals"]["tokens"]["input"] == 3000
    assert sum(group["sessions"] for group in data["groups"]) == data["totals"]["sessions"] == 1
    assert sum(group["steps"] for group in data["groups"]) == data["totals"]["steps"] == 2

    calls = _parse_calls(monkeypatch)
    warm, _ = _json(capsys, "stats", "--all", "--by", "model", "--format", "json")
    assert calls == []
    assert warm["groups"] == data["groups"]


def test_a_sidecar_change_invalidates_the_cached_summary(capsys, isolated_settings, monkeypatch):
    """The Droid settings file contributes the session's usage, so editing only it must
    invalidate the cached record (a warm run used to keep the old totals)."""
    project = isolated_settings / "sidecar"
    project.mkdir()
    session = droid_session(isolated_settings, project, "sidecar", _started(1), model=MODEL,
                            usage=(1000, 0, 0, 0), message_usage=False)
    cold, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert cold["totals"]["tokens"]["input"] == 1000
    assert cold["totals"]["cost"] == pytest.approx(0.003, abs=1e-9)

    calls = _parse_calls(monkeypatch)
    warm, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert calls == [] and warm["totals"] == cold["totals"]

    droid_settings(session, MODEL, (9000, 0, 0, 0))
    calls.clear()
    after, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert calls == [str(session)]
    assert after["totals"]["tokens"]["input"] == 9000
    assert after["totals"]["cost"] == pytest.approx(0.027, abs=1e-9)

    fresh, _ = _json(capsys, "stats", "--all", "--no-cache", "--format", "json")
    assert fresh["totals"] == after["totals"]


def test_a_subagent_change_invalidates_the_cached_summary(capsys, isolated_settings, monkeypatch):
    """A Claude session's totals include its sub-agent transcripts, so changing one of
    those files (not the transcript) must invalidate the cached record."""
    project = isolated_settings / "subagents"
    project.mkdir()
    parent = claude_session(isolated_settings, project, "parent", _started(1))
    claude_subagent(parent, "agent-1", _started(1), [(MODEL, (500, 0, 0, 0))])
    cold, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert cold["totals"]["tokens"]["input"] == 1000 + 500

    calls = _parse_calls(monkeypatch)
    warm, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert calls == [] and warm["totals"] == cold["totals"]

    claude_subagent(parent, "agent-1", _started(1),
                    [(MODEL, (500, 0, 0, 0)), (MODEL, (250, 0, 0, 0))])
    calls.clear()
    after, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert calls == [str(parent)]
    assert after["totals"]["tokens"]["input"] == 1000 + 750


def test_grouping_by_model_keeps_a_session_with_no_model(capsys, fixture_a):
    """A session that holds no usage at all keeps a row of its own under `--by model`."""
    folder = fixture_a["home"] / ".claude" / "projects" / encode_project_path(str(fixture_a["p1"]))
    (folder / "header-only.jsonl").write_text(
        json.dumps({"cwd": str(fixture_a["p1"]), "sessionId": "header-only",
                    "timestamp": _stamp(_started(1))}) + "\n", encoding="utf-8")
    data, _ = _json(capsys, "stats", "--all", "--since", "30d", "--by", "model", "--format", "json")
    groups = {group["key"]: group for group in data["groups"]}
    assert groups["(no model)"]["sessions"] == 1
    assert groups["(no model)"]["cost"] is None
    assert sum(group["sessions"] for group in data["groups"]) == data["totals"]["sessions"] == 4


def test_a_record_with_malformed_request_numbers_is_reparsed(capsys, fixture_a):
    """A record whose request numbers are not readable is dropped, never priced."""
    cache_file = fixture_a["home"] / ".runledger" / "cache" / "stats-v1.json"
    reference, _ = _json(capsys, "stats", "--all", "--format", "json")
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    key = next(iter(raw["entries"]))
    raw["entries"][key]["models"] = {"m": {"input": 1, "requests": ["nonsense"]}}
    cache_file.write_text(json.dumps(raw), encoding="utf-8")
    data, _ = _json(capsys, "stats", "--all", "--format", "json")
    assert _without_generated(data) == _without_generated(reference)


def test_fixture_paths_stay_short_enough_for_encoded_folders(isolated_settings):
    """Claude encodes the project path into its session folder name and Droid encodes the
    working folder, so the fixtures must not live under a long pytest tmp path (a longer
    `--basetemp` used to make their setup fail with WinError 206)."""
    project = isolated_settings / "projects" / "p1"
    project.mkdir(parents=True)
    claude = claude_session(isolated_settings, project, "short", _started(1))
    droid = droid_session(isolated_settings, project, "short", _started(1))
    assert max(len(str(claude)), len(str(droid))) < 220
