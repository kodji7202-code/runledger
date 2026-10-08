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
.btn.approve{background:var(--accent);border-color:var(--accent);color:var(--bg)}
.btn.deny{color:var(--red);border-color:var(--red)}
button:disabled{opacity:.5;cursor:default}
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

<section class="approvals" aria-labelledby="approvals-title">
  <div class="approvals-head">
    <h2 id="approvals-title">Pending approvals</h2>
    <span class="count" id="approvals-count" aria-live="polite">0</span>
  </div>
  <div id="approvals-notice" class="notice" role="alert"></div>
  <div id="approvals-list" class="approvals-list"></div>
  <div id="approvals-empty" class="empty"></div>
</section>

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

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(parseBody);
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Accept": "application/json", "Content-Type": "application/json", "X-Requested-With": "runledger" },
      body: JSON.stringify(payload || {})
    }).then(parseBody);
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

  // Pending approvals: refreshed every 3 seconds. Only the newest request is applied,
  // so a slow earlier response cannot overwrite a newer one.
  var APPROVAL_POLL_MS = 3000;
  var approvalsView = { seq: 0, signature: null };

  function approvalNotice(message) {
    var box = $("approvals-notice");
    box.textContent = message || "";
    box.className = message ? "notice show error" : "notice";
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

    var approve = el("button", "btn approve", "Approve");
    var deny = el("button", "btn deny", "Deny");
    approve.type = "button";
    deny.type = "button";
    var pair = [approve, deny];
    approve.addEventListener("click", function () { decide(a.id, "approve", pair); });
    deny.addEventListener("click", function () { decide(a.id, "deny", pair); });
    var details = el("a", null, "Details");
    details.href = "/approvals/" + encodeURIComponent(a.id);

    var actions = el("div", "approval-actions");
    actions.appendChild(approve);
    actions.appendChild(deny);
    actions.appendChild(details);
    card.appendChild(actions);
    return card;
  }

  function renderApprovals(items) {
    $("approvals-count").textContent = String(items.length);
    $("approvals-empty").textContent = items.length
      ? ""
      : "Nothing waiting. Risky agent actions appear here for a yes or no.";
    var signature = JSON.stringify(items);
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
      approvalNotice("");
      renderApprovals((body && body.approvals) || []);
    }, function (err) {
      if (mine !== approvalsView.seq) { return; }
      approvalNotice(explain(err));
    });
  }

  function decisionError(err) {
    if (err.status === 409) { return "This approval was already decided or has expired."; }
    if (err.status === 404) { return "This approval no longer exists."; }
    return "Could not record the decision: " + explain(err);
  }

  function decide(id, decision, buttons) {
    buttons.forEach(function (b) { b.disabled = true; });
    approvalNotice("");
    postJSON("/api/approvals/" + encodeURIComponent(id) + "/decision", { decision: decision })
      .then(function () {}, function (err) { approvalNotice(decisionError(err)); })
      .then(function () {
        approvalsView.signature = null;  // redraw from the server, which also re-enables the buttons
        return loadApprovals();
      });
  }

  loadApprovals();
  setInterval(loadApprovals, APPROVAL_POLL_MS);

  refresh();
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

<div class="card" id="decide">
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
    $("decide").style.display = open ? "" : "none";
    if (!open && timer) { clearInterval(timer); timer = null; }
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
    if (busy) { return; }
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
  load();
  timer = setInterval(load, POLL_MS);
})();
</script>
</body>
</html>
"""
