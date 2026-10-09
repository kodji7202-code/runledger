"""Tests for the hosted plan: Polar webhook signatures, the subscription lifecycle (team and key
creation, welcome email, past due grace, read only, deletion), seats, key recovery, retention,
configuration, and the `runledger billing list` command.

Each server test runs a real server on a free port with a temporary database and a fake mailer
that records what would have been emailed. The billing clock is moved by patching billing.now.
"""
import base64
import hashlib
import hmac
import http.client
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from runledger.cli import main
from runledger.server import billing
from runledger.server.app import make_server
from runledger.server.db import Database

SECRET = "polar_whs_test_secret_value"
CSRF = {"X-Requested-With": "runledger"}
KEY_RE = re.compile(r"rl_[A-Za-z0-9_-]{40}")


class FakeMailer:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.lock = threading.Lock()

    def send(self, to, subject, text, idempotency_key=None):
        if self.fail:
            raise billing.MailError("Resend answered HTTP 500")
        with self.lock:
            self.sent.append({"to": to, "subject": subject, "text": text, "idem": idempotency_key})

    def wait_for(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.sent) >= count:
                    return list(self.sent)
            time.sleep(0.02)
        with self.lock:
            return list(self.sent)


def config(**overrides):
    values = dict(webhook_secret=SECRET, resend_api_key="re_test", mail_from="RunLedger <team@mail.example.com>",
                  support_email="help@example.com", portal_url="https://polar.example.com/portal")
    values.update(overrides)
    return billing.BillingConfig(**values)


@contextmanager
def running(db_path, mailer=None, **options):
    options.setdefault("billing_config", config())
    srv = make_server(str(db_path), host="127.0.0.1", port=0, mailer=mailer or FakeMailer(),
                      public_url="https://app.example.com", **options)
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
    mailer = FakeMailer()
    with running(tmp_path / "billing.db", mailer=mailer) as (srv, base):
        yield srv, base, mailer


@pytest.fixture
def clock(monkeypatch):
    state = {"offset": timedelta(0)}
    monkeypatch.setattr(billing, "now", lambda: datetime.now(timezone.utc) + state["offset"])

    class Clock:
        def advance(self, **kwargs):
            state["offset"] += timedelta(**kwargs)

    return Clock()


def _http(base, method, path, body=None, headers=None):
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


def _json(raw):
    return json.loads(raw.decode("utf-8"))


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


_counter = [0]


def subscription(status="active", sub_id="sub_1", seats=3, email="Owner@Acme.example", modified=None, **extra):
    _counter[0] += 1
    data = {
        "id": sub_id,
        "status": status,
        "customer_id": "cus_1",
        "product_id": "prod_team",
        "seats": seats,
        "current_period_end": "2026-11-09T10:00:00.123456Z",
        "cancel_at_period_end": False,
        "ended_at": None,
        "modified_at": modified or (datetime.now(timezone.utc) + timedelta(microseconds=_counter[0])).isoformat(),
        "customer": {"id": "cus_1", "email": email, "name": "Acme Ops"},
        "custom_field_data": {"team_name": "Acme"},
        "metadata": {},
    }
    data.update(extra)
    return data


def deliver(base, event_type, data, webhook_id=None, secret=SECRET, timestamp=None):
    _counter[0] += 1
    webhook_id = webhook_id or f"msg_{_counter[0]}"
    body = json.dumps({"type": event_type, "timestamp": "2026-10-09T10:00:00Z", "data": data}).encode("utf-8")
    ts = int(time.time()) if timestamp is None else timestamp
    headers = {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": billing.sign(secret, webhook_id, ts, body),
    }
    status, _, raw = _http(base, "POST", "/billing/polar/webhook", body=body, headers=headers)
    return status, _json(raw)


def provision(base, mailer, **kwargs):
    """Deliver subscription.active and return the admin key from the welcome email."""
    status, body = deliver(base, "subscription.active", subscription(**kwargs))
    assert status == 200, body
    assert body["result"] == "provisioned", body
    mail = mailer.sent[-1]
    return KEY_RE.search(mail["text"]).group(0)


def push(base, key, run_id, user="alice", started=None):
    payload = {"session_id": run_id, "user": user, "project": "api"}
    if started:
        payload["started"] = started
    return _http(base, "POST", "/api/runs", body=payload, headers=_auth(key))


# Signatures


def test_sign_matches_a_manual_standard_webhooks_signature():
    body = b'{"type":"x"}'
    expected = base64.b64encode(hmac.new(SECRET.encode(), b"msg_1.1700000000." + body, hashlib.sha256).digest())
    assert billing.sign(SECRET, "msg_1", 1700000000, body) == "v1," + expected.decode()


