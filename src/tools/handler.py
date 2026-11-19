# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo SaaS business tools behind AgentCore Gateway (a Lambda target).

``tenantId`` arrives in the event because the REQUEST interceptor injected it after verifying
the signed tenant context. The function still does not trust its own key-building: every
DynamoDB call uses a TenantToolsRole session tagged with that tenant, and the role only allows
items whose partition key is ``TENANT#${aws:PrincipalTag/TenantId}``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared import tenancy
from shared.tenant_session import TenantSessionCache

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_sessions: TenantSessionCache | None = None


def _tenant_dynamodb(storage_id: str) -> Any:
    global _sessions
    if _sessions is None:
        _sessions = TenantSessionCache(boto3.client("sts"), os.environ["TOOLS_ROLE_ARN"], "tools",
                                       os.environ["AWS_REGION"])
    return _sessions.get(storage_id).client("dynamodb")


def _tool_name(context: Any) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName", "")
    return name.split("___", 1)[1] if "___" in name else name


def _job(item: dict) -> dict:
    return {k: list(v.values())[0] for k, v in item.items() if k not in ("pk",)}


def handler(event: dict, context: Any) -> dict:
    storage_id = (event or {}).get("tenantId")
    if not tenancy.is_storage_id(storage_id):
        return {"error": "no verified tenant on this call"}
    ddb = _tenant_dynamodb(storage_id)
    table = os.environ["SAAS_TABLE"]
    pk = tenancy.tenant_pk(storage_id)
    tool = _tool_name(context)

    if tool == "list_jobs":
        response = ddb.query(TableName=table, KeyConditionExpression="pk = :pk AND begins_with(sk, :job)",
                             ExpressionAttributeValues={":pk": {"S": pk}, ":job": {"S": "JOB#"}}, Limit=50)
        jobs = [_job(i) for i in response.get("Items", [])]
        status = event.get("status")
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        return {"jobs": jobs}

    if tool == "get_job":
        job_id = str(event.get("jobId", ""))
        if not _JOB_ID.match(job_id):
            return {"error": "invalid jobId"}
        item = ddb.get_item(TableName=table, Key={"pk": {"S": pk}, "sk": {"S": f"JOB#{job_id}"}}).get("Item")
        return {"job": _job(item)} if item else {"error": "job not found"}

    if tool == "create_quote":
        job_id = str(event.get("jobId", ""))
        if not _JOB_ID.match(job_id):
            return {"error": "invalid jobId"}
        op_key = event.get("operationKey")
        if not tenancy.is_operation_key(op_key):
            # Mutating tools refuse calls without an operation key rather than risk a duplicate.
            return {"error": "missing operation key"}
        return _create_quote_once(ddb, table, pk, op_key, job_id, event)

    LOG.warning(json.dumps({"event": "unknown_tool", "tool": tool}))
    return {"error": f"unknown tool {tool}"}


def _create_quote_once(ddb: Any, table: str, pk: str, op_key: str, job_id: str, event: dict) -> dict:
    """Create the quote and its execution-ledger record in one transaction.

    SQS and Lambda deliver at least once, so the same tool call can arrive twice: after a worker
    crash, a lost Gateway response or a redelivered turn. The ledger record (OP#<key>) and the
    quote are written together or not at all, so there is no window in which the quote exists but
    its outcome is unknown. A repeat of the key reads the stored outcome instead of creating a
    second quote; that lookup is how this tool reconciles an unknown outcome.
    """
    quote_id = uuid.uuid4().hex[:12]
    outcome = {"quoteId": quote_id, "jobId": job_id}
    now = str(int(time.time()))
    try:
        ddb.transact_write_items(TransactItems=[
            {"Put": {"TableName": table, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
                "pk": {"S": pk}, "sk": {"S": tenancy.operation_sk(op_key)}, "tool": {"S": "create_quote"},
                "outcome": {"S": json.dumps(outcome)}, "createdAt": {"N": now}}}},
            {"Put": {"TableName": table, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
                "pk": {"S": pk}, "sk": {"S": f"QUOTE#{quote_id}"}, "jobId": {"S": job_id},
                "description": {"S": str(event.get("description", ""))[:2000]},
                "amountCents": {"N": str(int(event.get("amountCents", 0)))},
                "operationKey": {"S": op_key}, "createdAt": {"N": now}}}},
        ])
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        stored = ddb.get_item(TableName=table, ConsistentRead=True,
                              Key={"pk": {"S": pk}, "sk": {"S": tenancy.operation_sk(op_key)}}).get("Item")
        if not stored:
            raise
        LOG.info(json.dumps({"event": "operation_replayed", "tool": "create_quote"}))
        return {**json.loads(stored["outcome"]["S"]), "replayed": True}
    return outcome
