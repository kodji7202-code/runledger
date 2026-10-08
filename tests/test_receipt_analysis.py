"""Receipt sections for the quality score, cost recommendations and AI risk review
(runledger/receipt.py) in HTML, Markdown and JSON, including absent data and escaping."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from runledger import adapters
from runledger.parser import Run, Usage
from runledger.pricing import apply_costs
from runledger.quality import analyze
from runledger.receipt import render
from runledger.risk import assess
from runledger.summarize import apply_templates

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample_session.jsonl"
EVIL = '<script>alert(1)</script><img src=x onerror="alert(2)">'


def _pipeline(path):
    run = adapters.detect(path).parse(path)
    apply_costs(run)
    apply_templates(run)
    score, level, risks = assess(run)
    return run, score, level, risks


def _sample():
    return _pipeline(SAMPLE)


def _ai(**overrides):
    review = {
        "model": "claude-sonnet-5-5",
        "verdict": "needs_review",
        "summary": "Removed a legacy spec and read a template file.",
        "risk_assessments": [
            {"step": 7, "code": "test_deleted", "assessment": "confirmed",
             "explanation": "Removes a real test file."},
            {"step": 4, "code": "secret_file", "assessment": "false_positive",
             "explanation": "Only a template was read."},
        ],
        "diff_explanations": [{"file": "src/payments/retry.ts", "explanation": "Adds exponential backoff."}],
        "cost_usd": 0.0123,
        "tokens": {"input": 1200, "output": 300},
    }
    review.update(overrides)
    return review


def _analysed(with_ai=True):
    run, score, level, risks = _sample()
    analyze(run, risks)
    if with_ai:
        run.ai_review = _ai()
    return run, score, level, risks


# ---------------------------------------------------------------- presence and order

def test_html_has_all_three_sections_when_analysed():
    run, score, level, risks = _analysed()
    html = render(run, score, level, risks, "html")
    assert "<h2>Quality score</h2>" in html
    assert "<h2>Cost recommendations</h2>" in html
    assert "<h2>AI risk review</h2>" in html
    assert "AI explanation — the rule-based score is unchanged." in html


def test_html_sections_follow_the_receipt_order():
    run, score, level, risks = _analysed()
    html = render(run, score, level, risks, "html")
    order = [html.index(h) for h in ("<h2>Overview</h2>", "<h2>Quality score</h2>", "<h2>Risk ·",
                                     "<h2>AI risk review</h2>", "<h2>Cost recommendations</h2>",
                                     "<h2>Files changed</h2>", "<h2>Steps</h2>")]
    assert order == sorted(order)


def test_html_hides_the_sections_without_analysis():
    run, score, level, risks = _sample()
    html = render(run, score, level, risks, "html")
    for text in ("Quality score", "Cost recommendations", "AI risk review", "qhead", "ai-review"):
        assert text not in html


def test_markdown_has_the_sections_when_analysed():
    run, score, level, risks = _analysed()
    md = render(run, score, level, risks, "md")
    assert "## Quality score" in md
    assert "## Cost recommendations" in md
    assert "## AI risk review" in md
    assert md.index("## Quality score") < md.index("## Why it was flagged") < md.index("## AI risk review")


def test_markdown_hides_the_sections_without_analysis():
    run, score, level, risks = _sample()
    md = render(run, score, level, risks, "md")
    assert "## Quality score" not in md
    assert "## Cost recommendations" not in md
    assert "## AI risk review" not in md
    assert "— AI:" not in md


def test_json_keys_when_analysed():
    run, score, level, risks = _analysed()
    data = json.loads(render(run, score, level, risks, "json"))
    assert data["quality"]["grade"] == "B"
    assert data["quality"]["score"] == run.quality["score"]
    assert data["recommendations"][0]["kind"] == "model_downgrade"
    assert data["ai_review"]["verdict"] == "needs_review"


def test_json_keys_when_absent():
    run, score, level, risks = _sample()
    data = json.loads(render(run, score, level, risks, "json"))
    assert data["quality"] is None
    assert data["recommendations"] == []
    assert data["ai_review"] is None


def test_json_keeps_the_existing_keys():
    run, score, level, risks = _analysed()
    data = json.loads(render(run, score, level, risks, "json"))
    for key in ("session_id", "risk", "totals", "models", "files", "steps", "overview"):
        assert key in data


# ---------------------------------------------------------------- quality section

def test_quality_badge_and_signal_table_render():
    run, score, level, risks = _analysed()
    html = render(run, score, level, risks, "html")
    assert f"<b>{run.quality['score']}/100</b>" in html
    assert 'class="grade gB">B</span>' in html
    assert "Last test run" in html and "Cost per changed line" in html
    assert "The last test run passed (142 passed, 0 failed)." in html


def test_signed_points_use_an_ascii_minus_in_the_quality_section():
    run, score, level, risks = _analysed()
    html = render(run, score, level, risks, "html")
    section = html[html.index("<h2>Quality score</h2>"):html.index("<h2>Risk ·")]
    assert ">-10<" in section
    assert "−" not in section


def test_markdown_quality_table_rows():
    run, score, level, risks = _analysed()
    md = render(run, score, level, risks, "md")
    assert "| Last test run | pass | +15 |" in md
    assert "| Tests deleted, skipped or weakened | 1 | -10 |" in md


# ---------------------------------------------------------------- recommendations section

def test_recommendation_cards_show_estimated_savings():
    run, score, level, risks = _analysed()
    html = render(run, score, level, risks, "html")
    assert "Run read-only steps on Haiku 5.5" in html
    assert "est. saving $0.042" in html
    assert "Steps 1, 2" in html
    assert "Estimated saving:" in html


def test_markdown_recommendation_bullet():
    run, score, level, risks = _analysed()
    md = render(run, score, level, risks, "md")
    assert "- **Run read-only steps on Haiku 5.5** · Model downgrade · est. saving $0.042 · steps 1, 2" in md


def test_no_material_savings_message_when_analysed_without_recommendations():
    run, score, level, risks = _analysed(with_ai=False)
    run.recommendations = []
    html = render(run, score, level, risks, "html")
    md = render(run, score, level, risks, "md")
    assert "No material savings were found for this run." in html
    assert "No material savings were found for this run." in md


def test_recommendation_without_estimate_says_so():
    run, score, level, risks = _sample()
    run.recommendations = [{"kind": "unknown_pricing", "title": "No price for 1 model(s)",
                            "detail": "No list price is known for gpt-5-codex.", "est_savings_usd": None,
                            "steps": [1, 2]}]
    html = render(run, score, level, risks, "html")
    assert "saving not estimated" in html
    assert "Unknown price" in html


# ---------------------------------------------------------------- AI risk review

def test_ai_badges_sit_next_to_the_rule_risks():
    run, score, level, risks = _analysed()
    html = render(run, score, level, risks, "html")
    blocks = [b for b in html.split('<div class="risk">') if "test file" in b or "secrets file" in b]
    assert any('<span class="ai confirmed" title="Removes a real test file.">AI: Confirmed</span>' in b
               for b in blocks)
    assert any('<span class="ai false_positive" title="Only a template was read.">AI: False positive</span>' in b
               for b in blocks)


def test_markdown_suffix_next_to_the_rule_risk():
    run, score, level, risks = _analysed()
    md = render(run, score, level, risks, "md")
    assert "Deleted a test file (retry.legacy.spec.ts) — AI: confirmed" in md


@pytest.mark.parametrize("verdict, label, cls", [
    ("looks_safe", "Looks safe", "safe"),
    ("needs_review", "Needs review", "review"),
    ("dangerous", "Dangerous", "danger"),
])
def test_verdict_badges(verdict, label, cls):
    run, score, level, risks = _analysed()
    run.ai_review = _ai(verdict=verdict)
    html = render(run, score, level, risks, "html")
    assert f'<span class="verdict {cls}">{label}</span>' in html
    assert f"Verdict: {label}" in render(run, score, level, risks, "md")


def test_unknown_verdict_is_shown_as_not_rated_without_injection():
    run, score, level, risks = _analysed()
    run.ai_review = _ai(verdict='"><script>x</script>')
    html = render(run, score, level, risks, "html")
    assert '<span class="verdict review">Not rated</span>' in html
    assert "<script>" not in html


def test_model_and_analysis_cost_are_shown():
    run, score, level, risks = _analysed()
    html = render(run, score, level, risks, "html")
    assert "claude-sonnet-5-5 · analysis cost $0.012 (1.2k in, 300 out)" in html
    md = render(run, score, level, risks, "md")
    assert "model claude-sonnet-5-5 · analysis cost $0.012 (1.2k in, 300 out)" in md


def test_diff_explanations_are_listed():
    run, score, level, risks = _analysed()
    html = render(run, score, level, risks, "html")
    assert "Changes explained" in html and "Adds exponential backoff." in html
    assert "- `src/payments/retry.ts`: Adds exponential backoff." in render(run, score, level, risks, "md")


def test_ai_review_without_risk_assessments_renders():
    run, score, level, risks = _analysed()
    run.ai_review = _ai(risk_assessments=[], diff_explanations=[])
    html = render(run, score, level, risks, "html")
    assert "<h2>AI risk review</h2>" in html
    assert "Risk assessments" not in html


# ---------------------------------------------------------------- escaping

def test_hostile_text_is_escaped_in_every_section_of_the_html():
    run, score, level, risks = _sample()
    run.prompts = [EVIL]
    run.final_message = EVIL
    analyze(run, risks)
    run.quality["signals"][0]["note"] = EVIL
    run.quality["signals"][1]["name"] = EVIL
    run.quality["signals"][2]["value"] = EVIL
    run.quality["grade"] = EVIL
    run.recommendations = [{"kind": EVIL, "title": EVIL, "detail": EVIL, "est_savings_usd": 0.5,
                            "steps": [1, "x", True]}]
    run.ai_review = _ai(
        model=EVIL, verdict=EVIL, summary=EVIL,
        risk_assessments=[
            {"step": 7, "code": "test_deleted", "assessment": "confirmed",
             "explanation": '"><script>alert(3)</script>'},
            {"step": 4, "code": EVIL, "assessment": EVIL, "explanation": EVIL},
        ],
        diff_explanations=[{"file": EVIL, "explanation": EVIL}],
    )
    html = render(run, score, level, risks, "html")
    assert "<script" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&quot;&gt;&lt;script&gt;alert(3)&lt;/script&gt;" in html  # attribute breakout escaped
    assert 'class="grade gX">–</span>' in html  # a grade outside A-F is shown as a dash


def test_hostile_text_is_escaped_in_the_markdown_tables():
    run, score, level, risks = _analysed(with_ai=False)
    run.quality["signals"][0]["note"] = "a | b\nnext line"
    md = render(run, score, level, risks, "md")
    assert "a \\| b next line" in md
    row = [line for line in md.splitlines() if "a \\| b next line" in line]
    # 4 cells need 5 structural pipes; the escaped pipe in the note adds one more
    assert len(row) == 1 and row[0].count("|") == 6


def test_unknown_grade_is_shown_as_a_dash():
    run, score, level, risks = _analysed(with_ai=False)
    run.quality["grade"] = "<b>x</b>"
    html = render(run, score, level, risks, "html")
    assert 'class="grade gX">–</span>' in html
    assert "<b>x</b>" not in html


def test_unknown_assessment_is_shown_as_uncertain():
    run, score, level, risks = _analysed()
    run.ai_review = _ai(risk_assessments=[{"step": 7, "code": "test_deleted", "assessment": "<x>",
                                           "explanation": "y"}])
    html = render(run, score, level, risks, "html")
    assert '<span class="ai uncertain" title="y">AI: Uncertain</span>' in html


def test_non_integer_step_numbers_do_not_break_the_render():
    run, score, level, risks = _analysed()
    run.ai_review = _ai(risk_assessments=[{"step": ["7"], "code": "test_deleted", "assessment": "confirmed",
                                           "explanation": "z"}])
    run.recommendations = [{"kind": "retry_loop", "title": "t", "detail": "d", "est_savings_usd": None,
                            "steps": ["1", None]}]
    assert "<h2>AI risk review</h2>" in render(run, score, level, risks, "html")
    assert "## Cost recommendations" in render(run, score, level, risks, "md")


# ---------------------------------------------------------------- fixtures

@pytest.mark.parametrize("name", [
    "sample_session.jsonl", "codex_session.jsonl", "native_session.json", "aider/.aider.chat.history.md",
])
def test_fixture_runs_analyse_and_render_in_every_format(name):
    run, score, level, risks = _pipeline(FIXTURES / name)
    analyze(run, risks)
    run.ai_review = _ai()
    html = render(run, score, level, risks, "html")
    md = render(run, score, level, risks, "md")
    data = json.loads(render(run, score, level, risks, "json"))
    assert "<h2>Quality score</h2>" in html and "## Quality score" in md
    assert data["quality"]["grade"] in ("A", "B", "C", "D", "F")
    assert 0 <= data["quality"]["score"] <= 100
    assert data["ai_review"]["verdict"] == "needs_review"
    assert data["recommendations"] == run.recommendations


def test_json_quality_round_trips_exactly():
    run, score, level, risks = _analysed()
    data = json.loads(render(run, score, level, risks, "json"))
    assert data["quality"] == json.loads(json.dumps(run.quality))
    assert data["recommendations"] == json.loads(json.dumps(run.recommendations))


def test_bare_run_renders_without_analysis_data():
    run = Run(session_id="abcdef12", path="x", cwd=None, git_branch=None, started=None, ended=None,
              prompts=[], steps=[], final_message="", usage=Usage(), models={})
    html = render(run, 0, "Low", [], "html")
    md = render(run, 0, "Low", [], "md")
    data = json.loads(render(run, 0, "Low", [], "json"))
    assert "<!doctype html>" in html and "## Overview" in md
    assert data["quality"] is None and data["recommendations"] == [] and data["ai_review"] is None
