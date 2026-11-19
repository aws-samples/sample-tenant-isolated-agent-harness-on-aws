# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Authenticated ingress: turns one request into one queued, tenant-ordered agent turn.

Routes (API Gateway HTTP API, payload format 2.0):
  POST /turns              Cognito JWT. Tenant comes from the token's custom:tenant_id claim.
  GET  /turns/{requestId}  Cognito JWT. Reads only the caller's own tenant partition.
  POST /events             SigV4 (IAM). Internal event producers on an allowlist assert the tenant.

The tenant is never taken from the request body for user traffic. A body ``customerId`` that
disagrees with the authenticated tenant is rejected rather than silently ignored, so attempts
show up in logs and metrics.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared import tenancy

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

MAX_MESSAGE_BYTES = 32 * 1024
REQUEST_TTL_SECONDS = 7 * 24 * 3600
# A FIFO queue looks for available message groups only among its first 120,000 messages, so one
# tenant's backlog can hold up every other tenant. Each tenant gets this many requests in flight
# (queued or running); the worker frees a slot when the turn ends. A slot the worker never freed
# stops counting after SLOT_TTL_SECONDS.
MAX_OPEN_REQUESTS = int(os.environ.get("MAX_OPEN_REQUESTS", "20"))
SLOT_TTL_SECONDS = 6 * 3600
TENANT_CLAIM = "custom:tenant_id"

_clients: dict[str, Any] = {}


def _client(name: str) -> Any:
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(body)}


def handler(event: dict, context: Any = None) -> dict:
    route = event.get("routeKey")
    try:
        if route == "POST /turns":
            return submit_user_turn(event)
        if route == "GET /turns/{requestId}":
            return get_turn(event)
        if route == "POST /events":
            return submit_system_event(event)
        return _response(404, {"message": "not found"})
    except HttpError as err:
        LOG.warning(json.dumps({"route": route, "status": err.status, "reason": err.message}))
        return _response(err.status, {"message": err.message})
    except tenancy.TenancyError as err:
        return _response(400, {"message": str(err)})


def tenant_from_jwt(event: dict) -> str:
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    # Only the Cognito ID token carries custom:tenant_id. Reject an access token (which has no tenant
    # claim and a different audience shape) rather than relying on the missing claim to fail later.
    if claims.get("token_use") != "id":
        raise HttpError(403, "an ID token is required")
    customer_id = claims.get(TENANT_CLAIM)
    if not isinstance(customer_id, str) or not tenancy.CUSTOMER_ID_RE.match(customer_id):
        raise HttpError(403, "token carries no tenant")
    return customer_id


def _canonical_role_arn(arn: str) -> str | None:
    """Reduce a role ARN to arn:<partition>:iam::<account>:role/<name>, dropping any path.

    The IAM authorizer gives the caller as an STS assumed-role ARN, which carries no path, so the
    allowlist and the caller are compared on partition, account and role name. Comparing the name
    alone would let a same-named role in any account through.
    """
    parts = arn.split(":")
    if len(parts) < 6 or not parts[5].startswith("role/") or not parts[4].isdigit():
        return None
    return f"arn:{parts[1]}:iam::{parts[4]}:role/{parts[5].split('/')[-1]}"


def _principal_role_arn(caller: str) -> str | None:
    # arn:<partition>:sts::<account>:assumed-role/<RoleName>/<session>
    parts = caller.split(":")
    if len(parts) < 6 or ":assumed-role/" not in caller or not parts[4].isdigit():
        return None
    return f"arn:{parts[1]}:iam::{parts[4]}:role/{caller.split(':assumed-role/')[1].split('/')[0]}"


def _allowed_producer(event: dict) -> str:
    iam = (((event.get("requestContext") or {}).get("authorizer") or {}).get("iam")) or {}
    caller = _principal_role_arn(iam.get("userArn") or "")
    allowed = {c for c in (_canonical_role_arn(a) for a in os.environ.get("EVENT_PRODUCER_ROLE_ARNS", "").split(",")) if c}
    if caller is None or caller not in allowed:
        raise HttpError(403, "caller is not an allowed event producer")
    return caller.rsplit("/", 1)[1]  # the role name, for the source label


def _body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    if len(raw.encode()) > MAX_MESSAGE_BYTES + 4096:
        raise HttpError(413, "request too large")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HttpError(400, "body is not JSON") from exc
    if not isinstance(body, dict):
        raise HttpError(400, "body must be an object")
    return body


def submit_user_turn(event: dict) -> dict:
    customer_id = tenant_from_jwt(event)
    body = _body(event)
    claimed = body.get("customerId")
    if claimed is not None and claimed != customer_id:
        raise HttpError(403, "customerId does not match the authenticated tenant")
    return _enqueue(customer_id, body, source="user")


def submit_system_event(event: dict) -> dict:
    producer = _allowed_producer(event)
    body = _body(event)
    customer_id = body.get("customerId")
    if not isinstance(customer_id, str) or not tenancy.CUSTOMER_ID_RE.match(customer_id):
        raise HttpError(400, "customerId is required for system events")
    return _enqueue(customer_id, body, source=f"event:{producer}")


