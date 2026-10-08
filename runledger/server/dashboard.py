"""The team dashboard: one self-contained page (inline CSS and JS, no CDN).

Server data is rendered with DOM text APIs only, so a developer name such as
<script> shows up as plain text. The server sends this page with a
per-response nonce CSP that allows only the script below.
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>RunLedger Team</title>
<style>
:root{--bg:#0a0b0d;--panel:#111316;--line:#1f2328;--text:#eef0f2;--muted:#9aa1ab;--accent:#4fe0b0;--amber:#f5b547;--red:#ff6b6b;--chip:#171a1e}
@media (prefers-color-scheme: light){:root{--bg:#f7f8f9;--panel:#fff;--line:#e3e6ea;--text:#121417;--muted:#5d6670;--accent:#047857;--amber:#b45309;--red:#c62828;--chip:#f0f2f4}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text)}
body{font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:28px 16px 64px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px;letter-spacing:.05em;text-transform:uppercase}
.mark{width:26px;height:26px;border-radius:7px;background:var(--accent);color:var(--bg);display:grid;place-items:center;font:700 12px ui-monospace,monospace}
h1{font-size:22px;margin:6px 0 0}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
select,input,button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
button{cursor:pointer}
.label-inline{color:var(--muted);font-size:12px}
.notice{display:none;margin:0 0 16px;padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel)}
.notice.show{display:block}
.notice.error{border-color:var(--red);color:var(--red)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.kpi-label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.kpi-value{font-size:24px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.kpi-sub{color:var(--muted);font-size:12px;margin-top:2px}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-top:12px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:26px 0 10px;font-weight:600}
.panel-title{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px;font-weight:600}
.bar-row{display:grid;grid-template-columns:minmax(80px,34%) 1fr auto;gap:10px;align-items:center;padding:5px 0;font-size:13px}
.bar-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{height:8px;border-radius:99px;background:var(--line);overflow:hidden}
.bar-fill{height:100%;background:var(--accent);border-radius:99px}
.bar-val{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;font-size:12px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.filters input{min-width:0;flex:1 1 150px}
.table-wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px}
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
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <div class="brand"><span class="mark">RL</span><span>RunLedger team</span></div>
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

<div id="notice" class="notice" role="alert"></div>

<section class="kpis">
  <div class="card"><div class="kpi-label">Total cost</div><div class="kpi-value" id="k-cost">-</div><div class="kpi-sub" id="k-cost-sub"></div></div>
  <div class="card"><div class="kpi-label">Runs</div><div class="kpi-value" id="k-runs">-</div><div class="kpi-sub" id="k-runs-sub"></div></div>
  <div class="card"><div class="kpi-label">Average risk</div><div class="kpi-value" id="k-risk">-</div><div class="kpi-sub">out of 100</div></div>
  <div class="card"><div class="kpi-label">High-risk runs</div><div class="kpi-value" id="k-high">-</div><div class="kpi-sub" id="k-high-sub"></div></div>
</section>

<section class="panels">
  <div class="card"><div class="panel-title">Cost by developer</div><div id="by-user"></div></div>
  <div class="card"><div class="panel-title">Cost by model</div><div id="by-model"></div></div>
  <div class="card"><div class="panel-title">Cost by project</div><div id="by-project"></div></div>
  <div class="card"><div class="panel-title">Top risk reasons</div><div id="top-risks"></div></div>
</section>

<h2>Runs</h2>
<div class="filters">
  <input id="f-user" type="search" placeholder="Developer" aria-label="Filter by developer" maxlength="200">
  <input id="f-project" type="search" placeholder="Project" aria-label="Filter by project" maxlength="200">
  <input id="f-min" type="number" min="0" max="100" placeholder="Min risk" aria-label="Minimum risk score">
</div>
<div class="table-wrap">
<table>
  <thead>
    <tr><th>When</th><th>Developer</th><th>Project</th><th>Request</th><th class="num">Steps</th><th class="num">Cost</th><th>Risk</th><th class="num">Receipt</th></tr>
  </thead>
  <tbody id="runs"></tbody>
</table>
</div>
<div id="runs-empty" class="empty"></div>

<footer>Costs are estimates from public Claude API list prices; subscription plans are billed differently. Risk scores are rule-based.</footer>
</div>

<script nonce="__CSP_NONCE__">
(function () {
  "use strict";

  var state = { days: 30 };

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

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (res) {
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
      });
  }

  function explain(err) {
    if (err.status === 401) {
      return "Not signed in, or the session expired. Open this dashboard once with ?key=YOUR_API_KEY.";
    }
    return "Could not load data: " + err.message;
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

  function renderStats(stats) {
    var t = stats.totals || {};
    if (stats.team) { $("team-name").textContent = stats.team + " · team runs"; }

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
      function (r) { return r.user; },
      function (r) { return r.cost_usd; },
      function (r) { return money(r.cost_usd) + " · " + runCount(r.runs); },
      "No runs in this window.");
    renderBars($("by-model"), stats.by_model,
      function (r) { return r.model; },
      function (r) { return r.cost_usd; },
      function (r) { return money(r.cost_usd) + " · " + tokens(r.tokens) + " tok"; },
      "No runs in this window.");
    renderBars($("by-project"), stats.by_project,
      function (r) { return r.project; },
      function (r) { return r.cost_usd; },
      function (r) { return money(r.cost_usd) + " · " + runCount(r.runs); },
      "No runs in this window.");
    renderBars($("top-risks"), stats.top_risk_codes,
      function (r) { return String(r.code).replace(/_/g, " "); },
      function (r) { return r.occurrences; },
      function (r) { return r.occurrences + " × in " + runCount(r.runs); },
      "No risky actions recorded.");
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
      var req = cell(r.title || "-", "req");
      req.title = r.title || "";
      tr.appendChild(req);
      tr.appendChild(cell(r.steps, "num"));
      tr.appendChild(cell(money(r.cost_usd), "num"));
      tr.appendChild(cell(el("span", "badge " + lv, String(r.risk_score) + " " + lv)));
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
    var min = $("f-min").value.trim();
    if (min !== "") {
      params.set("min_risk", String(Math.min(100, Math.max(0, Math.floor(Number(min)) || 0))));
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

  $("days").addEventListener("change", function () {
    state.days = Number(this.value) || 30;
    report(loadStats());
  });
  $("refresh").addEventListener("click", refresh);
  ["f-user", "f-project", "f-min"].forEach(function (id) {
    $(id).addEventListener("input", applyFilters);
  });

  refresh();
})();
</script>
</body>
</html>
"""
