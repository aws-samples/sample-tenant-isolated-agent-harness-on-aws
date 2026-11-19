# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Drives a real Strands agent turn through the worker with a scripted model, moto S3/DynamoDB/STS,
a fake Gateway and a fake runtime. Checks the isolation properties of the control plane."""

import asyncio
import io
import json
import time
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from shared import tenancy, tenant_token
from tests.conftest import FakeKms

REGION = "ap-southeast-2"
GATEWAY_URL = "https://gw-1.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp"
AUD = "split-harness-tools-test01"


class ScriptedModel:
    """Minimal Strands model: replays a fixed list of assistant turns."""

    def __init__(self, turns: list[dict], fail_first: bool = False):
        self.turns, self.calls, self.fail_first = turns, [], fail_first

    def update_config(self, **kwargs: Any) -> None:
        pass

    def get_config(self) -> dict:
        return {}

    def structured_output(self, *args: Any, **kwargs: Any):
        raise NotImplementedError

    @property
    def stateful(self) -> bool:
        return False

    @property
    def context_window_limit(self) -> None:
        return None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append({"messages": json.loads(json.dumps(messages, default=str)),
                           "tools": [t["name"] for t in tool_specs or []]})
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("model unavailable")
        turn = self.turns[min(len(self.calls) - 1 - (1 if self.fail_first else 0), len(self.turns) - 1)]
        yield {"messageStart": {"role": "assistant"}}
        uses = turn.get("tools") or ([turn] if "tool" in turn else [])  # several tool calls in one message
        if uses:
            for use in uses:
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": use["id"], "name": use["tool"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(use["input"])}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": turn["text"]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                            "metrics": {"latencyMs": 1}}}


class FakeHttp(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeAgentCore:
    def __init__(self):
        self.invocations = []

    def invoke_agent_runtime(self, **kwargs):
        self.invocations.append({**kwargs, "payload": json.loads(kwargs["payload"])})
        return {"response": io.BytesIO(json.dumps({"status": "completed", "result": {
            "exitCode": 0, "stdout": "normalised 3 rows", "stderr": "", "workspace": {"uploaded": 1}}}).encode())}


class Context:
    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture
def harness(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="tenant-bucket", CreateBucketConfiguration={"LocationConstraint": REGION})
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(TableName="coord", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        for key, value in {"COORDINATION_TABLE": "coord", "TENANT_BUCKET": "tenant-bucket",
                           "HARNESS_ROLE_ARN": "arn:aws:iam::123456789012:role/TenantHarnessRole",
                           "RUNTIME_ROLE_ARN": "arn:aws:iam::123456789012:role/TenantRuntimeRole",
                           "TENANT_TOKEN_KEY_ID": "key", "GATEWAY_URL": GATEWAY_URL, "TOKEN_AUDIENCE": AUD,
                           "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:ap-southeast-2:123456789012:runtime/exec-1",
                           "MODEL_ID": "apac.amazon.nova-lite-v1:0"}.items():
            monkeypatch.setenv(key, value)

        from worker import agent as agent_module, gateway_client, handler
        kms, agentcore, gateway_calls = FakeKms(), FakeAgentCore(), []
        handler._clients.clear()
        handler._clients["kms"] = kms
        gateway_client._tools_cache.clear()

        def gateway(request, timeout):
            body = json.loads(request.data)
            gateway_calls.append(({k.lower(): v for k, v in request.header_items()}, body))
            if body["method"] == "tools/list":
                return FakeHttp({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{
                    "name": "saas___list_jobs", "description": "List jobs",
                    "inputSchema": {"type": "object", "properties": {"status": {"type": "string"},
                                                                     "tenantId": {"type": "string"}}}}]}})
            return FakeHttp({"jsonrpc": "2.0", "id": body["id"],
                             "result": {"content": [{"type": "text", "text": "{\"jobs\": [{\"id\": \"J7\"}]}"}]}})

        monkeypatch.setattr(gateway_client, "_default_opener", gateway)
        monkeypatch.setattr(handler, "agentcore_client", lambda session, region: agentcore)
        state = {"model": None}

        def build_with_scripted_model(**kwargs):
            return agent_module.build_agent(**kwargs, model=state["model"])

        monkeypatch.setattr(handler, "build_agent", build_with_scripted_model)
        yield handler, state, s3, ddb, kms, agentcore, gateway_calls
        handler._clients.clear()


TURNS = [
    {"id": "tool-1", "tool": "saas__list_jobs", "input": {"status": "open", "tenantId": "0" * 32}},
    {"id": "tool-2", "tool": "execute_process", "input": {"script": "echo normalise", "inputs": ["uploads/prices.csv"]}},
    {"text": "You have one open job, J7, and the price list is normalised."},
]


def _record(customer="anycompany-plumbing", rid="req-00000001", message="what's open, and normalise my prices"):
    sid = tenancy.storage_id_for(customer)
    return sid, {"messageId": "m1", "attributes": {"ApproximateReceiveCount": "1"},
                 "body": json.dumps({"customerId": customer, "storageId": sid, "requestId": rid, "message": message,
                                     "source": "user"})}


def _seed(ddb, record):
    """The request record and open-request slot ingress writes before it enqueues the message."""
    body = json.loads(record["body"])
    pk = {"S": tenancy.tenant_pk(body["storageId"])}
    ddb.put_item(TableName="coord", Item={
        "pk": pk, "sk": {"S": tenancy.request_sk(body["requestId"])},
        "state": {"S": "QUEUED"}, "inputHash": {"S": tenancy.input_hash(body["message"], body["source"])}})
    ddb.put_item(TableName="coord", Item={"pk": pk, "sk": {"S": tenancy.open_sk(body["requestId"])},
                                          "expiresAt": {"N": "9999999999"}})


def _slot_held(ddb, sid, rid):
    key = {"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": tenancy.open_sk(rid)}}
    return "Item" in ddb.get_item(TableName="coord", Key=key)


def test_turn_runs_entirely_under_the_tenants_session(harness):
    handler, state, s3, ddb, kms, agentcore, gateway_calls = harness
    sid, record = _record()
    _seed(ddb, record)
    state["model"] = model = ScriptedModel(TURNS)

    handler.process_record(record, Context())

    item = ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)},
                                               "sk": {"S": tenancy.request_sk("req-00000001")}})["Item"]
    assert item["state"]["S"] == "COMPLETED" and "J7" in item["resultText"]["S"]
    assert [t["S"] for t in item["toolTrace"]["L"]] == ["saas___list_jobs", "execute_process"]

    # The model saw model-safe tools with no tenant field.
    assert model.calls[0]["tools"] == ["saas__list_jobs", "execute_process"]

    # Conversation state landed only under this tenant's harness prefix.
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="tenant-bucket").get("Contents", [])]
    assert keys and all(k.startswith(f"tenants/{sid}/harness/") for k in keys)

    # Gateway calls: signed with the tenant session (not the worker's own credentials), carried a
    # verifiable tenant context, and never forwarded the model's tenantId.
    for headers, body in gateway_calls:
        assert "Credential=testing/" not in headers["authorization"]
        claims = tenant_token.verify(kms, "key", headers["x-tenant-context"], audience=AUD)
        assert claims["tid"] == sid and claims["rid"] == "req-00000001"
    [call] = [b for _, b in gateway_calls if b["method"] == "tools/call"]
    assert call["params"]["arguments"] == {"status": "open"}

    # The runtime task targets this tenant's session, prefixes and fresh tenant-scoped credentials.
    [invocation] = agentcore.invocations
    payload = invocation["payload"]
    assert invocation["runtimeSessionId"] == tenancy.runtime_session_id(sid)
    assert payload["customerStorageId"] == sid and payload["inputs"] == ["uploads/prices.csv"]
    assert payload["workspace"]["prefix"] == tenancy.Layout.workspace_prefix(sid)
    assert payload["workspace"]["credentials"]["accessKeyId"] not in ("testing", "")

    # The lease is released and a duplicate delivery is a no-op.
    lease = ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": tenancy.LEASE_SK}})["Item"]
    assert "owner" not in lease
    calls_before = len(model.calls)
    handler.process_record(record, Context())
    assert len(model.calls) == calls_before


def test_a_redelivered_turn_resumes_without_repeating_the_user_message(harness):
    handler, state, s3, ddb, *_ = harness
    sid, record = _record(rid="req-00000002", message="list open jobs")
    _seed(ddb, record)
    state["model"] = ScriptedModel([{"text": "You have one open job."}], fail_first=True)

    with pytest.raises(Exception):
        handler.process_record(record, Context())
    assert ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)},
                                               "sk": {"S": tenancy.request_sk("req-00000002")}})["Item"]["state"]["S"] == "RETRYING"
    assert _slot_held(ddb, sid, "req-00000002")  # a retry is still in flight

    record["attributes"]["ApproximateReceiveCount"] = "2"
    handler.process_record(record, Context())
    assert not _slot_held(ddb, sid, "req-00000002")  # freed when the turn completed
    final = state["model"].calls[-1]["messages"]
    user_texts = [b.get("text") for m in final if m["role"] == "user" for b in m["content"] if "text" in b]
    assert user_texts.count("list open jobs") == 1


class WorkerDied(BaseException):
    """Stands in for the Lambda being killed mid-tool. Strands only catches Exception, so this
    escapes the tool executor the way a timeout would, leaving the tool call without a result."""


def _state(ddb, sid, rid):
    return ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)},
                                               "sk": {"S": tenancy.request_sk(rid)}})["Item"]["state"]["S"]


def test_a_script_cut_off_by_a_crash_is_not_run_again_on_redelivery(harness, monkeypatch):
    handler, state, s3, ddb, kms, agentcore, gateway_calls = harness
    sid, record = _record(rid="req-00000003", message="normalise my prices")
    _seed(ddb, record)
    state["model"] = model = ScriptedModel([TURNS[1], {"text": "The earlier run may have finished; I checked."}])
    real_invoke = agentcore.invoke_agent_runtime

    def dies_mid_script(**kwargs):
        real_invoke(**kwargs)
        raise WorkerDied()

    monkeypatch.setattr(agentcore, "invoke_agent_runtime", dies_mid_script)
    with pytest.raises(WorkerDied):
        handler.process_record(record, Context())
    assert len(agentcore.invocations) == 1
    run_key = tenancy.run_sk(tenancy.operation_key(sid, "req-00000003", "tool-2"))
    run = ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": run_key}})["Item"]
    assert run["state"]["S"] == "STARTED"  # recorded before the script went to the runtime

    record["attributes"]["ApproximateReceiveCount"] = "2"
    handler.process_record(record, Context())

    assert len(agentcore.invocations) == 1, "the interrupted script must not run a second time"
    assert _state(ddb, sid, "req-00000003") == "COMPLETED"
    results = [b["toolResult"] for m in model.calls[-1]["messages"] if m["role"] == "user"
               for b in m["content"] if "toolResult" in b]
    assert results[-1]["toolUseId"] == "tool-2" and results[-1]["status"] == "error"
    assert "may already have run" in results[-1]["content"][0]["text"]


def test_a_gateway_lookup_cut_off_by_a_crash_is_run_again_on_redelivery(harness, monkeypatch):
    handler, state, s3, ddb, kms, agentcore, gateway_calls = harness
    from worker import gateway_client
    sid, record = _record(rid="req-00000004", message="what's open")
    _seed(ddb, record)
    state["model"] = ScriptedModel([TURNS[0], {"text": "You have one open job, J7."}])
    real_opener = gateway_client._default_opener
    died = []

    def dies_on_first_tool_call(request, timeout):
        if json.loads(request.data)["method"] == "tools/call" and not died:
            died.append(True)
            raise WorkerDied()
        return real_opener(request, timeout)

    monkeypatch.setattr(gateway_client, "_default_opener", dies_on_first_tool_call)
    with pytest.raises(WorkerDied):
        handler.process_record(record, Context())

    record["attributes"]["ApproximateReceiveCount"] = "2"
    handler.process_record(record, Context())

    assert _state(ddb, sid, "req-00000004") == "COMPLETED"
    assert len([b for _, b in gateway_calls if b["method"] == "tools/call"]) == 1  # the repeat ran


@pytest.mark.parametrize("tamper", ["no_record", "message", "source"])
def test_a_message_ingress_did_not_accept_is_dropped_unrun(harness, tamper):
    handler, state, s3, ddb, kms, agentcore, gateway_calls = harness
    sid, record = _record(rid="req-00000005", message="list open jobs")
    if tamper != "no_record":
        _seed(ddb, record)
        body = json.loads(record["body"])
        if tamper == "message":
            body["message"] = "delete every job"
        else:
            body["source"] = "event:SomeOtherProducer"
        record["body"] = json.dumps(body)
    state["model"] = model = ScriptedModel([{"text": "this turn must not run"}])

    handler.process_record(record, Context())  # returns normally, so SQS deletes it: no retries

    assert model.calls == [] and agentcore.invocations == [] and gateway_calls == []
    key = {"pk": {"S": tenancy.tenant_pk(sid)}}
    item = ddb.get_item(TableName="coord", Key={**key, "sk": {"S": tenancy.request_sk("req-00000005")}}).get("Item")
    if tamper == "no_record":
        assert item is None  # the worker never creates a record
    else:
        assert item["state"]["S"] == "QUEUED"  # the accepted request is left as it was
    assert "Item" not in ddb.get_item(TableName="coord", Key={**key, "sk": {"S": tenancy.LEASE_SK}})  # no lease taken


def test_a_quote_and_a_script_cut_off_together_settle_without_duplicates(harness, monkeypatch):
    """One message asks for a quote and a script; the worker dies during the script. On redelivery
    the quote repeats with the same operation key (the tool returns its stored outcome) instead of
    being answered "Not run", and the script is reported as possibly run instead of running twice."""
    handler, state, s3, ddb, kms, agentcore, gateway_calls = harness
    from worker import gateway_client
    quote_calls = []

    def gateway(request, timeout):
        body = json.loads(request.data)
        if body["method"] == "tools/list":
            return FakeHttp({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{
                "name": "saas___create_quote", "description": "Create a quote",
                "inputSchema": {"type": "object", "properties": {"jobId": {"type": "string"},
                                                                 "operationKey": {"type": "string"}}}}]}})
        quote_calls.append(body["params"]["arguments"])
        return FakeHttp({"jsonrpc": "2.0", "id": body["id"],
                         "result": {"content": [{"type": "text", "text": "{\"quoteId\": \"Q1\"}"}]}})

    monkeypatch.setattr(gateway_client, "_default_opener", gateway)
    sid, record = _record(rid="req-00000006", message="quote job 7 and normalise my prices")
    _seed(ddb, record)
    state["model"] = model = ScriptedModel([
        {"tools": [{"id": "tool-q", "tool": "saas__create_quote", "input": {"jobId": "J7"}}, TURNS[1]]},
        {"text": "Quote Q1 is ready; the price script may already have run, so I checked first."}])
    real_invoke = agentcore.invoke_agent_runtime

    def dies_mid_script(**kwargs):
        real_invoke(**kwargs)
        raise WorkerDied()

    monkeypatch.setattr(agentcore, "invoke_agent_runtime", dies_mid_script)
    with pytest.raises(WorkerDied):
        handler.process_record(record, Context())
    record["attributes"]["ApproximateReceiveCount"] = "2"
    handler.process_record(record, Context())

    key = tenancy.operation_key(sid, "req-00000006", "tool-q")
    assert quote_calls == [{"jobId": "J7", "operationKey": key}] * 2  # repeated under the same key
    assert len(agentcore.invocations) == 1, "the interrupted script must not run a second time"
    results = {b["toolResult"]["toolUseId"]: b["toolResult"] for m in model.calls[-1]["messages"]
               if m["role"] == "user" for b in m["content"] if "toolResult" in b}
    assert results["tool-q"]["status"] == "success" and "Q1" in results["tool-q"]["content"][0]["text"]
    assert results["tool-2"]["status"] == "error" and "may already have run" in results["tool-2"]["content"][0]["text"]
    assert _state(ddb, sid, "req-00000006") == "COMPLETED"


class SlowModel(ScriptedModel):
    """Streams a long answer slowly (5 s), as a stalled model call would."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append({"messages": json.loads(json.dumps(messages, default=str)), "tools": []})
        yield {"messageStart": {"role": "assistant"}}
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield {"contentBlockDelta": {"delta": {"text": "."}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 100, "totalTokens": 110},
                            "metrics": {"latencyMs": 5000}}}


