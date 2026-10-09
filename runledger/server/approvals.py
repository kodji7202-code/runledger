"""Human approvals for risky agent actions: input checks, settings checks and webhooks.

A guard client creates an approval (POST /api/approvals) and polls it until a
person decides it in the dashboard or through the API. A team can point the
server at a Slack incoming webhook and/or a generic JSON webhook. Those calls run
on a background thread, so they never delay the request that created the
approval, and a failed delivery is only written to the server's stderr. Budget
alerts (see budgets.py) use the same two webhooks and the same background sender.

Webhook URLs are secrets (a Slack URL lets anyone post to the channel), so they
are never written to the log.
"""
from __future__ import annotations

import json
import http.client
import ipaddress
import math
import os
import secrets
import socket
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

APPROVAL_STATUSES = ("pending", "approved", "denied", "expired")
SEVERITIES = ("low", "medium", "high")

MAX_TOOL = 100
MAX_SESSION_ID = 200
MAX_SUMMARY = 2000
MAX_RISKS = 50
MAX_RISK_CODE = 64
MAX_RISK_REASON = 500
MAX_CWD = 500
MAX_REASON = 500
MAX_NAME = 100
MAX_URL = 2000

MIN_TTL_S = 1
DEFAULT_TTL_S = 600
MAX_TTL_S = 7 * 24 * 3600

CREATE_BODY_LIMIT = 128 * 1024  # an approval request is at most a few KB; this is generous
SMALL_BODY_LIMIT = 16 * 1024    # decisions and settings

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
WEBHOOK_TIMEOUT_S = 5
LOOPBACK_OPT_IN = "RUNLEDGER_ALLOW_LOOPBACK_WEBHOOKS"


class InvalidInput(ValueError):
    """A request field failed validation. The message is safe to send to the client."""


def new_approval_id() -> str:
    """A random, URL-safe id (128 bits), so approval links cannot be guessed."""
    return secrets.token_urlsafe(16)


