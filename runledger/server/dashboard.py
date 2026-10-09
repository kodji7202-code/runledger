"""The team dashboard: one self-contained page (inline CSS and JS, no CDN).

Server data is rendered with DOM text APIs only, so a developer name such as
<script> shows up as plain text. The server sends this page with a
per-response nonce CSP that allows only the script below. The script calls the
API with cookie credentials; every POST or PUT sends X-Requested-With, which a
cross-site form cannot set.

Views are switched by location hash (#runs, #insights, #approvals, and for admins #keys,
#audit, #settings). Features whose endpoints are not on the server yet (budgets,
exports, notification settings) hide themselves on 404 or 403. The Insights view reads
/api/insights; its trend chart is an inline SVG built with createElementNS.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>RunLedger Team</title>
<style>
:root{--bg:#0a0b0d;--panel:#111316;--line:#1f2328;--control:#6b737e;--text:#eef0f2;--muted:#9aa1ab;--accent:#4fe0b0;--amber:#f5b547;--red:#ff6b6b;--chip:#171a1e;--on-accent:#0a0b0d;--ink-good:#4fe0b0;--ink-warn:#f5b547;--ink-bad:#ff6b6b}
@media (prefers-color-scheme: light){:root{--bg:#f7f8f9;--panel:#fff;--line:#e3e6ea;--control:#7b8490;--text:#121417;--muted:#5d6670;--accent:#047857;--amber:#b45309;--red:#c62828;--chip:#f0f2f4;--on-accent:#fff;--ink-good:#065f46;--ink-warn:#92400e;--ink-bad:#991b1b}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--text)}
body{font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1120px;margin:0 auto;padding:28px 16px 64px;min-width:0}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:14px}
.brand{display:flex;flex-wrap:wrap;align-items:center;gap:10px;color:var(--muted);font-size:12px;letter-spacing:.05em;text-transform:uppercase}
.mark{width:26px;height:26px;border-radius:7px;background:var(--accent);color:var(--on-accent);display:grid;place-items:center;font:700 12px ui-monospace,monospace}
.role-badge{font:700 11px ui-monospace,monospace;letter-spacing:.04em;text-transform:uppercase;padding:1px 9px;border-radius:99px;background:var(--chip);color:var(--muted);border:1px solid var(--control)}
.role-badge.role-admin{color:var(--accent);border-color:var(--accent)}
.role-badge.role-viewer{color:var(--amber);border-color:var(--amber)}
h1{font-size:22px;margin:6px 0 0;overflow-wrap:anywhere}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
select,input,button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--control);border-radius:8px;padding:6px 10px;min-width:0}
button{cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.label-inline{color:var(--muted);font-size:12px}
.tabs-wrap{overflow-x:auto;border-bottom:1px solid var(--line);margin:0 -16px 18px;padding:0 16px}
.tabs{display:flex;gap:4px;min-width:max-content}
.tab{appearance:none;background:none;border:0;border-bottom:2px solid transparent;border-radius:0;color:var(--muted);font-weight:600;padding:10px 12px;margin-bottom:-1px;white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.tab:hover{color:var(--text)}
.tab[aria-selected="true"]{color:var(--text);border-bottom-color:var(--accent)}
.view:not([hidden]){animation:view-in .18s ease-out}
@keyframes view-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.notice{display:none;margin:0 0 16px;padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel)}
.notice.show{display:block}
.notice.error{border-color:var(--red);color:var(--red)}
.notice.warn{border-color:var(--amber)}
.msg{margin:6px 0;font-size:13px}
.msg:empty{margin:0}
.msg.error{color:var(--red)}
.msg.ok{color:var(--accent)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.kpi-label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.kpi-value{font-size:24px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.kpi-sub{color:var(--muted);font-size:12px;margin-top:2px}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-top:12px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:26px 0 10px;font-weight:600}
h2.panel-title{margin:0 0 8px}
.panel-title{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px;font-weight:600}
.bar-row{display:grid;grid-template-columns:minmax(80px,34%) 1fr auto;gap:10px;align-items:center;padding:5px 0;font-size:13px}
.bar-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{height:8px;border-radius:99px;background:var(--line);overflow:hidden}
.bar-fill{height:100%;background:var(--accent);border-radius:99px;transition:width .4s ease}
.bar-val{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;font-size:12px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;align-items:center}
.filters input,.filters select{min-width:0;flex:1 1 150px}
.filters .linkbtn{flex:0 0 auto}
.table-wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px;max-width:100%}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.mono{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);white-space:nowrap}
.req{max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{display:inline-block;font:700 11px ui-monospace,monospace;padding:2px 8px;border-radius:99px;white-space:nowrap}
.badge.low{background:var(--chip);color:var(--muted)}
.badge.medium{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--amber)}
.badge.high{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--red)}
a{color:var(--accent)}
footer{margin-top:28px;color:var(--muted);font-size:12px}
.approvals{margin:0 0 22px}
.approvals-head{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.approvals-head h2{margin:0}
.count{font:700 12px ui-monospace,monospace;background:var(--chip);color:var(--muted);border-radius:99px;padding:2px 9px}
.approvals-list{display:grid;gap:12px}
.approval{border-left:3px solid var(--amber)}
.approval-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px}
.approval-tool{font:600 13px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
.approval-summary{margin:10px 0 0;white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);border-radius:8px;padding:10px 12px;max-height:220px;overflow:auto}
.approval-risks{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.approval-meta{color:var(--muted);font-size:12px;margin-top:8px;overflow-wrap:anywhere}
.approval-actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:12px}
.btn{font-weight:600;padding:7px 16px}
.btn.approve{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn.deny{color:var(--red);border-color:var(--red)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
.btn.btn-danger{color:var(--red);border-color:var(--red);font-weight:600}
button.small{padding:4px 10px;font-size:12px}
button.small.btn-danger{color:var(--red);border-color:var(--red)}
.linkbtn{display:inline-block;text-decoration:none;color:var(--text);background:var(--panel);border:1px solid var(--control);border-radius:8px;padding:6px 12px;font-weight:600}
.linkbtn:hover{border-color:var(--accent)}
.form-row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin:0 0 10px}
.form-row label,.field{display:grid;gap:4px;color:var(--muted);font-size:12px;min-width:0;flex:1 1 180px}
.form-row input,.form-row select{width:100%}
.form-row button{flex:0 0 auto}
.key-once{border:1px solid var(--amber);border-radius:12px;padding:14px 16px;margin:0 0 14px;background:var(--panel)}
.key-warn{margin:0 0 10px}
.key-box{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 10px}
.key-box code{flex:1 1 200px;min-width:0;overflow-wrap:anywhere;background:var(--chip);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}
.details{max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:12px ui-monospace,monospace;color:var(--muted)}
.budget{margin-top:14px}
.budget-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}
.budget-grid{display:grid;gap:14px;grid-template-columns:minmax(0,1fr)}
@media (min-width:720px){.budget-grid{grid-template-columns:minmax(0,3fr) minmax(0,2fr)}}
.budget-figure{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums;margin:0;overflow-wrap:anywhere}
.progress{height:12px;border-radius:99px;background:var(--line);overflow:hidden;margin:10px 0 6px}
.progress-fill{height:100%;background:var(--accent);border-radius:99px;transition:width .4s ease}
.progress-fill.warn{background:var(--amber)}
.progress-fill.danger{background:var(--red)}
.budget-form{margin-top:12px;border-top:1px solid var(--line);padding-top:12px}
.section-gap{margin-top:8px}
.link-row{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 8px}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px;padding:0;width:min(92vw,420px);max-width:92vw}
dialog::backdrop{background:rgba(5,6,8,.6)}
.dialog-body{padding:18px}
.dialog-title{font-weight:650;font-size:16px;margin:0 0 8px}
.dialog-actions{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:16px}
.insights-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px;margin:26px 0 10px}
.insights-head h2{margin:0}
.kpi-grade{margin-left:8px;vertical-align:middle}
.grade{display:inline-block;min-width:26px;text-align:center;font:700 12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;padding:0 6px;border-radius:6px;background:var(--chip);color:var(--muted);white-space:nowrap}
.grade.g-good{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--ink-good)}
.grade.g-warn{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--ink-warn)}
.grade.g-bad{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--ink-bad)}
.quality{white-space:nowrap}
.quality .score{margin-left:6px;font-variant-numeric:tabular-nums}
.verdict{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;font-size:12px}
.verdict-icon{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:99px;font:700 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);color:var(--muted)}
.verdict.v-safe .verdict-icon{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--ink-good)}
.verdict.v-review .verdict-icon{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--ink-warn)}
.verdict.v-danger .verdict-icon{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--ink-bad)}
.share{display:grid;grid-template-columns:minmax(60px,1fr) auto;gap:8px;align-items:center;min-width:140px}
.spark{display:block;width:100%;height:auto;max-height:150px;overflow:visible}
.spark polyline{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.spark circle{fill:var(--accent)}
.spark-axis{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;margin-top:4px}
.rec-list{margin:0;padding-left:24px}
.rec-list li{margin:0 0 12px;padding-left:4px}
.rec-title{font-weight:600;overflow-wrap:anywhere}
.rec-meta{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
@media (prefers-reduced-motion: reduce){
  .view:not([hidden]){animation:none}
  .bar-fill,.progress-fill{transition:none}
}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <div class="brand"><span class="mark">RL</span><span>RunLedger team</span><span class="role-badge" id="role-badge" hidden></span></div>
    <h1 id="team-name">Team runs</h1>
  </div>
  <div class="controls">
    <label class="label-inline" for="days">Stats window</label>
    <select id="days">
      <option value="7">7 days</option>
      <option value="30" selected>30 days</option>
      <option value="90">90 days</option>
      <option value="365">1 year</option>
    </select>
    <button type="button" id="refresh">Refresh</button>
  </div>
</header>

<div id="billing-notice" class="notice" role="status"></div>

<div class="tabs-wrap">
<div class="tabs" role="tablist" id="tabs" aria-label="Dashboard sections">
  <button type="button" role="tab" class="tab" id="tab-runs" data-view="runs" aria-controls="view-runs" aria-selected="true" tabindex="0">Runs</button>
  <button type="button" role="tab" class="tab" id="tab-insights" data-view="insights" aria-controls="view-insights" aria-selected="false" tabindex="-1">Insights</button>
  <button type="button" role="tab" class="tab" id="tab-approvals" data-view="approvals" aria-controls="view-approvals" aria-selected="false" tabindex="-1">Approvals <span class="count" id="tab-approvals-count">0</span></button>
  <button type="button" role="tab" class="tab" id="tab-keys" data-view="keys" aria-controls="view-keys" aria-selected="false" tabindex="-1" hidden>API keys</button>
  <button type="button" role="tab" class="tab" id="tab-audit" data-view="audit" aria-controls="view-audit" aria-selected="false" tabindex="-1" hidden>Audit log</button>
  <button type="button" role="tab" class="tab" id="tab-settings" data-view="settings" aria-controls="view-settings" aria-selected="false" tabindex="-1" hidden>Settings</button>
</div>
</div>

<section class="view approvals" id="view-approvals" role="tabpanel" aria-labelledby="tab-approvals" tabindex="-1" hidden>
  <div class="approvals-head">
    <h2 id="approvals-title">Pending approvals</h2>
    <span class="count" id="approvals-count" aria-live="polite">0</span>
  </div>
  <p class="msg" id="approvals-readonly" hidden>Read only: only an admin can approve or deny requests.</p>
  <div id="approvals-notice" class="notice" role="alert"></div>
  <div id="approvals-list" class="approvals-list"></div>
  <div id="approvals-empty" class="empty"></div>
</section>

<div id="notice" class="notice" role="alert"></div>

<section class="view" id="view-runs" role="tabpanel" aria-labelledby="tab-runs" tabindex="-1">
<section class="kpis">
  <div class="card"><div class="kpi-label">Total cost</div><div class="kpi-value" id="k-cost">-</div><div class="kpi-sub" id="k-cost-sub"></div></div>
  <div class="card"><div class="kpi-label">Runs</div><div class="kpi-value" id="k-runs">-</div><div class="kpi-sub" id="k-runs-sub"></div></div>
  <div class="card"><div class="kpi-label">Average risk</div><div class="kpi-value" id="k-risk">-</div><div class="kpi-sub">out of 100</div></div>
  <div class="card"><div class="kpi-label">High-risk runs</div><div class="kpi-value" id="k-high">-</div><div class="kpi-sub" id="k-high-sub"></div></div>
</section>

<section class="card budget" id="budget-card" aria-labelledby="budget-title" hidden>
  <div class="budget-head">
    <h2 class="panel-title" id="budget-title">Monthly budget</h2>
    <span class="label-inline" id="budget-month"></span>
  </div>
  <p class="msg" id="budget-status" role="status" aria-live="polite"></p>
  <div class="budget-grid">
    <div>
      <p class="budget-figure" id="budget-figure"></p>
      <div class="progress" id="budget-bar" role="progressbar" aria-labelledby="budget-title" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" hidden>
        <div class="progress-fill" id="budget-fill"></div>
      </div>
      <p class="kpi-sub"><span id="budget-note"></span> <span class="badge high" id="budget-over" hidden>Over budget</span></p>
    </div>
    <div>
      <div class="panel-title">Top developers this month</div>
      <div id="budget-users"></div>
    </div>
  </div>
  <form class="budget-form" id="budget-form" hidden novalidate>
    <div class="form-row">
      <label>Team monthly budget (USD)
        <input id="budget-monthly" type="number" min="0" max="1000000000" step="0.01" inputmode="decimal" placeholder="No limit">
      </label>
      <label>Per developer, monthly (USD)
        <input id="budget-per-user" type="number" min="0" max="1000000000" step="0.01" inputmode="decimal" placeholder="No limit">
      </label>
      <button type="submit" class="btn primary" id="budget-save">Save budget</button>
    </div>
    <p class="msg" id="budget-msg" role="status" aria-live="polite"></p>
  </form>
</section>

<section class="panels">
  <div class="card"><div class="panel-title">Cost by developer</div><div id="by-user"></div></div>
  <div class="card" id="card-by-agent" hidden><div class="panel-title">Cost by agent</div><div id="by-agent"></div></div>
  <div class="card"><div class="panel-title">Cost by model</div><div id="by-model"></div></div>
  <div class="card"><div class="panel-title">Cost by project</div><div id="by-project"></div></div>
  <div class="card"><div class="panel-title">Top risk reasons</div><div id="top-risks"></div></div>
</section>

<h2>Runs</h2>
<div class="filters">
  <input id="f-user" type="search" placeholder="Developer" aria-label="Filter by developer" maxlength="200">
  <input id="f-project" type="search" placeholder="Project" aria-label="Filter by project" maxlength="200">
  <input id="f-agent" type="search" placeholder="Agent" aria-label="Filter by agent" maxlength="200">
  <input id="f-min" type="number" min="0" max="100" placeholder="Min risk" aria-label="Minimum risk score">
  <input id="f-quality" type="number" min="0" max="100" placeholder="Min quality" aria-label="Minimum quality score">
</div>
<div class="table-wrap">
<table>
  <thead>
    <tr><th>When</th><th>Developer</th><th>Project</th><th>Agent</th><th>Request</th><th class="num">Steps</th><th class="num">Cost</th><th>Risk</th><th class="num">Quality</th><th>AI review</th><th class="num">Receipt</th></tr>
  </thead>
  <tbody id="runs"></tbody>
</table>
</div>
<div id="runs-empty" class="empty"></div>
</section>

<section class="view" id="view-insights" role="tabpanel" aria-labelledby="tab-insights" tabindex="-1" hidden>
  <div class="insights-head">
    <h2>Insights</h2>
    <span class="label-inline" id="insights-window"></span>
  </div>
  <p class="msg" id="insights-msg" role="status" aria-live="polite"></p>
  <section class="kpis" aria-label="Quality and AI review summary">
    <div class="card"><div class="kpi-label">Average quality</div><div class="kpi-value"><span id="i-quality">-</span><span class="kpi-grade" id="i-quality-grade"></span></div><div class="kpi-sub" id="i-quality-sub"></div></div>
    <div class="card"><div class="kpi-label">Estimated savings</div><div class="kpi-value" id="i-savings">-</div><div class="kpi-sub" id="i-savings-sub"></div></div>
    <div class="card"><div class="kpi-label">AI-reviewed runs</div><div class="kpi-value" id="i-reviewed">-</div><div class="kpi-sub" id="i-reviewed-sub"></div></div>
    <div class="card"><div class="kpi-label">False-positive rate</div><div class="kpi-value" id="i-fp">-</div><div class="kpi-sub" id="i-fp-sub"></div></div>
  </section>
  <section class="panels">
    <div class="card"><div class="panel-title">Quality trend</div><div id="i-trend"></div></div>
    <div class="card"><div class="panel-title">Grades</div><div id="i-grades"></div></div>
  </section>
  <h2>Models</h2>
  <div class="table-wrap">
  <table aria-label="Cost and quality by model">
    <thead><tr><th>Model</th><th class="num">Runs</th><th class="num">Cost</th><th class="num">Cost per run</th><th class="num">Avg quality</th><th>Share of cost</th></tr></thead>
    <tbody id="i-models"></tbody>
  </table>
  </div>
  <div class="empty" id="i-models-empty"></div>
  <h2>Agents</h2>
  <div class="table-wrap">
  <table aria-label="Cost and quality by agent">
    <thead><tr><th>Agent</th><th class="num">Runs</th><th class="num">Avg quality</th><th class="num">Avg risk</th><th class="num">Cost</th></tr></thead>
    <tbody id="i-agents"></tbody>
  </table>
  </div>
  <div class="empty" id="i-agents-empty"></div>
  <h2>Top recommendations</h2>
  <ol class="rec-list" id="i-recs"></ol>
  <div class="empty" id="i-recs-empty"></div>
</section>

<section class="view" id="view-keys" role="tabpanel" aria-labelledby="tab-keys" tabindex="-1" hidden>
  <h2>API keys</h2>
  <p class="msg">Each key has one role. A new key is shown once, so copy it before you leave this page.</p>
  <form class="form-row" id="key-form" novalidate>
    <label class="field">Label
      <input id="key-label" type="text" maxlength="100" placeholder="For example Ana laptop" autocomplete="off">
    </label>
    <label class="field">Role
      <select id="key-role">
        <option value="member" selected>member</option>
        <option value="admin">admin</option>
        <option value="viewer">viewer</option>
      </select>
    </label>
    <button type="submit" class="btn primary" id="key-create">Create key</button>
  </form>
  <div class="key-once" id="key-once" tabindex="-1" hidden>
    <p class="key-warn"><strong>Copy this key now. You won't see it again.</strong> <span id="key-once-label"></span></p>
    <div class="key-box"><code id="key-value"></code><button type="button" id="key-copy">Copy key</button></div>
    <p class="msg" id="key-copy-msg" role="status" aria-live="polite"></p>
    <button type="button" id="key-dismiss">I have copied it</button>
  </div>
  <p class="msg" id="keys-msg" role="status" aria-live="polite"></p>
  <div class="table-wrap" id="keys-table">
  <table aria-labelledby="keys-title">
    <thead><tr><th>Label</th><th>Role</th><th>Key</th><th>Created</th><th>Last used</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead>
    <tbody id="keys-body"></tbody>
  </table>
  </div>
  <div class="empty" id="keys-empty"></div>
</section>

<section class="view" id="view-audit" role="tabpanel" aria-labelledby="tab-audit" tabindex="-1" hidden>
  <h2>Audit log</h2>
  <div class="filters">
    <input id="audit-filter" type="search" placeholder="Filter by action" aria-label="Filter loaded events by action" maxlength="100">
    <label class="label-inline" for="audit-days">Export window</label>
    <select id="audit-days">
      <option value="7">7 days</option>
      <option value="30">30 days</option>
      <option value="90" selected>90 days</option>
      <option value="365">1 year</option>
    </select>
    <a class="linkbtn" id="audit-export" download hidden>Export CSV</a>
  </div>
  <p class="msg" id="audit-msg" role="status" aria-live="polite"></p>
  <div class="table-wrap" id="audit-table">
  <table aria-labelledby="audit-title">
    <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Details</th></tr></thead>
    <tbody id="audit-body"></tbody>
  </table>
  </div>
  <div class="empty" id="audit-empty"></div>
  <div class="section-gap"><button type="button" id="audit-more" hidden>Load more</button></div>
</section>

<section class="view" id="view-settings" role="tabpanel" aria-labelledby="tab-settings" tabindex="-1" hidden>
  <h2>Exports</h2>
  <div class="card" id="exports-card">
    <div class="form-row">
      <label class="field">Window
        <select id="exp-days">
          <option value="7">7 days</option>
          <option value="30" selected>30 days</option>
          <option value="90">90 days</option>
          <option value="365">1 year</option>
        </select>
      </label>
    </div>
    <div class="link-row">
      <a class="linkbtn" id="exp-runs" download hidden>Runs (CSV)</a>
      <a class="linkbtn" id="exp-audit" download hidden>Audit log (CSV)</a>
      <a class="linkbtn" id="exp-report" target="_blank" rel="noopener" hidden>Compliance report (opens in a new tab)</a>
    </div>
    <div class="empty" id="exports-empty"></div>
  </div>

  <section id="notify-section">
    <h2>Notifications</h2>
    <div class="card">
      <form id="notify-form" novalidate hidden>
        <div class="form-row">
          <label class="field">Slack webhook URL
            <input id="notify-slack" type="text" maxlength="2000" autocomplete="off" spellcheck="false" placeholder="Not set">
          </label>
        </div>
        <div class="form-row">
          <label class="field">Webhook URL (JSON)
            <input id="notify-webhook" type="text" maxlength="2000" autocomplete="off" spellcheck="false" placeholder="Not set">
          </label>
        </div>
        <div class="form-row">
          <label class="field">Approval expires after (seconds)
            <input id="notify-ttl" type="number" min="1" max="604800" step="1" inputmode="numeric">
          </label>
          <button type="submit" class="btn primary" id="notify-save">Save notifications</button>
        </div>
        <p class="msg">Use https:// URLs. Plain http:// is allowed only for 127.0.0.1 and localhost.</p>
      </form>
      <p class="msg" id="notify-msg" role="status" aria-live="polite"></p>
    </div>
  </section>
</section>

<footer>Costs are estimates from public Claude API list prices; subscription plans are billed differently. Risk scores are rule-based.</footer>
</div>

<dialog id="confirm" aria-labelledby="confirm-title" aria-describedby="confirm-text">
  <form method="dialog" class="dialog-body">
    <p class="dialog-title" id="confirm-title">Confirm</p>
    <p id="confirm-text"></p>
    <div class="dialog-actions">
      <button type="submit" value="cancel" id="confirm-cancel">Cancel</button>
      <button type="submit" value="ok" id="confirm-ok" class="btn btn-danger">Confirm</button>
    </div>
  </form>
</dialog>

<script nonce="__CSP_NONCE__">
(function () {
  "use strict";

  var APPROVAL_POLL_MS = 3000;
  var VIEWS = ["runs", "insights", "approvals", "keys", "audit", "settings"];
  var ADMIN_VIEWS = { keys: true, audit: true, settings: true };
  var ROLES = ["admin", "member", "viewer"];
  var AUDIT_PAGE = 100;
  var MAX_BUDGET_USD = 1000000000;
  var MAX_TTL_S = 604800;
  var EXPORT_PATHS = {
    runs: "/api/export/runs.csv",
    audit: "/api/export/audit.csv",
    report: "/api/export/report.html"
  };

  var state = { days: 30, role: null, keyRef: null, meLoaded: false, view: null, thresholds: [50, 80, 100] };
  var budgetState = { edited: false, settings: null, status: null };
  var approvalsView = { seq: 0, signature: null, items: [] };
  var keysView = { items: [] };
  var auditView = { events: [], busy: false, more: false };
  var exportReady = {};

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function cell(content, className) {
    var td = el("td", className);
    if (content instanceof Node) {
      td.appendChild(content);
    } else {
      td.textContent = (content === null || content === undefined) ? "" : String(content);
    }
    return td;
  }

  function money(value) {
    if (value === null || value === undefined) { return "n/a"; }
    var v = Number(value);
    if (!isFinite(v)) { return "n/a"; }
    if (v === 0) { return "$0.00"; }
    if (v < 0.01) { return "$" + v.toFixed(4); }
    if (v < 1) { return "$" + v.toFixed(3); }
    return "$" + v.toFixed(2);
  }

  function tokens(n) {
    var v = Number(n) || 0;
    return v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(v);
  }

  function when(iso) {
    if (!iso) { return "-"; }
    var d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  function level(value) {
    return value === "high" || value === "medium" ? value : "low";
  }

  function runCount(n) {
    return n + (Number(n) === 1 ? " run" : " runs");
  }

  function pct(value) {
    if (value === null || value === undefined || value === "") { return null; }
    var v = Number(value);
    return isFinite(v) ? Math.round(v) : null;
  }

  function nameOf(value, fallback) {
    var text = (value === null || value === undefined) ? "" : String(value).trim();
    return text ? text : fallback;
  }

  function setMsg(node, message, isError) {
    node.textContent = message || "";
    node.className = "msg" + (message ? (isError ? " error" : " ok") : "");
  }

  function parseBody(res) {
    return res.text().then(function (text) {
      var body = {};
      try { body = text ? JSON.parse(text) : {}; } catch (e) { body = {}; }
      if (!res.ok) {
        var info = (body && body.error) || {};
        var err = new Error(info.message || ("Request failed with HTTP " + res.status));
        err.status = res.status;
        err.code = info.code || null;
        throw err;
      }
      return body;
    });
  }

  // The only place that sends API requests. Anything other than GET carries the CSRF header.
  function request(method, url, payload) {
    var opts = { method: method, credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" } };
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-Requested-With"] = "runledger";
      opts.body = JSON.stringify(payload === undefined ? {} : payload);
    }
    return fetch(url, opts).then(parseBody);
  }

  function getJSON(url) { return request("GET", url); }
  function postJSON(url, payload) { return request("POST", url, payload); }
  function putJSON(url, payload) { return request("PUT", url, payload); }

  function explain(err) {
    if (err.status === 401) {
      return "Not signed in, or the session expired. Open this dashboard once with ?key=YOUR_API_KEY.";
    }
    return "Could not load data: " + err.message;
  }

  function unavailable(err) {
    if (err.status === 404) { return "This part of the dashboard is not available on this server yet."; }
    if (err.status === 403) { return "Only team admins can use this part of the dashboard."; }
    return explain(err);
  }

  function saveError(err) {
    if (err.status === 400 || err.status === 409) { return err.message; }
    if (err.status === 401) { return explain(err); }
    if (err.status === 403) { return unavailable(err); }
    return "Could not save: " + err.message;
  }

  function isMissingOrDenied(err) {
    return err.status === 404 || err.status === 403;
  }

  function notify(message, isError) {
    var box = $("notice");
    box.textContent = message;
    box.className = "notice show" + (isError ? " error" : "");
  }

  function hideNotice() {
    $("notice").className = "notice";
  }

  function report(promise) {
    return promise.then(hideNotice, function (err) { notify(explain(err), true); });
  }

  function renderBars(container, rows, label, value, detail, emptyText) {
    clear(container);
    if (!rows || rows.length === 0) {
      container.appendChild(el("div", "empty", emptyText));
      return;
    }
    var max = 0;
    rows.forEach(function (r) { max = Math.max(max, Number(value(r)) || 0); });
    rows.forEach(function (r) {
      var v = Number(value(r)) || 0;
      var name = el("div", "bar-name", label(r));
      name.title = label(r);
      var track = el("div", "bar-track");
      var fill = el("div", "bar-fill");
      fill.style.width = max > 0 ? ((v / max) * 100).toFixed(1) + "%" : "0%";
      track.appendChild(fill);
      var row = el("div", "bar-row");
      row.appendChild(name);
      row.appendChild(track);
      row.appendChild(el("div", "bar-val", detail(r)));
      container.appendChild(row);
    });
  }

  function setTeam(name) {
    $("team-name").textContent = nameOf(name, "Team") + " · team runs";
  }

  function renderStats(stats) {
    var t = stats.totals || {};
    if (stats.team) { setTeam(stats.team); }

    $("k-cost").textContent = money(t.cost_usd);
    $("k-cost-sub").textContent = t.unpriced_runs
      ? runCount(t.unpriced_runs) + " left out: model price unknown"
      : "last " + state.days + " days";
    $("k-runs").textContent = String(t.runs || 0);
    $("k-runs-sub").textContent = tokens(t.tokens) + " tokens · " + (t.steps || 0) + " steps · " + (t.files_changed || 0) + " files";
    $("k-risk").textContent = (t.avg_risk === null || t.avg_risk === undefined) ? "-" : Number(t.avg_risk).toFixed(1);
    $("k-high").textContent = String(t.high_risk_runs || 0);
    $("k-high-sub").textContent = t.runs
      ? Math.round((100 * (t.high_risk_runs || 0)) / t.runs) + "% of runs"
      : "no runs in this window";

    renderBars($("by-user"), stats.by_user,
      function (r) { return nameOf(r.user, "unknown"); },
      function (r) { return r.cost_usd; },
      function (r) { return money(r.cost_usd) + " · " + runCount(r.runs); },
      "No runs in this window.");

    var agentCard = $("card-by-agent");
    agentCard.hidden = !Array.isArray(stats.by_agent);
    if (Array.isArray(stats.by_agent)) {
      renderBars($("by-agent"), stats.by_agent,
        function (r) { return nameOf(r.agent, "unknown"); },
        function (r) { return r.cost_usd; },
        function (r) { return money(r.cost_usd) + " · " + runCount(r.runs); },
        "No runs in this window.");
    }

    renderBars($("by-model"), stats.by_model,
      function (r) { return nameOf(r.model, "unknown"); },
      function (r) { return r.cost_usd; },
      function (r) { return money(r.cost_usd) + " · " + tokens(r.tokens) + " tok"; },
      "No runs in this window.");
    renderBars($("by-project"), stats.by_project,
      function (r) { return nameOf(r.project, "unknown"); },
      function (r) { return r.cost_usd; },
      function (r) { return money(r.cost_usd) + " · " + runCount(r.runs); },
      "No runs in this window.");
    renderBars($("top-risks"), stats.top_risk_codes,
      function (r) { return String(r.code).replace(/_/g, " "); },
      function (r) { return r.occurrences; },
      function (r) { return r.occurrences + " × in " + runCount(r.runs); },
      "No risky actions recorded.");
  }

  function renderRuns(body) {
    var runs = (body && body.runs) || [];
    var tbody = $("runs");
    clear(tbody);
    $("runs-empty").textContent = runs.length
      ? ""
      : "No runs match these filters. Push a session with: runledger push --server URL --key KEY";
    runs.forEach(function (r) {
      var lv = level(r.risk_level);
      var tr = el("tr");
      tr.appendChild(cell(when(r.started_at || r.created_at), "mono"));
      tr.appendChild(cell(r.user || "-"));
      tr.appendChild(cell(r.project || "-"));
      tr.appendChild(cell(r.agent || "-"));
      var req = cell(r.title || "-", "req");
      req.title = r.title || "";
      tr.appendChild(req);
      tr.appendChild(cell(r.steps, "num"));
      tr.appendChild(cell(money(r.cost_usd), "num"));
      tr.appendChild(cell(el("span", "badge " + lv, String(r.risk_score) + " " + lv)));
      tr.appendChild(cell(qualityNode(r), "num"));
      tr.appendChild(cell(verdictNode(r.ai_verdict)));
      var link = cell("", "num");
      if (r.has_html) {
        var a = el("a", null, "Open");
        a.href = "/runs/" + encodeURIComponent(r.id);
        a.target = "_blank";
        a.rel = "noopener";
        link.appendChild(a);
      }
      tr.appendChild(link);
      tbody.appendChild(tr);
    });
  }

  function runQuery() {
    var params = new URLSearchParams();
    var user = $("f-user").value.trim();
    if (user) { params.set("user", user); }
    var project = $("f-project").value.trim();
    if (project) { params.set("project", project); }
    var agent = $("f-agent").value.trim();
    if (agent) { params.set("agent", agent); }
    var min = $("f-min").value.trim();
    if (min !== "") {
      params.set("min_risk", String(Math.min(100, Math.max(0, Math.floor(Number(min)) || 0))));
    }
    var minQuality = $("f-quality").value.trim();
    if (minQuality !== "") {
      params.set("min_quality", String(Math.min(100, Math.max(0, Math.floor(Number(minQuality)) || 0))));
    }
    params.set("limit", "200");
    return params.toString();
  }

  function loadStats() {
    return getJSON("/api/stats?days=" + encodeURIComponent(String(state.days))).then(renderStats);
  }

  function loadRuns() {
    return getJSON("/api/runs?" + runQuery()).then(renderRuns);
  }

  function refresh() {
    return report(Promise.all([loadStats(), loadRuns()]));
  }

  function debounce(fn, ms) {
    var timer = null;
    return function () {
      clearTimeout(timer);
      timer = setTimeout(fn, ms);
    };
  }

  var applyFilters = debounce(function () { report(loadRuns()); }, 300);

  // Budget card. Hidden when /api/budgets is missing or forbidden.

  function topSpenders(list) {
    return (Array.isArray(list) ? list.slice() : []).sort(function (a, b) {
      return (Number(b.spend_usd) || 0) - (Number(a.spend_usd) || 0);
    }).slice(0, 5);
  }

  function toneFor(p) {
    if (p >= 100) { return " danger"; }
    var step = 0;
    state.thresholds.forEach(function (t, i) { if (p >= t) { step = i + 1; } });
    return step >= 2 ? " danger" : (step === 1 ? " warn" : "");
  }

  function fillBudgetForm(settings) {
    $("budget-monthly").value = (settings.monthly_usd === null || settings.monthly_usd === undefined) ? "" : String(settings.monthly_usd);
    $("budget-per-user").value = (settings.per_user_monthly_usd === null || settings.per_user_monthly_usd === undefined) ? "" : String(settings.per_user_monthly_usd);
  }

  function renderBudget() {
    var settings = budgetState.settings;
    var status = budgetState.status;
    if (!settings || !status) { return; }
    $("budget-card").hidden = false;
    setMsg($("budget-status"), "", false);

    var thresholds = (Array.isArray(settings.alert_thresholds) ? settings.alert_thresholds : [])
      .map(Number)
      .filter(function (n) { return isFinite(n) && n > 0; })
      .sort(function (a, b) { return a - b; });
    state.thresholds = thresholds.length ? thresholds : [50, 80, 100];

    var limitValue = (status.monthly_usd !== null && status.monthly_usd !== undefined)
      ? status.monthly_usd
      : settings.monthly_usd;
    var limit = Number(limitValue);
    var hasLimit = limitValue !== null && limitValue !== undefined && isFinite(limit) && limit > 0;
    var spend = Number(status.spend_usd) || 0;
    var p = null;
    if (hasLimit) {
      p = pct(status.pct);
      if (p === null) { p = Math.round((100 * spend) / limit); }
    }

    $("budget-month").textContent = status.month ? "Month " + status.month : "";
    $("budget-figure").textContent = hasLimit ? money(spend) + " of " + money(limit) : money(spend) + " spent";
    $("budget-note").textContent = hasLimit
      ? p + "% of the team budget used"
      : "No team budget is set. Admins can set one below.";
    $("budget-over").hidden = !(hasLimit && p >= 100);

    var bar = $("budget-bar");
    bar.hidden = !hasLimit;
    var shown = hasLimit ? Math.min(100, Math.max(0, p)) : 0;
    bar.setAttribute("aria-valuenow", String(shown));
    bar.setAttribute("aria-valuetext", hasLimit ? p + "% of the team budget used" : "No team budget set");
    var fill = $("budget-fill");
    fill.style.width = shown + "%";
    fill.className = "progress-fill" + (hasLimit ? toneFor(p) : "");

    renderBars($("budget-users"), topSpenders(status.per_user),
      function (r) { return nameOf(r.user, "unknown"); },
      function (r) { return r.spend_usd; },
      function (r) {
        var up = pct(r.pct);
        return money(r.spend_usd) + (up === null ? "" : " · " + up + "%");
      },
      "No developer spend recorded this month.");

    $("budget-form").hidden = !isAdmin();
    if (isAdmin() && !budgetState.edited) { fillBudgetForm(settings); }
  }

  function loadBudget() {
    return Promise.all([getJSON("/api/budgets"), getJSON("/api/budgets/status")]).then(function (pair) {
      budgetState.settings = pair[0] || {};
      budgetState.status = pair[1] || {};
      renderBudget();
    }, function (err) {
      if (isMissingOrDenied(err)) {
        $("budget-card").hidden = true;
        return;
      }
      $("budget-card").hidden = false;
      setMsg($("budget-status"), explain(err), true);
    });
  }

  function budgetInput(raw, label) {
    var text = raw.trim();
    if (text === "") { return { value: null }; }
    var n = Number(text);
    if (!isFinite(n) || n <= 0 || n > MAX_BUDGET_USD) {
      return { error: label + " must be a positive amount up to " + MAX_BUDGET_USD + ", or left blank for no limit." };
    }
    return { value: Math.round(n * 100) / 100 };
  }

  function saveBudget(ev) {
    ev.preventDefault();
    var monthly = budgetInput($("budget-monthly").value, "The team budget");
    var perUser = budgetInput($("budget-per-user").value, "The per-developer budget");
    var msg = $("budget-msg");
    if (monthly.error || perUser.error) {
      setMsg(msg, monthly.error || perUser.error, true);
      return;
    }
    var button = $("budget-save");
    button.disabled = true;
    putJSON("/api/budgets", {
      monthly_usd: monthly.value,
      per_user_monthly_usd: perUser.value,
      alert_thresholds: state.thresholds
    }).then(function () {
      budgetState.edited = false;
      setMsg(msg, "Budget saved.", false);
      return loadBudget();
    }, function (err) {
      setMsg(msg, saveError(err), true);
    }).then(function () {
      button.disabled = false;
    });
  }

  // Approvals

  function notifyApproval(message) {
    var box = $("approvals-notice");
    box.textContent = message || "";
    box.className = message ? "notice show error" : "notice";
  }

  function requestedByThisKey(a) {
    if (!a || !state.keyRef || !a.requested_by) { return false; }
    return String(a.requested_by).endsWith("(" + state.keyRef + ")");
  }

  function canDecide(a) {
    return isAdmin() && !!(a && a.requested_by) && !requestedByThisKey(a);
  }

  function approvalCard(a) {
    var card = el("article", "card approval");
    var head = el("div", "approval-head");
    head.appendChild(el("div", "approval-tool", a.tool));
    head.appendChild(el("span", "mono", when(a.created_at)));
    card.appendChild(head);
    card.appendChild(el("pre", "approval-summary", a.summary));

    if (a.risks && a.risks.length) {
      var risks = el("div", "approval-risks");
      a.risks.forEach(function (r) {
        var chip = el("span", "badge " + level(r.severity), String(r.code || "risk").replace(/_/g, " "));
        chip.title = r.reason || "";
        risks.appendChild(chip);
      });
      card.appendChild(risks);
    }

    var where = [];
    if (a.session_id) { where.push("session " + a.session_id); }
    if (a.cwd) { where.push(a.cwd); }
    card.appendChild(el("div", "approval-meta", where.join(" · ")));

    var actions = el("div", "approval-actions");
    if (canDecide(a)) {
      var approve = el("button", "btn approve", "Approve");
      var deny = el("button", "btn deny", "Deny");
      approve.type = "button";
      deny.type = "button";
      var pair = [approve, deny];
      approve.addEventListener("click", function () { decide(a.id, "approve", pair); });
      deny.addEventListener("click", function () { decide(a.id, "deny", pair); });
      actions.appendChild(approve);
      actions.appendChild(deny);
    } else if (!a.requested_by) {
      actions.appendChild(el("span", "msg", "Requester identity is unavailable; re-request this approval."));
    } else if (isAdmin() && requestedByThisKey(a)) {
      actions.appendChild(el("span", "msg", "Requested with this key; another admin must decide."));
    }
    var details = el("a", null, "Details");
    details.href = "/approvals/" + encodeURIComponent(a.id);
    actions.appendChild(details);
    card.appendChild(actions);
    return card;
  }

  function renderApprovals(items) {
    approvalsView.items = items;
    $("approvals-count").textContent = String(items.length);
    $("tab-approvals-count").textContent = String(items.length);
    $("approvals-empty").textContent = items.length
      ? ""
      : "Nothing waiting. Risky agent actions appear here for a yes or no.";
    var signature = JSON.stringify([state.role, state.keyRef, items]);
    if (signature === approvalsView.signature) { return; }  // unchanged: keep the buttons as they are
    approvalsView.signature = signature;
    var list = $("approvals-list");
    clear(list);
    items.forEach(function (a) { list.appendChild(approvalCard(a)); });
  }

  function loadApprovals() {
    var mine = ++approvalsView.seq;
    return getJSON("/api/approvals?status=pending&limit=50").then(function (body) {
      if (mine !== approvalsView.seq) { return; }
      notifyApproval("");
      renderApprovals((body && body.approvals) || []);
    }, function (err) {
      if (mine !== approvalsView.seq) { return; }
      notifyApproval(explain(err));
    });
  }

  function decisionError(err) {
    if (err.status === 409) { return "This approval was already decided or has expired."; }
    if (err.status === 404) { return "This approval no longer exists."; }
    return "Could not record the decision: " + explain(err);
  }

  function decide(id, decision, buttons) {
    buttons.forEach(function (b) { b.disabled = true; });
    notifyApproval("");
    postJSON("/api/approvals/" + encodeURIComponent(id) + "/decision", { decision: decision })
      .then(function () {}, function (err) { notifyApproval(decisionError(err)); })
      .then(function () {
        approvalsView.signature = null;  // redraw from the server, which also re-enables the buttons
        return loadApprovals();
      });
  }

  // Confirm dialog. Focus returns to the control that opened it.

  function confirmAction(title, text, okLabel, danger) {
    var dlg = $("confirm");
    if (typeof dlg.showModal !== "function") {
      return Promise.resolve(window.confirm(title + "\n\n" + text));
    }
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    var ok = $("confirm-ok");
    ok.textContent = okLabel;
    ok.className = danger ? "btn btn-danger" : "btn primary";
    var opener = document.activeElement;
    return new Promise(function (resolve) {
      function done() {
        dlg.removeEventListener("close", done);
        var yes = dlg.returnValue === "ok";
        if (opener && typeof opener.focus === "function" && document.contains(opener)) {
          opener.focus();
        } else {
          $("view-keys").focus();
        }
        resolve(yes);
      }
      dlg.returnValue = "";
      dlg.addEventListener("close", done);
      dlg.showModal();
      $("confirm-cancel").focus();
    });
  }

  // API keys (admin)

  function keyError(err) {
    if (err.status === 409 && err.code === "last_admin") {
      return err.message || "The team must keep at least one active admin key.";
    }
    if (err.status === 400 || err.status === 404 || err.status === 409) { return err.message; }
    return saveError(err);
  }

  function clearKeyOnce() {
    $("key-value").textContent = "";
    $("key-once-label").textContent = "";
    $("key-once").hidden = true;
    setMsg($("key-copy-msg"), "", false);
  }

  function showKeyOnce(created, text) {
    var key = created && created.key;
    if (!key) {
      setMsg($("keys-msg"), "The server did not return the new key.", true);
      return;
    }
    $("key-value").textContent = key;
    $("key-once-label").textContent = text;
    $("key-once").hidden = false;
    setMsg($("key-copy-msg"), "", false);
    $("key-once").focus();
  }

  function selectAndCopy() {
    var range = document.createRange();
    range.selectNodeContents($("key-value"));
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    try { return document.execCommand("copy"); } catch (e) { return false; }
  }

  function renderKeys() {
    var body = $("keys-body");
    clear(body);
    keysView.items.forEach(function (k) {
      var name = nameOf(k.label, String(k.id));
      var revoked = !!k.revoked_at;
      var tr = el("tr");
      tr.appendChild(cell(k.label || "-"));
      tr.appendChild(cell(k.role || "-"));
      tr.appendChild(cell(k.prefix ? k.prefix + "..." : "-", "mono"));
      tr.appendChild(cell(when(k.created_at), "mono"));
      tr.appendChild(cell(k.last_used_at ? when(k.last_used_at) : "never", "mono"));
      tr.appendChild(cell(revoked ? "revoked " + when(k.revoked_at) : "active", "mono"));
      var actions = cell("", "num");
      if (!revoked) {
        var rotate = el("button", "small", "Rotate");
        rotate.type = "button";
        rotate.setAttribute("aria-label", "Rotate key " + name);
        rotate.addEventListener("click", function () { rotateKey(k); });
        var revoke = el("button", "small btn-danger", "Revoke");
        revoke.type = "button";
        revoke.setAttribute("aria-label", "Revoke key " + name);
        revoke.addEventListener("click", function () { revokeKey(k); });
        actions.appendChild(rotate);
        actions.appendChild(document.createTextNode(" "));
        actions.appendChild(revoke);
      }
      tr.appendChild(actions);
      body.appendChild(tr);
    });
    $("keys-empty").textContent = keysView.items.length ? "" : "No keys yet. Create one above.";
  }

  function loadKeys() {
    return getJSON("/api/keys").then(function (body) {
      keysView.items = Array.isArray(body && body.keys) ? body.keys : [];
      $("key-form").hidden = false;
      $("keys-table").hidden = false;
      renderKeys();
    }, function (err) {
      keysView.items = [];
      $("key-form").hidden = true;
      $("keys-table").hidden = true;
      renderKeys();
      $("keys-empty").textContent = "";
      setMsg($("keys-msg"), unavailable(err), !isMissingOrDenied(err));
    });
  }

  function revokeKey(key) {
    var name = nameOf(key.label, String(key.id));
    confirmAction("Revoke this key?",
      "'" + name + "' stops working right away. This cannot be undone.",
      "Revoke key", true)
      .then(function (yes) {
        if (!yes) { return; }
        return postJSON("/api/keys/" + encodeURIComponent(key.id) + "/revoke", {}).then(function () {
          setMsg($("keys-msg"), "Key revoked.", false);
          return loadKeys();
        }, function (err) {
          setMsg($("keys-msg"), keyError(err), true);
        });
      });
  }

  function rotateKey(key) {
    var name = nameOf(key.label, String(key.id));
    confirmAction("Rotate this key?",
      "A new key replaces '" + name + "'. The old key may stop working. The new key is shown once.",
      "Rotate key", false)
      .then(function (yes) {
        if (!yes) { return; }
        return postJSON("/api/keys/" + encodeURIComponent(key.id) + "/rotate", {}).then(function (created) {
          showKeyOnce(created, "New key for '" + name + "'.");
          setMsg($("keys-msg"), "Key rotated.", false);
          return loadKeys();
        }, function (err) {
          setMsg($("keys-msg"), keyError(err), true);
        });
      });
  }

  // Audit log (admin)

  function oldestId(events) {
    var best = null;
    events.forEach(function (e) {
      var n = Number(e.id);
      if (e.id !== null && e.id !== undefined && e.id !== "" && isFinite(n) && (best === null || n < best)) { best = n; }
    });
    return best;
  }

  function detailsText(value) {
    if (value === null || value === undefined || value === "") { return "-"; }
    if (typeof value === "string") { return value; }
    try { return JSON.stringify(value); } catch (e) { return "-"; }
  }

  function renderAudit() {
    var query = $("audit-filter").value.trim().toLowerCase();
    var rows = auditView.events.filter(function (e) {
      return !query || String(e.action || "").toLowerCase().indexOf(query) >= 0;
    });
    var body = $("audit-body");
    clear(body);
    rows.forEach(function (e) {
      var tr = el("tr");
      tr.appendChild(cell(when(e.at), "mono"));
      tr.appendChild(cell(e.actor || "-"));
      tr.appendChild(cell(e.action || "-", "mono"));
      tr.appendChild(cell(e.target || "-"));
      var text = detailsText(e.details);
      var details = cell(text, "details");
      details.title = text;
      tr.appendChild(details);
      body.appendChild(tr);
    });
    $("audit-empty").textContent = rows.length
      ? ""
      : (auditView.events.length ? "No loaded events match this filter." : "No audit events yet.");
    $("audit-more").hidden = !(auditView.more && oldestId(auditView.events) !== null);
  }

  function loadAudit(fromTop) {
    if (auditView.busy) { return Promise.resolve(); }
    var before = fromTop ? null : oldestId(auditView.events);
    if (!fromTop && before === null) { return Promise.resolve(); }
    auditView.busy = true;
    $("audit-more").disabled = true;
    var url = "/api/audit?limit=" + AUDIT_PAGE + (before === null ? "" : "&before=" + encodeURIComponent(String(before)));
    return getJSON(url).then(function (body) {
      var rows = Array.isArray(body && body.events) ? body.events : [];
      var merged = fromTop ? [] : auditView.events.slice();
      var seen = {};
      merged.forEach(function (e) { seen[String(e.id)] = true; });
      rows.forEach(function (e) {
        if (!seen[String(e.id)]) { seen[String(e.id)] = true; merged.push(e); }
      });
      auditView.events = merged;
      auditView.more = rows.length >= AUDIT_PAGE;
      setMsg($("audit-msg"), "", false);
      $("audit-table").hidden = false;
      renderAudit();
    }, function (err) {
      auditView.events = [];
      auditView.more = false;
      $("audit-table").hidden = true;
      renderAudit();
      $("audit-empty").textContent = "";
      setMsg($("audit-msg"), unavailable(err), !isMissingOrDenied(err));
    }).then(function () {
      auditView.busy = false;
      $("audit-more").disabled = false;
    });
  }

  // Exports: a link is shown only when a cheap GET shows the endpoint exists.
  // The probe reads the response headers and drops the body.

  // Each probe is a fresh GET, so Refresh and re-entering a tab re-check the server.
  function probeExport(name) {
    return fetch(EXPORT_PATHS[name] + "?days=7", {
      method: "HEAD", credentials: "same-origin", cache: "no-store"
    }).then(function (res) {
      if (res.body && typeof res.body.cancel === "function") { res.body.cancel().catch(function () {}); }
      return res.ok || res.status >= 500;
    }, function () {
      return false;
    }).then(function (ok) {
      exportReady[name] = ok;
      return ok;
    });
  }

  function setExportLink(link, name, days) {
    var on = exportReady[name] === true;
    link.hidden = !on;
    if (on) { link.href = EXPORT_PATHS[name] + "?days=" + encodeURIComponent(days); }
  }

  function refreshExportLinks() {
    var days = $("exp-days").value;
    setExportLink($("exp-runs"), "runs", days);
    setExportLink($("exp-audit"), "audit", days);
    setExportLink($("exp-report"), "report", days);
    setExportLink($("audit-export"), "audit", $("audit-days").value);
  }

  function loadExports() {
    return Promise.all([probeExport("runs"), probeExport("audit"), probeExport("report")]).then(function () {
      refreshExportLinks();
      var any = exportReady.runs || exportReady.audit || exportReady.report;
      $("exports-card").hidden = false;
      $("exports-empty").textContent = any ? "" : "Exports are not available for this session.";
    });
  }

  // Notification settings (admin): the existing team settings endpoint.

  function setNotifyFields(s) {
    $("notify-slack").value = s.slack_webhook_url || "";
    $("notify-webhook").value = s.webhook_url || "";
    $("notify-ttl").value = (s.approval_ttl_s === null || s.approval_ttl_s === undefined) ? "" : String(s.approval_ttl_s);
  }

  function loadNotifications() {
    return getJSON("/api/team/settings").then(function (s) {
      $("notify-section").hidden = false;
      $("notify-form").hidden = false;
      setNotifyFields(s);
    }, function (err) {
      if (isMissingOrDenied(err)) {
        $("notify-section").hidden = true;
        return;
      }
      $("notify-section").hidden = false;
      $("notify-form").hidden = true;
      setMsg($("notify-msg"), err.status === 401
        ? "Notification settings are not available in this browser session."
        : "Could not load notification settings: " + err.message, true);
    });
  }

  function loadSettings() {
    loadNotifications();
    return loadExports();
  }

  // Quality and AI review cells for the runs table.

  var GRADE_LIST = ["A", "B", "C", "D", "E", "F"];
  var VERDICT_INFO = {
    looks_safe: { tone: "v-safe", glyph: "✓", label: "Looks safe" },
    needs_review: { tone: "v-review", glyph: "!", label: "Needs review" },
    dangerous: { tone: "v-danger", glyph: "✕", label: "Dangerous" }
  };

  function gradeTone(grade) {
    if (grade === "A" || grade === "B") { return "g-good"; }
    if (grade === "C" || grade === "D") { return "g-warn"; }
    return "g-bad";
  }

  function gradeNode(grade) {
    if (GRADE_LIST.indexOf(grade) < 0) { return null; }
    return el("span", "grade " + gradeTone(grade), grade);
  }

  function qualityNode(run) {
    var score = pct(run.quality_score);
    if (score === null) { return "-"; }
    var wrap = el("span", "quality");
    var grade = gradeNode(run.quality_grade);
    if (grade) { wrap.appendChild(grade); }
    wrap.appendChild(el("span", "score", String(score)));
    return wrap;
  }

  function verdictNode(name) {
    var info = Object.prototype.hasOwnProperty.call(VERDICT_INFO, name) ? VERDICT_INFO[name] : null;
    if (!info) { return "-"; }
    var wrap = el("span", "verdict " + info.tone);
    var icon = el("span", "verdict-icon", info.glyph);
    icon.setAttribute("aria-hidden", "true");
    wrap.appendChild(icon);
    wrap.appendChild(el("span", "verdict-text", info.label));
    return wrap;
  }

  // Insights view (any role): GET /api/insights. Shows nothing it was not sent.

  var SVG_NS = "http://www.w3.org/2000/svg";

  function scoreText(value) {
    return value === null || value === undefined ? "-" : Number(value).toFixed(1);
  }

  function topGrade(byGrade) {
    var best = null;
    var bestCount = 0;
    GRADE_LIST.forEach(function (g) {
      var n = Number((byGrade || {})[g]) || 0;
      if (n > bestCount) { best = g; bestCount = n; }
    });
    return best;
  }

  function renderInsightKpis(data) {
    var q = data.quality || {};
    var ai = data.ai_review || {};
    var recs = Array.isArray(data.top_recommendations) ? data.top_recommendations : [];
    var scored = Number(q.scored_runs) || 0;

    $("i-quality").textContent = scoreText(q.avg);
    var top = topGrade(q.by_grade);
    clear($("i-quality-grade"));
    var badge = top ? gradeNode(top) : null;
    if (badge) { $("i-quality-grade").appendChild(badge); }
    $("i-quality-sub").textContent = scored
      ? runCount(scored) + " scored" + (top ? " · most common grade " + top : "")
      : "No scored runs in this window.";

    $("i-savings").textContent = money(data.est_savings_usd);
    $("i-savings-sub").textContent = recs.length
      ? "Estimated from recommendations"
      : "No recommendations in this window.";

    var reviewed = Number(ai.reviewed_runs) || 0;
    $("i-reviewed").textContent = String(reviewed);
    $("i-reviewed-sub").textContent = reviewed
      ? (Number(ai.dangerous) || 0) + " dangerous · " + (Number(ai.needs_review) || 0) + " need review · " +
        (Number(ai.looks_safe) || 0) + " look safe"
      : "No AI review in this window.";

    var rate = ai.false_positive_rate;
    var hasRate = rate !== null && rate !== undefined && isFinite(Number(rate));
    $("i-fp").textContent = hasRate ? (Number(rate) * 100).toFixed(1) + "%" : "-";
    $("i-fp-sub").textContent = hasRate
      ? "Rule risks the AI marked as false positives"
      : "No AI-assessed rule risks in this window.";
  }

  // The trend is one polyline and a dot per day, on a fixed 0 to 100 scale. Built with
  // createElementNS and textContent only.
  function sparkline(points) {
    var W = 320;
    var H = 110;
    var P = 10;
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "spark");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("role", "img");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("aria-label", "Average quality per day over " + points.length + " days, scale 0 to 100");
    var step = points.length > 1 ? (W - 2 * P) / (points.length - 1) : 0;
    var marks = points.map(function (p, i) {
      var avg = Math.max(0, Math.min(100, Number(p.avg) || 0));
      return {
        x: points.length > 1 ? P + i * step : W / 2,
        y: H - P - (avg / 100) * (H - 2 * P),
        date: String(p.date),
        avg: avg
      };
    });
    if (marks.length > 1) {
      var line = document.createElementNS(SVG_NS, "polyline");
      line.setAttribute("points", marks.map(function (m) {
        return m.x.toFixed(1) + "," + m.y.toFixed(1);
      }).join(" "));
      svg.appendChild(line);
    }
    marks.forEach(function (m) {
      var dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("cx", m.x.toFixed(1));
      dot.setAttribute("cy", m.y.toFixed(1));
      dot.setAttribute("r", "3");
      var tip = document.createElementNS(SVG_NS, "title");
      tip.textContent = m.date + ": " + m.avg.toFixed(1);
      dot.appendChild(tip);
      svg.appendChild(dot);
    });
    return svg;
  }

  function renderTrend(points) {
    var box = $("i-trend");
    clear(box);
    var list = Array.isArray(points) ? points : [];
    if (!list.length) {
      box.appendChild(el("div", "empty", "No scored runs in this window."));
      return;
    }
    box.appendChild(sparkline(list));
    var axis = el("div", "spark-axis");
    axis.appendChild(el("span", null, list[0].date));
    axis.appendChild(el("span", null, list[list.length - 1].date));
    box.appendChild(axis);
  }

  function renderGrades(byGrade) {
    var counts = byGrade || {};
    var rows = GRADE_LIST.map(function (g) { return { grade: g, n: Number(counts[g]) || 0 }; });
    var total = rows.reduce(function (sum, r) { return sum + r.n; }, 0);
    renderBars($("i-grades"), total > 0 ? rows : [],
      function (r) { return "Grade " + r.grade; },
      function (r) { return r.n; },
      function (r) { return runCount(r.n); },
      "No scored runs in this window.");
  }

  function shareNode(share) {
    if (share === null || share === undefined || !isFinite(Number(share))) { return "n/a"; }
    var value = Math.max(0, Math.min(100, Number(share) * 100));
    var wrap = el("div", "share");
    var track = el("div", "bar-track");
    var fill = el("div", "bar-fill");
    fill.style.width = value.toFixed(1) + "%";
    track.appendChild(fill);
    wrap.appendChild(track);
    wrap.appendChild(el("span", "bar-val", value.toFixed(1) + "%"));
    return wrap;
  }

  function renderModels(rows) {
    var list = Array.isArray(rows) ? rows : [];
    var body = $("i-models");
    clear(body);
    list.forEach(function (m) {
      var tr = el("tr");
      tr.appendChild(cell(nameOf(m.model, "unknown")));
      tr.appendChild(cell(String(m.runs || 0), "num"));
      tr.appendChild(cell(money(m.cost_usd), "num"));
      tr.appendChild(cell(money(m.cost_per_run), "num"));
      tr.appendChild(cell(scoreText(m.avg_quality), "num"));
      tr.appendChild(cell(shareNode(m.share_of_cost)));
      body.appendChild(tr);
    });
    $("i-models-empty").textContent = list.length ? "" : "No model usage in this window.";
  }

  function renderAgents(rows) {
    var list = Array.isArray(rows) ? rows : [];
    var body = $("i-agents");
    clear(body);
    list.forEach(function (a) {
      var tr = el("tr");
      tr.appendChild(cell(nameOf(a.agent, "unknown")));
      tr.appendChild(cell(String(a.runs || 0), "num"));
      tr.appendChild(cell(scoreText(a.avg_quality), "num"));
      tr.appendChild(cell(scoreText(a.avg_risk), "num"));
      tr.appendChild(cell(money(a.cost_usd), "num"));
      body.appendChild(tr);
    });
    $("i-agents-empty").textContent = list.length ? "" : "No agent runs in this window.";
  }

  function renderRecommendations(rows) {
    var list = Array.isArray(rows) ? rows : [];
    var box = $("i-recs");
    clear(box);
    list.forEach(function (r) {
      var item = el("li");
      item.appendChild(el("div", "rec-title", nameOf(r.title, "Untitled")));
      var times = Number(r.count) || 0;
      var savings = (r.est_savings_usd === null || r.est_savings_usd === undefined)
        ? "no estimate"
        : "est. " + money(r.est_savings_usd) + " saved";
      var kind = nameOf(r.kind, "other").replace(/_/g, " ");
      item.appendChild(el("div", "rec-meta",
        kind + " · given " + times + (times === 1 ? " time" : " times") + " · " + savings));
      box.appendChild(item);
    });
    $("i-recs-empty").textContent = list.length ? "" : "No recommendations in this window.";
  }

  function renderInsights(data) {
    var body = data || {};
    setMsg($("insights-msg"), "", false);
    $("insights-window").textContent = "Last " + (Number(body.days) || state.days) + " days, UTC";
    renderInsightKpis(body);
    var q = body.quality || {};
    renderTrend(q.trend);
    renderGrades(q.by_grade);
    renderModels(body.models);
    renderAgents(body.agents);
    renderRecommendations(body.top_recommendations);
  }

  function loadInsights() {
    return getJSON("/api/insights?days=" + encodeURIComponent(String(state.days))).then(renderInsights, function (err) {
      setMsg($("insights-msg"), unavailable(err), !isMissingOrDenied(err));
    });
  }

  // Dashboard session: role, views, and tabs.

  function isAdmin() {
    return state.role === "admin";
  }

  function viewAllowed(name) {
    return !ADMIN_VIEWS[name] || isAdmin();
  }

  function currentRoute() {
    var name = (location.hash || "").replace("#", "");
    return VIEWS.indexOf(name) >= 0 ? name : "runs";
  }

  function enterView(name) {
    if (name === "insights") { loadInsights(); }
    if (name === "keys") { loadKeys(); }
    if (name === "audit") {
      loadAudit(true);
      probeExport("audit").then(refreshExportLinks);
    }
    if (name === "settings") { loadSettings(); }
    if (name === "approvals") { loadApprovals(); }
  }

  function showView(requested) {
    var name = requested;
    if (!viewAllowed(name)) {
      name = "runs";
      if (state.meLoaded && location.hash !== "#runs") { history.replaceState(null, "", "#runs"); }
    }
    VIEWS.forEach(function (v) {
      var on = v === name;
      $("view-" + v).hidden = !on;
      var tab = $("tab-" + v);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      tab.classList.toggle("active", on);
    });
    if (state.view !== name) {
      if (state.view === "keys") { clearKeyOnce(); }
      state.view = name;
      enterView(name);
    }
  }

  function go(name) {
    if (location.hash === "#" + name) { showView(name); } else { location.hash = name; }
  }

  function applyRole() {
    var badge = $("role-badge");
    badge.textContent = state.role || "";
    badge.className = "role-badge" + (state.role ? " role-" + state.role : "");
    badge.hidden = !state.role;
    ["keys", "audit", "settings"].forEach(function (v) { $("tab-" + v).hidden = !isAdmin(); });
    $("budget-form").hidden = !isAdmin();
    if (isAdmin() && budgetState.settings && !budgetState.edited) { fillBudgetForm(budgetState.settings); }
    $("approvals-readonly").hidden = isAdmin();
    renderApprovals(approvalsView.items);
    showView(currentRoute());
  }

  function billingMessage(b) {
    var used = Number(b.seats_used) || 0, seats = Number(b.seats) || 0;
    if (b.state === "past_due") {
      return ["warn", "The last payment failed. Update the payment method before " + when(b.grace_until) +
        ", or the team becomes read only."];
    }
    if (b.state === "read_only") {
      return ["error", "Payment is overdue, so the team is read only: new runs and approval requests are refused. " +
        "Update the payment method to resume."];
    }
    if (b.state === "ended") {
      return ["error", "The subscription has ended, so the team is read only. Export what you need (Audit log tab) " +
        "before " + when(b.deletes_at) + ", when the team and its data are deleted."];
    }
    if (b.state === "canceling") {
      return ["warn", "The subscription is canceled and ends on " + when(b.current_period_end) + "."];
    }
    if (seats && used >= seats) {
      return ["warn", "All " + seats + " seats are in use. A developer without a seat cannot push runs until you add seats."];
    }
    return null;
  }

  function showBilling(b) {
    var box = $("billing-notice");
    box.textContent = "";
    var msg = b ? billingMessage(b) : null;
    box.className = "notice" + (msg ? " show " + msg[0] : "");
    if (!msg) { return; }
    box.appendChild(document.createTextNode(msg[1]));
    if (b.portal_url && /^https:\/\//.test(b.portal_url) && isAdmin()) {
      box.appendChild(document.createTextNode(" "));
      var a = document.createElement("a");
      a.href = b.portal_url;
      a.rel = "noopener noreferrer";
      a.target = "_blank";
      a.textContent = "Open the billing portal";
      box.appendChild(a);
    }
  }

  function loadMe() {
    return getJSON("/api/me").then(function (me) {
      state.meLoaded = true;
      state.role = ROLES.indexOf(me && me.role) >= 0 ? me.role : null;
      state.keyRef = me && me.key ? (me.key.prefix || me.key.id || null) : null;
      if (me && me.team && me.team.name) { setTeam(me.team.name); }
      showBilling(me && me.billing);
      applyRole();
    }, function () {
      // Without /api/me the role is unknown: keep the runs and approvals views, hide admin tabs.
      state.meLoaded = true;
      state.role = null;
      state.keyRef = null;
      applyRole();
    });
  }

  function refreshAll() {
    refresh();
    loadBudget();
    loadApprovals();
    if (state.view === "insights") { loadInsights(); }
    if (state.view === "keys") { loadKeys(); }
    if (state.view === "audit") { loadAudit(true); probeExport("audit").then(refreshExportLinks); }
    if (state.view === "settings") { loadSettings(); }
  }

  // Event wiring

  $("days").addEventListener("change", function () {
    state.days = Number(this.value) || 30;
    report(loadStats());
    if (state.view === "insights") { loadInsights(); }
  });
  $("refresh").addEventListener("click", refreshAll);
  ["f-user", "f-project", "f-agent", "f-min", "f-quality"].forEach(function (id) {
    $(id).addEventListener("input", applyFilters);
  });

  VIEWS.forEach(function (v) {
    $("tab-" + v).addEventListener("click", function () { go(v); });
  });
  $("tabs").addEventListener("keydown", function (ev) {
    var tabs = VIEWS.map(function (v) { return $("tab-" + v); }).filter(function (t) { return !t.hidden; });
    var i = tabs.indexOf(document.activeElement);
    if (i < 0) { return; }
    var next = -1;
    if (ev.key === "ArrowRight") { next = (i + 1) % tabs.length; }
    else if (ev.key === "ArrowLeft") { next = (i - 1 + tabs.length) % tabs.length; }
    else if (ev.key === "Home") { next = 0; }
    else if (ev.key === "End") { next = tabs.length - 1; }
    if (next < 0) { return; }
    ev.preventDefault();
    tabs[next].focus();
    go(tabs[next].getAttribute("data-view"));
  });
  window.addEventListener("hashchange", function () { showView(currentRoute()); });

  $("key-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var label = $("key-label").value.trim();
    var role = $("key-role").value;
    if (!label) {
      setMsg($("keys-msg"), "Enter a label for the key, such as the developer's name.", true);
      $("key-label").focus();
      return;
    }
    if (ROLES.indexOf(role) < 0) {
      setMsg($("keys-msg"), "Choose admin, member, or viewer.", true);
      return;
    }
    var button = $("key-create");
    button.disabled = true;
    postJSON("/api/keys", { label: label, role: role }).then(function (created) {
      $("key-label").value = "";
      showKeyOnce(created, "New key for '" + label + "' (" + role + ").");
      setMsg($("keys-msg"), "Key created.", false);
      return loadKeys();
    }, function (err) {
      setMsg($("keys-msg"), keyError(err), true);
    }).then(function () {
      button.disabled = false;
    });
  });
  $("key-copy").addEventListener("click", function () {
    var text = $("key-value").textContent;
    var show = function (ok) { setMsg($("key-copy-msg"), ok ? "Copied." : "Select the key and copy it by hand.", !ok); };
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(text).then(function () { show(true); }, function () { show(selectAndCopy()); });
    } else {
      show(selectAndCopy());
    }
  });
  $("key-dismiss").addEventListener("click", function () {
    clearKeyOnce();
    $("key-label").focus();
  });

  $("audit-filter").addEventListener("input", renderAudit);
  $("audit-more").addEventListener("click", function () { loadAudit(false); });
  $("audit-days").addEventListener("change", refreshExportLinks);
  $("exp-days").addEventListener("change", refreshExportLinks);

  $("budget-form").addEventListener("input", function () { budgetState.edited = true; });
  $("budget-form").addEventListener("submit", saveBudget);

  $("notify-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var msg = $("notify-msg");
    var ttlText = $("notify-ttl").value.trim();
    var ttl = Number(ttlText);
    if (ttlText === "" || !isFinite(ttl) || Math.floor(ttl) !== ttl || ttl < 1 || ttl > MAX_TTL_S) {
      setMsg(msg, "Expiry must be a whole number of seconds from 1 to " + MAX_TTL_S + " (7 days).", true);
      $("notify-ttl").focus();
      return;
    }
    var button = $("notify-save");
    button.disabled = true;
    putJSON("/api/team/settings", {
      slack_webhook_url: $("notify-slack").value.trim(),
      webhook_url: $("notify-webhook").value.trim(),
      approval_ttl_s: ttl
    }).then(function (s) {
      setNotifyFields(s);
      setMsg(msg, "Notification settings saved.", false);
    }, function (err) {
      setMsg(msg, saveError(err), true);
    }).then(function () {
      button.disabled = false;
    });
  });


  // Start

  loadMe();
  refresh();
  loadBudget();
  loadApprovals();
  setInterval(loadApprovals, APPROVAL_POLL_MS);
  showView(currentRoute());
})();
</script>
</body>
</html>
"""

