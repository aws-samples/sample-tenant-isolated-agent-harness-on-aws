# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two Gateway checks pass only on the resource policy's refusal: HTTP 403, JSON-RPC -32002.

They drive the real GatewayClient through a fake transport, so a refusal reaches the checks the
same way as in a live run (HTTPError, then GatewayError). The interceptor tests at the end feed it
the exact contexts the checks send: a caller that got past the resource policy would still be
refused there.
"""

import base64
import io
import json
import urllib.error

import boto3
import pytest
from botocore.exceptions import ClientError

from demo import checks
from shared import tenancy
from worker import gateway_client as gc

URL = "https://gw-123.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp"
GATEWAY_CHECKS = ["gateway-no-context", "gateway-forged"]
POLICY_REFUSAL = {"error": {"code": -32002, "message": "Authorization error - Insufficient permissions"}}


def rpc(body: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, **body}


def http(code: int, body) -> urllib.error.HTTPError:
    raw = body if isinstance(body, bytes) else json.dumps(rpc(body) if "error" in body else body).encode()
    return urllib.error.HTTPError(URL, code, "error", {}, io.BytesIO(raw))


class Answer(io.BytesIO):
    def __init__(self, payload: dict):
        super().__init__(json.dumps(payload).encode())
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDemo:
    outputs = {"GatewayUrl": URL}
    region = "ap-southeast-2"
    storage_id = staticmethod(tenancy.storage_id_for)

    def __init__(self):
        # Bandit B106: fake credentials for botocore's request signer; nothing is sent.
        self.session = boto3.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret",  # nosec B106
                                     aws_session_token="token", region_name=self.region)


@pytest.fixture
def gateway(monkeypatch):
    """Run a check against a Gateway that raises `outcome` or answers HTTP 200 with it."""
    sent: list[dict] = []

    def run(check_id: str, outcome) -> dict:
        def opener(request, timeout):
            sent.append({k.lower(): v for k, v in request.header_items()})
            if isinstance(outcome, BaseException):
                raise outcome
            return Answer(rpc(outcome))
        monkeypatch.setattr(gc, "_default_opener", opener)
        return checks.run(FakeDemo(), check_id)

    run.sent = sent
    return run


@pytest.mark.parametrize("check_id", GATEWAY_CHECKS)
def test_the_resource_policy_refusal_passes(gateway, check_id):
    result = gateway(check_id, http(403, POLICY_REFUSAL))
    assert result["passed"] is True
    assert result["observed"].startswith("refused by the resource policy: Gateway HTTP 403")


def _interceptor(reason: str) -> dict:
    return {"error": {"code": -32001, "message": f"tenant context rejected: {reason}"}}


def _throttled_until_retries_ran_out() -> ClientError:
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"},
                        "ResponseMetadata": {"MaxAttemptsReached": True, "RetryAttempts": 4}}, "InvokeGateway")


NEARBY = {
    # The call reached the interceptor, so the resource policy let a caller other than the harness through.
    "interceptor: missing context": http(403, _interceptor("missing tenant context")),
    "interceptor: forged context": http(403, _interceptor("tenant context is not authentic")),
    # Other refusals that are not the resource policy's answer.
    "unrelated AccessDenied, not JSON-RPC": http(403, {"message": "User: arn:aws:sts::111122223333:assumed-role/"
                                                       "Other/s is not authorized to perform: "
                                                       "bedrock-agentcore:InvokeGateway"}),
    "signature mismatch": http(403, {"message": "The request signature we calculated does not match"}),
    "policy code with HTTP 401": http(401, POLICY_REFUSAL),
    "HTTP 403 with another code": http(403, {"error": {"code": -32003, "message": "Authorization error"}}),
    "policy code as a string": http(403, {"error": {"code": "-32002", "message": "Authorization error"}}),
    "HTTP 403 with a body that is not JSON": http(403, b"Forbidden"),
    "policy code in an HTTP 200 answer": POLICY_REFUSAL,
    # Validation errors.
    "HTTP 400 protocol version": http(400, {"error": {"code": -32600, "message": "Unsupported protocol version"}}),
    "HTTP 200 invalid params": {"error": {"code": -32602, "message": "Invalid params"}},
    # Transient: nothing was proven about the refusal.
    "HTTP 500": http(500, b"internal error"),
    "HTTP 503": http(503, b"unavailable"),
    "HTTP 429 throttled": http(429, POLICY_REFUSAL),
    "timeout": TimeoutError("timed out"),
    "network error": urllib.error.URLError("connection reset"),
    "retries exhausted": _throttled_until_retries_ran_out(),
    # No refusal at all.
    "accepted": {"result": {"tools": [{"name": "saas___list_jobs"}]}},
}


@pytest.mark.parametrize("check_id", GATEWAY_CHECKS)
@pytest.mark.parametrize("outcome", NEARBY.values(), ids=NEARBY.keys())
def test_any_other_answer_fails(gateway, check_id, outcome):
    result = gateway(check_id, outcome)
    assert result["passed"] is False
    assert not result["observed"].startswith("refused by the resource policy")


def test_each_check_sends_the_context_it_names(gateway):
    gateway("gateway-no-context", http(403, POLICY_REFUSAL))
    gateway("gateway-forged", http(403, POLICY_REFUSAL))
    no_context, forged = (headers["x-tenant-context"] for headers in gateway.sent)
    assert no_context == ""
    payload, mac = forged.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["tid"] == tenancy.storage_id_for("anycompany-plumbing")
    assert len(base64.urlsafe_b64decode(mac + "=" * (-len(mac) % 4))) == 32  # random, not a KMS MAC


@pytest.fixture
def interceptor(monkeypatch, kms):
    from interceptor import handler as module
    monkeypatch.setenv("TENANT_TOKEN_KEY_ID", "key")
    monkeypatch.setenv("TOKEN_AUDIENCE", "split-harness-tools-abc123")
    monkeypatch.setattr(module, "_kms", kms)
    module._verified.clear()
    return module


@pytest.mark.parametrize("context, reason", [(None, "missing tenant context"), ("", "missing tenant context"),
                                             ("forged", "tenant context is not authentic")])
def test_the_interceptor_refuses_what_the_checks_send(interceptor, context, reason):
    token = checks.forged_context(tenancy.storage_id_for("anycompany-plumbing")) if context == "forged" else context
    headers = {} if token is None else {"X-Tenant-Context": token}
    out = interceptor.handler({"mcp": {"gatewayRequest": {
        "headers": headers, "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}}}})
    response = out["mcp"]["transformedGatewayResponse"]
    assert response["statusCode"] == 403
    assert response["body"]["error"] == {"code": -32001, "message": f"tenant context rejected: {reason}"}


@pytest.mark.parametrize("url", ["http://api.example", "file:///etc/passwd"])
def test_the_demo_api_url_must_use_https(url):
    from demo import client

    demo = object.__new__(client.Demo)  # skip __init__: it reads the stack's outputs
    demo.outputs = {"ApiUrl": url}
    with pytest.raises(client.DemoError, match="https"):
        demo.api("GET", "/turns/demo-0000000000000001", None)