def test_the_watchdog_cancels_a_turn_before_the_deadline_and_it_is_retried(harness, monkeypatch):
    handler, state, s3, ddb, *_ = harness
    from worker.agent import TurnCancelled
    sid, record = _record(rid="req-00000007", message="list open jobs")
    _seed(ddb, record)
    state["model"] = SlowModel([])
    # Context leaves 300 s and the deadline sits 15 s before the end: cancel half a second in.
    monkeypatch.setattr(handler, "CANCEL_MARGIN_SECONDS", 300 - 15 - 0.5)
    started = time.monotonic()
    with pytest.raises(TurnCancelled):
        handler.process_record(record, Context())
    assert time.monotonic() - started < 4  # stopped mid-stream, not after the 5 s answer
    assert _state(ddb, sid, "req-00000007") == "RETRYING"  # the normal retry path
    lease = ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": tenancy.LEASE_SK}})
    assert "owner" not in lease["Item"]

    # The redelivered turn resumes and finishes. Strands left a "Cancelled by user" placeholder as
    # the model's last reply; the model must see a history that ends with the user's message.
    monkeypatch.setattr(handler, "CANCEL_MARGIN_SECONDS", 60)
    state["model"] = model = ScriptedModel([{"text": "You have one open job."}])
    record["attributes"]["ApproximateReceiveCount"] = "2"
    handler.process_record(record, Context())
    assert _state(ddb, sid, "req-00000007") == "COMPLETED"
    assert model.calls[-1]["messages"][-1] == {"role": "user", "content": [{"text": "list open jobs"}]}


