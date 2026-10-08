"""Render a Run as a self-contained HTML receipt, Markdown, or JSON."""
from __future__ import annotations

import html
import json
from dataclasses import asdict
from typing import Dict, List, Optional

from . import __version__
from .adapters import label as agent_label
from .parser import Run
from .pricing import friendly_model
from .risk import Risk
from .summarize import FileChange, file_changes


def _money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"${v:.4f}" if v < 0.01 else f"${v:.3f}" if v < 1 else f"${v:.2f}"


def _tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _dur(sec: Optional[float]) -> str:
    if sec is None:
        return "n/a"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


def _cost_text(run: Run) -> str:
    """Estimate from list prices, the agent's own figure, or both."""
    if run.cost is not None and run.reported_cost is not None:
        return f"{_money(run.cost)} est. · {_money(run.reported_cost)} reported by agent"
    if run.cost is not None:
        return _money(run.cost)
    if run.reported_cost is not None:
        return f"{_money(run.reported_cost)} reported by agent"
    return "n/a"


def _cost_card(run: Run) -> str:
    if run.cost is None and run.reported_cost is None:
        return '<div class="card"><span>Cost</span><b>n/a</b></div>'
    if run.cost is not None:
        note = (f"<small>est. · reported by agent {_money(run.reported_cost)}</small>"
                if run.reported_cost is not None else "")
        return f'<div class="card"><span>Cost</span><b>{_money(run.cost)}</b>{note}</div>'
    return (f'<div class="card"><span>Cost</span><b>{_money(run.reported_cost)}</b>'
            "<small>reported by agent</small></div>")


def to_dict(run: Run, score: int, level: str, risks: List[Risk]) -> Dict:
    changes = file_changes(run)
    return {
        "runledger_version": __version__,
        "session_id": run.session_id,
        "agent": agent_label(run.agent), "agent_id": run.agent,
        "cwd": run.cwd, "git_branch": run.git_branch,
        "started": run.started, "ended": run.ended,
        "duration_seconds": run.duration_seconds,
        "request": run.prompts[:3],
        "overview": run.overall_summary,
        "risk": {"score": score, "level": level,
                 "reasons": [asdict(r) for r in risks]},
        "totals": {"steps": len(run.steps), "tokens": run.usage.total,
                   "input_tokens": run.usage.input_tokens, "output_tokens": run.usage.output_tokens,
                   "cache_write_tokens": run.usage.cache_write_tokens,
                   "cache_read_tokens": run.usage.cache_read_tokens,
                   "cost_usd": run.cost, "reported_cost_usd": run.reported_cost,
                   "files_changed": len(changes)},
        "models": {m: {"tokens": u.total} for m, u in run.models.items()},
        "files": [asdict(c) for c in changes.values()],
        "steps": [{"n": s.index, "tool": s.tool, "summary": s.summary, "model": s.model,
                   "tokens": s.usage.total, "cost_usd": s.cost, "error": s.is_error,
                   "risks": [asdict(r) for r in s.risks]} for s in run.steps],
    }


def to_markdown(run: Run, score: int, level: str, risks: List[Risk]) -> str:
    changes = file_changes(run)
    out = [f"# Run receipt · {run.session_id[:8]}", ""]
    out.append(f"- **Agent:** {agent_label(run.agent)}  ·  **Folder:** `{run.cwd}`  ·  **Branch:** `{run.git_branch or '-'}`  ·  **Duration:** {_dur(run.duration_seconds)}")
    out.append(f"- **Risk:** {score}/100 ({level})  ·  **Cost:** {_cost_text(run)}  ·  **Tokens:** {_tok(run.usage.total)}  ·  **Files changed:** {len(changes)}")
    out += ["", "## Overview", run.overall_summary, ""]
    if risks:
        out.append("## Why it was flagged")
        for r in risks:
            out.append(f"- **{r.severity.upper()}** · step {r.step}: {r.reason}")
        out.append("")
    if changes:
        out.append("## Files changed")
        for c in changes.values():
            tag = " (deleted)" if c.deleted else " (new)" if c.created else ""
            out.append(f"- `{c.path}`{tag} +{c.added} −{c.removed}")
        out.append("")
    out.append("## Steps")
    for s in run.steps:
        flag = " ⚠️" if s.risks else ""
        out.append(f"{s.index}. {s.summary}{flag} — {friendly_model(s.model)}, {_tok(s.usage.total)} tok, {_money(s.cost)}")
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
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""


