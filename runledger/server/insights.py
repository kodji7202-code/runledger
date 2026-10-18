"""Quality scores, recommendations and AI review verdicts from pushed receipts, and the Insights summary.

A pushed receipt may carry three optional keys: "quality", "recommendations" and "ai_review".
This module checks them leniently. A value of the wrong type becomes null, a list item that does
not validate is dropped, lists are capped at MAX_ITEMS entries and strings at MAX_TEXT
characters, and no value can reject a push. The validated copies are what the server stores:
in receipt_json under the same keys, and in the runs columns quality_score, quality_grade,
ai_verdict and est_savings_usd, plus the extracted inputs of the summary (recommendations and
ai_assessment), which keep a report from reading every receipt.

compute() builds the Insights summary (GET /api/insights and the compliance report): the quality
average, grade counts and daily trend; cost and quality by model and by agent; the
recommendations that recur across runs; and the AI review counts with the false-positive rate
of the rule risks. The model figures attribute a run's quality to every model the run used, and
the cost per run counts only the runs that have a price.

This module does not import db, because db imports it for the upgrade of older databases.
Standard library only.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..pricing import friendly_model

# Quality never produces an E (the cutoffs are 85, 70, 55 and 40), so the distribution has
# five buckets. A receipt from another tool may still carry one: GRADE_LETTERS accepts it and
# the row keeps it, while GRADES decides what the distribution counts.
GRADES = ("A", "B", "C", "D", "F")
GRADE_LETTERS = ("A", "B", "C", "D", "E", "F")
VERDICTS = ("looks_safe", "needs_review", "dangerous")
ASSESSMENTS = ("confirmed", "false_positive", "uncertain")
MAX_ITEMS = 50
MAX_TEXT = 2000
TOP_LIMIT = 10
TABLE_LIMIT = 20
# json.dumps writes a non-empty list of objects as '"recommendations": [{'. The database uses
# this text to find the receipts that have recommendations, without parsing every receipt.
RECOMMENDATIONS_LIKE = '%"recommendations": [{%'
QUALITY_LIKE = '%"quality": {%'
AI_REVIEW_LIKE = '%"ai_review": {%'


# Leniency helpers

def _items(value: Any) -> List[Any]:
    """The first MAX_ITEMS entries of a list, or no entries for anything that is not a list."""
    return value[:MAX_ITEMS] if isinstance(value, list) else []


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()[:MAX_TEXT]
    return text or None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _whole(value: Any) -> Optional[int]:
    """A whole number (a step index), not a bool and not a float."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usd(value: Any) -> Optional[float]:
    number = _number(value)
    if number is None or number < 0:
        return None
    return round(number, 6)


