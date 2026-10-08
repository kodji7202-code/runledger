"""Exports for team admins: CSV files of runs and of the audit log, and a printable HTML report.

Each export covers one team and the window of whole days that ends now (UTC). Nothing here
writes to the database; app.py records every export in the audit log.

CSV files are UTF-8 with a byte-order mark, so Excel reads them as UTF-8, and the csv module
does the quoting. A text cell that starts with a formula character (= + - @, tab or carriage
return) gets a leading apostrophe, so a spreadsheet shows it as text instead of running it.

The HTML report is one self-contained page with no scripts and no outside resources. Every
value is escaped. It is served with the same Content-Security-Policy as stored receipts
(app.RECEIPT_CSP; a test keeps the two equal).

Standard library only.
"""
from __future__ import annotations

import csv
import html
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import quote

from .. import __version__
from . import budgets
from .db import TIME_FORMAT, Database

BOM = b"\xef\xbb\xbf"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
REPORT_LIMIT = 200  # rows of each long table in the report; the CSV exports always hold every row
REPORT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
RUN_HEADER = [
    "id", "started_at", "user", "project", "agent", "models", "steps", "tokens",
    "files_changed", "cost_usd", "risk_score", "risk_level", "title",
]
AUDIT_HEADER = ["id", "at", "actor", "action", "target", "details_json"]


def since_for(now: datetime, days: int) -> str:
    """The first moment of the export window, as TIME_FORMAT text."""
    return (now - timedelta(days=days)).astimezone(timezone.utc).strftime(TIME_FORMAT)


def describe(kind: str, now: datetime) -> Tuple[str, Dict[str, str]]:
    """The Content-Type and extra headers for an export. Cheap: it builds nothing."""
    stamp = now.strftime("%Y%m%d")
    if kind == "report":
        return "text/html; charset=utf-8", {
            "Content-Security-Policy": REPORT_CSP,
            "Content-Disposition": f'inline; filename="runledger-report-{stamp}.html"',
        }
    return "text/csv; charset=utf-8", {
        "Content-Disposition": f'attachment; filename="runledger-{kind}-{stamp}.csv"',
    }


def build(kind: str, db: Database, team_id: int, team_name: str, days: int, now: datetime) -> Tuple[bytes, int]:
    """The body of an export, and how many rows or runs it holds (for the audit entry)."""
    since = since_for(now, days)
    if kind == "runs":
        return _runs_csv(db, team_id, since)
    if kind == "audit":
        return _audit_csv(db, team_id, since)
    if kind == "report":
        return _report(db, team_id, team_name, days, now)
    raise ValueError(f"unknown export: {kind}")


def safe_cell(value: Any) -> Any:
    """A CSV cell. Text that a spreadsheet would read as a formula gets a leading apostrophe.
    Numbers and empty values pass through unchanged."""
    if isinstance(value, str) and value.lstrip(" ")[:1] in FORMULA_START:
        return "'" + value
    return value


def _csv_bytes(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow([safe_cell(value) for value in row])
    return BOM + out.getvalue().encode("utf-8")


def _runs_csv(db: Database, team_id: int, since: str) -> Tuple[bytes, int]:
    rows: List[List[Any]] = []
    for run in db.export_runs(team_id, since):
        models = json.loads(run["models"] or "{}")
        cost = run["cost"]
        rows.append([
            run["id"], run["started_at"] or "", run["user"], run["project"], run["agent"] or "",
            "; ".join(sorted(models)), run["steps"], run["tokens"], run["files_changed"],
            "" if cost is None else round(cost, 6), run["risk_score"], run["risk_level"], run["title"] or "",
        ])
    return _csv_bytes(RUN_HEADER, rows), len(rows)


def _audit_csv(db: Database, team_id: int, since: str) -> Tuple[bytes, int]:
    events = db.audit_events(team_id, limit=None, since=since, ascending=True)
    rows = [
        [e["id"], e["at"], e["actor"], e["action"], e["target"] or "",
         json.dumps(e["details"], sort_keys=True, ensure_ascii=False)]
        for e in events
    ]
    return _csv_bytes(AUDIT_HEADER, rows), len(rows)


# The printable report

def _e(value: Any) -> str:
    """Escape any value for HTML text or a double-quoted attribute."""
    return html.escape("" if value is None else str(value), quote=True)


def _usd(amount: float) -> str:
    return f"${amount:,.4f}"


def _pct_text(pct: Any) -> str:
    return "no limit" if pct is None else f"{pct:.1f}%"


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]], numeric: Sequence[int] = ()) -> str:
    """A table. The cells must already be escaped (or be markup this module wrote)."""
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="num">{cell}</td>' if i in numeric else f"<td>{cell}</td>" for i, cell in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _more(shown: int, fetched: int) -> str:
    if fetched <= shown:
        return ""
    return f'<p class="note">Showing the first {shown} rows. The CSV exports hold every row for the period.</p>'