def _headers(sig, ts=1700000000, msg="msg_1"):
    return {"webhook-id": msg, "webhook-timestamp": str(ts), "webhook-signature": sig}


def test_verify_accepts_a_valid_signature_among_several():
    body = b"{}"
    good = billing.sign(SECRET, "msg_1", 1700000000, body)
    assert billing.verify_webhook(SECRET, _headers("v1,AAAA " + good), body, 1700000010) == "msg_1"


@pytest.mark.parametrize("change", ["body", "secret", "old", "future", "missing", "version"])
def test_verify_rejects_bad_signatures(change):
    body = b"{}"
    sig = billing.sign(SECRET, "msg_1", 1700000000, body)
    headers, now_ts, check_body, secret = _headers(sig), 1700000000, body, SECRET
    if change == "body":
        check_body = b'{"a":1}'
    elif change == "secret":
        secret = "other"
    elif change == "old":
        now_ts += billing.SIGNATURE_TOLERANCE_S + 1
    elif change == "future":
        now_ts -= billing.SIGNATURE_TOLERANCE_S + 1
    elif change == "missing":
        headers.pop("webhook-signature")
    elif change == "version":
        headers["webhook-signature"] = sig.replace("v1,", "v2,")
    with pytest.raises(billing.InvalidSignature):
        billing.verify_webhook(secret, headers, check_body, now_ts)


def test_verify_accepts_a_generic_whsec_base64_secret():
    raw = b"0123456789abcdef0123456789abcdef"
    secret = "whsec_" + base64.b64encode(raw).decode()
    body = b"{}"
    digest = hmac.new(raw, b"msg_1.1700000000." + body, hashlib.sha256).digest()
    sig = "v1," + base64.b64encode(digest).decode()
    assert billing.verify_webhook(secret, _headers(sig), body, 1700000000) == "msg_1"


# Payload parsing


def test_parse_subscription_reads_the_fields_billing_needs():
    sub = billing.parse_subscription(subscription(modified="2026-10-09T12:30:00.5+02:00"))
    assert sub["id"] == "sub_1"
    assert sub["email"] == "owner@acme.example"
    assert sub["team_name"] == "Acme"
    assert sub["seats"] == 3
    assert sub["current_period_end"] == "2026-11-09T10:00:00Z"
    assert sub["modified_at"] == "2026-10-09T10:30:00.500000Z"


def test_parse_subscription_falls_back_for_the_team_name_and_seats():
    data = subscription(seats=None, custom_field_data={}, customer={"email": "dev@x.example", "name": ""})
    sub = billing.parse_subscription(data)
    assert sub["team_name"] == "dev"
    assert sub["seats"] == 1


@pytest.mark.parametrize("data", [
    None,
    {"status": "active"},
    subscription(status="weird"),
    subscription(customer={"email": "not-an-email"}),
])
def test_parse_subscription_rejects_unusable_payloads(data):
    with pytest.raises(billing.InvalidEvent):
        billing.parse_subscription(data)


# Lifecycle over HTTP


def test_billing_routes_are_absent_when_billing_is_off(tmp_path):
    with running(tmp_path / "off.db", billing_config=None) as (srv, base):
        assert srv.billing is None
        assert _http(base, "POST", "/billing/polar/webhook", body={})[0] == 404
        assert _http(base, "GET", "/recover")[0] == 404


def test_a_bad_signature_is_refused(env):
    srv, base, mailer = env
    status, body = deliver(base, "subscription.active", subscription(), secret="wrong")
    assert status == 401
    assert body["error"]["code"] == "invalid_signature"
    assert srv.db.list_subscriptions() == []


def test_an_old_timestamp_is_refused(env):
    srv, base, mailer = env
    status, _ = deliver(base, "subscription.active", subscription(), timestamp=int(time.time()) - 3600)
    assert status == 401


