"""Tests for the quality, recommendation and AI review fields of a pushed receipt, and for the
Insights summary (GET /api/insights), the runs filters that use them, the exports, and the
dashboard's Insights view.

Receipts are pushed to a real server on a free port with a temporary database. The clock is
fixed at 2026-10-15 12:00 UTC by patching runledger.server.budgets.now.
"""
import copy
import csv
import http.client
import io
import json
import re
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from runledger import client
from runledger.pricing import friendly_model
from runledger.server import budgets, exports, insights
from runledger.server.app import make_server, receipt_to_run
from runledger.server.dashboard import DASHBOARD_HTML
from runledger.server.db import SCHEMA, Database

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
FIXED_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
CSRF = {"X-Requested-With": "runledger"}
SONNET = "claude-sonnet-4-5-20250929"
HAIKU = "claude-haiku-4-5-20251001"
NUMBER_OF_EXPORT_COLUMNS_ADDED = 4
REVIEW_KEYS = ("quality_score", "quality_grade", "ai_verdict", "est_savings_usd")
_BASE = None


# Helpers

@contextmanager
def running(db_path):
    srv = make_server(str(db_path), host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        srv.db.close()
        thread.join(5)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(budgets, "now", lambda: FIXED_NOW)


@pytest.fixture
def env(tmp_path, clock):
    with running(tmp_path / "insights.db") as pair:
        yield pair


def _http(base, method, path, body=None, headers=None):
    """Plain HTTP call that never follows redirects. Returns (status, headers, body)."""
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _json(raw):
    return json.loads(raw.decode("utf-8"))


def _base():
    global _BASE
    if _BASE is None:
        _BASE = client.build_payload(FIX, user="base@example.com", project="payments-service")
    return _BASE


def receipt(run_id, started="2026-10-05T10:00:00Z", user="dev@example.com", models=None, cost=0.6,
            risk="low", **extra):
    """A pushed receipt for the sample session with a chosen id, start, cost and models. Extra keys
    (quality, recommendations, ai_review, agent, ...) are set as given, including None."""
    payload = copy.deepcopy(_base())
    payload["session_id"] = run_id
    payload["user"] = user
    payload["started"] = started
    payload["totals"]["cost_usd"] = cost
    payload["models"] = models if models is not None else {SONNET: {"tokens": 1000, "cost_usd": cost}}
    payload["risk"] = {"score": {"low": 10, "medium": 40, "high": 80}[risk], "level": risk, "reasons": []}
    payload["html"] = None
    # The client now builds a quality score and recommendations into every receipt (cli.build), and
    # the fixture gets them too. A receipt starts with no review; a test sets the keys it needs.
    payload["quality"] = None
    payload["recommendations"] = []
    payload["ai_review"] = None
    payload.update(extra)
    return payload


def quality(score, grade="B", signals=None):
    return {"score": score, "grade": grade, "signals": [] if signals is None else signals}


def rec(kind, title, savings=None, steps=()):
    return {"kind": kind, "title": title, "detail": "detail", "est_savings_usd": savings, "steps": list(steps)}


def ai(verdict, assessments=(), **extra):
    review = {
        "model": "claude-haiku-4-5", "verdict": verdict, "summary": "A short summary.",
        "risk_assessments": [
            {"step": index, "code": "secret_file", "assessment": a, "explanation": "why"}
            for index, a in enumerate(assessments)
        ],
        "diff_explanations": [], "cost_usd": 0.01, "tokens": {"input": 100, "output": 20},
    }
    review.update(extra)
    return review


def push(base, key, payload, expect=201):
    status, _, raw = _http(base, "POST", "/api/runs", body=payload, headers=_auth(key))
    assert status == expect, raw
    return _json(raw) if raw else {}


def team_with_keys(srv, name="alpha"):
    team_id, admin = srv.db.create_team(name)
    member = srv.db.create_key(team_id, "member-key", "member", "test")["key"]
    viewer = srv.db.create_key(team_id, "viewer-key", "viewer", "test")["key"]
    return team_id, admin, member, viewer


def insights_of(base, key, query="?days=30"):
    status, _, raw = _http(base, "GET", "/api/insights" + query, headers=_auth(key))
    assert status == 200, raw
    return _json(raw)


def stored(srv, team_id, run_id):
    """The four review columns exactly as they sit in the database."""
    with closing(sqlite3.connect(str(srv.db.path))) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT quality_score, quality_grade, ai_verdict, est_savings_usd, receipt_json FROM runs "
            "WHERE team_id = ? AND id = ?", (team_id, run_id),
        ).fetchone()
    return row