def _count(value: Any) -> Optional[int]:
    """A token count: a non-negative number, kept as a whole number."""
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _score(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None or not 0 <= number <= 100:
        return None
    return int(round(number))


def _grade(value: Any) -> Optional[str]:
    text = value.strip().upper() if isinstance(value, str) else ""
    return text if text in GRADE_LETTERS else None


def _enum(value: Any, allowed: Tuple[str, ...]) -> Optional[str]:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in allowed else None


def _scalar(value: Any) -> Any:
    """A signal value or impact: a short text, a number, a bool or null. Anything else is null."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


# Validation of the three keys

def normalize_quality(value: Any) -> Optional[Dict[str, Any]]:
    """{"score", "grade", "signals"} or None. A score outside 0..100 or not a number drops the
    whole value. An unknown grade is stored as null."""
    if not isinstance(value, dict):
        return None
    score = _score(value.get("score"))
    if score is None:
        return None
    signals = []
    for item in _items(value.get("signals")):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if name is None:
            continue
        signals.append({
            "name": name,
            "value": _scalar(item.get("value")),
            "impact": _scalar(item.get("impact")),
            "note": _text(item.get("note")),
        })
    return {"score": score, "grade": _grade(value.get("grade")), "signals": signals}


def normalize_recommendations(value: Any) -> List[Dict[str, Any]]:
    """The recommendations that validate: each needs a kind and a title."""
    out: List[Dict[str, Any]] = []
    for item in _items(value):
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("kind"))
        title = _text(item.get("title"))
        if kind is None or title is None:
            continue
        out.append({
            "kind": kind,
            "title": title,
            "detail": _text(item.get("detail")),
            "est_savings_usd": _usd(item.get("est_savings_usd")),
            "steps": [s for s in (_whole(x) for x in _items(item.get("steps"))) if s is not None],
        })
    return out


def normalize_ai_review(value: Any) -> Optional[Dict[str, Any]]:
    """The AI review, or None when it is not an object or its verdict is not one of the three.
    Assessments with an unknown assessment value, and diff entries without a file, are dropped."""
    if not isinstance(value, dict):
        return None
    verdict = _enum(value.get("verdict"), VERDICTS)
    if verdict is None:
        return None
    assessments = []
    for item in _items(value.get("risk_assessments")):
        if not isinstance(item, dict):
            continue
        assessment = _enum(item.get("assessment"), ASSESSMENTS)
        if assessment is None:
            continue
        assessments.append({
            "step": _whole(item.get("step")),
            "code": _text(item.get("code")),
            "assessment": assessment,
            "explanation": _text(item.get("explanation")),
        })
    diffs = []
    for item in _items(value.get("diff_explanations")):
        if not isinstance(item, dict):
            continue
        path = _text(item.get("file"))
        if path is None:
            continue
        diffs.append({"file": path, "explanation": _text(item.get("explanation"))})
    tokens = value.get("tokens")
    counts = None
    if isinstance(tokens, dict):
        counts = {"input": _count(tokens.get("input")), "output": _count(tokens.get("output"))}
    return {
        "model": _text(value.get("model")),
        "verdict": verdict,
        "summary": _text(value.get("summary")),
        "risk_assessments": assessments,
        "diff_explanations": diffs,
        "cost_usd": _usd(value.get("cost_usd")),
        "tokens": counts,
    }


def review_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The validated keys for a pushed receipt, and the runs columns derived from them.
    Keys: quality, recommendations, ai_review (stored in receipt_json), plus quality_score,
    quality_grade, ai_verdict, est_savings_usd, recommendation_counts and assessment_counts.
    est_savings_usd is the sum of the recommendations' estimates, or None when no recommendation
    has one. recommendation_counts and assessment_counts are the extracted inputs of the Insights
    summary, kept on the run row so a report never has to read a receipt."""
    quality = normalize_quality(payload.get("quality"))
    recommendations = normalize_recommendations(payload.get("recommendations"))
    ai_review = normalize_ai_review(payload.get("ai_review"))
    estimates = [r["est_savings_usd"] for r in recommendations if r["est_savings_usd"] is not None]
    return {
        "quality": quality,
        "recommendations": recommendations,
        "ai_review": ai_review,
        "quality_score": quality["score"] if quality else None,
        "quality_grade": quality["grade"] if quality else None,
        "ai_verdict": ai_review["verdict"] if ai_review else None,
        "est_savings_usd": round(sum(estimates), 6) if estimates else None,
        "recommendation_counts": [
            {"kind": r["kind"], "title": r["title"], "est_savings_usd": r["est_savings_usd"]}
            for r in recommendations
        ],
        "assessment_counts": assessment_counts(ai_review),
    }


def assessment_counts(review: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """How many rule risks a validated AI review assessed, and how many it called false positives.
    Those two numbers are all the Insights summary needs from a review."""
    if review is None:
        return {"assessed": 0, "false_positive": 0}
    return {
        "assessed": len(review["risk_assessments"]),
        "false_positive": sum(1 for item in review["risk_assessments"]
                              if item["assessment"] == "false_positive"),
    }


# The Insights summary

def _quality_block(parts: Dict[str, Any]) -> Dict[str, Any]:
    by_grade = {grade: 0 for grade in GRADES}
    for row in parts["grades"]:
        if row["grade"] in by_grade:
            by_grade[row["grade"]] += int(row["n"])
    avg = parts["quality_avg"]
    return {
        "avg": None if avg is None else round(float(avg), 1),
        "scored_runs": int(parts["scored"]),
        "by_grade": by_grade,
        "trend": [{"date": day, "avg": round(float(value), 1)} for day, value in parts["trend"]],
    }


def _models_block(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    slots: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        try:
            names = json.loads(row["models"] or "{}")
        except ValueError:
            names = {}
        if not isinstance(names, dict):
            continue
        quality = row["quality_score"]
        for name, info in names.items():
            label = friendly_model(str(name))
            slot = slots.setdefault(label, {"runs": 0, "priced": 0, "cost": 0.0, "q_sum": 0.0, "q_n": 0})
            slot["runs"] += 1
            cost = _usd(info.get("cost_usd")) if isinstance(info, dict) else None
            if cost is not None:
                slot["priced"] += 1
                slot["cost"] += cost
            if quality is not None:
                slot["q_sum"] += float(quality)
                slot["q_n"] += 1
    total = sum(slot["cost"] for slot in slots.values())
    out = []
    for label, slot in slots.items():
        out.append({
            "model": label,
            "runs": slot["runs"],
            # No priced run: the model's cost is unknown (n/a), not zero.
            "cost_usd": round(slot["cost"], 6) if slot["priced"] else None,
            "priced_runs": slot["priced"],
            "avg_quality": round(slot["q_sum"] / slot["q_n"], 1) if slot["q_n"] else None,
            "cost_per_run": round(slot["cost"] / slot["priced"], 6) if slot["priced"] else None,
            "share_of_cost": round(slot["cost"] / total, 4) if total > 0 else None,
        })
    out.sort(key=lambda m: (m["cost_usd"] is None, -(m["cost_usd"] or 0.0), -m["runs"], m["model"]))
    return out[:TABLE_LIMIT]


def _agents_block(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        avg_quality = row["avg_quality"]
        avg_risk = row["avg_risk"]
        priced = int(row["priced_runs"] or 0) if "priced_runs" in row.keys() else 0
        cost = row["cost_usd"]
        out.append({
            "agent": row["name"],
            "runs": int(row["runs"]),
            "avg_quality": None if avg_quality is None else round(float(avg_quality), 1),
            "avg_risk": None if avg_risk is None else round(float(avg_risk), 1),
            # An agent whose runs were never priced has an unknown cost, not a zero one.
            "cost_usd": None if cost is None else round(float(cost), 6),
            "priced_runs": priced,
        })
    out.sort(key=lambda a: (a["cost_usd"] is None, -(a["cost_usd"] or 0.0), -a["runs"], a["agent"]))
    return out[:TABLE_LIMIT]


def _extracted_recommendations(raw: Any) -> List[Dict[str, Any]]:
    """The recommendation entries a stored extraction holds. Unreadable or odd values are skipped."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return normalize_recommendations(value)


def _extracted_assessment(raw: Any) -> Tuple[int, int]:
    """The (assessed, false positive) pair a stored extraction holds, or zeroes."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return 0, 0
    if not isinstance(value, dict):
        return 0, 0
    return _whole(value.get("assessed")) or 0, _whole(value.get("false_positive")) or 0


def _review_inputs(parts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int, int]:
    """Everything the recommendations and AI review blocks count for the window: the copies the
    server extracted at push time, plus the receipts stored before those columns existed."""
    recommendations: List[Dict[str, Any]] = []
    assessed = 0
    false_positive = 0
    for row in parts["recommendation_rows"]:
        recommendations.extend(_extracted_recommendations(row["recommendations"]))
    for row in parts["assessment_rows"]:
        counts = _extracted_assessment(row["ai_assessment"])
        assessed += counts[0]
        false_positive += counts[1]
    for raw in parts["legacy_receipts"]:
        try:
            stored = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(stored, dict):
            continue
        recommendations.extend(normalize_recommendations(stored.get("recommendations")))
        legacy = assessment_counts(normalize_ai_review(stored.get("ai_review")))
        assessed += legacy["assessed"]
        false_positive += legacy["false_positive"]
    return recommendations, assessed, false_positive


def _recommendations_block(recommendations: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Recommendations grouped by kind and title. count is how often it was given; the savings
    add up the estimates given, and are null when none was estimated."""
    slots: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for rec in recommendations:
        key = (rec["kind"], rec["title"])
        slot = slots.setdefault(key, {"kind": rec["kind"], "title": rec["title"], "count": 0,
                                      "savings": 0.0, "priced": False})
        slot["count"] += 1
        if rec["est_savings_usd"] is not None:
            slot["savings"] += rec["est_savings_usd"]
            slot["priced"] = True
    out = [
        {
            "kind": s["kind"], "title": s["title"], "count": s["count"],
            "est_savings_usd": round(s["savings"], 6) if s["priced"] else None,
        }
        for s in slots.values()
    ]
    out.sort(key=lambda r: (-(r["est_savings_usd"] or 0.0), -r["count"], r["kind"], r["title"]))
    return out[:TOP_LIMIT]


def _ai_block(verdict_rows: Iterable[Dict[str, Any]], assessed: int, false_positive: int) -> Dict[str, Any]:
    counts = {verdict: 0 for verdict in VERDICTS}
    for row in verdict_rows:
        if row["verdict"] in counts:
            counts[row["verdict"]] += int(row["n"])
    return {
        "reviewed_runs": sum(counts.values()),
        "dangerous": counts["dangerous"],
        "needs_review": counts["needs_review"],
        "looks_safe": counts["looks_safe"],
        # Share of the rule risks the AI assessed (confirmed, false positive or uncertain).
        "false_positive_rate": round(false_positive / assessed, 4) if assessed else None,
    }


def compute(db: Any, team_id: int, since: str, days: int) -> Dict[str, Any]:
    """The Insights summary for one team, for runs that started (or were pushed) at or after `since`.
    `db` is a runledger.server.db.Database; its insight_parts() supplies the raw rows."""
    parts = db.insight_parts(team_id, since)
    recommendations, assessed, false_positive = _review_inputs(parts)
    return {
        "days": days,
        "since": since,
        "quality": _quality_block(parts),
        "models": _models_block(parts["model_rows"]),
        "agents": _agents_block(parts["agents"]),
        "top_recommendations": _recommendations_block(recommendations),
        "est_savings_usd": round(float(parts["est_savings_usd"] or 0.0), 6),
        "ai_review": _ai_block(parts["verdicts"], assessed, false_positive),
    }
