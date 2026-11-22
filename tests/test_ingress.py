# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json

import boto3
import pytest
from moto import mock_aws

from shared import tenancy


@pytest.fixture
def ingress(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ap-southeast-2")
        ddb.create_table(TableName="coord", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        sqs = boto3.client("sqs", region_name="ap-southeast-2")
        url = sqs.create_queue(QueueName="turns.fifo", Attributes={
            "FifoQueue": "true", "DeduplicationScope": "messageGroup", "FifoThroughputLimit": "perMessageGroupId"})["QueueUrl"]
        monkeypatch.setenv("COORDINATION_TABLE", "coord")
        monkeypatch.setenv("TURN_QUEUE_URL", url)
        monkeypatch.setenv("EVENT_PRODUCER_ROLE_ARNS",
                           "arn:aws:iam::111111111111:role/JobWebhookProducer,arn:aws:iam::111111111111:role/ops/Billing")
        from ingress import handler as module
        module._clients.clear()
        yield module, sqs, url, ddb
        module._clients.clear()


def jwt_event(route, tenant, body=None, path=None):
    claims = {"token_use": "id"}
    if tenant:
        claims["custom:tenant_id"] = tenant
    return {"routeKey": route, "body": json.dumps(body or {}), "pathParameters": path or {},
            "requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def _messages(sqs, url):
    return sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, AttributeNames=["All"]).get("Messages", [])


def test_turn_is_queued_in_the_authenticated_tenants_lane(ingress):
    module, sqs, url, _ = ingress
    response = module.handler(jwt_event("POST /turns", "anycompany-plumbing", {"message": "list my open jobs",
                                                                          "requestId": "req-00000001"}))
    assert response["statusCode"] == 202
    [msg] = _messages(sqs, url)
    body, sid = json.loads(msg["Body"]), tenancy.storage_id_for("anycompany-plumbing")
    assert body["storageId"] == sid and body["customerId"] == "anycompany-plumbing"
    assert msg["Attributes"]["MessageGroupId"] == sid
    assert msg["Attributes"]["MessageDeduplicationId"] == tenancy.fifo_deduplication_id(sid, "req-00000001")


def test_the_request_record_carries_the_hash_the_worker_checks(ingress):
    module, sqs, url, ddb = ingress
    module.handler(jwt_event("POST /turns", "anycompany-plumbing", {"message": "list my open jobs", "requestId": "req-00000004"}))
    [msg] = _messages(sqs, url)
    body = json.loads(msg["Body"])
    item = ddb.get_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(body["storageId"])},
                                                 "sk": {"S": tenancy.request_sk("req-00000004")}})["Item"]
    # The worker recomputes this from the queued body; any other message or source is refused.
    assert item["inputHash"]["S"] == tenancy.input_hash(body["message"], body["source"])


def test_body_tenant_that_disagrees_with_token_is_rejected(ingress):
    module, sqs, url, _ = ingress
    response = module.handler(jwt_event("POST /turns", "anycompany-plumbing",
                                        {"message": "hi", "customerId": "example-corp-electrical"}))
    assert response["statusCode"] == 403
    assert _messages(sqs, url) == []


def test_token_without_tenant_claim_is_rejected(ingress):
    module, *_ = ingress
    assert module.handler(jwt_event("POST /turns", None, {"message": "hi"}))["statusCode"] == 403


def test_get_turn_returns_the_error_code(ingress):
    module, sqs, url, ddb = ingress
    sid = tenancy.storage_id_for("anycompany-plumbing")
    ddb.put_item(TableName="coord", Item={"pk": {"S": tenancy.tenant_pk(sid)},
                                          "sk": {"S": tenancy.request_sk("req-00000060")},
                                          "state": {"S": "FAILED"}, "error": {"S": "MODEL_THROTTLED"},
                                          "updatedAt": {"N": "1"}})
    response = module.handler(jwt_event("GET /turns/{requestId}", "anycompany-plumbing", path={"requestId": "req-00000060"}))
    body = json.loads(response["body"])
    assert response["statusCode"] == 200 and body["state"] == "FAILED" and body["error"] == "MODEL_THROTTLED"