def _rows(raw):
    assert raw.startswith(b"\xef\xbb\xbf"), "the CSV must start with a byte-order mark"
    return list(csv.reader(io.StringIO(raw[3:].decode("utf-8"), newline="")))


# Storage: what a pushed receipt keeps

def test_push_with_review_keys_stores_the_columns_and_the_receipt_detail(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    payload = receipt(
        "run-full",
        quality=quality(82, "B", [{"name": "tests_passed", "value": True, "impact": "+8", "note": "all green"}]),
        recommendations=[rec("model_switch", "Use Haiku for search", 0.25, [2, 5]),
                         rec("shorter_prompts", "Trim the context", 0.5)],
        ai_review=ai("needs_review", ["confirmed", "false_positive"], summary="Looks mostly fine."),
    )
    push(base, admin, payload)

    row = stored(srv, team_id, "run-full")
    assert row["quality_score"] == 82 and row["quality_grade"] == "B"
    assert row["ai_verdict"] == "needs_review"
    assert row["est_savings_usd"] == pytest.approx(0.75)

    detail = _json(_http(base, "GET", "/api/runs/run-full", headers=_auth(admin))[2])["run"]
    assert detail["quality_score"] == 82 and detail["ai_verdict"] == "needs_review"
    assert detail["receipt"]["quality"]["signals"][0]["name"] == "tests_passed"
    assert [r["steps"] for r in detail["receipt"]["recommendations"]] == [[2, 5], []]
    assert detail["receipt"]["ai_review"]["summary"] == "Looks mostly fine."


def test_push_without_review_keys_stores_nulls_and_still_succeeds(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("run-plain"))

    row = stored(srv, team_id, "run-plain")
    assert all(row[k] is None for k in REVIEW_KEYS)
    detail = json.loads(row["receipt_json"])
    assert detail["quality"] is None and detail["ai_review"] is None and detail["recommendations"] == []


def test_a_receipt_built_from_a_real_session_stores_its_own_review(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    built = copy.deepcopy(_base())          # exactly what the client builds for the sample session
    built["session_id"] = "built-from-session"
    push(base, admin, built)
    row = stored(srv, team_id, "built-from-session")
    assert row["quality_score"] == built["quality"]["score"]
    assert row["quality_grade"] == built["quality"]["grade"]
    estimates = [r["est_savings_usd"] for r in built["recommendations"] if r["est_savings_usd"] is not None]
    assert row["est_savings_usd"] == pytest.approx(sum(estimates))
    assert row["ai_verdict"] is None         # the sample session has no AI review


@pytest.mark.parametrize("bad", ["high", True, 150, -1, [], {"score": "81"}, {"grade": "A"}])
def test_a_malformed_quality_is_stored_as_null_and_never_rejects_the_push(env, bad):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("run-bad-quality", quality=bad))
    row = stored(srv, team_id, "run-bad-quality")
    assert row["quality_score"] is None and row["quality_grade"] is None
    assert json.loads(row["receipt_json"])["quality"] is None


def test_a_grade_outside_a_to_f_keeps_the_score_and_drops_the_grade(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("run-z", quality={"score": 64, "grade": "Z", "signals": []}))
    row = stored(srv, team_id, "run-z")
    assert row["quality_score"] == 64 and row["quality_grade"] is None


def test_grades_are_upper_cased_and_fractional_scores_are_rounded(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("run-round", quality={"score": 72.6, "grade": "c"}))
    row = stored(srv, team_id, "run-round")
    assert row["quality_score"] == 73 and row["quality_grade"] == "C"


def test_recommendations_that_do_not_validate_are_dropped_one_by_one(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("run-recs", recommendations=[
        "junk",
        {"kind": "no-title"},
        rec("bad_savings", "Negative estimate", -3),
        {"kind": "text_savings", "title": "String estimate", "est_savings_usd": "1.0", "steps": [1, "2", True, 4]},
    ]))
    row = stored(srv, team_id, "run-recs")
    assert row["est_savings_usd"] is None   # no recommendation carried a usable estimate
    stored_recs = json.loads(row["receipt_json"])["recommendations"]
    assert [r["title"] for r in stored_recs] == ["Negative estimate", "String estimate"]
    assert stored_recs[0]["est_savings_usd"] is None
    assert stored_recs[1]["steps"] == [1, 4]


def test_est_savings_is_the_sum_of_the_estimates_that_are_given(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("run-sum", recommendations=[
        rec("a", "A", 0.1), rec("b", "B", 0.2), rec("c", "C", None), rec("d", "D", 0.35),
    ]))
    assert stored(srv, team_id, "run-sum")["est_savings_usd"] == pytest.approx(0.65)


def test_lists_are_capped_at_fifty_items_and_strings_at_two_thousand_characters(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    recs = [rec("k", f"title {i}", 0.01) for i in range(60)]
    recs[0]["title"] = "x" * 5000
    push(base, admin, receipt("run-caps", recommendations=recs,
                              quality={"score": 50, "grade": "D",
                                       "signals": [{"name": f"s{i}", "value": i} for i in range(60)]}))
    detail = json.loads(stored(srv, team_id, "run-caps")["receipt_json"])
    assert len(detail["recommendations"]) == 50
    assert len(detail["recommendations"][0]["title"]) == 2000
    assert len(detail["quality"]["signals"]) == 50
    assert stored(srv, team_id, "run-caps")["est_savings_usd"] == pytest.approx(0.5)   # the first 50 of 0.01 each


def test_an_unknown_ai_verdict_drops_the_review_but_keeps_the_rest(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("run-verdict", quality=quality(55, "C"), ai_review=ai("maybe")))
    row = stored(srv, team_id, "run-verdict")
    assert row["ai_verdict"] is None and row["quality_score"] == 55


def test_bad_assessments_and_unnamed_signals_are_dropped_from_a_valid_review(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    review = ai("dangerous", ["confirmed", "uncertain"])
    review["risk_assessments"] += [{"code": "x", "assessment": "maybe"}, "not an object"]
    review["diff_explanations"] = [{"explanation": "no file"}, {"file": "a.py", "explanation": "changed"}]
    push(base, admin, receipt("run-assess", ai_review=review, quality=quality(20, "F", [
        {"value": 1}, {"name": "kept", "value": "yes", "impact": None},
    ])))
    detail = json.loads(stored(srv, team_id, "run-assess")["receipt_json"])
    assert [a["assessment"] for a in detail["ai_review"]["risk_assessments"]] == ["confirmed", "uncertain"]
    assert detail["ai_review"]["diff_explanations"] == [{"file": "a.py", "explanation": "changed"}]
    assert [s["name"] for s in detail["quality"]["signals"]] == ["kept"]


@pytest.mark.parametrize("value", [None, 1, "x", [], {}, {"verdict": 3}, [{"kind": None}], {"score": [1]}])
def test_review_fields_turn_odd_values_into_nulls_and_never_raise(value):
    fields = insights.review_fields({"quality": value, "recommendations": value, "ai_review": value})
    assert fields["recommendations"] == []
    assert fields["quality"] is None and fields["ai_review"] is None
    assert all(fields[k] is None for k in REVIEW_KEYS)


def test_receipt_to_run_keeps_the_columns_and_the_validated_receipt_keys():
    run = receipt_to_run(receipt("run-direct", quality=quality(90, "A"), recommendations=[rec("k", "t", 2.5)],
                                 ai_review=ai("looks_safe")))
    assert (run["quality_score"], run["quality_grade"], run["ai_verdict"], run["est_savings_usd"]) == \
        (90, "A", "looks_safe", 2.5)
    assert run["receipt"]["quality"]["score"] == 90


# Migration of an existing database

def _old_schema():
    """The current schema without the four review columns: the runs table as it was before them."""
    drop = set(REVIEW_KEYS)
    return "\n".join(line for line in SCHEMA.splitlines() if line.strip().split(" ")[0] not in drop)


def test_an_older_database_gains_the_columns_and_backfills_from_stored_receipts(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(_old_schema())
        conn.execute("INSERT INTO teams (name, api_key_hash, created_at) VALUES ('old', 'h', '2026-10-01T00:00:00Z')")
        stored_receipt = receipt("old-review", quality=quality(80, "b"), ai_review=ai("dangerous"),
                                 recommendations=[rec("k", "Cut retries", 0.4)])
        stored_plain = receipt("old-plain")
        for run_id, payload in (("old-review", stored_receipt), ("old-plain", stored_plain)):
            conn.execute(
                "INSERT INTO runs (id, team_id, user, project, agent, title, started_at, ended_at, models, steps, "
                "tokens, files_changed, cost, risk_score, risk_level, receipt_json, receipt_html, created_at, "
                "updated_at) VALUES (?, 1, 'dev', 'p', 'Claude Code', NULL, '2026-10-05T10:00:00Z', NULL, '{}', "
                "0, 0, 0, NULL, 10, 'low', ?, NULL, '2026-10-05T10:00:00Z', '2026-10-05T10:00:00Z')",
                (run_id, json.dumps(payload)),
            )
        conn.commit()

    with closing(Database(str(path))) as db:
        review = db.get_run(1, "old-review")
        assert review["quality_score"] == 80 and review["quality_grade"] == "B"
        assert review["ai_verdict"] == "dangerous" and review["est_savings_usd"] == pytest.approx(0.4)
        assert review["receipt"]["quality"]["grade"] == "B"        # rewritten with the validated value
        plain = db.get_run(1, "old-plain")
        assert plain["quality_score"] is None and plain["ai_verdict"] is None

    # A second open finds the columns in place and changes nothing.
    with closing(Database(str(path))) as db:
        again = db.get_run(1, "old-review")
        assert again["quality_score"] == 80 and again["ai_verdict"] == "dangerous"


def test_a_new_database_has_the_review_columns_from_the_start(tmp_path):
    with closing(Database(str(tmp_path / "fresh.db"))) as db:
        names = {row["name"] for row in db._conn.execute("PRAGMA table_info(runs)")}
    assert set(REVIEW_KEYS) <= names


# Runs list: filters and fields

def test_the_runs_list_filters_by_quality_range_and_ai_verdict(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("q-high", quality=quality(90, "A"), ai_review=ai("looks_safe")))
    push(base, admin, receipt("q-mid", quality=quality(60, "C"), ai_review=ai("needs_review")))
    push(base, admin, receipt("q-low", quality=quality(30, "F"), ai_review=ai("dangerous")))
    push(base, admin, receipt("q-none"))

    def ids(query):
        status, _, raw = _http(base, "GET", "/api/runs" + query, headers=_auth(admin))
        assert status == 200, raw
        return sorted(r["id"] for r in _json(raw)["runs"])

    assert ids("?min_quality=60") == ["q-high", "q-mid"]
    assert ids("?max_quality=59") == ["q-low"]
    assert ids("?min_quality=60&max_quality=60") == ["q-mid"]
    assert ids("?min_quality=0") == ["q-high", "q-low", "q-mid"]    # a run without a score matches no bound
    assert ids("?ai_verdict=dangerous") == ["q-low"]
    assert ids("?ai_verdict=looks_safe&min_quality=50") == ["q-high"]
    assert ids("?ai_verdict=looks_safe&min_quality=95") == []


@pytest.mark.parametrize("query", ["?ai_verdict=maybe", "?ai_verdict=DANGEROUS", "?min_quality=101",
                                   "?max_quality=-1", "?min_quality=abc"])
def test_bad_review_filters_are_refused_with_a_json_error(env, query):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    status, headers, raw = _http(base, "GET", "/api/runs" + query, headers=_auth(admin))
    assert status == 400 and headers["content-type"].startswith("application/json")
    assert _json(raw)["error"]["code"] == "bad_request"


def test_list_items_and_run_details_carry_the_review_fields(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("listed", quality=quality(71, "C"), ai_review=ai("needs_review"),
                              recommendations=[rec("k", "t", 0.2)]))
    item = _json(_http(base, "GET", "/api/runs", headers=_auth(admin))[2])["runs"][0]
    assert item["quality_score"] == 71 and item["quality_grade"] == "C"
    assert item["ai_verdict"] == "needs_review" and item["est_savings_usd"] == pytest.approx(0.2)


# Insights: the summary

def test_insights_on_an_empty_team_report_nulls_and_zeros(env):
    srv, base = env
    _, _, _, viewer = team_with_keys(srv)
    body = insights_of(base, viewer)
    assert body["days"] == 30 and body["since"] == "2026-09-15T12:00:00Z"
    assert body["quality"]["avg"] is None and body["quality"]["scored_runs"] == 0
    assert body["quality"]["by_grade"] == {g: 0 for g in "ABCDEF"}
    assert body["quality"]["trend"] == [] and body["models"] == [] and body["agents"] == []
    assert body["top_recommendations"] == [] and body["est_savings_usd"] == 0
    assert body["ai_review"] == {"reviewed_runs": 0, "dangerous": 0, "needs_review": 0, "looks_safe": 0,
                                 "false_positive_rate": None}


def test_quality_average_grade_counts_and_scored_run_count(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("g-1", quality=quality(80, "B")))
    push(base, admin, receipt("g-2", quality=quality(90, "A")))
    push(base, admin, receipt("g-3", quality=quality(70, "C")))
    push(base, admin, receipt("g-4"))                                       # not scored: not counted
    quality_block = insights_of(base, admin)["quality"]
    assert quality_block["avg"] == 80.0
    assert quality_block["scored_runs"] == 3
    assert quality_block["by_grade"] == {"A": 1, "B": 1, "C": 1, "D": 0, "E": 0, "F": 0}


def test_trend_has_one_point_per_scored_day_in_date_order(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("t-1", started="2026-10-07T09:00:00Z", quality=quality(90, "A")))
    push(base, admin, receipt("t-2", started="2026-10-05T09:00:00Z", quality=quality(60, "C")))
    push(base, admin, receipt("t-3", started="2026-10-05T15:00:00Z", quality=quality(80, "B")))
    push(base, admin, receipt("t-4", started="2026-10-06T09:00:00Z"))       # no score: no point that day
    trend = insights_of(base, admin)["quality"]["trend"]
    assert trend == [{"date": "2026-10-05", "avg": 70.0}, {"date": "2026-10-07", "avg": 90.0}]


def test_the_window_follows_the_clock_and_the_days_parameter(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("w-old", started="2026-10-01T10:00:00Z", quality=quality(50, "D")))
    push(base, admin, receipt("w-new", started="2026-10-14T10:00:00Z", quality=quality(90, "A")))
    assert insights_of(base, admin, "?days=30")["quality"]["scored_runs"] == 2
    seven = insights_of(base, admin, "?days=7")
    assert seven["quality"]["scored_runs"] == 1 and seven["quality"]["avg"] == 90.0
    assert seven["since"] == "2026-10-08T12:00:00Z"


def test_models_report_cost_per_run_quality_and_share_of_cost(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("m-1", quality=quality(80, "B"),
                              models={SONNET: {"tokens": 10, "cost_usd": 1.0}}))
    push(base, admin, receipt("m-2", quality=quality(60, "C"), cost=1.0,
                              models={SONNET: {"tokens": 10, "cost_usd": 0.5}, HAIKU: {"tokens": 5, "cost_usd": 0.5}}))
    models = {m["model"]: m for m in insights_of(base, admin)["models"]}
    sonnet = models[friendly_model(SONNET)]
    haiku = models[friendly_model(HAIKU)]
    assert sonnet["runs"] == 2 and sonnet["cost_usd"] == pytest.approx(1.5)
    assert sonnet["cost_per_run"] == pytest.approx(0.75)
    assert sonnet["avg_quality"] == 70.0                                  # both runs used Sonnet
    assert sonnet["share_of_cost"] == pytest.approx(0.75)
    assert haiku["runs"] == 1 and haiku["cost_usd"] == pytest.approx(0.5)
    assert haiku["avg_quality"] == 60.0 and haiku["share_of_cost"] == pytest.approx(0.25)


def test_a_model_without_prices_has_no_cost_per_run_and_no_share(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    # Without per-step costs either, the server has no price for the model (it falls back to steps).
    payload = receipt("unpriced", models={SONNET: {"tokens": 10, "cost_usd": None}}, cost=None, steps=[])
    push(base, admin, payload)
    (model,) = insights_of(base, admin)["models"]
    assert model["runs"] == 1 and model["cost_usd"] == 0
    assert model["cost_per_run"] is None and model["share_of_cost"] is None and model["avg_quality"] is None


def test_agents_are_compared_with_unknown_for_runs_without_one(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("a-1", cost=1.0, risk="low", quality=quality(80, "B"), agent="Claude Code"))
    push(base, admin, receipt("a-2", cost=0.5, risk="medium", quality=quality(60, "C"), agent="Claude Code"))
    push(base, admin, receipt("a-3", cost=0.2, risk="low", agent=None))
    agents = {a["agent"]: a for a in insights_of(base, admin)["agents"]}
    claude = agents["Claude Code"]
    assert claude["runs"] == 2 and claude["cost_usd"] == pytest.approx(1.5)
    assert claude["avg_quality"] == 70.0 and claude["avg_risk"] == 25.0
    assert agents["unknown"]["avg_quality"] is None and agents["unknown"]["runs"] == 1


def test_recommendations_are_grouped_by_kind_and_title_across_runs(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("r-1", recommendations=[rec("model_switch", "Use Haiku for search", 0.4)]))
    push(base, admin, receipt("r-2", recommendations=[rec("model_switch", "Use Haiku for search", 0.6),
                                                     rec("shorter_prompts", "Trim the context")]))
    push(base, admin, receipt("r-3", recommendations=[rec("shorter_prompts", "Trim the context")]))
    top = insights_of(base, admin)["top_recommendations"]
    assert top[0] == {"kind": "model_switch", "title": "Use Haiku for search", "count": 2,
                      "est_savings_usd": pytest.approx(1.0)}
    assert top[1] == {"kind": "shorter_prompts", "title": "Trim the context", "count": 2,
                      "est_savings_usd": None}
    assert insights_of(base, admin)["est_savings_usd"] == pytest.approx(1.0)


def test_top_recommendations_are_limited_to_ten(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    for index in range(12):
        srv.db.upsert_run(team_id, receipt_to_run(receipt(
            f"many-{index}", recommendations=[rec("k", f"idea {index}", 0.1 * (index + 1))])))
    top = insights_of(base, admin)["top_recommendations"]
    assert len(top) == 10 and top[0]["title"] == "idea 11"


def test_ai_review_counts_and_false_positive_rate(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("ai-1", ai_review=ai("dangerous", ["false_positive", "confirmed"])))
    push(base, admin, receipt("ai-2", ai_review=ai("needs_review", ["uncertain", "false_positive"])))
    push(base, admin, receipt("ai-3", ai_review=ai("looks_safe", ["confirmed"])))
    push(base, admin, receipt("ai-none"))
    review = insights_of(base, admin)["ai_review"]
    assert review == {"reviewed_runs": 3, "dangerous": 1, "needs_review": 1, "looks_safe": 1,
                      "false_positive_rate": 0.4}          # 2 false positives out of 5 assessed risks


def test_the_false_positive_rate_is_null_when_no_risk_was_assessed(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("no-risks", ai_review=ai("looks_safe", [])))
    review = insights_of(base, admin)["ai_review"]
    assert review["reviewed_runs"] == 1 and review["false_positive_rate"] is None


def test_insights_are_scoped_to_the_callers_team(env):
    srv, base = env
    _, admin_a, _, _ = team_with_keys(srv, "alpha")
    _, admin_b, _, _ = team_with_keys(srv, "beta")
    push(base, admin_a, receipt("alpha-only", quality=quality(95, "A")))
    assert insights_of(base, admin_a)["quality"]["scored_runs"] == 1
    assert insights_of(base, admin_b)["quality"]["scored_runs"] == 0


@pytest.mark.parametrize("query", ["?days=0", "?days=3651", "?days=abc", "?days=1.5"])
def test_insights_days_outside_one_to_3650_are_refused(env, query):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    assert _http(base, "GET", "/api/insights" + query, headers=_auth(admin))[0] == 400


@pytest.mark.parametrize("query", ["", "?days=1", "?days=3650"])
def test_insights_accepts_the_edges_of_the_day_range(env, query):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    assert _http(base, "GET", "/api/insights" + query, headers=_auth(admin))[0] == 200


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
def test_every_signed_in_role_can_read_insights(env, role):
    srv, base = env
    _, admin, member, viewer = team_with_keys(srv)
    key = {"admin": admin, "member": member, "viewer": viewer}[role]
    assert _http(base, "GET", "/api/insights", headers=_auth(key))[0] == 200


def test_insights_need_a_key_or_a_session(env):
    srv, base = env
    _, admin, _, viewer = team_with_keys(srv)
    status, _, raw = _http(base, "GET", "/api/insights")
    assert status == 401 and _json(raw)["error"]["code"] == "unauthorized"
    status, headers, _ = _http(base, "GET", f"/?key={viewer}")
    cookie = headers["set-cookie"].split(";")[0]
    assert _http(base, "GET", "/api/insights", headers={"Cookie": cookie})[0] == 200


def test_insights_only_answers_get(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    status, headers, _ = _http(base, "POST", "/api/insights", body={}, headers=_auth(admin))
    assert status == 405 and headers["allow"] == "GET"


# Exports

def test_runs_csv_gains_the_review_columns_with_blanks_for_missing_values(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("csv-1", quality=quality(88, "A"), ai_review=ai("looks_safe"),
                              recommendations=[rec("k", "t", 1.25)]))
    push(base, admin, receipt("csv-2"))
    status, _, raw = _http(base, "GET", "/api/export/runs.csv?days=30", headers=_auth(admin))
    assert status == 200
    rows = _rows(raw)
    header = rows[0]
    assert header[-NUMBER_OF_EXPORT_COLUMNS_ADDED:] == ["quality_score", "quality_grade", "ai_verdict", "est_savings_usd"]
    by_id = {r[0]: dict(zip(header, r)) for r in rows[1:]}
    assert by_id["csv-1"]["quality_score"] == "88" and by_id["csv-1"]["quality_grade"] == "A"
    assert by_id["csv-1"]["ai_verdict"] == "looks_safe" and by_id["csv-1"]["est_savings_usd"] == "1.25"
    assert by_id["csv-2"]["quality_score"] == "" and by_id["csv-2"]["ai_verdict"] == ""
    assert by_id["csv-2"]["est_savings_usd"] == ""


def test_report_has_a_quality_and_ai_review_section_with_the_figures(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("rep-1", quality=quality(80, "B"), ai_review=ai("dangerous", ["false_positive"]),
                              recommendations=[rec("model_switch", "Use Haiku", 0.5)]))
    push(base, admin, receipt("rep-2", quality=quality(60, "C"), ai_review=ai("looks_safe", ["confirmed"])))
    status, headers, raw = _http(base, "GET", "/api/export/report.html?days=30", headers=_auth(admin))
    assert status == 200
    body = raw.decode("utf-8")
    assert "<h2>Quality and AI review</h2>" in body
    assert "70.0 of 100 across 2 scored runs" in body
    assert "<h3>Grades</h3>" in body and "<h3>AI verdicts</h3>" in body
    assert "<td>Dangerous</td><td class=\"num\">1</td>" in body
    assert "<td>Looks safe</td><td class=\"num\">1</td>" in body
    assert "50.0%" in body                                   # 1 false positive out of 2 assessed risks
    assert "Use Haiku" in body and "$0.5000" in body
    assert "<script" not in body and "src=" not in body
    assert headers["content-security-policy"] == exports.REPORT_CSP


def test_report_escapes_recommendation_text_and_kinds(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("rep-x", recommendations=[
        rec("<img src=x onerror=alert(1)>", "<script>alert(2)</script>", 0.3),
    ]))
    raw = _http(base, "GET", "/api/export/report.html?days=30", headers=_auth(admin))[2]
    assert b"<script>alert(2)" not in raw and b"<img src=x" not in raw
    assert b"&lt;script&gt;alert(2)&lt;/script&gt;" in raw
    assert b"&lt;img src=x onerror=alert(1)&gt;" in raw


def test_report_says_when_there_is_nothing_to_show(env):
    srv, base = env
    _, admin, _, _ = team_with_keys(srv)
    body = _http(base, "GET", "/api/export/report.html", headers=_auth(admin))[2].decode("utf-8")
    assert "no scored runs in this period" in body
    assert "No recommendations were recorded in this period." in body
    assert "no AI-assessed rule risks in this period" in body


def test_reading_insights_and_the_report_does_not_change_stored_runs(env):
    srv, base = env
    team_id, admin, _, _ = team_with_keys(srv)
    push(base, admin, receipt("stable", quality=quality(66, "C"), ai_review=ai("needs_review", ["confirmed"])))
    before = stored(srv, team_id, "stable")
    insights_of(base, admin)
    _http(base, "GET", "/api/export/report.html", headers=_auth(admin))
    after = stored(srv, team_id, "stable")
    assert dict(before) == dict(after)


# Dashboard: static checks on the served page

def _script(html):
    match = re.search(r'<script nonce="__CSP_NONCE__">(.*?)</script>', html, re.S)
    assert match, "the page has one inline script with the nonce placeholder"
    return match.group(1)


def test_the_page_has_an_insights_tab_wired_to_its_panel_and_the_api():
    assert 'id="tab-insights"' in DASHBOARD_HTML and 'data-view="insights"' in DASHBOARD_HTML
    assert 'aria-controls="view-insights"' in DASHBOARD_HTML
    assert 'id="view-insights"' in DASHBOARD_HTML and 'role="tabpanel"' in DASHBOARD_HTML
    js = _script(DASHBOARD_HTML)
    assert "/api/insights" in js
    assert 'var VIEWS = ["runs", "insights", "approvals", "keys", "audit", "settings"];' in js
    assert 'if (name === "insights") { loadInsights(); }' in js


def test_the_insights_tab_is_shown_to_every_role():
    tag = re.search(r'<button[^>]*id="tab-insights"[^>]*>', DASHBOARD_HTML).group(0)
    assert " hidden" not in tag
    panel = re.search(r'<section[^>]*id="view-insights"[^>]*>', DASHBOARD_HTML).group(0)
    assert "hidden" in panel      # hidden until the tab is chosen, never admin-only
    assert '"insights"' not in re.search(r"var ADMIN_VIEWS = \{[^}]*\};", _script(DASHBOARD_HTML)).group(0)


def test_the_trend_chart_is_built_with_create_element_ns_and_text_only():
    js = _script(DASHBOARD_HTML)
    assert 'document.createElementNS(SVG_NS, "svg")' in js
    assert 'document.createElementNS(SVG_NS, "polyline")' in js
    assert 'tip.textContent = ' in js
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "createContextualFragment", "document.write",
                 "eval(", "new Function", "srcdoc", ".html("):
        assert sink not in DASHBOARD_HTML, sink
    assert js.count("fetch(") == 2     # the same two call sites as before the Insights view


def test_the_runs_table_has_quality_and_review_columns_and_a_quality_filter():
    assert "<th class=\"num\">Quality</th><th>AI review</th>" in DASHBOARD_HTML
    assert 'id="f-quality"' in DASHBOARD_HTML and 'aria-label="Minimum quality score"' in DASHBOARD_HTML
    js = _script(DASHBOARD_HTML)
    assert 'params.set("min_quality"' in js
    assert '"f-quality"' in js


def test_the_insights_view_respects_reduced_motion_and_has_aa_ink_tokens():
    assert "@media (prefers-reduced-motion: reduce)" in DASHBOARD_HTML
    # Light and dark values for the ink colours. Light ink is darker so text on the tinted badges
    # keeps a 4.5:1 contrast ratio.
    assert "--ink-good:#4fe0b0" in DASHBOARD_HTML and "--ink-good:#065f46" in DASHBOARD_HTML
    assert "--ink-warn:#92400e" in DASHBOARD_HTML and "--ink-bad:#991b1b" in DASHBOARD_HTML
    assert ".spark polyline{fill:none;stroke:var(--accent)" in DASHBOARD_HTML


def test_the_insights_layout_fits_a_phone_without_page_overflow():
    assert "grid-template-columns:repeat(auto-fit,minmax(170px,1fr))" in DASHBOARD_HTML
    assert ".table-wrap{overflow-x:auto" in DASHBOARD_HTML
    assert ".spark{display:block;width:100%" in DASHBOARD_HTML
    assert ".rec-title{font-weight:600;overflow-wrap:anywhere}" in DASHBOARD_HTML


def test_every_form_control_and_lookup_of_the_insights_view_exists():
    js = _script(DASHBOARD_HTML)
    ids = set(re.findall(r'id="([^"]+)"', DASHBOARD_HTML))
    lookups = set(re.findall(r'\$\("([^"]+)"\)', js))
    expected = {"insights-window", "insights-msg", "i-quality", "i-quality-grade", "i-quality-sub", "i-savings",
                "i-savings-sub", "i-reviewed", "i-reviewed-sub", "i-fp", "i-fp-sub", "i-trend", "i-grades",
                "i-models", "i-models-empty", "i-agents", "i-agents-empty", "i-recs", "i-recs-empty"}
    assert expected <= ids
    assert expected <= lookups
    assert not (lookups - ids)


def test_the_served_dashboard_includes_the_insights_view(tmp_path, clock):
    with running(tmp_path / "served.db") as (srv, base):
        _, admin, _, _ = team_with_keys(srv)
        status, headers, _ = _http(base, "GET", f"/?key={admin}")
        cookie = headers["set-cookie"].split(";")[0]
        status, _, body = _http(base, "GET", "/", headers={"Cookie": cookie})
        assert status == 200 and b'id="view-insights"' in body and b"/api/insights" in body


# Pure helpers

def test_the_grade_and_verdict_vocabularies_are_the_documented_ones():
    assert insights.GRADES == ("A", "B", "C", "D", "E", "F")
    assert insights.VERDICTS == ("looks_safe", "needs_review", "dangerous")
    assert insights.ASSESSMENTS == ("confirmed", "false_positive", "uncertain")
    assert insights.MAX_ITEMS == 50 and insights.MAX_TEXT == 2000