def to_html(run: Run, score: int, level: str, risks: List[Risk]) -> str:
    e = html.escape
    changes = file_changes(run)
    color = "var(--accent)" if level == "Low" else "var(--amber)" if level == "Medium" else "var(--red)"

    risk_html = "".join(
        f'<div class="risk"><span class="sev {r.severity}">{r.severity.upper()}</span>'
        f'<div>{e(r.reason)} <span class="meta">· step {r.step}</span></div></div>' for r in risks
    ) or '<div class="risk"><span class="sev low">OK</span><div>No risky actions detected by the rules.</div></div>'

    files_html = "".join(
        f"<tr><td><code>{e(c.path)}</code>{' <span class=pill>deleted</span>' if c.deleted else ' <span class=pill>new</span>' if c.created else ''}</td>"
        f'<td class="num"><span class="add">+{c.added}</span> <span class="rem">−{c.removed}</span></td></tr>'
        for c in changes.values()
    ) or '<tr><td colspan="2" class="meta">No files changed.</td></tr>'

    rows = []
    for s in run.steps:
        worst = "high" if any(r.severity == "high" for r in s.risks) else ("medium" if s.risks else "")
        cls = f' class="flag {worst}"' if s.risks else ""
        summary = e(s.summary)
        # render `code` spans from templates
        parts = summary.split("`")
        summary = "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))
        rows.append(
            f"<tr{cls}><td class='num'>{s.index}</td><td>{summary}"
            + ("".join(f" <span class='sev {r.severity}'>{e(r.reason)}</span>" for r in s.risks))
            + ("" if not s.is_error else " <span class='pill'>error</span>")
            + f"</td><td><span class='pill'>{e(friendly_model(s.model))}</span></td>"
            f"<td class='num'>{_tok(s.usage.total)}</td><td class='num'>{_money(s.cost)}</td></tr>"
        )
    models = ", ".join(f"{friendly_model(m)} ({_tok(u.total)})" for m, u in run.models.items())
    request = e(run.prompts[0][:400]) if run.prompts else "—"
    reported_note = " Reported cost comes from the agent itself." if run.reported_cost is not None else ""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Run receipt · {e(run.session_id[:8])} · RunLedger</title><style>{CSS}</style></head>
<body><div class="wrap">
<div class="brand"><div class="mark">RL</div>RunLedger · Run receipt</div>
<h1>{request}</h1>
<div class="meta">#{e(run.session_id[:8])} · {e(agent_label(run.agent))} · {e(run.cwd or '?')} · branch {e(run.git_branch or '-')} · {e(run.started or '')}</div>
<div class="grid">
 <div class="card"><span>Risk</span><b style="color:{color}">{score}/100</b><div class="bar"><i style="width:{score}%;background:{color}"></i></div></div>
 {_cost_card(run)}
 <div class="card"><span>Tokens</span><b>{_tok(run.usage.total)}</b></div>
 <div class="card"><span>Steps</span><b>{len(run.steps)}</b></div>
 <div class="card"><span>Files changed</span><b>{len(changes)}</b></div>
 <div class="card"><span>Duration</span><b>{_dur(run.duration_seconds)}</b></div>
</div>
<h2>Overview</h2><div class="overview">{e(run.overall_summary)}</div>
<h2>Risk · {level}</h2><div class="overview">{risk_html}</div>
<h2>Files changed</h2><table>{files_html}</table>
<h2>Steps</h2><table><tr><th>#</th><th>What happened</th><th>Model</th><th style="text-align:right">Tokens</th><th style="text-align:right">Cost</th></tr>{''.join(rows)}</table>
<footer>Models: {e(models or 'n/a')}. Costs are estimates from public Claude API list prices; subscription plans are billed differently.{reported_note}
Risk score is rule-based. Generated by RunLedger {__version__} · runledger.site</footer>
</div></body></html>"""


def render(run: Run, score: int, level: str, risks: List[Risk], fmt: str = "html") -> str:
    if fmt == "json":
        return json.dumps(to_dict(run, score, level, risks), indent=2)
    if fmt == "md":
        return to_markdown(run, score, level, risks)
    return to_html(run, score, level, risks)
