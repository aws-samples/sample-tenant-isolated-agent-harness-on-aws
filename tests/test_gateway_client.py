# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import io
import json
import time
import urllib.error

import boto3
import pytest

from worker import gateway_client as gc

URL = "https://gw-123.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp"


class FakeResponse(io.BytesIO):
    def __init__(self, payload, content_type="application/json"):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Recorder:
    def __init__(self, responder):
        self.requests, self.timeouts, self._responder = [], [], responder

    def __call__(self, request, timeout):
        body = json.loads(request.data)
        self.requests.append((request, body))
        self.timeouts.append(timeout)
        return self._responder(body)


def client(opener, **kwargs):
    # Bandit B106: fake credentials for botocore's request signer; nothing is sent.
    session = boto3.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret",  # nosec B106
                            aws_session_token="token", region_name="ap-southeast-2")
    return gc.GatewayClient(URL, session, "ap-southeast-2", "payload.mac", opener=opener, **kwargs)


def test_requests_are_sigv4_signed_and_sign_the_tenant_header():
    rec = Recorder(lambda body: FakeResponse({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}}))
    client(rec).list_tools()
    request, body = rec.requests[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert body["method"] == "tools/list"
    assert headers["x-tenant-context"] == "payload.mac"
    assert headers["mcp-protocol-version"] == gc.MCP_PROTOCOL_VERSION
    assert "x-amz-security-token" in headers
    auth = headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256") and "/bedrock-agentcore/aws4_request" in auth
    assert "x-tenant-context" in auth.split("SignedHeaders=")[1].split(",")[0]


def test_event_stream_responses_are_parsed():
    def sse(body):
        stream = (f'data: {{"jsonrpc":"2.0","method":"notifications/progress"}}\n\n'
                  f'data: {json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "hi"}]}})}\n\n')
        return FakeResponse(stream.encode(), "text/event-stream")
    assert client(Recorder(sse)).call_tool("saas___get_job", {"jobId": "J1"})["content"][0]["text"] == "hi"


def test_jsonrpc_errors_raise():
    rec = Recorder(lambda body: FakeResponse({"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32001, "message": "tenant context rejected"}}))
    with pytest.raises(gc.GatewayError, match="tenant context rejected"):
        client(rec).call_tool("saas___get_job", {})


def test_aliases_are_model_safe_and_stable():
    assert gc.alias_for("saas___list_jobs") == "saas__list_jobs"
    long_name = "target___" + "x" * 100
    assert len(gc.alias_for(long_name)) == 64 and gc.alias_for(long_name) == gc.alias_for(long_name)
    assert gc.alias_for("weird.name/with spaces") == "weird_name_with_spaces"


def test_tenant_fields_are_hidden_from_the_model_and_stripped_from_calls():
    tools = [{"name": "saas___get_job", "description": "Get one job",
              "inputSchema": {"type": "object", "required": ["jobId"],
                              "properties": {"jobId": {"type": "string"}, "tenantId": {"type": "string"}}}}]
    seen = []

    def respond(body):
        seen.append(body["params"]["arguments"])
        return FakeResponse({"jsonrpc": "2.0", "id": body["id"],
                             "result": {"content": [{"type": "text", "text": "{\"job\": 1}"}]}})

    trace: list[str] = []
    [tool] = gc.model_safe_tools(client(Recorder(respond)), trace, tool_list=tools)
    spec = tool.tool_spec
    assert spec["name"] == "saas__get_job"
    assert "tenantId" not in spec["inputSchema"]["json"]["properties"]
    assert spec["inputSchema"]["json"]["required"] == ["jobId"]

    result = tool._tool_func({"toolUseId": "t1", "input": {"jobId": "J1", "tenantId": "someone-else"}})
    assert seen == [{"jobId": "J1"}]  # the model's tenantId never leaves the worker
    assert result["status"] == "success" and trace == ["saas___get_job"]


def test_mutating_calls_carry_a_derived_operation_key_the_model_cannot_set():
    from shared import tenancy
    tenant = tenancy.TenantContext.from_customer("anycompany-plumbing", "demo-00000001")
    tools = [{"name": "saas___create_quote", "description": "Create a quote",
              "inputSchema": {"type": "object", "required": ["jobId"],
                              "properties": {"jobId": {"type": "string"}, "tenantId": {"type": "string"},
                                             "operationKey": {"type": "string"}}}}]
    seen = []

    def respond(body):
        seen.append(body["params"]["arguments"])
        return FakeResponse({"jsonrpc": "2.0", "id": body["id"],
                             "result": {"content": [{"type": "text", "text": "{}"}]}})

    [tool] = gc.model_safe_tools(client(Recorder(respond)), [], tool_list=tools, tenant=tenant)
    assert "operationKey" not in tool.tool_spec["inputSchema"]["json"]["properties"]

    forged = {"toolUseId": "tooluse_1", "input": {"jobId": "J7", "operationKey": "f" * 32}}
    tool._tool_func(forged)
    tool._tool_func(forged)  # a redelivered turn replays the same toolUseId
    expected = tenancy.operation_key(tenant.storage_id, tenant.request_id, "tooluse_1")
    assert seen == [{"jobId": "J7", "operationKey": expected}] * 2

    [no_context] = gc.model_safe_tools(client(Recorder(respond)), [], tool_list=tools)
    result = no_context._tool_func({"toolUseId": "tooluse_2", "input": {"jobId": "J7"}})
    assert result["status"] == "error" and len(seen) == 2  # fails closed, nothing sent


def failing(*errors):
    """Responder that raises each error in turn, then answers."""
    pending = list(errors)

    def respond(body):
        if pending:
            raise pending.pop(0)
        return FakeResponse({"jsonrpc": "2.0", "id": body["id"],
                             "result": {"content": [{"type": "text", "text": "{\"quoteId\": \"Q1\"}"}]}})
    return respond


def http_error(code):
    return urllib.error.HTTPError(URL, code, "error", {}, io.BytesIO(b"upstream"))


def no_retry(seconds):
    pytest.fail("this call must not be retried")


def test_a_mutating_call_is_retried_with_the_same_operation_key():
    rec, sleeps = Recorder(failing(TimeoutError("timed out"), http_error(503))), []
    arguments = {"jobId": "J7", "operationKey": "k" * 32}
    result = client(rec, sleep=sleeps.append).call_tool("saas___create_quote", arguments, retry=True)
    assert result["content"][0]["text"] == '{"quoteId": "Q1"}'
    assert [body["params"]["arguments"] for _, body in rec.requests] == [arguments] * 3
    assert len(sleeps) == 2 and all(0 <= s <= 4 for s in sleeps)


@pytest.mark.parametrize("error", [TimeoutError("timed out"), urllib.error.URLError("reset"), http_error(502)])
def test_read_only_calls_are_not_retried(error):
    rec = Recorder(failing(error))
    with pytest.raises(gc.GatewayError) as err:
        client(rec, sleep=no_retry).call_tool("saas___get_job", {"jobId": "J1"})
    assert err.value.transient and len(rec.requests) == 1


def test_client_errors_are_not_retried():
    rec = Recorder(failing(http_error(400)))
    with pytest.raises(gc.GatewayError) as err:
        client(rec, sleep=no_retry).call_tool("saas___create_quote", {"jobId": "J7"}, retry=True)
    assert not err.value.transient and len(rec.requests) == 1


def test_retries_stop_at_the_turn_deadline(monkeypatch):
    monkeypatch.setattr(gc, "_backoff", lambda attempt: 0.3)
    rec = Recorder(failing(http_error(503), http_error(503)))
    # Time for one call, but not for a 0.3 s back-off plus another call.
    with pytest.raises(gc.GatewayError):
        client(rec, deadline=time.time() + 1.2, sleep=no_retry).call_tool("saas___create_quote", {"jobId": "J7"},
                                                                          retry=True)
    assert len(rec.requests) == 1


def test_the_model_is_told_an_unconfirmed_mutating_call_may_have_been_applied():
    from shared import tenancy
    tenant = tenancy.TenantContext.from_customer("anycompany-plumbing", "demo-00000001")
    tools = [{"name": "saas___create_quote",
              "inputSchema": {"type": "object", "properties": {"jobId": {"type": "string"}}}}]
    rec = Recorder(failing(*[TimeoutError("timed out")] * gc.MUTATING_ATTEMPTS))
    [tool] = gc.model_safe_tools(client(rec, sleep=lambda s: None), [], tool_list=tools, tenant=tenant)
    result = tool._tool_func({"toolUseId": "tooluse_1", "input": {"jobId": "J7"}})
    assert result["status"] == "error" and "may already have been applied" in result["content"][0]["text"]
    keys = {body["params"]["arguments"]["operationKey"] for _, body in rec.requests}
    assert len(rec.requests) == gc.MUTATING_ATTEMPTS and len(keys) == 1


def test_each_call_times_out_before_the_turns_deadline():
    rec = Recorder(failing())
    client(rec, deadline=time.time() + 12).call_tool("saas___get_job", {"jobId": "J1"})
    assert 10 < rec.timeouts[0] <= 12  # cut from the default 60 s to the time left
    client(rec).call_tool("saas___get_job", {"jobId": "J1"})
    assert rec.timeouts[1] == 60  # no deadline: the configured timeout
    with pytest.raises(gc.GatewayError, match="no time left"):
        client(rec, deadline=time.time()).call_tool("saas___get_job", {"jobId": "J1"})
    assert len(rec.requests) == 2  # nothing sent once the deadline has passed


@pytest.mark.parametrize("url", ["http://gw.example/mcp", "file:///etc/passwd"])
def test_the_gateway_url_must_use_https(url):
    # _default_opener relies on this check (Bandit B310).
    with pytest.raises(ValueError, match="https"):
        gc.GatewayClient(url, session=None, region="ap-southeast-2", tenant_context="t")
