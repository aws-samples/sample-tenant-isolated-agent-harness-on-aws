# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Exercises the runtime's HTTP layer on 127.0.0.1 with an ephemeral port."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from runtime import server
from shared import tenancy
from tests.test_runtime import make_task


@pytest.fixture
def httpd(tmp_path, monkeypatch):
    monkeypatch.setattr(server.Handler, "session_root", tmp_path)
    monkeypatch.setattr(server.Handler, "strict", True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


@pytest.fixture
def base_url(httpd):
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def _post(url, body, session=None):
    headers = {"Content-Type": "application/json"}
    if session:
        headers[server.SESSION_HEADER] = session
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        # Bandit B310: the URL is the local test server's http://127.0.0.1 address.
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_ping_reports_healthy_without_a_moving_timestamp(base_url):
    # Bandit B310: the URL is the local test server's http://127.0.0.1 address.
    with urllib.request.urlopen(base_url + "/ping", timeout=5) as response:  # nosec B310
        assert json.loads(response.read()) == {"status": "Healthy"}


def test_wrong_session_is_rejected_over_http(base_url, tenants):
    a, b = tenants.values()
    status, body = _post(base_url + "/invocations", make_task(a), session=tenancy.runtime_session_id(b))
    assert status == 403 and "does not match this runtime session" in body["error"]
    status, _ = _post(base_url + "/invocations", make_task(a))  # strict: header required
    assert status == 403


def test_busy_runtime_refuses_overlapping_tasks(base_url, tenants):
    a = tenants["anycompany-plumbing"]
    assert server.LOCK.acquire(blocking=False)
    try:
        status, body = _post(base_url + "/invocations", make_task(a), session=tenancy.runtime_session_id(a))
        assert status == 409 and body["error"] == "busy"
    finally:
        server.LOCK.release()


def test_unknown_paths_404(base_url):
    status, _ = _post(base_url + "/admin", {})
    assert status == 404


def test_default_bind_is_loopback(monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, address, handler):
            seen["address"] = address

        def serve_forever(self):
            pass

    monkeypatch.delenv("RUNTIME_BIND_HOST", raising=False)
    monkeypatch.setattr(server, "ThreadingHTTPServer", Recorder)
    server.serve()
    assert seen["address"] == ("127.0.0.1", 8080)


class RecordingReaper:
    def __init__(self, survivors=()):
        self.calls, self.survivors = 0, list(survivors)

    def reap(self):
        self.calls += 1
        return self.survivors


def test_strays_are_stopped_before_and_after_every_task(httpd, base_url, tenants, monkeypatch):
    httpd.reaper = reaper = RecordingReaper()
    monkeypatch.setattr(server, "run_task", lambda task, session_root: {"version": 1, "status": "completed",
                                                                          "result": {"exitCode": 0}})
    a = tenants["anycompany-plumbing"]
    status, _ = _post(base_url + "/invocations", make_task(a), session=tenancy.runtime_session_id(a))
    assert status == 200 and reaper.calls == 2


def test_no_task_starts_while_an_earlier_tasks_process_survives(httpd, base_url, tenants, monkeypatch):
    httpd.reaper = RecordingReaper(survivors=[4242])
    ran = []
    monkeypatch.setattr(server, "run_task", lambda task, session_root: ran.append(task))
    a = tenants["anycompany-plumbing"]
    status, body = _post(base_url + "/invocations", make_task(a), session=tenancy.runtime_session_id(a))
    assert status == 503 and "still running" in body["error"] and ran == []