def test_an_access_token_is_rejected_even_with_a_tenant_claim(ingress):
    module, sqs, url, _ = ingress
    event = {"routeKey": "POST /turns", "body": json.dumps({"message": "hi", "requestId": "req-00000050"}),
             "pathParameters": {},
             "requestContext": {"authorizer": {"jwt": {"claims": {"token_use": "access",
                                                                  "custom:tenant_id": "anycompany-plumbing"}}}}}
    assert module.handler(event)["statusCode"] == 403
    assert _messages(sqs, url) == []


def test_idempotency_key_reuse(ingress):
    module, *_ = ingress
    first = {"message": "quote job 7", "requestId": "req-00000002"}
    assert module.handler(jwt_event("POST /turns", "anycompany-plumbing", first))["statusCode"] == 202
    assert module.handler(jwt_event("POST /turns", "anycompany-plumbing", first))["statusCode"] == 202
    clash = {"message": "something else", "requestId": "req-00000002"}
    assert module.handler(jwt_event("POST /turns", "anycompany-plumbing", clash))["statusCode"] == 409


def test_same_request_id_in_two_tenants_does_not_collide(ingress):
    module, sqs, url, _ = ingress
    for tenant in ("anycompany-plumbing", "example-corp-electrical"):
        module.handler(jwt_event("POST /turns", tenant, {"message": "hi", "requestId": "req-00000003"}))
    groups = {m["Attributes"]["MessageGroupId"] for m in _messages(sqs, url)}
    assert groups == {tenancy.storage_id_for("anycompany-plumbing"), tenancy.storage_id_for("example-corp-electrical")}


def test_status_reads_only_the_callers_partition(ingress):
    module, *_ = ingress
    module.handler(jwt_event("POST /turns", "anycompany-plumbing", {"message": "hi", "requestId": "req-00000004"}))
    mine = module.handler(jwt_event("GET /turns/{requestId}", "anycompany-plumbing", path={"requestId": "req-00000004"}))
    theirs = module.handler(jwt_event("GET /turns/{requestId}", "example-corp-electrical", path={"requestId": "req-00000004"}))
    assert mine["statusCode"] == 200 and json.loads(mine["body"])["state"] == "QUEUED"
    assert theirs["statusCode"] == 404  # indistinguishable from "does not exist"


def test_system_events_need_an_allowlisted_producer(ingress):
    module, sqs, url, _ = ingress
    def event(user_arn, rid="evt-00000001"):
        return {"routeKey": "POST /events", "body": json.dumps({"customerId": "anycompany-plumbing", "message": "job created",
                                                                 "requestId": rid}),
                "requestContext": {"authorizer": {"iam": {"userArn": user_arn}}}}
    assert module.handler(event("arn:aws:sts::111111111111:assumed-role/SomeOtherRole/x"))["statusCode"] == 403
    # Same role name in another account is now refused: the account, not just the name, must match.
    assert module.handler(event("arn:aws:sts::222222222222:assumed-role/JobWebhookProducer/x"))["statusCode"] == 403
    assert module.handler(event("arn:aws:sts::111111111111:assumed-role/JobWebhookProducer/x"))["statusCode"] == 202
    assert json.loads(_messages(sqs, url)[0]["Body"])["source"] == "event:JobWebhookProducer"
    # A role the allowlist gives with a path matches: the assumed-role ARN carries only the name.
    assert module.handler(event("arn:aws:sts::111111111111:assumed-role/Billing/x", "evt-00000002"))["statusCode"] == 202


PRODUCER = "JobWebhookProducer"


