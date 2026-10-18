"""Tests for the team dashboard page.

Most checks are static: they read the HTML string the server sends and check its
markup, its inline script, and the API calls it makes. A few tests run a real
server on a free port with a temporary database, sign in with a session cookie,
and check what the page is served with.
"""
import http.client
import json
import re
import threading
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest

from runledger.server.app import make_server
from runledger.server.dashboard import APPROVAL_HTML, DASHBOARD_HTML

NONCE_PLACEHOLDER = "__CSP_NONCE__"
VIEWS = ["runs", "insights", "approvals", "keys", "audit", "settings"]
ADMIN_TABS = ["keys", "audit", "settings"]

# Every endpoint the page calls, from the team API contract.
CONTRACT_ENDPOINTS = [
    "/api/me",
    "/api/runs",
    "/api/stats",
    "/api/insights",
    "/api/keys",
    "/api/audit",
    "/api/budgets",
    "/api/budgets/status",
    "/api/export/runs.csv",
    "/api/export/audit.csv",
    "/api/export/report.html",
    "/api/team/settings",
    "/api/approvals",
]

FORBIDDEN_SINKS = [
    "innerHTML", "outerHTML", "insertAdjacentHTML", "createContextualFragment",
    "document.write", "eval(", "new Function", "srcdoc", ".html(",
]