def test_incomplete_then_active_creates_one_team_and_emails_its_key(env):
    srv, base, mailer = env
    status, body = deliver(base, "subscription.created", subscription(status="incomplete"))
    assert (status, body["result"]) == (200, "pending")
    assert mailer.sent == []

    key = provision(base, mailer)
    mail = mailer.sent[-1]
    assert mail["to"] == "owner@acme.example"
    assert mail["subject"] == "Your RunLedger Team server is ready"
    assert "https://app.example.com/?key=" + key in mail["text"]
    assert "https://polar.example.com/portal" in mail["text"]
    assert mail["idem"].startswith("welcome-sub_1-rl_")

    status, _, raw = _http(base, "GET", "/api/me", headers=_auth(key))
    me = _json(raw)
    assert status == 200 and me["role"] == "admin" and me["team"]["name"] == "Acme"
    assert me["billing"]["state"] == "active"
    assert me["billing"]["seats"] == 3 and me["billing"]["seats_used"] == 0
    assert me["billing"]["portal_url"] == "https://polar.example.com/portal"

    # Later events for the same subscription never create a second team or resend the key.
    status, body = deliver(base, "subscription.updated", subscription(seats=5))
    assert (status, body["result"]) == (200, "updated")
    assert len(mailer.sent) == 1
    assert len(srv.db.list_subscriptions()) == 1
    assert srv.db.list_subscriptions()[0]["seats"] == 5


def test_a_redelivered_event_is_applied_once(env):
    srv, base, mailer = env
    data = subscription()
    assert deliver(base, "subscription.active", data, webhook_id="msg_same")[1]["result"] == "provisioned"
    status, body = deliver(base, "subscription.active", data, webhook_id="msg_same")
    assert (status, body["result"]) == (200, "duplicate")
    assert len(mailer.sent) == 1


def test_teams_without_a_subscription_are_not_billed(env):
    srv, base, mailer = env
    team_id, key = srv.db.create_team("self-made")
    status, _, raw = _http(base, "GET", "/api/me", headers=_auth(key))
    assert "billing" not in _json(raw)
    for n in range(5):
        assert push(base, key, f"s{n}", user=f"dev{n}")[0] == 201


def test_a_failed_welcome_email_is_retried_with_a_fresh_key(tmp_path):
    mailer = FakeMailer(fail=True)
    with running(tmp_path / "mail.db", mailer=mailer) as (srv, base):
        data = subscription()
        status, body = deliver(base, "subscription.active", data, webhook_id="msg_retry")
        assert status == 503 and body["error"]["code"] == "mail_failed"
        assert len(srv.db.list_subscriptions()) == 1  # the team exists, the event is not recorded

        mailer.fail = False
        status, body = deliver(base, "subscription.active", data, webhook_id="msg_retry")
        assert (status, body["result"]) == (200, "welcomed")
        key = KEY_RE.search(mailer.sent[-1]["text"]).group(0)
        assert _http(base, "GET", "/api/me", headers=_auth(key))[0] == 200
        assert len(srv.db.list_subscriptions()) == 1


def test_team_names_stay_unique(env):
    srv, base, mailer = env
    srv.db.create_team("Acme")
    key = provision(base, mailer)
    status, _, raw = _http(base, "GET", "/api/me", headers=_auth(key))
    assert _json(raw)["team"]["name"] == "Acme 2"


def test_out_of_order_events_are_ignored(env):
    srv, base, mailer = env
    provision(base, mailer, modified="2026-10-09T10:00:05Z")
    status, body = deliver(base, "subscription.updated", subscription(seats=9, modified="2026-10-09T10:00:01Z"))
    assert body["result"] == "stale"
    status, body = deliver(base, "subscription.created", subscription(status="incomplete"))
    assert body["result"] == "stale"
    row = srv.db.list_subscriptions()[0]
    assert (row["status"], row["seats"]) == ("active", 3)


def test_other_products_are_ignored(tmp_path):
    mailer = FakeMailer()
    with running(tmp_path / "p.db", mailer=mailer, billing_config=config(product_ids=("prod_other",))) as (srv, base):
        status, body = deliver(base, "subscription.active", subscription())
        assert (status, body["result"]) == (200, "other_product")
        assert srv.db.list_subscriptions() == []


def test_unrelated_and_malformed_events_are_acknowledged(env):
    srv, base, mailer = env
    assert deliver(base, "order.paid", {"id": "ord_1"})[1]["result"] == "ignored"
    assert deliver(base, "subscription.updated", {"id": "sub_x"})[1]["result"] == "ignored"


# Seats


def test_seats_bind_to_authenticated_keys_not_receipt_user(env):
    srv, base, mailer = env
    key = provision(base, mailer, seats=1)
    team_id = srv.db.key_for_token(key)["team_id"]
    second = srv.db.create_key(team_id, "second", "member", "test")["key"]

    assert push(base, key, "a1", user="alice")[0] == 201
    assert push(base, key, "b1", user="bob")[0] == 201  # caller-controlled user cannot consume another seat
    assert push(base, key, "a1", user="alice")[0] == 201  # exact retry is idempotent

    status, _, raw = push(base, second, "c1", user="alice")
    assert status == 402
    err = _json(raw)["error"]
    assert err["code"] == "seat_limit" and "All 1 seat on this team" in err["message"]
    assert _json(_http(base, "GET", "/api/me", headers=_auth(key))[2])["billing"]["seats_used"] == 1

    deliver(base, "subscription.updated", subscription(seats=2))
    assert push(base, second, "c1", user="anything")[0] == 201
    status, _, raw = _http(base, "GET", "/api/me", headers=_auth(key))
    assert _json(raw)["billing"]["seats_used"] == 2


