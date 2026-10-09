"""Paid hosted Team plan: Polar webhooks, subscription state, seats, email and data lifecycle.

Billing is off unless RUNLEDGER_POLAR_WEBHOOK_SECRET is set, so a self-hosted server behaves
exactly as before. With it on:

  POST /billing/polar/webhook   Polar (Standard Webhooks) events. A paid subscription creates a
                                team and an admin key, and the key is emailed to the customer.
  POST /billing/recover         {"email"}: a new admin key for that customer's team, by email.
  GET  /recover                 a small page for the above.

A team with a subscription row is a billed team. Its access follows the subscription:

  active, trialing   full access
  past_due           full access for GRACE_DAYS, then read only
  canceled, unpaid   read only (dashboard, API reads and exports work); the team and its data
                     are deleted DELETE_AFTER_DAYS after the subscription ended
  incomplete         no team yet (the first payment has not gone through)

A seat is associated with the authenticated API key that pushed a run in the last
SEAT_WINDOW_DAYS, not the caller-supplied run "user" label. A push with an additional key
when every seat is taken is refused with 402. Teams created with
`runledger team create` have no subscription row and none of these limits.

Email goes through the Resend HTTP API. Standard library only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PRECISE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

GRACE_DAYS = 7
DELETE_AFTER_DAYS = 30
SEAT_WINDOW_DAYS = 30
SIGNATURE_TOLERANCE_S = 300
WEBHOOK_BODY_LIMIT = 1024 * 1024
RECOVERY_INTERVAL_S = 600
MAIL_TIMEOUT_S = 8

ACTIVE = ("active", "trialing")
PAST_DUE = ("past_due",)
ENDED = ("canceled", "unpaid", "incomplete_expired", "revoked")
KNOWN_STATUSES = ACTIVE + PAST_DUE + ENDED + ("incomplete",)

SECRET_ENV = "RUNLEDGER_POLAR_WEBHOOK_SECRET"
PRODUCTS_ENV = "RUNLEDGER_POLAR_PRODUCT_IDS"
RESEND_KEY_ENV = "RUNLEDGER_RESEND_API_KEY"
MAIL_FROM_ENV = "RUNLEDGER_MAIL_FROM"
SUPPORT_ENV = "RUNLEDGER_SUPPORT_EMAIL"
PORTAL_ENV = "RUNLEDGER_BILLING_PORTAL_URL"
RETENTION_ENV = "RUNLEDGER_RETENTION_DAYS"

RESEND_URL = "https://api.resend.com/emails"
_EMAIL = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})?$"
)


def now() -> datetime:
    """The billing clock. Tests patch it."""
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_stamp(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    for fmt in (TIME_FORMAT, PRECISE_FORMAT):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class BillingConfigError(ValueError):
    """Billing is half configured. The message is safe to print."""


class InvalidSignature(ValueError):
    """The webhook did not come from Polar, or it is too old."""


class InvalidEvent(ValueError):
    """The webhook payload is not a subscription we can read."""


class MailError(RuntimeError):
    """An email could not be sent. The message never contains the email body or the API key."""


@dataclass(frozen=True)
class BillingConfig:
    webhook_secret: str
    resend_api_key: str
    mail_from: str
    support_email: Optional[str] = None
    portal_url: Optional[str] = None
    product_ids: Tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> Optional["BillingConfig"]:
        """The billing settings from the environment, or None when billing is off.
        Raises BillingConfigError when the webhook secret is set but email is not."""
        env = os.environ if environ is None else environ
        secret = (env.get(SECRET_ENV) or "").strip()
        if not secret:
            return None
        resend = (env.get(RESEND_KEY_ENV) or "").strip()
        sender = (env.get(MAIL_FROM_ENV) or "").strip()
        if not resend or not sender:
            raise BillingConfigError(
                f"{SECRET_ENV} is set, so billing is on, but {RESEND_KEY_ENV} and {MAIL_FROM_ENV} "
                "are also required: new customers get their API key by email."
            )
        products = tuple(p.strip() for p in (env.get(PRODUCTS_ENV) or "").split(",") if p.strip())
        return cls(
            webhook_secret=secret,
            resend_api_key=resend,
            mail_from=sender,
            support_email=(env.get(SUPPORT_ENV) or "").strip() or None,
            portal_url=(env.get(PORTAL_ENV) or "").strip().rstrip("/") or None,
            product_ids=products,
        )


def retention_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[int]:
    """RUNLEDGER_RETENTION_DAYS as a whole number of days (1 or more), or None when unset."""
    env = os.environ if environ is None else environ
    raw = (env.get(RETENTION_ENV) or "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        raise BillingConfigError(f"{RETENTION_ENV} must be a whole number of days, not {raw!r}.") from None
    if days < 1:
        raise BillingConfigError(f"{RETENTION_ENV} must be 1 or more.")
    return days


# Webhook signatures (https://www.standardwebhooks.com)


def _signing_keys(secret: str) -> List[bytes]:
    # Polar signs with the UTF-8 bytes of the secret exactly as shown in its dashboard (its SDK
    # base64-encodes the secret before handing it to the Standard Webhooks library, which decodes
    # it again). A generic Standard Webhooks secret "whsec_<base64>" is keyed by the decoded bytes.
    keys = [secret.encode("utf-8")]
    if secret.startswith("whsec_"):
        try:
            keys.append(base64.b64decode(secret[len("whsec_"):], validate=True))
        except ValueError:
            pass
    return keys


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """The webhook-signature header value for a payload: what Polar sends. Used by tests and tooling."""
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify_webhook(secret: str, headers: Mapping[str, Optional[str]], body: bytes, now_ts: float,
                   tolerance_s: int = SIGNATURE_TOLERANCE_S) -> str:
    """Check the Standard Webhooks headers. Returns the webhook id. Raises InvalidSignature."""
    msg_id = (headers.get("webhook-id") or "").strip()
    raw_ts = (headers.get("webhook-timestamp") or "").strip()
    signatures = (headers.get("webhook-signature") or "").strip()
    if not msg_id or not raw_ts or not signatures:
        raise InvalidSignature("missing webhook-id, webhook-timestamp or webhook-signature")
    if len(msg_id) > 200:
        raise InvalidSignature("webhook-id is too long")
    try:
        timestamp = int(raw_ts)
    except ValueError:
        raise InvalidSignature("webhook-timestamp is not a number") from None
    if abs(now_ts - timestamp) > tolerance_s:
        raise InvalidSignature("webhook-timestamp is too old or in the future")
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body
    expected = [
        base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
        for key in _signing_keys(secret)
    ]
    for part in signatures.split():
        version, _, value = part.partition(",")
        if version == "v1" and any(hmac.compare_digest(value, e) for e in expected):
            return msg_id
    raise InvalidSignature("no matching signature")


# Subscription payloads


def _iso_to_utc(value: Any, precise: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = _ISO.match(value.strip())
    if not match:
        return None
    day, clock, fraction, zone = match.groups()
    moment = datetime.strptime(f"{day}T{clock}", "%Y-%m-%dT%H:%M:%S")
    micros = int((fraction or ".0")[1:7].ljust(6, "0"))
    moment = moment.replace(microsecond=micros, tzinfo=timezone.utc)
    if zone and zone != "Z":
        sign_ = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        moment -= sign_ * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return moment.strftime(PRECISE_FORMAT if precise else TIME_FORMAT)


def _clean_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:60].strip() or None


def valid_email(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text.lower() if len(text) <= 254 and _EMAIL.match(text) else None


def parse_subscription(data: Any) -> Dict[str, Any]:
    """The fields of a Polar subscription object that billing uses. Raises InvalidEvent."""
    if not isinstance(data, dict):
        raise InvalidEvent("data is not an object")
    sub_id = data.get("id")
    if not isinstance(sub_id, str) or not sub_id.strip() or len(sub_id) > 100:
        raise InvalidEvent("subscription id is missing")
    status = data.get("status")
    if status not in KNOWN_STATUSES:
        raise InvalidEvent(f"unknown subscription status {status!r}")
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    email = valid_email(customer.get("email"))
    if email is None and isinstance(data.get("user"), dict):
        email = valid_email(data["user"].get("email"))
    if email is None:
        raise InvalidEvent("the subscription has no customer email")
    seats = data.get("seats")
    if isinstance(seats, bool) or not isinstance(seats, int) or seats < 1:
        seats = 1
    fields = data.get("custom_field_data") if isinstance(data.get("custom_field_data"), dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    team_name = (
        _clean_name(fields.get("team_name"))
        or _clean_name(metadata.get("team_name"))
        or _clean_name(customer.get("name"))
        or _clean_name(email.split("@")[0])
        or "team"
    )
    product_id = data.get("product_id")
    if not isinstance(product_id, str) and isinstance(data.get("product"), dict):
        product_id = data["product"].get("id")
    customer_id = data.get("customer_id") or customer.get("id")
    return {
        "id": sub_id.strip(),
        "status": status,
        "email": email,
        "customer_id": customer_id if isinstance(customer_id, str) else None,
        "product_id": product_id if isinstance(product_id, str) else None,
        "seats": min(int(seats), 10000),
        "current_period_end": _iso_to_utc(data.get("current_period_end")),
        "cancel_at_period_end": bool(data.get("cancel_at_period_end")),
        "ended_at": _iso_to_utc(data.get("ended_at")),
        "modified_at": _iso_to_utc(data.get("modified_at") or data.get("created_at"), precise=True),
        "team_name": team_name,
    }


# Access


def access(row: Mapping[str, Any], moment: Optional[datetime] = None) -> Dict[str, Any]:
    """What a billed team may do now, and why. `state` is one of active, canceling, past_due,
    read_only, ended or pending; `can_write` says whether pushes and approval requests work."""
    moment = moment or now()
    status = row["status"]
    info: Dict[str, Any] = {
        "status": status,
        "seats": int(row["seats"]),
        "current_period_end": row["current_period_end"],
        "cancel_at_period_end": bool(row["cancel_at_period_end"]),
        "grace_until": None,
        "deletes_at": None,
    }
    if status in ACTIVE:
        info.update(state="canceling" if row["cancel_at_period_end"] else "active", can_write=True)
    elif status in PAST_DUE:
        since = parse_stamp(row["past_due_since"]) or moment
        grace = since + timedelta(days=GRACE_DAYS)
        info["grace_until"] = stamp(grace)
        info.update(state="past_due" if moment < grace else "read_only", can_write=moment < grace)
    elif status in ENDED:
        ended = parse_stamp(row["ended_at"]) or moment
        info["deletes_at"] = stamp(ended + timedelta(days=DELETE_AFTER_DAYS))
        info.update(state="ended", can_write=False)
    else:
        info.update(state="pending", can_write=False)
    return info


def write_refusal(info: Mapping[str, Any]) -> Optional[str]:
    """The message for a refused push or approval request, or None when writes are allowed."""
    if info["can_write"]:
        return None
    if info["state"] == "read_only":
        return ("Payment for this team is overdue, so the team is read only. Update the payment method "
                "in the billing portal to resume pushes.")
    if info["state"] == "ended":
        return (f"This team's subscription has ended, so the team is read only. Export your data before "
                f"{info['deletes_at']}, when it is deleted.")
    return "This team's subscription is not active yet."


def seat_refusal(seats: int) -> str:
    return (f"All {seats} seat{'s' if seats != 1 else ''} on this team are in use "
            f"(a seat is an authenticated API key that pushed a run in the last {SEAT_WINDOW_DAYS} days). "
            "Add seats in the billing portal, or push using a key that already occupies a seat.")


# Email


class ResendMailer:
    """Sends plain-text email through the Resend API."""

    def __init__(self, api_key: str, sender: str, reply_to: Optional[str] = None) -> None:
        self._api_key = api_key
        self.sender = sender
        self.reply_to = reply_to

    def send(self, to: str, subject: str, text: str, idempotency_key: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {"from": self.sender, "to": [to], "subject": subject, "text": text}
        if self.reply_to:
            payload["reply_to"] = self.reply_to
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "RunLedger",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key[:256]
        req = urllib.request.Request(RESEND_URL, data=json.dumps(payload).encode("utf-8"),
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=MAIL_TIMEOUT_S) as resp:
                resp.read(4096)
        except urllib.error.HTTPError as exc:
            raise MailError(f"Resend answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise MailError(f"cannot reach Resend ({type(exc).__name__})") from None


def mailer_for(config: BillingConfig) -> ResendMailer:
    return ResendMailer(config.resend_api_key, config.mail_from, config.support_email)


def _footer(config: BillingConfig) -> str:
    lines = ["", "--", "RunLedger"]
    if config.portal_url:
        lines.append(f"Billing, invoices and seats: {config.portal_url}")
    if config.support_email:
        lines.append(f"Help: {config.support_email} (or reply to this email)")
    return "\n".join(lines)


def welcome_email(config: BillingConfig, public_url: str, team_name: str, seats: int, key: str) -> Tuple[str, str]:
    text = f"""Thanks for subscribing to RunLedger Team. Your team "{team_name}" is ready, with {seats} seat{'s' if seats != 1 else ''}.

