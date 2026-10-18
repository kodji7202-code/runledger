"""Render a Run as a self-contained HTML receipt, Markdown, or JSON."""
from __future__ import annotations

import html
import json
import re
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from . import labels
from .adapters import label as agent_label
from .guard import redact
from .guard_receipt import guard_counts, guard_summary
from .parser import Run, Step, Usage
from .pricing import (PRICING_HELP, UNATTRIBUTED, UNATTRIBUTED_NOTE, friendly_model, is_stale,
                      partial_note, stale_note, unknown_models, unpriced)
from .risk import Risk
from .summarize import FileChange, file_changes


def _money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"${v:.4f}" if v < 0.01 else f"${v:.3f}" if v < 1 else f"${v:.2f}"


def _tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _run_tok(run: Run, n: int) -> str:
    """Show unknown usage as n/a, so a missing settings file never reads as zero tokens."""
    return "n/a" if run.usage_source == "missing" else _tok(n)


def _step_known(run: Run, step: Step) -> bool:
    return step.usage_known and run.usage_source != "missing"


def _step_tok(run: Run, step: Step) -> str:
    return _tok(step.usage.total) if _step_known(run, step) else "n/a"


def _usage_note(run: Run) -> str:
    if run.usage_source == "missing":
        stem = run.path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0] or run.session_id
        return (f"Token usage not found: Droid keeps it in {redact(stem)}.settings.json "
                "next to the session file.")
    if any(s.usage_estimated for s in run.steps):
        source = f" in {redact(run.usage_file)}" if run.usage_file else ""
        return f"Per-step tokens are estimated: an even split of the session total{source}."
    return ""


def _dur(sec: Optional[float]) -> str:
    if sec is None:
        return "n/a"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


def _run_money(run: Run) -> str:
    """The run estimate; a lower bound reads `≥ $X (N models unpriced)`."""
    note = partial_note(run)
    return f"≥ {_money(run.cost)} ({note})" if note else _money(run.cost)


def _cost_text(run: Run) -> str:
    """Estimate from list prices, the agent's own figure, or both."""
    if run.cost is not None and run.reported_cost is not None:
        return f"{_run_money(run)} est. · {_money(run.reported_cost)} reported by agent"
    if run.cost is not None:
        return _run_money(run)
    if run.reported_cost is not None:
        return f"{_money(run.reported_cost)} reported by agent"
    return "n/a"


def _cost_card(run: Run) -> str:
    if run.cost is None and run.reported_cost is None:
        return '<div class="card"><span>Cost</span><b>n/a</b></div>'
    if run.cost is not None:
        note = (f"<small>est. · reported by agent {_money(run.reported_cost)}</small>"
                if run.reported_cost is not None else "")
        if run.cost_partial:
            note = f"<small>at least · {_e(partial_note(run))}</small>" + note
        return f'<div class="card"><span>Cost</span><b>{_money(run.cost)}</b>{note}</div>'
    return (f'<div class="card"><span>Cost</span><b>{_money(run.reported_cost)}</b>'
            "<small>reported by agent</small></div>")


# ---------------------------------------------------------------- analysis sections
# Quality score, cost recommendations and the AI risk review. Each section appears only
# when its data is present, so a receipt without analysis renders exactly as before.