def test_a_seat_frees_up_after_the_window(env, clock):
    srv, base, mailer = env
    key = provision(base, mailer, seats=1)
    team_id = srv.db.key_for_token(key)["team_id"]
    second = srv.db.create_key(team_id, "second", "member", "test")["key"]
    assert push(base, key, "a1", user="alice")[0] == 201
    assert push(base, second, "b1", user="alice")[0] == 402
    clock.advance(days=billing.SEAT_WINDOW_DAYS + 1)
    assert push(base, second, "b1", user="alice")[0] == 201


def test_legacy_user_seats_transition_without_a_window_lockout(env):
    srv, base, mailer = env
    key = provision(base, mailer, seats=1)
    team_id = srv.db.key_for_token(key)["team_id"]
    second = srv.db.create_key(team_id, "second", "member", "test")["key"]
    assert push(base, key, "legacy", user="alice")[0] == 201

    # Simulate a row written before authenticated key attribution existed.
    with sqlite3.connect(str(srv.db.path)) as conn:
        conn.execute("UPDATE runs SET pushed_by_key_id = NULL WHERE team_id = ? AND id = 'legacy'", (team_id,))
        conn.commit()
    assert _json(_http(base, "GET", "/api/me", headers=_auth(key))[2])["billing"]["seats_used"] == 1

    # The first post-upgrade push establishes the key-backed ledger instead of
    # counting both the legacy user string and this authenticated key.
    assert push(base, key, "new", user="totally-different")[0] == 201
    assert _json(_http(base, "GET", "/api/me", headers=_auth(key))[2])["billing"]["seats_used"] == 1
    assert push(base, second, "blocked", user="alice")[0] == 402


