# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import time

import boto3
import pytest
from moto import mock_aws

from shared import tenancy
from worker.coordination import Coordination, LeaseBusy


@pytest.fixture
def coord():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ap-southeast-2")
        ddb.create_table(TableName="coord", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        yield Coordination(ddb, "coord"), ddb


def _seed(ddb, sid, rid):
    ddb.put_item(TableName="coord", Item={"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": tenancy.request_sk(rid)},
                                          "state": {"S": "QUEUED"}, "inputHash": {"S": "h"}})


def test_lease_is_exclusive_per_tenant_and_fenced(coord, tenants):
    c, _ = coord
    a, b = tenants.values()
    first = c.acquire_lease(a, "req-00000001", ttl_seconds=60)
    with pytest.raises(LeaseBusy):
        c.acquire_lease(a, "req-00000002", ttl_seconds=60)
    assert c.acquire_lease(b, "req-00000002", ttl_seconds=60) == 1  # other tenants are unaffected
    assert c.acquire_lease(a, "req-00000001", ttl_seconds=60) == first + 1  # same holder re-acquires
    c.release_lease(a, "req-00000002")  # not the holder: no effect
    with pytest.raises(LeaseBusy):
        c.acquire_lease(a, "req-00000003", ttl_seconds=60)
    c.release_lease(a, "req-00000001")
    assert c.acquire_lease(a, "req-00000003", ttl_seconds=60) == first + 2


def test_expired_lease_can_be_taken_over(coord, tenants):
    c, _ = coord
    a = tenants["anycompany-plumbing"]
    c.acquire_lease(a, "req-00000001", ttl_seconds=-5)
    time.sleep(0.01)
    assert c.acquire_lease(a, "req-00000002", ttl_seconds=60) == 2


def test_request_lifecycle(coord, tenants):
    c, ddb = coord
    a = tenants["anycompany-plumbing"]
    _seed(ddb, a, "req-00000001")
    c.mark_running(a, "req-00000001", attempt=1, fence=1)
    c.mark_failed(a, "req-00000001", "Throttling: slow down", final=False)
    assert c.get_request(a, "req-00000001")["state"] == "RETRYING"
    c.mark_running(a, "req-00000001", attempt=2, fence=2)
    c.mark_completed(a, "req-00000001", "Here are your open jobs", ["saas___list_jobs"])
    record = c.get_request(a, "req-00000001")
    assert record == {"state": "COMPLETED", "resultText": "Here are your open jobs", "inputHash": "h"}
    with pytest.raises(Exception, match="ConditionalCheckFailed"):
        c.mark_running(a, "req-00000001", attempt=3, fence=3)  # never re-run a completed turn


@pytest.mark.parametrize("mark", [
    lambda c, sid: c.mark_running(sid, "req-00000009", attempt=1, fence=1),
    lambda c, sid: c.mark_completed(sid, "req-00000009", "done", []),
    lambda c, sid: c.mark_failed(sid, "req-00000009", "boom", final=True),
], ids=["running", "completed", "failed"])
def test_no_mark_creates_a_request_record(coord, tenants, mark):
    c, _ = coord
    sid = tenants["anycompany-plumbing"]
    with pytest.raises(Exception, match="ConditionalCheckFailed"):
        mark(c, sid)
    assert c.get_request(sid, "req-00000009") is None  # only ingress creates request records


def test_a_script_run_is_recorded_before_it_is_sent_and_when_it_answers(coord, tenants):
    c, _ = coord
    a, b = tenants.values()
    key = tenancy.operation_key(a, "req-00000001", "tooluse_1")
    assert c.start_run(a, key) is None  # first attempt: send it
    assert c.start_run(a, key) == "STARTED"  # cut off after sending: it may have run
    c.finish_run(a, key)
    assert c.start_run(a, key) == "COMPLETED"  # it ran, but the result was not saved
    assert c.start_run(b, key) is None  # each tenant's partition is separate