def _has_control_chars(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _text(source: Dict[str, Any], field: str, limit: int, required: bool = True) -> Optional[str]:
    value = source.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise InvalidInput(f"'{field}' must be a string.")
    if required and not value.strip():
        raise InvalidInput(f"'{field}' is required.")
    if len(value) > limit:
        raise InvalidInput(f"'{field}' must be at most {limit} characters.")
    return value


def _object(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidInput("The body must be a JSON object.")
    return payload


def _risk(index: int, item: Any) -> Dict[str, str]:
    if not isinstance(item, dict):
        raise InvalidInput(f"risks[{index}] must be an object.")
    severity = item.get("severity")
    if not isinstance(severity, str) or severity.lower() not in SEVERITIES:
        raise InvalidInput(f"risks[{index}].severity must be one of low, medium, high.")
    code = _text(item, "code", MAX_RISK_CODE)
    if _has_control_chars(code):
        raise InvalidInput(f"risks[{index}].code must not contain control characters.")
    reason = _text(item, "reason", MAX_RISK_REASON, required=False) or ""
    return {"severity": severity.lower(), "code": code, "reason": reason}


def validate_new_approval(payload: Any) -> Dict[str, Any]:
    """Check an approval request and return the fields to store."""
    body = _object(payload)
    session_id = _text(body, "session_id", MAX_SESSION_ID)
    if _has_control_chars(session_id):
        raise InvalidInput("'session_id' must not contain control characters.")
    tool = _text(body, "tool", MAX_TOOL)
    summary = _text(body, "summary", MAX_SUMMARY)

    raw_risks = body.get("risks")
    if raw_risks is None:
        raw_risks = []
    if not isinstance(raw_risks, list):
        raise InvalidInput("'risks' must be a list.")
    if len(raw_risks) > MAX_RISKS:
        raise InvalidInput(f"'risks' can hold at most {MAX_RISKS} items.")
    risks = [_risk(i, item) for i, item in enumerate(raw_risks)]

    cwd = _text(body, "cwd", MAX_CWD, required=False) or None
    return {"session_id": session_id, "tool": tool, "summary": summary, "risks": risks, "cwd": cwd}


def validate_decision(payload: Any) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (decision, reason, name). decision is "approve" or "deny"."""
    body = _object(payload)
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        raise InvalidInput("'decision' must be \"approve\" or \"deny\".")
    reason = _text(body, "reason", MAX_REASON, required=False)
    name = _text(body, "name", MAX_NAME, required=False)
    return decision, (reason or "").strip() or None, (name or "").strip() or None


def check_ttl(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_TTL_S <= value <= MAX_TTL_S:
        raise InvalidInput(
            f"'approval_ttl_s' must be a whole number of seconds from {MIN_TTL_S} to {MAX_TTL_S}."
        )
    return value


def check_webhook_url(field: str, value: Any) -> Optional[str]:
    """Require public HTTPS endpoints; local HTTP needs an explicit development opt-in."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInput(f"'{field}' must be a URL string or null.")
    url = value.strip()
    if not url:
        return None
    if len(url) > MAX_URL or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise InvalidInput(f"'{field}' must be one URL of at most {MAX_URL} characters.")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise InvalidInput(f"'{field}' is not a valid URL.") from None
    if not host:
        raise InvalidInput(f"'{field}' must include a host name.")
    if parts.username is not None or parts.password is not None or parts.fragment or "%" in host:
        raise InvalidInput(f"'{field}' must not contain credentials, fragments or encoded hostnames.")
    if port == 0:
        raise InvalidInput(f"'{field}' must use a valid port.")
    scheme = parts.scheme.lower()
    local = host in LOOPBACK_HOSTS and os.environ.get(LOOPBACK_OPT_IN) == "1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not _public_address(address) and not (local and scheme == "http" and address.is_loopback):
        raise InvalidInput(f"'{field}' cannot target a private or local address.")
    if scheme == "https" and host not in LOOPBACK_HOSTS:
        return url
    if scheme == "http" and local:
        return url
    raise InvalidInput(
        f"'{field}' must use public https:// (local HTTP requires {LOOPBACK_OPT_IN}=1)."
    )


_SETTING_NAMES = ("slack_webhook_url", "webhook_url", "approval_ttl_s")


def validate_settings(payload: Any) -> Dict[str, Any]:
    """Check a PUT /api/team/settings body. Only the keys sent are changed."""
    body = _object(payload)
    changes: Dict[str, Any] = {}
    for key, value in body.items():
        if key in ("slack_webhook_url", "webhook_url"):
            changes[key] = check_webhook_url(key, value)
        elif key == "approval_ttl_s":
            changes[key] = check_ttl(value)
        else:
            shown = str(key)[:50]
            raise InvalidInput(
                f"Unknown setting '{shown}'. Use one of: {', '.join(_SETTING_NAMES)}."
            )
    if not changes:
        raise InvalidInput(f"Send at least one setting: {', '.join(_SETTING_NAMES)}.")
    return changes


# Webhook delivery

def _public_address(ip: ipaddress._BaseAddress) -> bool:
    """Require routable unicast, not just ipaddress.is_global (which includes multicast).

    For IPv6 allow only 2000::/3 global unicast, excluding special/reserved
    addresses and 6to4 tunnels that could embed private IPv4 destinations.
    """
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return (ip in ipaddress.IPv6Network("2000::/3")
                and ip not in ipaddress.IPv6Network("2002::/16"))
    return True


def _resolved_ips(host: str, port: int, allow_local: bool) -> List[str]:
    """Reject non-public DNS answers and pin the transport to validated IPs.

    Resolving only before a conventional URL opener leaves a DNS rebinding window:
    the opener would resolve a potentially different destination on connection.
    """
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("Webhook hostname did not resolve")
    candidates = []
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not (_public_address(ip) or (allow_local and ip.is_loopback)):
            raise ValueError("Webhook destination is not a public IP")
        if str(ip) not in candidates:
            candidates.append(str(ip))
    return candidates


def _post_json(url: str, payload: Any) -> None:
    # Revalidate older stored settings at the moment the network call is made.
    check_webhook_url("webhook_url", url)
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    allow_local = parts.scheme.lower() == "http" and host in LOOPBACK_HOSTS and os.environ.get(LOOPBACK_OPT_IN) == "1"
    pinned_ips = _resolved_ips(host, port, allow_local)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection_type = http.client.HTTPSConnection if parts.scheme.lower() == "https" else http.client.HTTPConnection
    conn = connection_type(host, port, timeout=WEBHOOK_TIMEOUT_S)
    # HTTPS still uses the original hostname for SNI and certificate verification;
    # only its TCP socket connects to the pinned, checked destination.
    def connect_pinned(addr, timeout, source_address=None):
        last_error = None
        for pinned_ip in pinned_ips:
            try:
                return socket.create_connection(
                    (pinned_ip, port), timeout=timeout, source_address=source_address
                )
            except OSError as exc:
                last_error = exc
        raise last_error if last_error is not None else OSError("No validated webhook destination")

    conn._create_connection = connect_pinned
    path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    try:
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/json; charset=utf-8", "User-Agent": "RunLedger",
        })
        response = conn.getresponse()
        response.read(1024)
        if not 200 <= response.status < 300:
            raise ValueError(f"Webhook returned HTTP {response.status}")
    finally:
        conn.close()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, ValueError) and str(exc).startswith("Webhook returned HTTP "):
        return str(exc).replace("Webhook returned ", "", 1)
    return type(exc).__name__