_e = html.escape
AI_NOTE = "AI explanation — the rule-based score is unchanged."
_GRADES = ("A", "B", "C", "D", "F")
_KIND_LABELS = {
    "model_downgrade": "Model downgrade",
    "cache_misses": "Prompt cache",
    "retry_loop": "Retry loop",
    "large_reads": "Large reads",
    "unknown_pricing": "Unknown price",
}
_SIGNAL_LABELS = {
    "tests_run": "Test runs",
    "test_outcome": "Last test run",
    "failing_then_passing": "Fixed after failing tests",
    "failing_streak": "Longest failing streak",
    "test_tampering": "Tests deleted, skipped or weakened",
    "test_changes": "Tests replaced or changed",
    "error_rate": "Steps with errors",
    "retry_loops": "Repeated commands and edits",
    "scope": "Changes unrelated to the request",
    "unfinished": "Unfinished work",
    "cost_efficiency": "Cost per changed line",
    "risk_level": "Rule-based risk",
}
_VERDICTS = {
    "looks_safe": ("Looks safe", "safe"),
    "needs_review": ("Needs review", "review"),
    "dangerous": ("Dangerous", "danger"),
}
_ASSESSMENTS = {
    "confirmed": ("Confirmed", "confirmed"),
    "false_positive": ("False positive", "false_positive"),
    "uncertain": ("Uncertain", "uncertain"),
}
ANALYSIS_CSS = """
.grade{display:inline-grid;place-items:center;width:52px;height:52px;border-radius:14px;font:700 26px ui-monospace,monospace;flex:none}
.grade.gA,.grade.gB{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}
.grade.gC,.grade.gD{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--amber)}
.grade.gF{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--red)}
.grade.gX{background:var(--line);color:var(--muted)}
.qhead{display:flex;gap:14px;align-items:center;margin-bottom:14px}.qhead b{font-size:20px}
.up{color:var(--accent)}.down{color:var(--red)}
.verdict{font:700 12px ui-monospace,monospace;padding:4px 10px;border-radius:99px;white-space:nowrap}
.verdict.safe,.ai.false_positive{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}
.verdict.review,.ai.uncertain{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--amber)}
.verdict.danger,.ai.confirmed{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--red)}
.ai{font:700 11px ui-monospace,monospace;padding:2px 8px;border-radius:99px;margin-left:6px;white-space:nowrap}
.ai-note{color:var(--muted);font-size:13px;font-style:italic;margin:0 0 10px}
.ai-review h3{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:20px 0 8px;font-weight:500}
.recs{display:grid;gap:12px}
.rec{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.rec-top{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}
.rec-save{font:600 13px ui-monospace,monospace;color:var(--accent)}
.rec-title{font-weight:600;margin-top:8px}
.rec-detail{color:var(--muted);font-size:14px;margin-top:4px}
"""


