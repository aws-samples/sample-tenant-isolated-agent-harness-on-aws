# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Replay-safe side effects for the mutating demo tool."""

from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from shared import tenancy

TABLE = "saas"


def _context(tool: str) -> SimpleNamespace:
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": f"demo___{tool}"}))


@pytest.fixture
def ddb(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-southeast-2")
        client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                                  {"AttributeName": "sk", "AttributeType": "S"}],
                            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                       {"AttributeName": "sk", "KeyType": "RANGE"}])
        from tools import handler
        monkeypatch.setenv("SAAS_TABLE", TABLE)
        monkeypatch.setattr(handler, "_tenant_dynamodb", lambda storage_id: client)
        yield client


def _quotes(client, storage_id):
    items = client.query(TableName=TABLE, KeyConditionExpression="pk = :pk AND begins_with(sk, :q)",
                         ExpressionAttributeValues={":pk": {"S": tenancy.tenant_pk(storage_id)},
                                                    ":q": {"S": "QUOTE#"}})["Items"]
    return items


def _call(storage_id, op_key, job="J7"):
    from tools import handler
    event = {"tenantId": storage_id, "jobId": job, "description": "Hot water system", "amountCents": 185000}
    if op_key is not None:
        event["operationKey"] = op_key
    return handler.handler(event, _context("create_quote"))


def test_same_operation_key_creates_one_quote(ddb, tenants):
    sid = tenants["anycompany-plumbing"]
    key = tenancy.operation_key(sid, "demo-00000001", "tooluse_abc")
    first = _call(sid, key)
    second = _call(sid, key)
    assert second == {**first, "replayed": True}
    assert len(_quotes(ddb, sid)) == 1


def test_different_tool_calls_create_separate_quotes(ddb, tenants):
    sid = tenants["anycompany-plumbing"]
    _call(sid, tenancy.operation_key(sid, "demo-00000001", "tooluse_abc"))
    _call(sid, tenancy.operation_key(sid, "demo-00000001", "tooluse_def"))
    assert len(_quotes(ddb, sid)) == 2


def test_missing_or_malformed_key_is_refused(ddb, tenants):
    sid = tenants["anycompany-plumbing"]
    assert _call(sid, None) == {"error": "missing operation key"}
    assert _call(sid, "not-a-key") == {"error": "missing operation key"}
    assert _quotes(ddb, sid) == []


def test_operation_keys_are_tenant_bound(ddb, tenants):
    a, b = tenants["anycompany-plumbing"], tenants["example-corp-electrical"]
    assert tenancy.operation_key(a, "demo-00000001", "tooluse_abc") != tenancy.operation_key(b, "demo-00000001", "tooluse_abc")
    # Tenant B replaying tenant A's key lands in B's own partition: it creates B's quote and
    # learns nothing about A's outcome.
    key_a = tenancy.operation_key(a, "demo-00000001", "tooluse_abc")
    out_a = _call(a, key_a)
    out_b = _call(b, key_a)
    assert "replayed" not in out_b and out_b["quoteId"] != out_a["quoteId"]
    assert len(_quotes(ddb, a)) == 1 and len(_quotes(ddb, b)) == 1


def test_operation_key_is_stable_and_validated(tenants):
    sid = tenants["anycompany-plumbing"]
    key = tenancy.operation_key(sid, "demo-00000001", "tooluse_abc")
    assert key == tenancy.operation_key(sid, "demo-00000001", "tooluse_abc")
    assert tenancy.is_operation_key(key)
    with pytest.raises(tenancy.TenancyError):
        tenancy.operation_key(sid, "demo-00000001", "")