APPROVAL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>RunLedger approval</title>
<style>
:root{--bg:#0a0b0d;--panel:#111316;--line:#1f2328;--text:#eef0f2;--muted:#9aa1ab;--accent:#4fe0b0;--amber:#f5b547;--red:#ff6b6b;--chip:#171a1e}
@media (prefers-color-scheme: light){:root{--bg:#f7f8f9;--panel:#fff;--line:#e3e6ea;--text:#121417;--muted:#5d6670;--accent:#047857;--amber:#b45309;--red:#c62828;--chip:#f0f2f4}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text)}
body{font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:28px 16px 64px}
.brand{color:var(--muted);font-size:12px;letter-spacing:.05em;text-transform:uppercase}
h1{font-size:22px;margin:6px 0 16px;overflow-wrap:anywhere}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:12px;min-width:0}
.row{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between}
.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em;margin-top:14px}
.label:first-child{margin-top:0}
.mono{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
pre.summary{white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);border-radius:8px;padding:12px;margin:6px 0 0;max-height:360px;overflow:auto}
.risks{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:8px 16px;margin:0}
dt{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em;padding-top:2px}
dd{margin:0;overflow-wrap:anywhere}
.badge{display:inline-block;font:700 11px ui-monospace,monospace;padding:2px 8px;border-radius:99px;white-space:nowrap;background:var(--chip);color:var(--muted)}
.badge.low{background:var(--chip);color:var(--muted)}
.badge.medium{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--amber)}
.badge.high{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--red)}
.badge.st-pending{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--amber)}
.badge.st-approved{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}
.badge.st-denied{background:color-mix(in srgb,var(--red) 18%,transparent);color:var(--red)}
.badge.st-expired{background:var(--chip);color:var(--muted)}
.fields{display:grid;gap:10px}
input,button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:0}
input{width:100%}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
.btn{font-weight:600;padding:9px 18px;cursor:pointer}
.btn.approve{background:var(--accent);border-color:var(--accent);color:var(--bg)}
.btn.deny{color:var(--red);border-color:var(--red)}
button:disabled{opacity:.5;cursor:default}
.notice{display:none;margin:0 0 12px;padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel)}
.notice.show{display:block}
.notice.error{border-color:var(--red);color:var(--red)}
.muted{color:var(--muted)}
a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
<div class="brand">RunLedger approval</div>
<h1 id="title">Approval request</h1>
<div id="notice" class="notice" role="alert"></div>