def _enqueue(customer_id: str, body: dict, *, source: str) -> dict:
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HttpError(400, "message is required")
    if len(message.encode()) > MAX_MESSAGE_BYTES:
        raise HttpError(413, "message too large")
    request_id = body.get("requestId") or uuid.uuid4().hex
    tenant = tenancy.TenantContext.from_customer(customer_id, request_id)
    input_hash = tenancy.input_hash(message, source)

    existing = _create_request_record(tenant, input_hash, source)
    if existing is not None:
        if existing.get("inputHash", {}).get("S") != input_hash:
            raise HttpError(409, "requestId was already used for a different request")
        state = existing.get("state", {}).get("S", "QUEUED")
        if state != "QUEUED":
            return _response(200, {"requestId": request_id, "state": state})
        # A replay of a still-queued request re-sends: covers a crash between the record write
        # and the enqueue. FIFO deduplication drops it if the original send succeeded.

    _client("sqs").send_message(
        QueueUrl=os.environ["TURN_QUEUE_URL"],
        MessageBody=json.dumps({
            "v": 1, "customerId": customer_id, "storageId": tenant.storage_id,
            "requestId": request_id, "message": message, "source": source,
            "submittedAt": int(time.time()),
        }),
        MessageGroupId=tenant.storage_id,
        MessageDeduplicationId=tenancy.fifo_deduplication_id(tenant.storage_id, request_id),
    )
    return _response(202, {"requestId": request_id, "state": "QUEUED"})


def _open_requests(storage_id: str, now: int) -> int:
    response = _client("dynamodb").query(
        TableName=os.environ["COORDINATION_TABLE"], Select="COUNT", ConsistentRead=True,
        KeyConditionExpression="pk = :pk AND begins_with(sk, :open)", FilterExpression="expiresAt > :now",
        ExpressionAttributeValues={":pk": {"S": tenancy.tenant_pk(storage_id)}, ":open": {"S": tenancy.OPEN_PREFIX},
                                   ":now": {"N": str(now)}})
    return response["Count"]


def _create_request_record(tenant: tenancy.TenantContext, input_hash: str, source: str) -> dict | None:
    """Create the request record and take one of the tenant's open-request slots. Returns the
    existing item instead if the requestId is already taken (a replay, which takes no slot).

    The count and the write are not one atomic step, so simultaneous submissions from one tenant
    can overshoot the limit by a few. It bounds a tenant's backlog; it is not a security control.
    """
    ddb, table, now = _client("dynamodb"), os.environ["COORDINATION_TABLE"], int(time.time())
    pk = {"S": tenancy.tenant_pk(tenant.storage_id)}
    key = {"pk": pk, "sk": {"S": tenancy.request_sk(tenant.request_id)}}
    existing = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    if existing:
        return existing
    if _open_requests(tenant.storage_id, now) >= MAX_OPEN_REQUESTS:
        raise HttpError(429, f"this business already has {MAX_OPEN_REQUESTS} requests in progress; "
                             "try again when one finishes")
    try:
        ddb.transact_write_items(TransactItems=[
            {"Put": {"TableName": table, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
                **key, "state": {"S": "QUEUED"}, "inputHash": {"S": input_hash}, "source": {"S": source},
                "createdAt": {"N": str(now)}, "updatedAt": {"N": str(now)},
                "expiresAt": {"N": str(now + REQUEST_TTL_SECONDS)}}}},
            {"Put": {"TableName": table, "Item": {
                "pk": pk, "sk": {"S": tenancy.open_sk(tenant.request_id)},
                "expiresAt": {"N": str(now + SLOT_TTL_SECONDS)}}}},
        ])
        return None
    except ClientError as err:
        if err.response["Error"]["Code"] != "TransactionCanceledException":
            raise
    # A simultaneous submission of the same requestId got there first: treat this one as a replay.
    item = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    if not item:
        raise HttpError(503, "could not record the request; try again")
    return item


def get_turn(event: dict) -> dict:
    customer_id = tenant_from_jwt(event)
    request_id = ((event.get("pathParameters") or {}).get("requestId")) or ""
    tenancy.require_request_id(request_id)
    storage_id = tenancy.storage_id_for(customer_id)
    item = _client("dynamodb").get_item(
        TableName=os.environ["COORDINATION_TABLE"],
        Key={"pk": {"S": tenancy.tenant_pk(storage_id)}, "sk": {"S": tenancy.request_sk(request_id)}},
        ConsistentRead=True,
    ).get("Item")
    if not item:
        # Same answer for "not yours" and "does not exist": no cross-tenant existence oracle.
        raise HttpError(404, "request not found")
    result = {"requestId": request_id, "state": item["state"]["S"],
              "updatedAt": int(item.get("updatedAt", {}).get("N", "0"))}
    if "resultText" in item:
        result["result"] = item["resultText"]["S"]
    if "toolTrace" in item:
        # Tool names only (no arguments or outputs): shows whether the turn stayed on the direct
        # Gateway path or started the isolated runtime.
        result["toolTrace"] = [t.get("S", "") for t in item["toolTrace"].get("L", [])]
    if "error" in item:
        result["error"] = item["error"]["S"]
    return _response(200, result)