def test_a_stalled_model_call_ends_inside_the_watchdog_margin(harness):
    handler = harness[0]
    from worker import agent as agent_module
    tenant = tenancy.TenantContext.from_customer("anycompany-plumbing", "req-00000008")
    agent = agent_module.build_agent(tenant=tenant, harness_session=boto3.Session(region_name=REGION),
                                     bucket="tenant-bucket", model_id="apac.amazon.nova-lite-v1:0", region=REGION,
                                     tools=[])
    config = agent.model.client.meta.config
    assert config.read_timeout == agent_module.MODEL_READ_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == agent_module.MODEL_ATTEMPTS
    worst = agent_module.MODEL_ATTEMPTS * (agent_module.MODEL_CONNECT_TIMEOUT_SECONDS
                                           + agent_module.MODEL_READ_TIMEOUT_SECONDS)
    assert worst < handler.CANCEL_MARGIN_SECONDS
    assert agent_module.MODEL_MAX_BACKOFF_SECONDS + worst / 2 < handler.CANCEL_MARGIN_SECONDS


class ThrottledModel(ScriptedModel):
    """Throttles the first ``throttles`` model calls, then answers."""

    def __init__(self, turns: list[dict], throttles: int):
        super().__init__(turns)
        self.throttles = throttles

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.throttles:
            from strands.types.exceptions import ModelThrottledException
            self.throttles -= 1
            raise ModelThrottledException("Too many requests, please wait before trying again.")
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def test_a_throttled_model_call_backs_off_within_the_cap_and_the_turn_finishes(harness, monkeypatch):
    handler, state, s3, ddb, *_ = harness
    from strands.event_loop import _retry
    from worker import agent as agent_module
    sid, record = _record(rid="req-00000011", message="list open jobs")
    _seed(ddb, record)
    delays, real_sleep = [], asyncio.sleep

    async def recorded_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    # Only Strands' retry back-off is recorded and skipped; its cancel watcher still really sleeps.
    monkeypatch.setattr(_retry, "asyncio", SimpleNamespace(sleep=recorded_sleep))
    state["model"] = ThrottledModel([{"text": "You have one open job."}], throttles=5)
    handler.process_record(record, Context())
    assert _state(ddb, sid, "req-00000011") == "COMPLETED"
    # Five back-offs, doubling and held at the cap the watchdog margin is sized for.
    assert len(delays) == 5 and max(delays) == agent_module.MODEL_MAX_BACKOFF_SECONDS