def test_concurrent_seat_admission_cannot_oversubscribe(env):
    srv, base, mailer = env
    owner = provision(base, mailer, seats=1)
    team_id = srv.db.key_for_token(owner)["team_id"]
    key_a = srv.db.create_key(team_id, "a", "member", "test")["key"]
    key_b = srv.db.create_key(team_id, "b", "member", "test")["key"]
    barrier = threading.Barrier(3)
    results = []
    lock = threading.Lock()

    def attempt(key, run_id):
        barrier.wait()
        status = push(base, key, run_id, user="same-spoofed-user")[0]
        with lock:
            results.append(status)

    threads = [
        threading.Thread(target=attempt, args=(key_a, "race-a")),
        threading.Thread(target=attempt, args=(key_b, "race-b")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(10)

    assert sorted(results) == [201, 402]
    assert _json(_http(base, "GET", "/api/me", headers=_auth(owner))[2])["billing"]["seats_used"] == 1


# Past due, cancel, end, deletion


def test_past_due_has_a_grace_period_then_is_read_only(env, clock):
    srv, base, mailer = env
    key = provision(base, mailer)
    status, body = deliver(base, "subscription.past_due", subscription(status="past_due"))
    assert body["result"] == "updated"
    mails = mailer.wait_for(2)
    assert mails[-1]["subject"] == "RunLedger Team: payment failed"
    assert push(base, key, "s1")[0] == 201

    _, _, raw = _http(base, "GET", "/api/me", headers=_auth(key))
    assert _json(raw)["billing"]["state"] == "past_due"

    clock.advance(days=billing.GRACE_DAYS, minutes=1)
    status, _, raw = push(base, key, "s2")
    assert status == 402 and _json(raw)["error"]["code"] == "payment_required"
    approval = {"session_id": "s", "tool": "Bash", "summary": "rm -rf build"}
    assert _http(base, "POST", "/api/approvals", body=approval, headers=_auth(key))[0] == 402
    assert _http(base, "GET", "/api/runs", headers=_auth(key))[0] == 200
    _, _, raw = _http(base, "GET", "/api/me", headers=_auth(key))
    assert _json(raw)["billing"]["state"] == "read_only"

    deliver(base, "subscription.updated", subscription(status="active"))
    assert push(base, key, "s2")[0] == 201
    assert len(mailer.sent) == 2  # recovering sends nothing new


def test_cancel_at_period_end_keeps_access(env):
    srv, base, mailer = env
    key = provision(base, mailer)
    deliver(base, "subscription.canceled", subscription(cancel_at_period_end=True))
    _, _, raw = _http(base, "GET", "/api/me", headers=_auth(key))
    assert _json(raw)["billing"]["state"] == "canceling"
    assert push(base, key, "s1")[0] == 201


def test_an_ended_subscription_is_read_only_then_deleted(env):
    srv, base, mailer = env
    key = provision(base, mailer)
    assert push(base, key, "s1")[0] == 201
    deliver(base, "subscription.revoked", subscription(status="canceled", ended_at="2026-10-09T10:00:00Z"))
    mails = mailer.wait_for(2)
    assert mails[-1]["subject"] == "RunLedger Team: subscription ended"
    assert "2026-11-08T10:00:00Z" in mails[-1]["text"]

    status, _, raw = push(base, key, "s2")
    assert status == 402 and "has ended" in _json(raw)["error"]["message"]
    assert _http(base, "GET", "/api/export/runs.csv", headers=_auth(key))[0] == 200

    team_id = srv.db.list_subscriptions()[0]["team_id"]
    after = datetime(2026, 11, 8, 10, 0, 1, tzinfo=timezone.utc)
    assert billing.run_maintenance(srv.db, None, moment=after - timedelta(seconds=2))["teams_deleted"] == 0
    assert billing.run_maintenance(srv.db, None, moment=after)["teams_deleted"] == 1
    assert srv.db.team_name(team_id) is None
    assert _http(base, "GET", "/api/me", headers=_auth(key))[0] == 401
    row = srv.db.list_subscriptions()[0]
    assert row["team_id"] is None and row["email"] is None

    # A late event for the deleted team does not bring it back.
    assert deliver(base, "subscription.updated", subscription(status="canceled"))[1]["result"] == "stale"


def test_deletion_waits_for_the_period(env):
    srv, base, mailer = env
    provision(base, mailer)
    deliver(base, "subscription.revoked", subscription(status="canceled"))
    assert billing.run_maintenance(srv.db, None)["teams_deleted"] == 0
    later = datetime.now(timezone.utc) + timedelta(days=billing.DELETE_AFTER_DAYS, minutes=1)
    assert billing.run_maintenance(srv.db, None, moment=later)["teams_deleted"] == 1


# Key recovery


def test_recovery_key_inherits_an_occupied_admin_seat_without_invalidating_the_old_key(env):
    srv, base, mailer = env
    old = provision(base, mailer, seats=1)
    old_me = _json(_http(base, "GET", "/api/me", headers=_auth(old))[2])
    old_id = old_me["key"]["id"]
    assert push(base, old, "before-recovery", user="owner")[0] == 201
    status, _, raw = _http(base, "POST", "/billing/recover", body={"email": "OWNER@acme.example"}, headers=CSRF)
    assert status == 202 and _json(raw)["message"] == "If that email has a RunLedger Team subscription, " \
        "a new admin key is on its way. Check your inbox in a few minutes."
    mails = mailer.wait_for(2)
    assert mails[-1]["subject"] == "Your new RunLedger admin key"
    new = KEY_RE.search(mails[-1]["text"]).group(0)
    assert new != old
    assert _http(base, "GET", "/api/me", headers=_auth(new))[0] == 200
    assert _http(base, "GET", "/api/me", headers=_auth(old))[0] == 200
    new_me = _json(_http(base, "GET", "/api/me", headers=_auth(new))[2])
    assert new_me["role"] == "admin" and new_me["key"]["id"] != old_id
    assert push(base, new, "after-recovery", user="spoof-does-not-matter")[0] == 201
    assert _json(_http(base, "GET", "/api/me", headers=_auth(new))[2])["billing"]["seats_used"] == 1
    with sqlite3.connect(str(srv.db.path)) as conn:
        rows = dict(conn.execute(
            "SELECT id, pushed_by_key_id FROM runs WHERE id IN ('before-recovery', 'after-recovery')"
        ).fetchall())
        alias = conn.execute(
            "SELECT seat_key_id FROM seat_aliases WHERE key_id = ?", (new_me["key"]["id"],)
        ).fetchone()
    assert rows == {"before-recovery": old_id, "after-recovery": new_me["key"]["id"]}
    assert alias == (old_id,)

    outsider = srv.db.create_key(old_me["team"]["id"], "another developer", "member", "test")["key"]
    status, _, raw = push(base, outsider, "outside-seat", user="owner")
    assert status == 402 and _json(raw)["error"]["code"] == "seat_limit"

    # A second request soon after sends nothing.
    assert _http(base, "POST", "/billing/recover", body={"email": "owner@acme.example"}, headers=CSRF)[0] == 202
    time.sleep(0.2)
    assert len(mailer.sent) == 2


def test_repeated_recovery_rotates_one_alias_instead_of_accumulating_free_keys(env):
    srv, base, mailer = env
    old = provision(base, mailer, seats=1)
    assert push(base, old, "seed")[0] == 201
    assert _http(base, "POST", "/billing/recover", body={"email": "owner@acme.example"}, headers=CSRF)[0] == 202
    first = KEY_RE.search(mailer.wait_for(2)[-1]["text"]).group(0)
    first_id = _json(_http(base, "GET", "/api/me", headers=_auth(first))[2])["key"]["id"]

    with sqlite3.connect(str(srv.db.path)) as conn:
        conn.execute("UPDATE billing_subscriptions SET last_recovery_at = '2000-01-01T00:00:00Z'")
        conn.commit()
    assert _http(base, "POST", "/billing/recover", body={"email": "owner@acme.example"}, headers=CSRF)[0] == 202
    second = KEY_RE.search(mailer.wait_for(3)[-1]["text"]).group(0)
    second_id = _json(_http(base, "GET", "/api/me", headers=_auth(second))[2])["key"]["id"]

    assert second != first and second_id == first_id
    assert _http(base, "GET", "/api/me", headers=_auth(first))[0] == 401
    assert _http(base, "GET", "/api/me", headers=_auth(old))[0] == 200
    assert push(base, second, "recovered-again")[0] == 201
    with sqlite3.connect(str(srv.db.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM seat_aliases WHERE team_id = 1").fetchone()[0] == 1


def test_recovery_can_reclaim_a_revoked_admins_occupied_seat(env):
    srv, base, mailer = env
    old = provision(base, mailer, seats=1)
    me = _json(_http(base, "GET", "/api/me", headers=_auth(old))[2])
    team_id, old_id = me["team"]["id"], me["key"]["id"]
    assert push(base, old, "owner-seat")[0] == 201

    second = _json(_http(
        base, "POST", "/api/keys", body={"label": "backup admin", "role": "admin"}, headers=_auth(old)
    )[2])
    assert _http(base, "POST", f"/api/keys/{old_id}/revoke", headers=_auth(second["key"]))[0] == 200
    assert _http(base, "GET", "/api/me", headers=_auth(old))[0] == 401
    assert push(base, second["key"], "backup-blocked")[0] == 402

    assert _http(base, "POST", "/billing/recover", body={"email": "owner@acme.example"}, headers=CSRF)[0] == 202
    recovered = KEY_RE.search(mailer.wait_for(2)[-1]["text"]).group(0)
    recovered_id = _json(_http(base, "GET", "/api/me", headers=_auth(recovered))[2])["key"]["id"]
    assert recovered_id != old_id
    assert push(base, recovered, "replacement-seat")[0] == 201
    assert push(base, second["key"], "still-blocked")[0] == 402
    assert _json(_http(base, "GET", "/api/me", headers=_auth(recovered))[2])["billing"]["seats_used"] == 1
    with sqlite3.connect(str(srv.db.path)) as conn:
        assert conn.execute(
            "SELECT seat_key_id FROM seat_aliases WHERE key_id = ?", (recovered_id,)
        ).fetchone() == (old_id,)


def test_recovery_does_not_alias_a_member_only_seat(env):
    srv, base, mailer = env
    owner = provision(base, mailer, seats=1)
    team_id = _json(_http(base, "GET", "/api/me", headers=_auth(owner))[2])["team"]["id"]
    member = srv.db.create_key(team_id, "developer", "member", "test")["key"]
    assert push(base, member, "member-seat")[0] == 201

    assert _http(base, "POST", "/billing/recover", body={"email": "owner@acme.example"}, headers=CSRF)[0] == 202
    recovered = KEY_RE.search(mailer.wait_for(2)[-1]["text"]).group(0)
    recovered_id = _json(_http(base, "GET", "/api/me", headers=_auth(recovered))[2])["key"]["id"]
    status, _, raw = push(base, recovered, "must-not-steal-member-seat")
    assert status == 402 and _json(raw)["error"]["code"] == "seat_limit"
    with sqlite3.connect(str(srv.db.path)) as conn:
        assert conn.execute(
            "SELECT seat_key_id FROM seat_aliases WHERE key_id = ?", (recovered_id,)
        ).fetchone() is None


def test_rotating_an_occupied_key_keeps_the_same_seat_identity(env):
    srv, base, mailer = env
    old = provision(base, mailer, seats=1)
    me = _json(_http(base, "GET", "/api/me", headers=_auth(old))[2])
    key_id = me["key"]["id"]
    assert push(base, old, "before-rotate")[0] == 201

    status, _, raw = _http(base, "POST", f"/api/keys/{key_id}/rotate", headers=_auth(old))
    assert status == 200
    rotated = _json(raw)["key"]
    assert rotated != old
    assert _http(base, "GET", "/api/me", headers=_auth(old))[0] == 401
    assert _json(_http(base, "GET", "/api/me", headers=_auth(rotated))[2])["key"]["id"] == key_id
    assert push(base, rotated, "after-rotate")[0] == 201
    assert _json(_http(base, "GET", "/api/me", headers=_auth(rotated))[2])["billing"]["seats_used"] == 1


def test_recovered_seat_and_new_key_race_cannot_oversubscribe(env):
    srv, base, mailer = env
    old = provision(base, mailer, seats=1)
    team_id = _json(_http(base, "GET", "/api/me", headers=_auth(old))[2])["team"]["id"]
    assert push(base, old, "race-seed")[0] == 201
    assert _http(base, "POST", "/billing/recover", body={"email": "owner@acme.example"}, headers=CSRF)[0] == 202
    recovered = KEY_RE.search(mailer.wait_for(2)[-1]["text"]).group(0)
    outsider = srv.db.create_key(team_id, "outsider", "member", "test")["key"]
    barrier = threading.Barrier(3)
    results = []
    lock = threading.Lock()

    def attempt(key, run_id):
        barrier.wait()
        status = push(base, key, run_id, user="same-user")[0]
        with lock:
            results.append((run_id, status))

    threads = [
        threading.Thread(target=attempt, args=(recovered, "race-recovered")),
        threading.Thread(target=attempt, args=(outsider, "race-outsider")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(10)

    assert sorted(results) == [("race-outsider", 402), ("race-recovered", 201)]
    assert _json(_http(base, "GET", "/api/me", headers=_auth(recovered))[2])["billing"]["seats_used"] == 1


def test_run_admission_uses_fresh_seat_count_after_interleaved_subscription_update(env, monkeypatch):
    srv, base, mailer = env
    owner = provision(base, mailer, seats=2)
    team_id = _json(_http(base, "GET", "/api/me", headers=_auth(owner))[2])["team"]["id"]
    second = srv.db.create_key(team_id, "second", "member", "test")["key"]
    assert push(base, owner, "occupied")[0] == 201

    original = srv.db.upsert_run
    changed = [False]

    def interleaved(*args, **kwargs):
        if not changed[0]:
            changed[0] = True
            assert deliver(base, "subscription.updated", subscription(seats=1))[0] == 200
        return original(*args, **kwargs)

    monkeypatch.setattr(srv.db, "upsert_run", interleaved)
    status, _, raw = push(base, second, "must-see-shrink")
    assert status == 402 and _json(raw)["error"]["code"] == "seat_limit"
    assert srv.db.get_run(team_id, "must-see-shrink") is None


def test_run_admission_uses_fresh_write_state_after_interleaved_subscription_update(env, monkeypatch):
    srv, base, mailer = env
    owner = provision(base, mailer, seats=1)
    team_id = _json(_http(base, "GET", "/api/me", headers=_auth(owner))[2])["team"]["id"]
    original = srv.db.upsert_run
    changed = [False]

    def interleaved(*args, **kwargs):
        if not changed[0]:
            changed[0] = True
            assert deliver(
                base,
                "subscription.revoked",
                subscription(status="canceled", ended_at="2026-10-09T10:00:00Z"),
            )[0] == 200
        return original(*args, **kwargs)

    monkeypatch.setattr(srv.db, "upsert_run", interleaved)
    status, _, raw = push(base, owner, "must-see-ended")
    assert status == 402 and _json(raw)["error"]["code"] == "payment_required"
    assert srv.db.get_run(team_id, "must-see-ended") is None


def test_recovery_answers_the_same_for_unknown_emails(env):
    srv, base, mailer = env
    status, _, raw = _http(base, "POST", "/billing/recover", body={"email": "nobody@x.example"}, headers=CSRF)
    assert status == 202
    time.sleep(0.2)
    assert mailer.sent == []


def test_recovery_needs_the_header_a_valid_email_and_is_rate_limited(env):
    srv, base, mailer = env
    assert _http(base, "POST", "/billing/recover", body={"email": "a@b.example"})[0] == 403
    assert _http(base, "POST", "/billing/recover", body={"email": "nope"}, headers=CSRF)[0] == 400
    codes = [_http(base, "POST", "/billing/recover", body={"email": "a@b.example"}, headers=CSRF)[0]
             for _ in range(6)]
    assert codes[-1] == 429


def test_recover_page_is_served(env):
    srv, base, mailer = env
    status, headers, raw = _http(base, "GET", "/recover")
    assert status == 200
    assert "nonce-" in headers["content-security-policy"]
    assert b"/billing/recover" in raw and b"__CSP_NONCE__" not in raw


# Retention


def test_retention_removes_old_runs_and_events(tmp_path):
    with running(tmp_path / "r.db", billing_config=None) as (srv, base):
        team_id, key = srv.db.create_team("t")
        old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert push(base, key, "old", started=old)[0] == 201
        assert push(base, key, "new")[0] == 201
        removed = billing.run_maintenance(srv.db, 90)
        assert removed["runs"] == 1 and removed["teams_deleted"] == 0
        ids = [r["id"] for r in srv.db.list_runs(team_id)]
        assert ids == ["new"]


def test_maintenance_only_starts_when_needed(tmp_path):
    with running(tmp_path / "m1.db", billing_config=None) as (srv, base):
        assert srv.start_maintenance() is False
    with running(tmp_path / "m2.db", billing_config=None, retention_days=90) as (srv, base):
        assert srv.start_maintenance(interval_s=3600) is True


# Configuration


def test_config_from_env():
    assert billing.BillingConfig.from_env({}) is None
    with pytest.raises(billing.BillingConfigError):
        billing.BillingConfig.from_env({billing.SECRET_ENV: "s"})
    cfg = billing.BillingConfig.from_env({
        billing.SECRET_ENV: "s", billing.RESEND_KEY_ENV: "re_x", billing.MAIL_FROM_ENV: "a@b.example",
        billing.PRODUCTS_ENV: "p1, p2", billing.PORTAL_ENV: "https://polar.sh/acme/portal/",
    })
    assert cfg.product_ids == ("p1", "p2")
    assert cfg.portal_url == "https://polar.sh/acme/portal"


@pytest.mark.parametrize("raw, expected", [("", None), ("90", 90)])
def test_retention_from_env(raw, expected):
    assert billing.retention_from_env({billing.RETENTION_ENV: raw}) == expected


@pytest.mark.parametrize("raw", ["0", "ninety", "-5"])
def test_retention_from_env_rejects_bad_values(raw):
    with pytest.raises(billing.BillingConfigError):
        billing.retention_from_env({billing.RETENTION_ENV: raw})


def test_serve_refuses_half_configured_billing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(billing.SECRET_ENV, "s")
    monkeypatch.delenv(billing.RESEND_KEY_ENV, raising=False)
    assert main(["serve", "--port", "0", "--db", str(tmp_path / "x.db")]) == 1
    assert billing.RESEND_KEY_ENV in capsys.readouterr().err


# CLI


def test_billing_list(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    db = Database(str(db_path))
    try:
        db.apply_subscription(billing.parse_subscription(subscription()))
    finally:
        db.close()
    assert main(["billing", "list", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "sub_1" in out and "Acme" in out and "active" in out and "owner@acme.example" in out


def test_backup_copies_the_database_and_keeps_the_newest(tmp_path, capsys):
    db_path = tmp_path / "live.db"
    db = Database(str(db_path))
    db.create_team("kept")
    db.close()
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in ("20200101", "20200102", "20200103"):
        (folder / f"runledger-{day}-000000.db").write_bytes(b"old")
    assert main(["backup", "--db", str(db_path), "--dir", str(folder), "--keep", "2"]) == 0
    files = sorted(p.name for p in folder.glob("runledger-*.db"))
    assert len(files) == 2 and files[0] == "runledger-20200103-000000.db"
    copy = Database(str(folder / files[1]))
    try:
        assert copy.team_name(1) == "kept"
    finally:
        copy.close()
    assert "removed 2 older" in capsys.readouterr().out


def test_backup_needs_an_existing_database(tmp_path, capsys):
    assert main(["backup", "--db", str(tmp_path / "missing.db"), "--dir", str(tmp_path / "b")]) == 1
    assert "no database" in capsys.readouterr().err


def test_billing_list_empty(tmp_path, capsys):
    assert main(["billing", "list", "--db", str(tmp_path / "e.db")]) == 0
    assert "No subscriptions." in capsys.readouterr().out