<div class="card">
  <div class="row">
    <span class="badge" id="status" aria-live="polite">loading</span>
    <span class="mono muted" id="created"></span>
  </div>
  <div class="label">Tool</div>
  <div class="mono" id="tool"></div>
  <div class="label">What the agent wants to do</div>
  <pre class="summary" id="summary"></pre>
  <div class="label">Risks</div>
  <div class="risks" id="risks"></div>
</div>

<div class="card">
  <dl>
    <dt>Session</dt><dd class="mono" id="session"></dd>
    <dt>Folder</dt><dd class="mono" id="cwd"></dd>
    <dt>Expires</dt><dd id="expires"></dd>
    <dt>Decided by</dt><dd id="decided-by"></dd>
    <dt>Decided at</dt><dd id="decided-at"></dd>
    <dt>Reason</dt><dd id="reason"></dd>
  </dl>
</div>

<p class="muted" id="decision-note"></p>

<div class="card" id="decide" hidden>
  <div class="fields">
    <input id="who" type="text" maxlength="100" placeholder="Your name (optional)" aria-label="Your name">
    <input id="why" type="text" maxlength="500" placeholder="Reason (optional)" aria-label="Reason for the decision">
  </div>
  <div class="actions">
    <button type="button" class="btn approve" id="approve">Approve</button>
    <button type="button" class="btn deny" id="deny">Deny</button>
  </div>