def _num(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _real(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _steps_text(steps: Any) -> str:
    nums = [str(s) for s in (steps or []) if isinstance(s, int) and not isinstance(s, bool)]
    return ", ".join(nums[:30]) + (" …" if len(nums) > 30 else "")


def _signed(points: int) -> str:
    return f"+{points}" if points > 0 else f"-{-points}" if points < 0 else "0"


def _signal_value(sig: Dict[str, Any]) -> str:
    value = sig.get("value")
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        if sig.get("name") == "error_rate":
            return f"{value:.0%}"
        if sig.get("name") == "cost_efficiency":
            return f"{_money(float(value))}/line"
        return f"{value:g}"
    return str(value)


def _grade_parts(quality: Dict[str, Any]) -> Tuple[str, str]:
    """(text to show, CSS letter). Anything but A-F is shown as a dash."""
    grade = str(quality.get("grade") or "")
    return (grade, grade) if grade in _GRADES else ("–", "X")


def _recs_list(run: Run) -> List[Dict[str, Any]]:
    return [r for r in (run.recommendations or []) if isinstance(r, dict)]


def _ai_dict(run: Run) -> Dict[str, Any]:
    return run.ai_review if isinstance(run.ai_review, dict) else {}


def _ai_map(ai: Dict[str, Any]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for a in ai.get("risk_assessments") or []:
        if isinstance(a, dict) and isinstance(a.get("step"), int) and not isinstance(a.get("step"), bool):
            out.setdefault((a["step"], str(a.get("code") or "")), a)
    return out


def _ai_facts(ai: Dict[str, Any]) -> Dict[str, Any]:
    """The AI review with every enum mapped through a fixed table and every text left raw."""
    verdict, verdict_cls = _VERDICTS.get(str(ai.get("verdict") or ""), ("Not rated", "review"))
    cost = _real(ai.get("cost_usd"))
    tokens = ai.get("tokens") if isinstance(ai.get("tokens"), dict) else {}
    assessments = []
    for a in ai.get("risk_assessments") or []:
        if not isinstance(a, dict):
            continue
        step = a.get("step")
        label, cls = _ASSESSMENTS.get(str(a.get("assessment") or ""), ("Uncertain", "uncertain"))
        assessments.append({
            "step": step if isinstance(step, int) and not isinstance(step, bool) else None,
            "code": str(a.get("code") or ""), "label": label, "cls": cls,
            "explanation": str(a.get("explanation") or ""),
        })
    diffs = [{"file": str(d.get("file") or ""), "explanation": str(d.get("explanation") or "")}
             for d in ai.get("diff_explanations") or [] if isinstance(d, dict)]
    return {
        "verdict": verdict, "verdict_cls": verdict_cls,
        "model": str(ai.get("model") or "not recorded"),
        "cost": _money(cost) if cost is not None else "n/a",
        "tokens": f"{_tok(_num(tokens.get('input')))} in, {_tok(_num(tokens.get('output')))} out",
        "summary": str(ai.get("summary") or ""),
        "assessments": assessments, "diffs": diffs,
    }


def _ai_badge_html(risk: Risk, ai_map: Dict[Tuple[int, str], Dict[str, Any]]) -> str:
    a = ai_map.get((risk.step, risk.code))
    if not a:
        return ""
    label, cls = _ASSESSMENTS.get(str(a.get("assessment") or ""), ("Uncertain", "uncertain"))
    tip = str(a.get("explanation") or "")
    return f' <span class="ai {cls}" title="{_e(tip)}">AI: {label}</span>'


def _quality_html(quality: Any) -> str:
    if not isinstance(quality, dict):
        return ""
    shown, letter = _grade_parts(quality)
    score = max(0, min(100, _num(quality.get("score"))))
    rows = []
    for sig in quality.get("signals") or []:
        if not isinstance(sig, dict):
            continue
        name = str(sig.get("name") or "")
        points = _num(sig.get("impact"))
        cls = "up" if points > 0 else "down" if points < 0 else ""
        label = _e(_SIGNAL_LABELS.get(name, name))
        value = _e(_signal_value(sig))
        note = _e(str(sig.get("note") or ""))
        rows.append(f"<tr><td>{label}</td><td class='num'>{value}</td>"
                    f"<td class='num {cls}'>{_signed(points)}</td><td class='meta'>{note}</td></tr>")
    return (
        '<h2>Quality score</h2><div class="overview">'
        f'<div class="qhead"><span class="grade g{letter}">{_e(shown)}</span><div>'
        f'<b>{score}/100</b><div class="meta">Grade {_e(shown)} · starts at 70 and moves with the signals below</div>'
        "</div></div>"
        "<table><tr><th>Signal</th><th style='text-align:right'>Value</th>"
        "<th style='text-align:right'>Points</th><th>Why</th></tr>"
        + "".join(rows) + "</table></div>"
    )


def _rec_card(rec: Dict[str, Any]) -> str:
    kind = str(rec.get("kind") or "")
    label = _KIND_LABELS.get(kind, kind or "Recommendation")
    est = _real(rec.get("est_savings_usd"))
    saving = f"est. saving {_money(est)}" if est is not None else "saving not estimated"
    steps = _steps_text(rec.get("steps"))
    where = f'<div class="meta">Steps {_e(steps)}</div>' if steps else ""
    return (f'<div class="rec"><div class="rec-top"><span class="pill">{_e(label)}</span>'
            f'<span class="rec-save">{_e(saving)}</span></div>'
            f'<div class="rec-title">{_e(str(rec.get("title") or ""))}</div>'
            f'<div class="rec-detail">{_e(str(rec.get("detail") or ""))}</div>{where}</div>')


def _recs_html(run: Run) -> str:
    recs = _recs_list(run)
    if not recs and run.quality is None:
        return ""
    if recs:
        body = '<div class="recs">' + "".join(_rec_card(r) for r in recs) + "</div>"
    else:
        body = '<div class="overview meta">No material savings were found for this run.</div>'
    return f"<h2>Cost recommendations</h2>{body}"


def _ai_html(ai: Dict[str, Any], risks: List[Risk]) -> str:
    if not ai:
        return ""
    facts = _ai_facts(ai)
    reasons = {(r.step, r.code): r.reason for r in risks}
    rows = []
    for a in facts["assessments"]:
        step = "—" if a["step"] is None else str(a["step"])
        rule = reasons.get((a["step"], a["code"]), "")
        rows.append(f"<tr><td class='num'>{_e(step)}</td>"
                    f"<td><code>{_e(a['code'])}</code><div class='meta'>{_e(rule)}</div></td>"
                    f"<td><span class='ai {a['cls']}'>{_e(a['label'])}</span></td>"
                    f"<td>{_e(a['explanation'])}</td></tr>")
    assess_table = ("<h3>Risk assessments</h3><table><tr><th>Step</th><th>Rule risk</th><th>AI</th>"
                    "<th>Explanation</th></tr>" + "".join(rows) + "</table>") if rows else ""
    diff_rows = "".join(f"<tr><td><code>{_e(d['file'])}</code></td><td>{_e(d['explanation'])}</td></tr>"
                        for d in facts["diffs"])
    diff_table = ("<h3>Changes explained</h3><table><tr><th>File</th><th>Explanation</th></tr>"
                  + diff_rows + "</table>") if diff_rows else ""
    return (
        '<h2>AI risk review</h2><div class="overview ai-review">'
        f'<div class="qhead"><span class="verdict {facts["verdict_cls"]}">{_e(facts["verdict"])}</span>'
        f'<span class="meta">{_e(facts["model"])} · analysis cost {_e(facts["cost"])} ({_e(facts["tokens"])})</span></div>'
        f'<p class="ai-note">{_e(AI_NOTE)}</p>'
        f'<p>{_e(facts["summary"])}</p>{assess_table}{diff_table}</div>'
    )


def _md_cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _md_text(text: Any) -> str:
    return str(text).replace("\r", " ").replace("\n", " ").replace("`", "'")


def _md_angles(text: str) -> str:
    """Stop raw HTML tags; unlike html.escape, `&` stays readable in Markdown."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


_MD_LINE_START = re.compile(r"^(\s*)([#+=*-]|\d+[.)])")


def _md_inline(text: Any, line_start: bool = True) -> str:
    """One line of free text that cannot open a heading, list, block quote or HTML tag."""
    text = _md_angles(re.sub(r"\s+", " ", _md_text(text)))
    if not line_start:
        return text
    return _MD_LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2), text)


def _md_summary(text: Any) -> str:
    """A step summary: code spans from the templates stay, text around them is escaped."""
    parts = re.sub(r"\s+", " ", str(text)).split("`")
    if len(parts) % 2 == 0:  # unpaired backtick from an AI summary
        return _md_inline(text)
    return "".join(f"`{p}`" if i % 2 else _md_inline(p, i == 0) if p.strip() else p
                   for i, p in enumerate(parts))


def _md_code(text: Any) -> str:
    return "`" + _md_text(text) + "`"


def _quality_md(quality: Any) -> List[str]:
    if not isinstance(quality, dict):
        return []
    shown, _ = _grade_parts(quality)
    score = max(0, min(100, _num(quality.get("score"))))
    out = ["## Quality score",
           f"**{score}/100 · grade {shown}** · starts at 70 and moves with the signals below", "",
           "| Signal | Value | Points | Why |", "|---|---|---:|---|"]
    for sig in quality.get("signals") or []:
        if isinstance(sig, dict):
            name = str(sig.get("name") or "")
            out.append(f"| {_md_cell(_SIGNAL_LABELS.get(name, name))} | {_md_cell(_signal_value(sig))} "
                       f"| {_signed(_num(sig.get('impact')))} | {_md_cell(sig.get('note') or '')} |")
    out.append("")
    return out


def _recs_md(run: Run) -> List[str]:
    recs = _recs_list(run)
    if not recs and run.quality is None:
        return []
    out = ["## Cost recommendations"]
    if not recs:
        return out + ["No material savings were found for this run.", ""]
    for r in recs:
        kind = str(r.get("kind") or "")
        label = _KIND_LABELS.get(kind, kind or "Recommendation")
        est = _real(r.get("est_savings_usd"))
        saving = f"est. saving {_money(est)}" if est is not None else "saving not estimated"
        steps = _steps_text(r.get("steps"))
        where = f" · steps {steps}" if steps else ""
        out.append(f"- **{_md_text(r.get('title') or '')}** · {label} · {saving}{where}")
        out.append(f"  {_md_text(r.get('detail') or '')}")
    out.append("")
    return out


def _ai_md(ai: Dict[str, Any], risks: List[Risk]) -> List[str]:
    if not ai:
        return []
    facts = _ai_facts(ai)
    out = ["## AI risk review",
           f"**Verdict: {facts['verdict']}** · model {_md_text(facts['model'])} · "
           f"analysis cost {facts['cost']} ({facts['tokens']})", "", AI_NOTE, ""]
    if facts["summary"]:
        out += [_md_text(facts["summary"]), ""]
    for a in facts["assessments"]:
        step = "?" if a["step"] is None else str(a["step"])
        out.append(f"- Step {step} · `{_md_text(a['code'])}`: **{a['label']}** — {_md_text(a['explanation'])}")
    if facts["diffs"]:
        out += ["", "Changes explained:"]
        out += [f"- `{_md_text(d['file'])}`: {_md_text(d['explanation'])}" for d in facts["diffs"]]
    out.append("")
    return out


def _ai_md_suffix(risk: Risk, ai_map: Dict[Tuple[int, str], Dict[str, Any]]) -> str:
    a = ai_map.get((risk.step, risk.code))
    if not a:
        return ""
    label, _ = _ASSESSMENTS.get(str(a.get("assessment") or ""), ("Uncertain", "uncertain"))
    return f" — AI: {label.lower()}"


# ---------------------------------------------------------------- redaction

# Step inputs that reach a receipt: file paths in "Files changed" (rm arguments come from the command).
_SHOWN_INPUT_KEYS = ("file_path", "notebook_path", "path", "command", "url", "pattern", "query")


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    return value


def redact_run(run: Run, risks: List[Risk]) -> None:
    """Mask secrets in every text a receipt, a push or a PR comment shows, in place.
    Call it after scoring and analysis: the risk rules need the raw text to find secrets.
    File contents and command output are not shown, so they are left as they are."""
    # Read the whole-path labels of the raw commands now: masking them takes a spaced drive path
    # apart, and the renderers put a label on the output copy before it is redacted
    # (`labels.show_reason`), never after it.
    if run.show_labels is None:
        run.show_labels = labels.step_label_map(run)
    run.session_id = redact(run.session_id)
    run.agent = redact(run.agent)
    run.cwd = redact(run.cwd) if run.cwd is not None else None
    run.git_branch = redact(run.git_branch) if run.git_branch is not None else None
    run.git_changes = {redact(p): c for p, c in run.git_changes.items()}
    run.started = redact(run.started) if run.started is not None else None
    run.ended = redact(run.ended) if run.ended is not None else None
    redacted_models: Dict[str, Usage] = {}
    for model, usage in run.models.items():
        redacted_models.setdefault(redact(model), Usage()).add(usage)
    run.models = redacted_models
    run.prompts = [redact(p) for p in run.prompts]
    run.overall_summary = redact(run.overall_summary)
    run.final_message = redact(run.final_message)
    for s in run.steps:
        s.tool = redact(s.tool)
        s.model = redact(s.model) if s.model is not None else None
        s.summary = redact(s.summary)
        for key in _SHOWN_INPUT_KEYS:
            if isinstance(s.input.get(key), str):
                s.input[key] = redact(s.input[key])
    every = {id(r): r for r in list(risks) + [r for s in run.steps for r in s.risks]}
    for r in every.values():
        r.reason = redact(r.reason)
    run.quality = _redact_value(run.quality)
    run.recommendations = _redact_value(run.recommendations)
    run.ai_review = _redact_value(run.ai_review)
    run.guard = _redact_value(run.guard)
    run.notes = [redact(n) for n in (run.notes or []) if isinstance(n, str)]


# ---------------------------------------------------------------- output formats

def _guard_html(run: Run) -> str:
    if not run.guard:
        return ""
    cards = []
    for decision in run.guard:
        step = f'step {decision["step"]}' if decision["step"] is not None else "unmatched"
        codes = ", ".join(decision["codes"])
        cards.append(
            f'<div class="card"><span>{_e(decision["decision"].upper())} · '
            f'{_e(decision["tool"])} · {step}</span>'
            f'<div><strong>{_e(decision["outcome"])}</strong></div>'
            f'<div><code>{_e(decision["summary"])}</code></div>'
            f'<div>{_e(decision["reason"])}</div>'
            f'<small>{_e(codes)} · {_e(decision["ts"] or "")}</small></div>'
        )
    return '<h2>Guard decisions</h2><div class="guard-decisions">' + "".join(cards) + "</div>"


def _guard_md(run: Run) -> List[str]:
    if not run.guard:
        return []
    out = ["## Guard decisions", "",
           "| Step | Tool | Summary | Decision | Outcome | Reason | Codes | Timestamp |",
           "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for decision in run.guard:
        cells = [decision["step"] if decision["step"] is not None else "unmatched",
                 decision["tool"], decision["summary"], decision["decision"],
                 decision["outcome"], decision["reason"], ", ".join(decision["codes"]),
                 decision["ts"] or ""]
        out.append("| " + " | ".join(_md_cell(_md_angles(str(c))) for c in cells) + " |")
    return out + [""]


def _rollback(run: Run) -> Optional[Dict[str, str]]:
    from .rollback import ID_RE
    if not run.checkpoint_id or not ID_RE.fullmatch(run.checkpoint_id):
        return None
    return {"checkpoint_id": run.checkpoint_id,
            "command": f"runledger rollback {run.checkpoint_id}"}


# JSON receipt contract (receipt.schema.json, docs/receipt-json.md). Within a version, changes
# are additive only; removing a key or changing its type needs a new version.
SCHEMA_VERSION = 1


def md_trailer(run: Run) -> str:
    """A last line that records the mode in a Markdown receipt without showing it."""
    return f"<!-- runledger: safe={'true' if run.safe else 'false'} schema={SCHEMA_VERSION} -->\n"


def _pricing_note(run: Run) -> Optional[str]:
    notes: List[str] = []
    if unknown_models(run):
        notes.append(PRICING_HELP)
    if is_stale():
        notes.append(stale_note())
    return " ".join(notes) if notes else None


def to_dict(run: Run, score: int, level: str, risks: List[Risk]) -> Dict:
    changes = file_changes(run)
    by_step = labels.step_label_map(run)
    data = {
        "schema_version": SCHEMA_VERSION,
        "runledger_version": __version__,
        "safe": run.safe,
        "session_id": run.session_id,
        "rollback": _rollback(run),
        "agent": agent_label(run.agent), "agent_id": run.agent,
        "cwd": run.cwd, "cwd_source": run.cwd_source, "git_branch": run.git_branch,
        "git_base": run.git_base,
        "started": run.started, "ended": run.ended,
        "duration_seconds": run.duration_seconds,
        "request": run.prompts[:3],
        "overview": run.overall_summary,
        "risk": {"score": score, "level": level,
                 "reasons": [asdict(r) for r in labels.show_risks(risks, by_step)]},
        "totals": {"steps": len(run.steps), "tokens": run.usage.total,
                   "input_tokens": run.usage.input_tokens, "output_tokens": run.usage.output_tokens,
                   "cache_write_tokens": run.usage.cache_write_tokens,
                   "cache_read_tokens": run.usage.cache_read_tokens,
                   "cost_usd": run.cost, "reported_cost_usd": run.reported_cost,
                   "cost_partial": run.cost_partial, "unknown_models": unknown_models(run),
                   "files_changed": len(changes), "usage_source": run.usage_source},
        "models": {m: {"tokens": u.total} for m, u in run.models.items()},
        "pricing_note": _pricing_note(run),
        "files": [asdict(c) for c in changes.values()],
        "steps": [{"n": s.index, "tool": s.tool, "summary": s.summary, "model": s.model,
                   "tokens": s.usage.total, "tokens_known": _step_known(run, s),
                   "tokens_estimated": s.usage_estimated,
                   "cost_usd": s.cost, "error": s.is_error,
                   "risks": [asdict(r) for r in labels.show_risks(s.risks, by_step)]}
                  for s in run.steps],
        "quality": run.quality,
        "recommendations": _recs_list(run),
        "ai_review": run.ai_review if run.ai_review else None,
    }
    if run.guard:
        data["guard"] = run.guard
        data["totals"]["guard"] = guard_counts(run.guard)
    if run.notes:
        data["notes"] = list(run.notes)
    return data


def to_markdown(run: Run, score: int, level: str, risks: List[Risk]) -> str:
    changes = file_changes(run)
    by_step = labels.step_label_map(run)
    out = [f"# Run receipt · {run.session_id[:8]}", ""]
    out.append(f"- **Agent:** {agent_label(run.agent)}  ·  **Folder:** {_md_code(run.cwd) if run.cwd else '-'}  ·  **Branch:** {_md_code(run.git_branch or '-')}  ·  **Duration:** {_dur(run.duration_seconds)}")
    out.append(f"- **Risk:** {score}/100 ({level})  ·  **Cost:** {_cost_text(run)}  ·  **Tokens:** {_run_tok(run, run.usage.total)}  ·  **Files changed:** {len(changes)}")
    if run.guard:
        out.append(f"- **Guard:** {guard_summary(run.guard)}")
    for n in run.notes or []:
        out.append(f"- **Note:** {_md_inline(n)}")
    if unknown_models(run):
        out.append(f"- **Unknown model price:** {PRICING_HELP}")
    if is_stale():
        out.append(f"- **Pricing note:** {stale_note()}")
    if UNATTRIBUTED in unpriced(run):
        out.append(f"- **Unattributed tokens:** {UNATTRIBUTED_NOTE}")
    rollback = _rollback(run)
    if rollback:
        out.append(f"- **Rollback:** `{rollback['command']}` (creates a safety checkpoint; run in this repository)")
    out += ["", "## Overview", _md_inline(run.overall_summary), ""]
    out += _quality_md(run.quality)
    ai = _ai_dict(run)
    ai_map = _ai_map(ai) if ai else {}
    if risks:
        out.append("## Why it was flagged")
        for r in labels.show_risks(risks, by_step):
            out.append(f"- **{r.severity.upper()}** · step {r.step}: {_md_inline(r.reason)}{_ai_md_suffix(r, ai_map)}")
        out.append("")
    out += _ai_md(ai, risks)
    out += _recs_md(run)
    out += _guard_md(run)
    if changes:
        out.append("## Files changed")
        for c in changes.values():
            tag = " (deleted)" if c.deleted else " (new)" if c.created else ""
            counts = "(binary)" if c.binary else f"+{c.added} −{c.removed}"
            out.append(f"- {_md_code(c.path)}{tag} {counts}")
        out.append("")
    out.append("## Steps")
    for s in run.steps:
        flag = " ⚠️" if s.risks else ""
        out.append(f"{s.index}. {_md_summary(s.summary)}{flag} — {friendly_model(s.model)}, {_step_tok(run, s)} tok, {_money(s.cost)}")
    note = _usage_note(run)
    if note:
        out += ["", _md_text(note)]
    return "\n".join(out) + "\n"


CSS = """
:root{--bg:#0a0b0d;--panel:#111316;--line:#1f2328;--text:#eef0f2;--muted:#9aa1ab;--accent:#4fe0b0;--amber:#f5b547;--red:#ff6b6b}
@media (prefers-color-scheme: light){:root{--bg:#f7f8f9;--panel:#fff;--line:#e3e6ea;--text:#121417;--muted:#5d6670;--accent:#047857;--amber:#b45309;--red:#c62828}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:40px 20px 80px}
.brand{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px;letter-spacing:.04em;text-transform:uppercase}
.mark{width:26px;height:26px;border-radius:7px;background:var(--accent);color:var(--bg);display:grid;place-items:center;font:700 12px ui-monospace,monospace}
h1{font-size:28px;margin:14px 0 4px}h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:36px 0 12px}
.meta{color:var(--muted);font:13px ui-monospace,SFMono-Regular,Menlo,monospace}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:24px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card b{display:block;font-size:22px}.card span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.bar{height:8px;border-radius:99px;background:var(--line);overflow:hidden;margin-top:10px}.bar i{display:block;height:100%}
.overview{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.risk{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}.risk:last-child{border:0}
.sev{font:700 11px ui-monospace,monospace;padding:2px 8px;border-radius:99px;height:fit-content;white-space:nowrap}
.sev.high{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--red)}.sev.medium{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--amber)}.sev.low{background:var(--line);color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td.num{font:13px ui-monospace,monospace;color:var(--muted);white-space:nowrap;text-align:right}
tr.flag td:first-child{box-shadow:inset 3px 0 var(--amber)}tr.flag.high td:first-child{box-shadow:inset 3px 0 var(--red)}
.pill{font:12px ui-monospace,monospace;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:1px 6px;white-space:nowrap}
code{font:13px ui-monospace,monospace;background:var(--line);padding:1px 5px;border-radius:5px;word-break:break-all}
.add{color:var(--accent)}.rem{color:var(--red)}
.card small{display:block;color:var(--muted);font-size:12px;margin-top:4px}
.guard-decisions{display:grid;gap:12px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""


def to_html(run: Run, score: int, level: str, risks: List[Risk]) -> str:
    e = html.escape
    changes = file_changes(run)
    by_step = labels.step_label_map(run)
    color = "var(--accent)" if level == "Low" else "var(--amber)" if level == "Medium" else "var(--red)"

    ai = _ai_dict(run)
    ai_map = _ai_map(ai) if ai else {}
    risk_html = "".join(
        f'<div class="risk"><span class="sev {r.severity}">{r.severity.upper()}</span>'
        f'<div>{e(r.reason)} <span class="meta">· step {r.step}</span>{_ai_badge_html(r, ai_map)}</div></div>'
        for r in labels.show_risks(risks, by_step)
    ) or '<div class="risk"><span class="sev low">OK</span><div>No risky actions detected by the rules.</div></div>'
    recs = _recs_list(run)
    has_analysis = run.quality is not None or bool(recs) or bool(ai)
    analysis_css = ANALYSIS_CSS if has_analysis else ""
    quality_html = _quality_html(run.quality)
    ai_html = _ai_html(ai, risks)
    recs_html = _recs_html(run)

    files_html = "".join(
        f"<tr><td><code>{e(c.path)}</code>{' <span class=pill>deleted</span>' if c.deleted else ' <span class=pill>new</span>' if c.created else ''}</td>"
        + ('<td class="num">(binary)</td></tr>' if c.binary else
           f'<td class="num"><span class="add">+{c.added}</span> <span class="rem">−{c.removed}</span></td></tr>')
        for c in changes.values()
    ) or '<tr><td colspan="2" class="meta">No files changed.</td></tr>'

    rows = []
    for s in run.steps:
        step_risks = labels.show_risks(s.risks, by_step)
        worst = "high" if any(r.severity == "high" for r in s.risks) else ("medium" if s.risks else "")
        cls = f' class="flag {worst}"' if s.risks else ""
        summary = e(s.summary)
        # render `code` spans from templates
        parts = summary.split("`")
        summary = "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))
        rows.append(
            f"<tr{cls}><td class='num'>{s.index}</td><td>{summary}"
            + ("".join(f" <span class='sev {r.severity}'>{e(r.reason)}</span>" for r in step_risks))
            + ("" if not s.is_error else " <span class='pill'>error</span>")
            + f"</td><td><span class='pill'>{e(friendly_model(s.model))}</span></td>"
            f"<td class='num'>{_step_tok(run, s)}</td><td class='num'>{_money(s.cost)}</td></tr>"
        )
    models = ", ".join(f"{friendly_model(m)} ({_tok(u.total)})" for m, u in run.models.items())
    request = e(run.prompts[0][:400]) if run.prompts else "—"
    reported_note = " Reported cost comes from the agent itself." if run.reported_cost is not None else ""
    usage_note = _usage_note(run)
    usage_note = f" {e(usage_note)}" if usage_note else ""
    pricing_note = (f'<div class="meta">Unknown model price. {e(PRICING_HELP)}</div>'
                    if unknown_models(run) else "")
    if is_stale():
        pricing_note += f'<div class="meta">{e(stale_note())}</div>'
    if UNATTRIBUTED in unpriced(run):
        pricing_note += f'<div class="meta">Unattributed tokens. {e(UNATTRIBUTED_NOTE)}</div>'
    rollback = _rollback(run)
    rollback_html = (
        f'<div class="meta">Rollback: <code>{e(rollback["command"])}</code> '
        '(creates a safety checkpoint; run in this repository)</div>'
    ) if rollback else ""
    guard_note = (f'<div class="meta">Guard: {_e(guard_summary(run.guard))}</div>'
                  if run.guard else "")
    notes_html = "".join(f'<div class="meta">Note: {_e(n)}</div>' for n in (run.notes or []))

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Run receipt · {e(run.session_id[:8])} · RunLedger</title><style>{CSS}{analysis_css}</style></head>
<body><div class="wrap">
<div class="brand"><div class="mark">RL</div>RunLedger · Run receipt</div>
<h1>{request}</h1>
<div class="meta">#{e(run.session_id[:8])} · {e(agent_label(run.agent))} · {e(run.cwd or '?')} · branch {e(run.git_branch or '-')} · {e(run.started or '')}</div>
{rollback_html}
<div class="grid">
 <div class="card"><span>Risk</span><b style="color:{color}">{score}/100</b><div class="bar"><i style="width:{score}%;background:{color}"></i></div></div>
 {_cost_card(run)}
 <div class="card"><span>Tokens</span><b>{_run_tok(run, run.usage.total)}</b></div>
 <div class="card"><span>Steps</span><b>{len(run.steps)}</b></div>
 <div class="card"><span>Files changed</span><b>{len(changes)}</b></div>
 <div class="card"><span>Duration</span><b>{_dur(run.duration_seconds)}</b></div>
</div>
{guard_note}
{notes_html}
{pricing_note}
<h2>Overview</h2><div class="overview">{e(run.overall_summary)}</div>{quality_html}
<h2>Risk · {level}</h2><div class="overview">{risk_html}</div>{ai_html}{recs_html}
{_guard_html(run)}
<h2>Files changed</h2><table>{files_html}</table>
<h2>Steps</h2><table><tr><th>#</th><th>What happened</th><th>Model</th><th style="text-align:right">Tokens</th><th style="text-align:right">Cost</th></tr>{''.join(rows)}</table>
<footer>Models: {e(models or 'n/a')}. Costs estimate model token usage from public API list prices, not actual charges.{reported_note}{usage_note}
Risk score is rule-based. Generated by RunLedger {__version__} · runledger.site</footer>
</div></body></html>"""


def render(run: Run, score: int, level: str, risks: List[Risk], fmt: str = "html") -> str:
    if fmt == "json":
        return json.dumps(to_dict(run, score, level, risks), indent=2)
    if fmt == "md":
        return to_markdown(run, score, level, risks)
    return to_html(run, score, level, risks)