def test_a_final_failure_frees_the_tenants_slot(harness):
    handler, state, s3, ddb, *_ = harness
    sid, record = _record(rid="req-00000009", message="list open jobs")
    _seed(ddb, record)
    state["model"] = ScriptedModel([{"text": "never reached"}], fail_first=True)
    record["attributes"]["ApproximateReceiveCount"] = str(handler.MAX_ATTEMPTS)
    with pytest.raises(Exception):
        handler.process_record(record, Context())
    assert _state(ddb, sid, "req-00000009") == "FAILED" and not _slot_held(ddb, sid, "req-00000009")


def _event_record(ddb, rid, message="a job was booked", producer="event:JobWebhookProducer"):
    sid = tenancy.storage_id_for("anycompany-plumbing")
    rec = {"messageId": "m1", "attributes": {"ApproximateReceiveCount": "1"},
           "body": json.dumps({"customerId": "anycompany-plumbing", "storageId": sid, "requestId": rid,
                               "message": message, "source": producer})}
    _seed(ddb, rec)
    return sid, rec


def test_event_turns_cannot_use_mutating_tools_or_scripts_unless_opted_in(harness, monkeypatch):
    handler, state, s3, ddb, kms, agentcore, gateway_calls = harness
    from worker import gateway_client

    def gateway(request, timeout):
        body = json.loads(request.data)
        if body["method"] == "tools/list":
            return FakeHttp({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [
                {"name": "saas___list_jobs", "description": "List jobs",
                 "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}},
                {"name": "saas___create_quote", "description": "Create a quote",
                 "inputSchema": {"type": "object", "properties": {"jobId": {"type": "string"}}}}]}})
        return FakeHttp({"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "{}"}]}})

    monkeypatch.setattr(gateway_client, "_default_opener", gateway)

    sid, rec = _event_record(ddb, "req-00000100")
    state["model"] = scoped = ScriptedModel([{"text": "Noted."}])
    handler.process_record(rec, Context())
    assert scoped.calls[0]["tools"] == ["saas__list_jobs"]  # no create_quote, no execute_process
    framed = scoped.calls[0]["messages"][0]["content"][0]["text"]
    assert framed.startswith("Automated event from event:JobWebhookProducer") and "a job was booked" in framed

    monkeypatch.setenv("EVENT_TURN_TOOLS", "all")
    _, rec = _event_record(ddb, "req-00000101")
    state["model"] = opted_in = ScriptedModel([{"text": "Done."}])
    handler.process_record(rec, Context())
    assert set(opted_in.calls[0]["tools"]) == {"saas__list_jobs", "saas__create_quote", "execute_process"}


def test_a_user_turn_keeps_every_tool_and_is_not_reframed(harness):
    handler, state, s3, ddb, *_ = harness
    sid, record = _record(rid="req-00000102", message="what's open")
    _seed(ddb, record)
    state["model"] = model = ScriptedModel([{"text": "One open job."}])
    handler.process_record(record, Context())
    assert "execute_process" in model.calls[0]["tools"]
    assert model.calls[0]["messages"][0]["content"][0]["text"] == "what's open"  # no event framing


def test_a_failed_turn_records_a_code_not_raw_error_text(harness):
    handler, state, s3, ddb, *_ = harness
    sid, record = _record(rid="req-00000200", message="list open jobs")
    _seed(ddb, record)
    state["model"] = ScriptedModel([{"text": "never reached"}], fail_first=True)  # raises RuntimeError
    with pytest.raises(Exception):
        handler.process_record(record, Context())
    item = ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)},
                                               "sk": {"S": tenancy.request_sk("req-00000200")}})["Item"]
    assert item["error"]["S"] == "INTERNAL"  # the code, not "RuntimeError: model unavailable"
    assert "RuntimeError" not in json.dumps(item)


def test_error_code_maps_each_failure_to_a_short_reason():
    from worker.agent import TurnCancelled
    from worker.gateway_client import GatewayError
    from worker.handler import error_code
    assert error_code(TurnCancelled("x")) == "TIMEOUT"
    assert error_code(type("ModelThrottledException", (Exception,), {})("slow down")) == "MODEL_THROTTLED"
    assert error_code(GatewayError("boom")) == "TOOL_FAILED"
    assert error_code(RuntimeError("anything else")) == "INTERNAL"