</div>

<p><a href="/">Back to the dashboard</a></p>
</div>

<script nonce="__CSP_NONCE__">
(function () {
  "use strict";

  var POLL_MS = 3000;
  var approvalId = location.pathname.slice("/approvals/".length);
  var timer = null;
  var busy = false;
  var role = null;
  var keyRef = null;
  var meLoaded = false;
  var currentApproval = null;

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function when(iso) {
    if (!iso) { return "-"; }
    var d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  function level(value) {
    return value === "high" || value === "medium" ? value : "low";
  }

  function parseBody(res) {
    return res.text().then(function (text) {
      var body = {};
      try { body = text ? JSON.parse(text) : {}; } catch (e) { body = {}; }
      if (!res.ok) {
        var err = new Error((body.error && body.error.message) || ("Request failed with HTTP " + res.status));
        err.status = res.status;
        throw err;
      }
      return body;
    });
  }

  function request(method, url, payload) {
    var opts = { method: method, credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (payload !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-Requested-With"] = "runledger";
      opts.body = JSON.stringify(payload);
    }
    return fetch(url, opts).then(parseBody);
  }

  function explain(err) {
    if (err.status === 401) {
      return "Not signed in, or the session expired. Open the dashboard once with ?key=YOUR_API_KEY, then open this page again.";
    }
    if (err.status === 404) { return "This approval was not found for your team."; }
    return "Could not load the approval: " + err.message;
  }

  function notice(message) {
    var box = $("notice");
    box.textContent = message || "";
    box.className = message ? "notice show error" : "notice";
  }

  function setButtons(disabled) {
    $("approve").disabled = disabled;
    $("deny").disabled = disabled;
  }

  function requestedByThisKey(a) {
    if (!a || !keyRef || !a.requested_by) { return false; }
    return String(a.requested_by).endsWith("(" + keyRef + ")");
  }

  function canDecide(a) {
    return role === "admin" && !!(a && a.requested_by) && !requestedByThisKey(a);
  }

  function applyDecisionAccess() {
    var pending = currentApproval && currentApproval.status === "pending";
    $("decide").hidden = !(pending && canDecide(currentApproval));
    if (!pending) {
      $("decision-note").textContent = "";
    } else if (!currentApproval.requested_by) {
      $("decision-note").textContent = "Requester identity is unavailable for this legacy approval. Re-request it before deciding.";
    } else if (!meLoaded) {
      $("decision-note").textContent = "Checking whether this session can decide the request…";
    } else if (role !== "admin") {
      $("decision-note").textContent = "Read only: only an admin can approve or deny this request.";
    } else if (requestedByThisKey(currentApproval)) {
      $("decision-note").textContent = "This key requested the approval. Use a different admin key to decide it.";
    } else {
      $("decision-note").textContent = "";
    }
  }

  function renderRisks(risks) {
    var box = $("risks");
    clear(box);
    if (!risks || !risks.length) {
      box.appendChild(el("span", "muted", "None recorded"));
      return;
    }
    risks.forEach(function (r) {
      var chip = el("span", "badge " + level(r.severity), String(r.code || "risk").replace(/_/g, " "));
      chip.title = r.reason || "";
      box.appendChild(chip);
    });
  }

  function render(a) {
    currentApproval = a;
    var status = a.status || "pending";
    var badge = $("status");
    badge.textContent = status;
    badge.className = "badge st-" + status;
    $("title").textContent = a.tool ? "Approval: " + a.tool : "Approval request";
    $("tool").textContent = a.tool || "-";
    $("summary").textContent = a.summary || "";
    $("created").textContent = when(a.created_at);
    renderRisks(a.risks);
    $("session").textContent = a.session_id || "-";
    $("cwd").textContent = a.cwd || "-";
    $("expires").textContent = when(a.expires_at);
    $("decided-by").textContent = a.decided_by || "-";
    $("decided-at").textContent = a.decided_at ? when(a.decided_at) : "-";
    $("reason").textContent = a.reason || "-";
    var open = status === "pending";
    applyDecisionAccess();
    if (!open && timer) { clearInterval(timer); timer = null; }
  }

  function loadMe() {
    return request("GET", "/api/me").then(function (me) {
      role = me && me.role ? me.role : null;
      keyRef = me && me.key ? (me.key.prefix || me.key.id || null) : null;
      meLoaded = true;
      applyDecisionAccess();
    }, function () {
      role = null;
      keyRef = null;
      meLoaded = true;
      applyDecisionAccess();
    });
  }

  function load() {
    return request("GET", "/api/approvals/" + encodeURIComponent(approvalId)).then(function (a) {
      notice("");
      render(a);
    }, function (err) {
      notice(explain(err));
    });
  }

  function decisionError(err) {
    if (err.status === 409) { return "This approval was already decided or has expired. Showing the current state."; }
    return "Could not record the decision: " + explain(err);
  }

  function decide(decision) {
    if (busy || !canDecide(currentApproval)) { return; }
    busy = true;
    setButtons(true);
    var payload = { decision: decision };
    var who = $("who").value.trim();
    var why = $("why").value.trim();
    if (who) { payload.name = who; }
    if (why) { payload.reason = why; }
    request("POST", "/api/approvals/" + encodeURIComponent(approvalId) + "/decision", payload)
      .then(function (a) {
        notice("");
        render(a);
      }, function (err) {
        notice(decisionError(err));
        return load();
      })
      .then(function () {
        busy = false;
        setButtons(false);
      });
  }

  $("approve").addEventListener("click", function () { decide("approve"); });
  $("deny").addEventListener("click", function () { decide("deny"); });
  loadMe();
  load();
  timer = setInterval(load, POLL_MS);
})();
</script>
</body>
</html>
"""
