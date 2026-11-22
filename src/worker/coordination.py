# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Coordination records in DynamoDB: request status, idempotency and a per-tenant lease.

The FIFO message group already serialises turns per tenant. The lease is a second guard: if a
visibility timeout expires while a turn is still running, the redelivered copy cannot start a
concurrent turn for the same tenant. A fence counter grows on every acquisition and is recorded on
the request; no writer checks it yet, so it does not yet reject a stale holder.

The table holds small control-plane records only. Conversation history, workspace files and
SaaS data live behind tenant-scoped sessions; the final reply text is kept here (bounded) so
the status API can return it.
"""

from __future__ import annotations

import time
from typing import Any

from botocore.exceptions import ClientError

from shared import tenancy

MAX_RESULT_CHARS = 32 * 1024
RUN_TTL_SECONDS = 7 * 24 * 3600  # as long as a request record


class LeaseBusy(Exception):
    """Another worker holds this tenant's lease; the message goes back to the queue."""


def _str(item: dict, name: str) -> str | None:
    value = item.get(name)
    return value.get("S") if value else None


class Coordination:
    """Only ingress creates request records. Every mark_* requires the record to exist, so the
    worker never creates one, whatever a queue message names."""

    def __init__(self, dynamodb: Any, table_name: str):
        self._ddb, self._table = dynamodb, table_name

    def _request_key(self, storage_id: str, request_id: str) -> dict:
        return {"pk": {"S": tenancy.tenant_pk(storage_id)}, "sk": {"S": tenancy.request_sk(request_id)}}

    def get_request(self, storage_id: str, request_id: str) -> dict | None:
        item = self._ddb.get_item(TableName=self._table, Key=self._request_key(storage_id, request_id),
                                  ConsistentRead=True).get("Item")
        if not item:
            return None
        return {"state": _str(item, "state"), "resultText": _str(item, "resultText"),
                "inputHash": _str(item, "inputHash")}

    def acquire_lease(self, storage_id: str, request_id: str, ttl_seconds: int) -> int:
        now = int(time.time())
        try:
            response = self._ddb.update_item(
                TableName=self._table,
                Key={"pk": {"S": tenancy.tenant_pk(storage_id)}, "sk": {"S": tenancy.LEASE_SK}},
                UpdateExpression=("SET #owner = :rid, leaseExpiresAt = :exp, updatedAt = :now, "
                                  "fence = if_not_exists(fence, :zero) + :one"),
                ConditionExpression="attribute_not_exists(#owner) OR leaseExpiresAt < :now OR #owner = :rid",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":rid": {"S": request_id}, ":exp": {"N": str(now + ttl_seconds)},
                                           ":now": {"N": str(now)}, ":zero": {"N": "0"}, ":one": {"N": "1"}},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise LeaseBusy(storage_id) from err
            raise
        return int(response["Attributes"]["fence"]["N"])

    def release_lease(self, storage_id: str, request_id: str) -> None:
        try:
            self._ddb.update_item(
                TableName=self._table,
                Key={"pk": {"S": tenancy.tenant_pk(storage_id)}, "sk": {"S": tenancy.LEASE_SK}},
                UpdateExpression="REMOVE #owner, leaseExpiresAt",
                ConditionExpression="#owner = :rid",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":rid": {"S": request_id}},
            )
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def _run_key(self, storage_id: str, op_key: str) -> dict:
        return {"pk": {"S": tenancy.tenant_pk(storage_id)}, "sk": {"S": tenancy.run_sk(op_key)}}

    def start_run(self, storage_id: str, op_key: str) -> str | None:
        """Record that a script is about to go to the runtime. Returns None for the first attempt,
        otherwise what an earlier attempt of the turn left: STARTED (cut off after sending, so it
        may have run) or COMPLETED (it ran, but its result was not saved)."""
        key, now = self._run_key(storage_id, op_key), int(time.time())
        try:
            self._ddb.put_item(TableName=self._table, ConditionExpression="attribute_not_exists(pk)",
                               Item={**key, "state": {"S": "STARTED"}, "startedAt": {"N": str(now)},
                                     "expiresAt": {"N": str(now + RUN_TTL_SECONDS)}})
            return None
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        item = self._ddb.get_item(TableName=self._table, Key=key, ConsistentRead=True).get("Item") or {}
        return _str(item, "state") or "STARTED"

    def finish_run(self, storage_id: str, op_key: str) -> None:
        self._ddb.update_item(TableName=self._table, Key=self._run_key(storage_id, op_key),
                              UpdateExpression="SET #state = :done, finishedAt = :now",
                              ConditionExpression="attribute_exists(pk)",
                              ExpressionAttributeNames={"#state": "state"},
                              ExpressionAttributeValues={":done": {"S": "COMPLETED"},
                                                         ":now": {"N": str(int(time.time()))}})

    def mark_running(self, storage_id: str, request_id: str, attempt: int, fence: int) -> None:
        self._update(storage_id, request_id, "SET #state = :state, attempts = :attempt, fence = :fence, updatedAt = :now",
                     {":state": {"S": "RUNNING"}, ":attempt": {"N": str(attempt)}, ":fence": {"N": str(fence)},
                      ":done": {"S": "COMPLETED"}},
                     condition="attribute_exists(pk) AND #state <> :done")

    def mark_completed(self, storage_id: str, request_id: str, text: str, tool_trace: list[str]) -> None:
        self._update(storage_id, request_id,
                     "SET #state = :state, resultText = :text, toolTrace = :trace, updatedAt = :now REMOVE #error",
                     {":state": {"S": "COMPLETED"}, ":text": {"S": (text or "")[:MAX_RESULT_CHARS] or " "},
                      ":trace": {"L": [{"S": t} for t in tool_trace[:100]]}},
                     condition="attribute_exists(pk)", names={"#error": "error"})

    def mark_failed(self, storage_id: str, request_id: str, error: str, final: bool) -> None:
        self._update(storage_id, request_id, "SET #state = :state, #error = :error, updatedAt = :now",
                     {":state": {"S": "FAILED" if final else "RETRYING"}, ":error": {"S": error[:1000] or "error"}},
                     condition="attribute_exists(pk)", names={"#error": "error"})

    def release_slot(self, storage_id: str, request_id: str) -> None:
        """Free the open-request slot ingress took for this request. Safe to repeat."""
        self._ddb.delete_item(TableName=self._table, Key={"pk": {"S": tenancy.tenant_pk(storage_id)},
                                                          "sk": {"S": tenancy.open_sk(request_id)}})

    def mark_abandoned(self, storage_id: str, request_id: str, code: str) -> bool:
        """Final FAILED for a turn the queue gave up on. Returns False, changing nothing, if the
        request completed in the meantime or has no record."""
        try:
            self._update(storage_id, request_id, "SET #state = :state, #error = :error, updatedAt = :now",
                         {":state": {"S": "FAILED"}, ":error": {"S": code}, ":done": {"S": "COMPLETED"}},
                         condition="attribute_exists(pk) AND #state <> :done", names={"#error": "error"})
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return False
        return True

    def _update(self, storage_id: str, request_id: str, expression: str, values: dict,
                condition: str | None = None, names: dict | None = None) -> None:
        kwargs: dict[str, Any] = dict(
            TableName=self._table, Key=self._request_key(storage_id, request_id),
            UpdateExpression=expression,
            ExpressionAttributeNames={"#state": "state", **(names or {})},
            ExpressionAttributeValues={**values, ":now": {"N": str(int(time.time()))}},
        )
        if condition:
            kwargs["ConditionExpression"] = condition
        if "#state" not in expression and "#state" not in (condition or ""):
            kwargs["ExpressionAttributeNames"].pop("#state")
        self._ddb.update_item(**kwargs)