@pytest.mark.parametrize("caller", [
    # IAM user paths may contain ':' and '/', so a user can carry the producer's assumed-role text.
    f"arn:aws:iam::111111111111:user/:assumed-role/{PRODUCER}/evil",
    f"arn:aws:iam::111111111111:user/x/assumed-role/{PRODUCER}/evil",
    f"arn:aws:iam::111111111111:user/{PRODUCER}",
    f"arn:aws:sts::111111111111:federated-user/{PRODUCER}",
    "arn:aws:iam::111111111111:root",
    f"arn:aws:iam::111111111111:role/{PRODUCER}",                      # a role ARN, not a session
    f"arn:aws:iam::111111111111:assumed-role/{PRODUCER}/x",             # wrong service
    f"arn:aws:sts:ap-southeast-2:111111111111:assumed-role/{PRODUCER}/x",  # Region set
    f"arn:aws:sts::11111111111:assumed-role/{PRODUCER}/x",              # 11-digit account
    f"arn:aws:sts::1111111111111:assumed-role/{PRODUCER}/x",            # 13-digit account
    f"arn:aws:sts::{chr(0x661) * 12}:assumed-role/{PRODUCER}/x",        # non-ASCII (Arabic-Indic) digits
    f"arn:aws-cn:sts::111111111111:assumed-role/{PRODUCER}/x",          # other partition
    f"arn:aws:sts::111111111111:assumed-role/{PRODUCER}",               # no session
    f"arn:aws:sts::111111111111:assumed-role/{PRODUCER}/",              # empty session
    f"arn:aws:sts::111111111111:assumed-role/{PRODUCER}/x/y",           # extra segment
    f"arn:aws:sts::111111111111:assumed-role/{PRODUCER}/x:y",           # ':' in the session
    f"arn:aws:sts::111111111111:assumed-role/{PRODUCER}/x\n",           # trailing newline
    f" arn:aws:sts::111111111111:assumed-role/{PRODUCER}/x",            # leading space
    f"ARN:aws:sts::111111111111:assumed-role/{PRODUCER}/x",
    f"arn:aws:sts::111111111111:assumed-role/{PRODUCER}x/x",            # longer role name
    "arn:aws:sts::111111111111:assumed-role//x",
    "arn:aws:sts::111111111111:assumed-role/" + "R" * 65 + "/x",        # longer than IAM allows
    "", "arn", "not an arn",
])
def test_only_an_assumed_role_session_of_an_allowlisted_role_passes(ingress, caller):
    module, sqs, url, _ = ingress
    event = {"routeKey": "POST /events",
             "body": json.dumps({"customerId": "example-corp-electrical", "message": "job created",
                                 "requestId": "evt-00000003"}),
             "requestContext": {"authorizer": {"iam": {"userArn": caller}}}}
    assert module.handler(event)["statusCode"] == 403
    assert _messages(sqs, url) == []


def test_role_and_session_names_may_use_every_character_iam_allows(ingress, monkeypatch):
    module, sqs, url, _ = ingress
    # The allowlist is comma-separated, so a role name with a comma cannot be listed (it fails closed).
    name = "svc+=.@_-Producer"
    monkeypatch.setenv("EVENT_PRODUCER_ROLE_ARNS", f"arn:aws:iam::111111111111:role/team/{name}")
    event = {"routeKey": "POST /events",
             "body": json.dumps({"customerId": "anycompany-plumbing", "message": "job created",
                                 "requestId": "evt-00000004"}),
             "requestContext": {"authorizer": {"iam": {
                 "userArn": f"arn:aws:sts::111111111111:assumed-role/{name}/s+=,.@_-1"}}}}
    assert module.handler(event)["statusCode"] == 202
    assert json.loads(_messages(sqs, url)[0]["Body"])["source"] == f"event:{name}"
    assert module._principal_role_arn("arn:aws:sts::111111111111:assumed-role/a+=,.@_-b/s") == \
        "arn:aws:iam::111111111111:role/a+=,.@_-b"
    # Other partitions keep their own allowlist entries.
    assert module._principal_role_arn("arn:aws-us-gov:sts::111111111111:assumed-role/P/s") == \
        "arn:aws-us-gov:iam::111111111111:role/P"


def test_a_tenant_cannot_have_more_than_its_limit_of_requests_in_flight(ingress, monkeypatch):
    module, sqs, url, ddb = ingress
    monkeypatch.setattr(module, "MAX_OPEN_REQUESTS", 2)

    def submit(tenant, i):
        body = {"message": f"quote job {i}", "requestId": f"req-0000010{i}"}
        return module.handler(jwt_event("POST /turns", tenant, body))["statusCode"]

    assert [submit("anycompany-plumbing", i) for i in range(3)] == [202, 202, 429]
    assert submit("anycompany-plumbing", 0) == 202  # a replay of an accepted request takes no new slot
    assert submit("example-corp-electrical", 2) == 202  # other tenants have their own slots
    # A slot whose turn never freed it stops counting once it expires.
    sid = tenancy.storage_id_for("anycompany-plumbing")
    ddb.update_item(TableName="coord", Key={"pk": {"S": tenancy.tenant_pk(sid)}, "sk": {"S": tenancy.open_sk("req-00000100")}},
                    UpdateExpression="SET expiresAt = :past", ExpressionAttributeValues={":past": {"N": "1"}})
    assert submit("anycompany-plumbing", 2) == 202