Your admin API key (keep it secret, it is shown only here):

    {key}

1. Open the dashboard (sign in once, then the key leaves the address bar):
   {public_url}/?key={key}

2. Give each developer their own key: in the dashboard, open "API keys" and create a member key.
   Do not share the admin key.

3. Each developer installs the CLI and pushes runs:
   pip install runledger-ai
   export RUNLEDGER_SERVER={public_url}
   export RUNLEDGER_API_KEY=<their member key>
   runledger push --latest

A seat is counted using the authenticated API key that pushed a run within the last
{SEAT_WINDOW_DAYS} days, not the editable developer name. Give each developer their own key.
Run history is kept for 90 days.
Lost this key? Get a new one at {public_url}/recover
{_footer(config)}
"""
    return "Your RunLedger Team server is ready", text


def past_due_email(config: BillingConfig, team_name: str, grace_until: str) -> Tuple[str, str]:
    text = f"""The latest payment for RunLedger Team ("{team_name}") did not go through.

Everything keeps working until {grace_until}. After that the team becomes read only: the dashboard
and exports still work, but new runs and approval requests are refused until the payment succeeds.

Update the payment method in the billing portal to fix it.
{_footer(config)}
"""
    return "RunLedger Team: payment failed", text


def ended_email(config: BillingConfig, public_url: str, team_name: str, deletes_at: str) -> Tuple[str, str]:
    text = f"""The RunLedger Team subscription for "{team_name}" has ended.

