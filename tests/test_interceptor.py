# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import time

import pytest

from shared import tenant_token

AUD = "split-harness-tools-abc123"


@pytest.fixture
def interceptor(monkeypatch, kms):
    from interceptor import handler as module
    monkeypatch.setenv("TENANT_TOKEN_KEY_ID", "key")
    monkeypatch.setenv("TOKEN_AUDIENCE", AUD)
    monkeypatch.setattr(module, "_kms", kms)
    module._verified.clear()
    return module


def _event(token, body, header="X-Tenant-Context"):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[header] = token
    return {"interceptorInputVersion": "1.0", "mcp": {"gatewayRequest": {"path": "/mcp", "httpMethod": "POST",
                                                                         "headers": headers, "body": body}}}


def _token(kms, sid):
    return tenant_token.mint(kms, "key", tenant_id=sid, request_id="req-00000001", audience=AUD,
                             ttl_seconds=600, now=time.time())


def test_tools_call_gets_verified_tenant(interceptor, kms, tenants):
    a = tenants["anycompany-plumbing"]
    out = interceptor.handler(_event(_token(kms, a), {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                                       "params": {"name": "saas___get_job", "arguments": {"jobId": "J1"}}}))
    body = out["mcp"]["transformedGatewayRequest"]["body"]
    assert body["params"]["arguments"] == {"jobId": "J1", "tenantId": a}


def test_model_supplied_tenant_is_overwritten(interceptor, kms, tenants):
    a, b = tenants.values()
    out = interceptor.handler(_event(_token(kms, a), {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                                       "params": {"name": "saas___list_jobs", "arguments": {"tenantId": b}}}))
    assert out["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]["tenantId"] == a


@pytest.mark.parametrize("token", [None, "", "garbage", "a.b"])
def test_missing_or_bad_context_is_denied_before_any_target(interceptor, token):
    out = interceptor.handler(_event(token, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                             "params": {"name": "saas___list_jobs", "arguments": {}}}))
    response = out["mcp"]["transformedGatewayResponse"]
    assert response["statusCode"] == 403
    assert response["body"]["error"]["code"] == -32001 and response["body"]["id"] == 3
    assert "transformedGatewayRequest" not in out["mcp"]


def test_header_lookup_is_case_insensitive_and_list_passes_through(interceptor, kms, tenants):
    token = _token(kms, tenants["anycompany-plumbing"])
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    out = interceptor.handler(_event(token, body, header="x-tenant-context"))
    assert out["mcp"]["transformedGatewayRequest"]["body"] == body


def test_verification_is_cached_per_token(interceptor, kms, tenants):
    token = _token(kms, tenants["anycompany-plumbing"])
    for _ in range(3):
        interceptor.handler(_event(token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert kms.calls.count("VerifyMac") == 1