class _Page(HTMLParser):
    """Collects what the static checks need from the markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []        # (tag, attrs dict) for every start tag
        self.ids = []
        self.label_for = set()
        self._label_depth = 0
        self.controls = []        # (tag, attrs dict, wrapped in a label)
        self.inline_handlers = []
        self.external_refs = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.elements.append((tag, a))
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "label":
            self._label_depth += 1
            if "for" in a:
                self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") != "hidden":
            self.controls.append((tag, a, self._label_depth > 0))
        if tag == "script":
            self.scripts.append(a)
        for name, value in attrs:
            if name.startswith("on"):
                self.inline_handlers.append((tag, name))
            if name in ("src", "href") and value and re.match(r"\s*(https?:)?//|\s*javascript:", value, re.I):
                self.external_refs.append((tag, name, value))

    def handle_endtag(self, tag):
        if tag == "label" and self._label_depth:
            self._label_depth -= 1


def _page(html):
    parser = _Page()
    parser.feed(html)
    parser.close()
    return parser


def _script(html):
    match = re.search(r'<script nonce="__CSP_NONCE__">(.*?)</script>', html, re.S)
    assert match, "the page has one inline script that uses the CSP nonce placeholder"
    return match.group(1)


# Static checks on the served HTML string

def test_page_uses_no_forbidden_dom_sinks():
    script = _script(DASHBOARD_HTML)
    for sink in FORBIDDEN_SINKS:
        assert sink not in DASHBOARD_HTML, sink
    assert "innerHTML" not in APPROVAL_HTML and "outerHTML" not in APPROVAL_HTML
    assert "console." not in script and "debugger" not in script


def test_every_write_goes_through_the_csrf_header_helper():
    js = _script(DASHBOARD_HTML)
    # fetch() is called in two places: request() (every API call) and probeExport() (HEAD only).
    assert js.count("fetch(") == 2
    assert 'if (method !== "GET") {' in js
    assert 'opts.headers["X-Requested-With"] = "runledger";' in js
    probe = js[js.index("function probeExport"):js.index("function setExportLink")]
    assert 'method: "HEAD"' in probe
    # No call site writes a method literal of its own; writes go through the helper.
    assert not re.search(r'method:\s*"(POST|PUT|DELETE|PATCH)"', js)
    writes = re.findall(r"\b(postJSON|putJSON)\(", js)
    assert len(writes) >= 6, writes  # decisions, key create/revoke/rotate, budget PUT, settings PUT


def test_page_calls_every_contract_endpoint():
    js = _script(DASHBOARD_HTML)
    missing = [path for path in CONTRACT_ENDPOINTS if path not in js]
    assert not missing, missing
    assert "/approvals/" in js and "/decision" in js  # approval decisions
    assert "/revoke" in js and "/rotate" in js  # key actions


def test_tabs_are_a_tablist_wired_to_their_panels():
    page = _page(DASHBOARD_HTML)
    by_id = {a.get("id"): (tag, a) for tag, a in page.elements if a.get("id")}
    assert any(a.get("role") == "tablist" for _, a in page.elements)

    tabs = [(tag, a) for tag, a in page.elements if a.get("role") == "tab"]
    panels = [(tag, a) for tag, a in page.elements if a.get("role") == "tabpanel"]
    assert len(tabs) == len(VIEWS) and len(panels) == len(VIEWS)
    for _, tab in tabs:
        panel_id = tab["aria-controls"]
        assert panel_id in by_id, panel_id
        panel_tag, panel = by_id[panel_id]
        assert panel.get("role") == "tabpanel"
        assert panel["aria-labelledby"] == tab["id"]
        assert tab.get("aria-selected") in ("true", "false")

    for name in VIEWS:
        assert f"view-{name}" in by_id and f"tab-{name}" in by_id
    assert f'var VIEWS = ["runs", "insights", "approvals", "keys", "audit", "settings"];' in _script(DASHBOARD_HTML)


def test_admin_tabs_start_hidden_so_members_and_viewers_never_see_them():
    page = _page(DASHBOARD_HTML)
    attrs = {a["id"]: a for _, a in page.elements if a.get("id")}
    for name in ADMIN_TABS:
        assert "hidden" in attrs[f"tab-{name}"], name
        assert "hidden" in attrs[f"view-{name}"], name
    assert "hidden" not in attrs["tab-runs"] and "hidden" not in attrs["view-runs"]
    js = _script(DASHBOARD_HTML)
    assert 'var ADMIN_VIEWS = { keys: true, audit: true, settings: true };' in js
    # Approval decisions are admin-only, and the requesting key cannot decide its own request.
    assert "if (canDecide(a)) {" in js
    assert 'return isAdmin() && !!(a && a.requested_by) && !requestedByThisKey(a);' in js
    assert 'state.keyRef = me && me.key ? (me.key.prefix || me.key.id || null) : null;' in js
    assert 'String(a.requested_by).endsWith("(" + state.keyRef + ")")' in js


def test_dashboard_explains_read_only_and_self_approval_without_offering_buttons():
    js = _script(DASHBOARD_HTML)
    assert "Read only: only an admin can approve or deny requests." in DASHBOARD_HTML
    assert '$("approvals-readonly").hidden = isAdmin();' in js
    assert 'else if (!a.requested_by) {' in js
    assert "Requester identity is unavailable; re-request this approval." in js
    assert 'else if (isAdmin() && requestedByThisKey(a)) {' in js
    assert "Requested with this key; another admin must decide." in js
    assert 'var signature = JSON.stringify([state.role, state.keyRef, items]);' in js


def test_individual_approval_page_starts_without_decision_controls_and_checks_me():
    page = _page(APPROVAL_HTML)
    attrs = {a["id"]: a for _, a in page.elements if a.get("id")}
    assert "hidden" in attrs["decide"]

    js = _script(APPROVAL_HTML)
    assert 'request("GET", "/api/me")' in js
    assert 'return role === "admin" && !!(a && a.requested_by) && !requestedByThisKey(a);' in js
    assert 'keyRef = me && me.key ? (me.key.prefix || me.key.id || null) : null;' in js
    assert 'String(a.requested_by).endsWith("(" + keyRef + ")")' in js
    assert 'if (busy || !canDecide(currentApproval)) { return; }' in js
    assert "Read only: only an admin can approve or deny this request." in js
    assert 'else if (!currentApproval.requested_by) {' in js
    assert "Requester identity is unavailable for this legacy approval. Re-request it before deciding." in js
    assert "This key requested the approval. Use a different admin key to decide it." in js


def test_every_element_id_is_unique_and_every_script_lookup_exists():
    page = _page(DASHBOARD_HTML)
    duplicates = sorted({i for i in page.ids if page.ids.count(i) > 1})
    assert not duplicates, duplicates

    lookups = set(re.findall(r'\$\("([^"]+)"\)', _script(DASHBOARD_HTML)))
    missing = sorted(lookups - set(page.ids))
    assert not missing, missing
    assert len(lookups) > 60  # the script really reads the page, not a stub


def test_every_form_control_has_an_accessible_name():
    page = _page(DASHBOARD_HTML)
    unnamed = []
    for tag, attrs, wrapped in page.controls:
        named = wrapped or attrs.get("id") in page.label_for or "aria-label" in attrs or "aria-labelledby" in attrs
        if not named:
            unnamed.append(attrs.get("id") or tag)
    assert not unnamed, unnamed


def test_inline_script_is_csp_compatible():
    page = _page(DASHBOARD_HTML)
    assert len(page.scripts) == 1
    assert page.scripts[0].get("nonce") == NONCE_PLACEHOLDER
    assert not page.inline_handlers, page.inline_handlers   # no onclick= style attributes
    assert not page.external_refs, page.external_refs        # no CDN or javascript: URLs
    assert "<style>" in DASHBOARD_HTML and DASHBOARD_HTML.count("<style>") == 1


def test_layout_and_motion_rules_for_phones_and_reduced_motion():
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in DASHBOARD_HTML
    assert ".table-wrap{overflow-x:auto" in DASHBOARD_HTML     # tables scroll inside their box
    assert ".tabs-wrap{overflow-x:auto" in DASHBOARD_HTML      # so do the tabs
    assert "@media (prefers-reduced-motion: reduce)" in DASHBOARD_HTML
    assert "@media (prefers-color-scheme: light)" in DASHBOARD_HTML
    assert ":focus-visible{" in DASHBOARD_HTML


def test_budget_and_exports_start_hidden_until_the_server_confirms_them():
    page = _page(DASHBOARD_HTML)
    attrs = {a["id"]: a for _, a in page.elements if a.get("id")}
    assert "hidden" in attrs["budget-card"]
    assert "hidden" in attrs["budget-form"]
    for link in ("exp-runs", "exp-audit", "exp-report", "audit-export"):
        assert "hidden" in attrs[link], link
    assert attrs["exp-report"].get("target") == "_blank" and "noopener" in attrs["exp-report"].get("rel", "")


def test_approvals_section_keeps_its_contract_with_the_existing_tests():
    assert "Pending approvals" in DASHBOARD_HTML
    assert DASHBOARD_HTML.index("Pending approvals") < DASHBOARD_HTML.index('id="notice"')
    assert "var APPROVAL_POLL_MS = 3000;" in _script(DASHBOARD_HTML)
    assert "X-Requested-With" in DASHBOARD_HTML
    assert "Approve" in DASHBOARD_HTML and "Deny" in DASHBOARD_HTML


def test_header_comment_describes_the_real_hiding_rules():
    """The old comment claimed panels hide on 404/403 because endpoints may not exist."""
    import runledger.server.dashboard as dashboard

    header = dashboard.__doc__
    assert header
    assert "may not exist" not in header
    assert "may not exist" not in dashboard.DASHBOARD_HTML
    assert "may not exist" not in _script(DASHBOARD_HTML)
    assert "403" in header and "admin" in header
    assert "filters live in that hash" in header


def test_filter_bar_offers_max_quality_and_ai_verdict():
    page = _page(DASHBOARD_HTML)
    attrs = {a["id"]: a for _, a in page.elements if a.get("id")}
    assert attrs["f-max-quality"].get("type") == "number"
    assert "aria-label" in attrs["f-max-quality"]
    assert any(tag == "select" and a.get("id") == "f-verdict" for tag, a in page.elements)

    select = re.search(r'<select id="f-verdict".*?</select>', DASHBOARD_HTML, re.S).group(0)
    for value in ("looks_safe", "needs_review", "dangerous"):
        assert f'value="{value}"' in select, value
    assert '<option value="" selected>' in select

    js = _script(DASHBOARD_HTML)
    assert 'params.set("max_quality", boundedNumber(maxQuality));' in js
    assert 'params.set("ai_verdict", verdict);' in js
    assert 'params.set("min_quality", boundedNumber(minQuality));' in js


def test_filters_are_written_to_the_url_hash_and_restored_from_it():
    js = _script(DASHBOARD_HTML)
    assert '"f-max-quality": "max_quality",' in js
    assert '"f-verdict": "ai_verdict"' in js
    assert "function applyFilterHash()" in js and "function writeFilterHash()" in js
    assert "applyFilterHash();" in js                     # on load, before the first query
    assert "writeFilterHash();" in js                     # every change lands in the hash
    assert "if (location.hash !== hash) { location.hash = hash; }" in js
    assert "window.addEventListener(\"hashchange\"" in js
    assert "filterValues()" in js                          # a hash the user edits is applied too


def test_polling_pauses_while_hidden_and_backs_off_after_a_failure():
    js = _script(DASHBOARD_HTML)
    assert "var APPROVAL_POLL_MS = 3000;" in js
    assert "var APPROVAL_POLL_MAX_MS = 30000;" in js
    assert "if (document.hidden) { later(); return; }  // hidden: stay scheduled, send nothing" in js
    assert 'document.addEventListener("visibilitychange", function () {' in js
    assert "approvalPoller.start();" in js
    assert "delay = ok ? baseMs : Math.min(maxMs, delay * 2);" in js
    assert "setInterval(loadApprovals" not in js
    # A resume cancels the timer that was already scheduled, so requests cannot multiply.
    assert "function clearTimer() {" in js
    assert "resume: function () { stopped = false; delay = baseMs; clearTimer(); run(); }" in js

    approval = _script(APPROVAL_HTML)
    assert "var POLL_MS = 3000;" in approval
    assert "var POLL_MAX_MS = 30000;" in approval
    assert "if (document.hidden) { later(); return; }  // hidden: stay scheduled, send nothing" in approval
    assert "resume: function () { stopped = false; delay = baseMs; clearTimer(); run(); }" in approval
    assert 'document.addEventListener("visibilitychange", function () {' in approval
    assert "poller.start();" in approval
    assert "setInterval(" not in approval
    assert "if (!open) { poller.stop(); }" in approval   # a decided approval stops polling


def test_cost_cells_render_unknown_cost_as_not_available_and_partial_as_a_lower_bound():
    js = _script(DASHBOARD_HTML)
    # n/a for an unknown cost, "≥" for a figure that leaves unpriced tokens out.
    assert 'return (lowerBound && text !== "n/a") ? "≥" + text : text;' in js
    assert 'return run.usage_source === "missing" ? "token usage was not recorded by the agent" : "";' in js
    assert 'costNode(r.cost_usd, r.cost_partial, costTitle(r))' in js
    assert "function groupLowerBound(row)" in js
    assert 'costText(r.cost_usd, groupLowerBound(r))' in js
    assert 'var GRADE_LIST = ["A", "B", "C", "D", "F"];' in js   # no E bucket, no E badge
    # The hint names the cause instead of blaming the model price for everything.
    assert "without recorded token usage" in js
    assert "with an unknown model price" in js
    assert "left out: model price unknown" not in js
    assert "var hint = unpricedHint(t);" in js


def test_budget_card_and_developer_rows_render_unknown_and_partial_spend_honestly():
    js = _script(DASHBOARD_HTML)
    # A group is a lower bound when one of its runs is partly priced, not only when one has
    # no price at all.
    assert "if (Number(row.partial_runs) > 0) { return true; }" in js
    # Budget figures stay numeric for the threshold arithmetic, so the display turns an
    # all-unknown month back into "n/a" and marks a partly priced one "≥".
    assert "function spendText(row)" in js
    assert 'if ((Number(row.runs) || 0) > 0 && !(Number(row.priced_runs) || 0)) { return "n/a"; }' in js
    assert "return costText(row.spend_usd, groupLowerBound(row));" in js
    assert ('$("budget-figure").textContent = hasLimit ? spendText(status) + " of " + money(limit)'
            ' : spendText(status) + " spent";') in js
    assert '"% of the team budget used" + coverageNote(status)' in js
    assert "function coverageNote(row)" in js
    assert 'return spendText(r) + (up === null ? "" : " · " + up + "%");' in js
    # The numbers behind the bar and the percentages are unchanged: the bar still gets the
    # numeric spend and the threshold arithmetic still runs on it.
    assert "var spend = Number(status.spend_usd) || 0;" in js
    assert "function (r) { return r.spend_usd; }," in js
    assert "p = Math.round((100 * spend) / limit);" in js


def test_run_detail_dialog_shows_who_pushed_the_run():
    page = _page(DASHBOARD_HTML)
    ids = {a["id"] for _, a in page.elements if a.get("id")}
    for name in ("run-detail", "run-detail-body", "run-detail-pushed", "run-detail-close", "run-detail-receipt"):
        assert name in ids, name

    js = _script(DASHBOARD_HTML)
    assert 'return "Pushed by " + nameOf(run.pushed_by, "an unknown key");' in js
    assert '["Pushed by", nameOf(run.pushed_by, "an unknown key")],' in js
    assert 'detail.addEventListener("click", function () { openRunDetail(r); });' in js
    assert "openRunDetail(r);" in js
    assert 'receipt.href = "/runs/" + encodeURIComponent(run.id);' in js


def test_new_key_is_shown_once_with_a_warning_and_copy_button():
    page = _page(DASHBOARD_HTML)
    assert "won't see it again" in DASHBOARD_HTML
    attrs = {a["id"]: a for _, a in page.elements if a.get("id")}
    assert attrs["key-once"].get("tabindex") == "-1"
    js = _script(DASHBOARD_HTML)
    assert '$("key-value").textContent = key;' in js       # plaintext goes in as text only
    assert js.count("clearKeyOnce();") >= 2                 # when the user dismisses it and when they leave the tab


# Checks against a real server

SESSION = "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42"


@contextmanager
def running_server(db_path):
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
def env(tmp_path):
    with running_server(tmp_path / "ui.db") as pair:
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


def _session_cookie(base, key):
    status, headers, _ = _http(base, "GET", f"/?key={key}")
    assert status == 302
    return headers["set-cookie"].split(";")[0]


def test_signed_out_visitors_get_the_sign_in_page_not_the_dashboard(env):
    srv, base = env
    srv.db.create_team("alpha")
    status, headers, body = _http(base, "GET", "/")
    assert status == 401
    assert b'id="tabs"' not in body and b"<script" not in body
    assert headers["content-type"].startswith("text/html")


def test_dashboard_is_served_with_a_nonce_csp_and_is_not_cached(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    cookie = _session_cookie(base, key)

    status, headers, body = _http(base, "GET", "/", headers={"Cookie": cookie})
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert headers["cache-control"] == "no-store"
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    nonce = csp.split("'nonce-")[1].split("'")[0]

    page = body.decode("utf-8")
    assert NONCE_PLACEHOLDER not in page
    assert f'<script nonce="{nonce}">' in page
    assert page == DASHBOARD_HTML.replace(NONCE_PLACEHOLDER, nonce)


def test_endpoints_the_page_calls_answer_with_json_even_when_not_built(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    cookie = _session_cookie(base, key)
    headers = {"Cookie": cookie}

    # Missing endpoints must come back as JSON errors the page can read, never HTML.
    for path in ["/api/me", "/api/keys", "/api/audit?limit=100", "/api/budgets",
                 "/api/budgets/status", "/api/team/settings"]:
        status, resp_headers, raw = _http(base, "GET", path, headers=headers)
        # 401 is what /api/team/settings returns to a cookie session (the key-only endpoint).
        assert status in (200, 401, 403, 404), (path, status)
        if status != 200:
            assert resp_headers["content-type"].startswith("application/json"), path
            assert "error" in json.loads(raw.decode("utf-8")), path
        else:
            assert resp_headers["content-type"].startswith("application/json"), path


def test_existing_stats_and_runs_still_load_for_the_signed_in_page(env):
    srv, base = env
    _, key = srv.db.create_team("alpha")
    cookie = _session_cookie(base, key)
    status, _, raw = _http(base, "GET", "/api/stats?days=30", headers={"Cookie": cookie})
    assert status == 200
    stats = json.loads(raw.decode("utf-8"))
    assert stats["team"] == "alpha" and "by_user" in stats
    status, _, raw = _http(base, "GET", "/api/runs?agent=Codex%20CLI&limit=200", headers={"Cookie": cookie})
    assert status == 200 and json.loads(raw.decode("utf-8"))["runs"] == []
