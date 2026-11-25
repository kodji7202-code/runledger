"""Short deterministic load checks for the team server, plus an opt-in soak harness.

The CI test proves that concurrent writers and readers complete without stale reads, 5xx responses
or lost rows. A real soak is environmental and therefore opt-in:
`RUNLEDGER_SOAK=60 pytest tests/test_server_load.py -k soak` runs the same mix for sixty seconds.
"""
import copy
import http.client
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from runledger import client
from runledger.server.app import make_server

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


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


def _http(base, method, path, body=None, headers=None, timeout=10):
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _payload(template, run_id, worker, version=1):
    body = copy.deepcopy(template)
    body["session_id"] = run_id
    body["user"] = f"load-{worker}@example.com"
    body["started"] = "2026-10-15T10:00:00Z"
    body["ended"] = f"2026-10-15T10:{version:02d}:00Z"
    body["totals"] = dict(body["totals"], steps=version * 4, tokens=1000, cost_usd=0.01 * version)
    body["html"] = f"<html>{run_id}</html>"
    return body


def _run_mix(base, key, template, *, writers, readers, rounds, deadline=None):
    auth = {"Authorization": f"Bearer {key}"}
    failures = []
    counts = {"write": 0, "read": 0}
    lock = threading.Lock()
    stop = threading.Event()

    def fail(message):
        with lock:
            failures.append(message)

    def writer(worker):
        for index in range(rounds):
            if deadline is not None and time.monotonic() >= deadline:
                break
            run_id = f"load-{worker}-{index}"
            for version in (1, 2):
                try:
                    status, raw = _http(base, "POST", "/api/runs", _payload(template, run_id, worker, version), auth)
                    if status not in (200, 201):
                        fail(f"write {run_id} v{version}: HTTP {status} {raw[:120]!r}")
                except Exception as exc:
                    fail(f"write {run_id} v{version}: {exc!r}")
                with lock:
                    counts["write"] += 1

    def reader(worker):
        paths = ("/api/runs?limit=20", "/api/stats", "/health")
        while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
            path = paths[(counts["read"] + worker) % len(paths)]
            try:
                status, raw = _http(base, "GET", path, headers=auth if path != "/health" else None)
                if status != 200:
                    fail(f"read {path}: HTTP {status} {raw[:120]!r}")
            except Exception as exc:
                fail(f"read {path}: {exc!r}")
            with lock:
                counts["read"] += 1

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    threads += [threading.Thread(target=reader, args=(i,)) for i in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads[:writers]:
        thread.join(90)
    stop.set()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads), "the load mix deadlocked"
    return counts, failures


def test_concurrent_write_and_read_load_completes_consistently(tmp_path):
    with running_server(tmp_path / "load.db") as (srv, base):
        _, key = srv.db.create_team("load")
        template = client.build_payload(FIX)
        counts, failures = _run_mix(base, key, template, writers=6, readers=3, rounds=6)

        assert failures == []
        assert counts["write"] == 6 * 6 * 2
        assert counts["read"] > 0
        runs = json.loads(_http(base, "GET", "/api/runs?limit=100", headers={"Authorization": f"Bearer {key}"})[1])["runs"]
        assert len(runs) == 36
        assert all(run["revision"] == 2 for run in runs)
        assert _http(base, "GET", "/health")[0] == 200


@pytest.mark.skipif(not os.environ.get("RUNLEDGER_SOAK"), reason="set RUNLEDGER_SOAK seconds for a soak run")
def test_server_soak_mix(tmp_path):
    seconds = float(os.environ.get("RUNLEDGER_SOAK", "60"))
    with running_server(tmp_path / "soak.db") as (srv, base):
        _, key = srv.db.create_team("soak")
        template = client.build_payload(FIX)
        counts, failures = _run_mix(
            base, key, template, writers=8, readers=4, rounds=10**9,
            deadline=time.monotonic() + seconds,
        )
        assert failures == []
        assert counts["write"] > 0 and counts["read"] > 0
        assert _http(base, "GET", "/health")[0] == 200
