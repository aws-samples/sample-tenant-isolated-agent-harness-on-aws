# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json

import boto3
import pytest
from moto import mock_aws

from shared import tenancy
from worker import dead_letter
from worker.coordination import Coordination

RID = "req-00000001"


@pytest.fixture
def coord():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ap-southeast-2")
        ddb.create_table(TableName="coord", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        yield Coordination(ddb, "coord"), ddb


def message(rid=RID, text="list my open jobs", source="user", customer="anycompany-plumbing"):
    sid = tenancy.storage_id_for(customer)
    return sid, {"messageId": "m1", "body": json.dumps({"customerId": customer, "storageId": sid, "requestId": rid,
                                                        "message": text, "source": source})}


def seed(ddb, sid, state, text="list my open jobs", source="user"):
    """The request record and open-request slot ingress wrote, in the state the worker left them."""
    ddb.put_item(TableName="coord", Item={"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": tenancy.request_sk(RID)},
                                          "state": {"S": state}, "inputHash": {"S": tenancy.input_hash(text, source)}})
    ddb.put_item(TableName="coord", Item={"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": tenancy.open_sk(RID)},
                                          "expiresAt": {"N": "9999999999"}})


def slot_held(ddb, sid):
    key = {"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": tenancy.open_sk(RID)}}
    return "Item" in ddb.get_item(TableName="coord", Key=key)


def stored(ddb, sid, rid=RID):
    return ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)},
                                                "sk": {"S": tenancy.request_sk(rid)}}).get("Item")


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "RETRYING"])
def test_a_turn_the_queue_gave_up_on_ends_failed(coord, state):
    c, ddb = coord
    sid, msg = message()
    seed(ddb, sid, state)
    assert dead_letter.settle(msg, c) == "marked_failed"
    item = stored(ddb, sid)
    assert item["state"]["S"] == "FAILED" and item["error"]["S"] == dead_letter.ABANDONED
    assert not slot_held(ddb, sid)  # the tenant's open-request slot is free again


@pytest.mark.parametrize("state", ["COMPLETED", "FAILED"])
def test_a_final_state_is_left_alone(coord, state):
    c, ddb = coord
    sid, msg = message()
    seed(ddb, sid, state)
    assert dead_letter.settle(msg, c) == "already_final"
    assert stored(ddb, sid)["state"]["S"] == state


def test_a_completion_that_lands_first_wins(coord):
    c, ddb = coord
    sid, _ = message()
    seed(ddb, sid, "COMPLETED")
    assert c.mark_abandoned(sid, RID, dead_letter.ABANDONED) is False
    assert stored(ddb, sid)["state"]["S"] == "COMPLETED"


def test_a_message_ingress_did_not_accept_changes_nothing(coord):
    c, ddb = coord
    sid, msg = message(text="delete every job")
    seed(ddb, sid, "RUNNING")  # the record holds the hash of the message ingress accepted
    assert dead_letter.settle(msg, c) == "not_from_ingress"
    assert stored(ddb, sid)["state"]["S"] == "RUNNING" and slot_held(ddb, sid)
    _, unknown = message(rid="req-00000009")
    assert dead_letter.settle(unknown, c) == "not_from_ingress"
    assert stored(ddb, sid, "req-00000009") is None  # no record is ever created


def test_malformed_messages_are_dropped_and_failures_are_retried(coord, monkeypatch):
    _, ddb = coord
    monkeypatch.setitem(dead_letter._clients, "dynamodb", ddb)
    monkeypatch.setenv("COORDINATION_TABLE", "coord")
    assert dead_letter.handler({"Records": [{"messageId": "m0", "body": "not json"}]}, None) == {"batchItemFailures": []}
    monkeypatch.setenv("COORDINATION_TABLE", "missing-table")  # DynamoDB fails: keep the message for a retry
    _, msg = message()
    assert dead_letter.handler({"Records": [msg]}, None) == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