_REPORT_STYLE = """
:root{color-scheme:light}
body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#15171a;background:#fff;margin:0}
main{max-width:980px;margin:0 auto;padding:28px 18px 48px}
.kicker{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#5b6270;margin:0}
h1{font-size:26px;margin:4px 0 12px;overflow-wrap:anywhere}
h2{font-size:17px;margin:28px 0 8px;padding-bottom:4px;border-bottom:1px solid #d9dde2}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px 22px;margin:0}
.facts dt{font-size:12px;color:#5b6270}
.facts dd{margin:0 0 6px}
.scope{border-left:3px solid #8a93a3;padding:4px 10px;background:#f5f6f8;margin:14px 0 0}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e6e9ed;vertical-align:top;overflow-wrap:anywhere}
th{font-weight:600;background:#f5f6f8}
.num{text-align:right;font-variant-numeric:tabular-nums}
.note{font-size:12px;color:#5b6270;margin:6px 0 0}
ol.method li{margin:0 0 6px}
a{color:#1d4ed8}
@media print{main{padding:0;max-width:none}h2{break-after:avoid}tr{break-inside:avoid}a{color:inherit;text-decoration:none}}
"""


def _report(db: Database, team_id: int, team_name: str, days: int, now: datetime) -> Tuple[bytes, int]:
    since = since_for(now, days)
    stats = db.stats(team_id, days, now=now)
    totals = stats["totals"]
    levels = db.risk_level_counts(team_id, since)
    high = db.high_risk_runs(team_id, since, REPORT_LIMIT + 1)
    decided = db.decided_approvals(team_id, since, REPORT_LIMIT + 1)
    events = db.audit_events(team_id, limit=REPORT_LIMIT + 1, since=since, action="key.")
    budget = budgets.status(db, team_id, now)

    moment = now.astimezone(timezone.utc)
    since_text = datetime.strptime(since, TIME_FORMAT).strftime("%Y-%m-%d %H:%M")
    until_text = moment.strftime("%Y-%m-%d %H:%M")
    generated = moment.strftime(TIME_FORMAT)

    avg = "n/a" if totals["avg_risk"] is None else f"{totals['avg_risk']:.1f}"
    summary = _table(
        ["Measure", "Value"],
        [
            ["Runs", _e(totals["runs"])],
            ["Estimated cost at list prices", _e(_usd(totals["cost_usd"]))],
            ["Runs without a price", _e(totals["unpriced_runs"])],
            ["Tokens", _e(f"{totals['tokens']:,}")],
            ["Steps", _e(f"{totals['steps']:,}")],
            ["Files changed", _e(f"{totals['files_changed']:,}")],
            ["Average risk score", _e(avg)],
            ["High-risk runs", _e(totals["high_risk_runs"])],
        ],
        numeric=(1,),
    )
    by_level = _table(
        ["Risk level", "Runs"],
        [[_e(level), _e(levels.get(level, 0))] for level in ("high", "medium", "low")],
        numeric=(1,),
    )
    if stats["top_risk_codes"]:
        codes = _table(
            ["Risk code", "Occurrences", "Runs"],
            [[_e(c["code"]), _e(c["occurrences"]), _e(c["runs"])] for c in stats["top_risk_codes"]],
            numeric=(1, 2),
        )
    else:
        codes = '<p class="note">No risk codes were recorded in this period.</p>'

    high_rows = []
    for run in high[:REPORT_LIMIT]:
        receipt = f'<a href="/runs/{_e(quote(run["id"], safe=""))}">Open receipt</a>' if run["has_html"] else "none"
        high_rows.append([
            _e(run["started_at"] or run["created_at"]), _e(run["user"]), _e(run["project"]),
            _e(run["agent"] or "unknown"), _e(run["risk_score"]), _e(run["title"] or ""), receipt,
        ])
    if high_rows:
        high_table = _table(
            ["Started", "Developer", "Project", "Agent", "Score", "Title", "Receipt"], high_rows, numeric=(4,),
        )
    else:
        high_table = '<p class="note">No high-risk runs in this period.</p>'

    approval_rows = [
        [_e(a["decided_at"]), _e(a["decided_by"] or "unknown"), _e(a["status"]), _e(a["tool"]), _e(a["id"])]
        for a in decided[:REPORT_LIMIT]
    ]
    if approval_rows:
        approval_table = _table(["Decided at", "Decided by", "Decision", "Tool", "Approval id"], approval_rows)
    else:
        approval_table = '<p class="note">No approvals were decided in this period.</p>'

    event_rows = [
        [_e(e["at"]), _e(e["actor"]), _e(e["action"]), _e(e["target"] or ""),
         _e(json.dumps(e["details"], sort_keys=True, ensure_ascii=False))]
        for e in events[:REPORT_LIMIT]
    ]
    if event_rows:
        event_table = _table(["At", "Actor", "Action", "Key id", "Details"], event_rows)
    else:
        event_table = '<p class="note">No key lifecycle events in this period.</p>'

    user_rows = [
        [_e(u["user"]), _e(_usd(u["spend_usd"])), _e(_pct_text(u["pct"]))] for u in budget["per_user"][:10]
    ]
    if budget["monthly_usd"] is None:
        limit_text = "no team budget set"
    else:
        limit_text = f"{_usd(budget['monthly_usd'])} budget, {_pct_text(budget['pct'])} used"
    if user_rows:
        user_table = _table(["Developer", "Spend", "Share of developer budget"], user_rows, numeric=(1, 2))
    else:
        user_table = '<p class="note">No developer spend recorded this month.</p>'
    budget_block = (
        f"<p>Month {_e(budget['month'])}: spend {_e(_usd(budget['spend_usd']))}, {_e(limit_text)}.</p>"
        + user_table
    )

    methodology = [
        "Risk scores and levels come from fixed rules that RunLedger applies to each session when it "
        "builds the receipt, for example destructive shell commands or edits to secret files. They flag a "
        "run for review. They are not a judgement that the run was correct or safe.",
        "Costs are estimates: token counts multiplied by published list prices (runledger/prices.json). "
        "They do not match an invoice, a subscription or a negotiated rate.",
        "A run counts toward the period by its start time, or by its push time when the start time is "
        "missing. Pushing the same session again replaces its record and its figures.",
        "Key lifecycle events are the create, rotate and revoke entries of the audit log. The full audit "
        "log for the period is in the audit CSV export.",
        "Budget figures cover the current UTC calendar month, not the report period. Each budget alert "
        "is sent once per threshold per month.",
        "The figures are computed from the team server database at generation time. Nothing in this "
        "report is signed or certified.",
    ]
    method_html = "".join(f"<li>{_e(text)}</li>" for text in methodology)

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>RunLedger report: {_e(team_name)}</title>
<style>{_REPORT_STYLE}</style>
</head>
<body>
<main>
<header>
<p class="kicker">RunLedger record-keeping report</p>
<h1>{_e(team_name)}</h1>
<dl class="facts">
<div><dt>Period</dt><dd>{_e(since_text)} to {_e(until_text)} UTC (last {_e(days)} days)</dd></div>
<div><dt>Generated</dt><dd>{_e(generated)}</dd></div>
<div><dt>Software</dt><dd>RunLedger {_e(__version__)}</dd></div>
</dl>
<p class="scope">Prepared for SOC 2 and EU AI Act record-keeping. It summarises data that this team
server holds. It is not a certification, an audit opinion or legal advice.</p>
</header>

<section>
<h2>Totals</h2>
{summary}
</section>

<section>
<h2>Runs by risk level</h2>
{by_level}
</section>

<section>
<h2>Top risk codes</h2>
{codes}
</section>

<section>
<h2>High-risk runs</h2>
{high_table}
{_more(min(len(high), REPORT_LIMIT), len(high))}
</section>

<section>
<h2>Approvals decided</h2>
{approval_table}
{_more(min(len(decided), REPORT_LIMIT), len(decided))}
</section>

<section>
<h2>Key lifecycle events</h2>
{event_table}
{_more(min(len(events), REPORT_LIMIT), len(events))}
</section>

<section>
<h2>Budget status</h2>
{budget_block}
</section>

<section>
<h2>Methodology</h2>
<ol class="method">{method_html}</ol>
</section>
</main>
</body>
</html>
"""
    return page.encode("utf-8"), totals["runs"]