def _deliver(jobs: List[Tuple[str, str, Any]]) -> None:
    for label, url, payload in jobs:
        try:
            _post_json(url, payload)
        except Exception as exc:  # a webhook must never break the server; log the kind of failure only
            sys.stderr.write(f"RunLedger: {label} notification failed ({_describe(exc)})\n")


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_text(approval: Dict[str, Any], link: str) -> str:
    lines = [f"RunLedger approval needed: {approval['tool']}", approval["summary"]]
    if approval["risks"]:
        lines.append("Risks:")
        lines.extend(f"- [{r['severity']}] {r['code']}: {r['reason']}" for r in approval["risks"])
    where = f"Session {approval['session_id']}"
    if approval.get("cwd"):
        where += f" in {approval['cwd']}"
    lines.append(where)
    lines.append(f"Review: {link}")
    return _slack_escape("\n".join(lines))


def _usd(amount: float) -> str:
    return f"${amount:,.2f}" if amount >= 0.01 or amount == 0 else f"${amount:.4f}"


def budget_slack_text(alert: Dict[str, Any]) -> str:
    """One line for Slack, e.g. "RunLedger budget: team spend $0.11 is 216% of $0.05 for 2026-10"."""
    who = "team spend" if alert.get("user") is None else f"developer {alert['user']} spend"
    text = (
        f"RunLedger budget: {who} {_usd(alert['spend_usd'])} is {math.floor(alert['pct'])}% "
        f"of {_usd(alert['limit_usd'])} for {alert['month']}"
    )
    return _slack_escape(text)


def budget_webhook_payload(team_name: str, alert: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "budget_alert",
        "team": team_name,
        "month": alert["month"],
        "scope": alert["scope"],
        "user": alert.get("user"),
        "threshold": alert["threshold"],
        "spend_usd": alert["spend_usd"],
        "limit_usd": alert["limit_usd"],
        "pct": alert["pct"],
    }


def notify_budget_alerts(settings: Dict[str, Any], team_name: str, alerts: List[Dict[str, Any]]) -> None:
    """Queue Slack and generic-webhook messages for newly fired budget alerts. Returns at once."""
    jobs: List[Tuple[str, str, Any]] = []
    slack = settings.get("slack_webhook_url")
    hook = settings.get("webhook_url")
    for alert in alerts:
        if slack:
            jobs.append(("Slack", slack, {"text": budget_slack_text(alert)}))
        if hook:
            jobs.append(("webhook", hook, budget_webhook_payload(team_name, alert)))
    if jobs:
        threading.Thread(target=_deliver, args=(jobs,), name="runledger-notify", daemon=True).start()


def notify_new_approval(settings: Dict[str, Any], approval: Dict[str, Any], public_url: str) -> None:
    """Queue the webhook messages for a new approval. Returns at once; delivery runs on a daemon thread."""
    link = f"{public_url}/approvals/{approval['id']}"
    jobs: List[Tuple[str, str, Any]] = []
    slack = settings.get("slack_webhook_url")
    if slack:
        jobs.append(("Slack", slack, {"text": slack_text(approval, link)}))
    hook = settings.get("webhook_url")
    if hook:
        jobs.append(("webhook", hook, dict(approval, url=link)))
    if jobs:
        threading.Thread(target=_deliver, args=(jobs,), name="runledger-notify", daemon=True).start()
