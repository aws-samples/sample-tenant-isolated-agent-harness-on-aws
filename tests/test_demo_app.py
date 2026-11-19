# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The local demo server: loopback only, and closed to other origins, hosts and form posts."""

import argparse
import http.client
import json
import threading

import pytest

from demo import app


class FakeDemo:
    region = "ap-southeast-2"
    outputs = {"ApiUrl": "https://api.example", "GatewayUrl": "https://gw.example", "RuntimeArn": "arn:aws:x"}

    def __init__(self):
        self.calls = []

    def submit(self, tenant, message):
        self.calls.append(("submit", tenant, message))
        return 202, {"requestId": "demo-0000000000000001", "state": "QUEUED"}

    def turn(self, tenant, request_id):
        return 200, {"requestId": request_id, "state": "COMPLETED"}


@pytest.fixture
def server():
    fake = FakeDemo()
    srv = app.make_server(argparse.Namespace(profile=None, region="ap-southeast-2", stack="SplitHarness", port=0),
                          factory=lambda: fake)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, fake
    srv.shutdown()
    srv.server_close()


def _call(srv, method, path, body=None, headers=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    sent = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    conn.request(method, path, body=data, headers=sent)
    response = conn.getresponse()
    return response.status, response.read(), response.getheader("Content-Security-Policy")


JSON = {"Content-Type": "application/json"}


def test_binds_to_loopback_only(server):
    srv, _ = server
    assert srv.server_address[0] == "127.0.0.1"


def test_serves_the_page_with_a_strict_csp(server):
    srv, _ = server
    for path in ("/", "/app.js"):
        status, body, csp = _call(srv, "GET", path)
        assert status == 200 and body and "script-src 'self'" in csp


def test_submits_a_turn_for_a_known_tenant(server):
    srv, fake = server
    status, body, _ = _call(srv, "POST", "/api/turn", {"tenant": "anycompany-plumbing", "message": "hi"}, JSON)
    assert status == 200 and json.loads(body)["status"] == 202
    assert fake.calls == [("submit", "anycompany-plumbing", "hi")]


@pytest.mark.parametrize("method,path,body,headers,want", [
    ("GET", "/api/info", None, {"Host": "attacker.example"}, 421),            # DNS rebinding
    ("POST", "/api/turn", {"tenant": "anycompany-plumbing", "message": "hi"}, {**JSON, "Origin": "https://evil.example"}, 403),
    ("POST", "/api/turn", b"tenant=anycompany-plumbing&message=hi", {"Content-Type": "application/x-www-form-urlencoded"}, 415),
    ("POST", "/api/turn", {"tenant": "someone-else", "message": "hi"}, JSON, 400),
    ("POST", "/api/turn", {"tenant": "anycompany-plumbing", "message": "x" * 5000}, JSON, 400),
    ("GET", "/api/turn?tenant=anycompany-plumbing&id=../../etc", None, None, 400),
    ("POST", "/api/check", {"id": "not-a-check"}, JSON, 400),
    ("GET", "/../../README.md", None, None, 404),
])
def test_refuses_requests_it_should_not_serve(server, method, path, body, headers, want):
    srv, fake = server
    status, _, _ = _call(srv, method, path, body, headers)
    assert status == want
    assert fake.calls == []  # nothing reached the AWS-facing client


def test_cost_defaults_reproduce_the_illustrative_model_without_touching_aws(server):
    srv, fake = server
    status, body, _ = _call(srv, "POST", "/api/cost", {}, JSON)
    result = json.loads(body)
    assert status == 200
    assert round(result["perTenant"]["allOn"]["total"], 2) == 4.11
    assert round(result["perTenant"]["split"]["total"], 2) == 1.67
    assert fake.calls == []


def test_cost_applies_overrides_and_refuses_bad_input(server):
    srv, _ = server
    status, body, _ = _call(srv, "POST", "/api/cost", {"code_share": 0.2, "include_isolation": True}, JSON)
    assert status == 200 and json.loads(body)["inputs"]["code_share"] == 0.2
    for bad in ({"code_share": 2}, {"tenants": "many"}, {"rm -rf": 1}):
        status, body, _ = _call(srv, "POST", "/api/cost", bad, JSON)
        assert status == 400 and json.loads(body)["message"]


@pytest.mark.parametrize("query", ["hours=0", "hours=500", "hours=abc", "hours=2.5"])
def test_measured_refuses_bad_windows_before_any_aws_call(server, query):
    srv, fake = server
    status, body, _ = _call(srv, "GET", f"/api/measured?{query}")
    assert status == 400 and "hours" in json.loads(body)["message"]
    assert fake.calls == []


def test_cross_site_api_reads_are_refused(server):
    srv, fake = server
    status, _, _ = _call(srv, "GET", "/api/turn?tenant=anycompany-plumbing&id=demo-0000000000000001",
                         headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    status, _, _ = _call(srv, "GET", "/api/measured?hours=1", headers={"Sec-Fetch-Site": "same-site"})
    assert status == 403
    status, _, _ = _call(srv, "GET", "/api/turn?tenant=anycompany-plumbing&id=demo-0000000000000001",
                         headers={"Sec-Fetch-Site": "same-origin"})
    assert status == 200
    assert fake.calls == []


def test_info_carries_the_eight_requirements_with_real_checks(server):
    from demo import checks

    srv, _ = server
    status, body, _ = _call(srv, "GET", "/api/info")
    info = json.loads(body)
    assert status == 200 and [row["number"] for row in info["requirements"]] == list(range(1, 9))
    for row in info["requirements"]:
        assert set(row["checks"]) <= set(checks.CHECKS)
        assert row["checks"] or row.get("evidence")  # every requirement shows a live check or its evidence


def test_diagrams_are_served_as_png_images(server):
    srv, _ = server
    status, body, csp = _call(srv, "GET", "/diagrams/split-harness.png")
    assert status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n"  # a real PNG, not a self-contained HTML page
    assert "img-src 'self'" in csp and "sandbox" not in csp  # an image needs no script sandbox
    _, info, _ = _call(srv, "GET", "/api/info")
    paths = [item["path"] for item in json.loads(info)["diagrams"]]
    assert paths == ["/diagrams/split-harness.png", "/diagrams/turn-isolation.png", "/diagrams/billed-time.png"]


@pytest.mark.parametrize("path", ["/diagrams/unknown.png", "/diagrams/split-harness.html",
                                  "/diagrams/split-harness", "/diagrams/../README.md",
                                  "/diagrams/%2e%2e/README.md", "/diagrams/src/split-harness.architecture.json"])
def test_unknown_diagram_paths_are_not_found(server, path):
    srv, _ = server
    status, _, _ = _call(srv, "GET", path)
    assert status == 404