The team is now read only. You can still sign in at {public_url} and export runs, the audit log and
the compliance report (Audit log tab) until {deletes_at}. On that date the team and all its data are
deleted from our server.

To keep using RunLedger Team, subscribe again from https://runledger.site/#pricing.
{_footer(config)}
"""
    return "RunLedger Team: subscription ended", text


def recovery_email(config: BillingConfig, public_url: str, team_name: str, key: str) -> Tuple[str, str]:
    text = f"""Someone (hopefully you) asked for a new admin key for the RunLedger team "{team_name}".

New admin API key:

    {key}

Sign in: {public_url}/?key={key}

Your other keys still work. If you did not ask for this, sign in and revoke the key labelled
"recovered" in the API keys tab.
{_footer(config)}
"""
    return "Your new RunLedger admin key", text


def send_quietly(mailer: Any, to: str, message: Tuple[str, str], idempotency_key: Optional[str] = None) -> None:
    """Send on a background thread. A failure is logged without the address or the body."""
    subject, text = message

    def run() -> None:
        try:
            mailer.send(to, subject, text, idempotency_key=idempotency_key)
        except Exception as exc:
            sys.stderr.write(f"RunLedger: email '{subject}' not sent ({exc})\n")

    threading.Thread(target=run, name="runledger-mail", daemon=True).start()


def run_maintenance(db: Any, retention_days: Optional[int], moment: Optional[datetime] = None) -> Dict[str, int]:
    """Apply the retention period (when set) and delete billed teams whose subscription ended
    more than DELETE_AFTER_DAYS ago. Returns how many rows went, per table, and teams_deleted."""
    moment = moment or now()
    result: Dict[str, int] = {}
    if retention_days:
        result.update(db.purge_before(stamp(moment - timedelta(days=retention_days))))
    due = db.teams_due_for_deletion(stamp(moment - timedelta(days=DELETE_AFTER_DAYS)))
    for item in due:
        db.delete_team(item["team_id"])
    result["teams_deleted"] = len(due)
    return result


RECOVER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RunLedger: new admin key</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:48px 16px;background:#0a0b0d;color:#eef0f2}
@media (prefers-color-scheme: light){body{background:#f7f8f9;color:#121417}}
main{max-width:520px;margin:0 auto}input,button{font:inherit;padding:8px 12px;border-radius:8px;border:1px solid #6b7280}
input{width:100%;box-sizing:border-box;margin:8px 0 12px;background:transparent;color:inherit}
button{cursor:pointer;background:#2563eb;color:#fff;border-color:#2563eb}#msg{margin-top:16px}</style>
</head><body><main>
<h1 style="font-size:20px">Get a new admin key</h1>
<p>Enter the email you subscribed with. If it has a RunLedger Team subscription, we email a new admin key for that team. Your other keys keep working.</p>
<form id="f"><label for="email">Email</label><input id="email" type="email" required autocomplete="email" maxlength="254">
<button type="submit">Email me a new key</button></form>
<p id="msg" role="status"></p>
</main>
<script nonce="__CSP_NONCE__">
document.getElementById("f").addEventListener("submit", function (e) {
  e.preventDefault();
  var msg = document.getElementById("msg");
  msg.textContent = "Sending...";
  fetch("/billing/recover", {method: "POST", headers: {"Content-Type": "application/json", "X-Requested-With": "runledger"},
    body: JSON.stringify({email: document.getElementById("email").value})})
    .then(function (r) { return r.json(); })
    .then(function (d) { msg.textContent = (d && (d.message || (d.error && d.error.message))) || "Done."; })
    .catch(function () { msg.textContent = "Could not reach the server. Try again."; });
});
</script></body></html>
"""
