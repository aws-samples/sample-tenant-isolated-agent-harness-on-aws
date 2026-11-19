# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dead-letter queue consumer: a turn the queue gave up on ends as FAILED, not RUNNING.

The turn queue moves a message here after its third receive. That happens when every attempt
failed, but also when Lambda stopped the worker mid-turn, before the worker's own mark_failed ran,
so the request record can still say RUNNING or RETRYING and the status API would show it as in
progress forever. This marks it FAILED with an error code, then lets SQS delete the message.

It never touches a request that completed, and it checks the message against the request record
the same way the worker does, so a message ingress did not accept changes nothing.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

from shared import tenancy
from worker.coordination import Coordination

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

ABANDONED = "TURN_ABANDONED"
_clients: dict[str, Any] = {}


def _client(name: str) -> Any:
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def handler(event: dict, context: Any) -> dict:
    coordination = Coordination(_client("dynamodb"), os.environ["COORDINATION_TABLE"])
    failures = []
    for record in event.get("Records", []):
        try:
            outcome = settle(record, coordination)
            LOG.info(json.dumps({"event": "dead_letter_settled", "outcome": outcome,
                                 "messageId": record.get("messageId")}))
        except Exception:  # noqa: BLE001 - keep the message and let SQS redeliver it
            LOG.exception("dead letter not settled")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def settle(record: dict, coordination: Coordination) -> str:
    """Returns what happened: marked_failed, already_final, not_from_ingress or malformed."""
    try:
        body = json.loads(record["body"])
        tenant = tenancy.TenantContext(body["customerId"], body["storageId"], body["requestId"]).validate()
    except (KeyError, TypeError, ValueError):
        return "malformed"
    existing = coordination.get_request(tenant.storage_id, tenant.request_id)
    if not existing or existing.get("inputHash") != tenancy.input_hash(body.get("message"), body.get("source")):
        return "not_from_ingress"
    if existing.get("state") in ("COMPLETED", "FAILED"):
        outcome = "already_final"
    else:
        marked = coordination.mark_abandoned(tenant.storage_id, tenant.request_id, ABANDONED)
        outcome = "marked_failed" if marked else "already_final"
    # Either way the turn is over, so its open-request slot is free again.
    coordination.release_slot(tenant.storage_id, tenant.request_id)
    return outcome
