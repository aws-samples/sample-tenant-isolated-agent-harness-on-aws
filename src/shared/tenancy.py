# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tenant identifiers and the tenant-partitioned storage layout.

Every stateful resource in the split harness is keyed by the tenant's storage ID: a
stable hash of the customer ID. The storage ID is an identifier, not a secret. It keeps
raw customer IDs out of S3 keys, spreads keys across S3 partitions, and is the value
carried in the ``TenantId`` STS session tag that the IAM policies match against.

A tenant is one customer organisation (one trades business), not one employee.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

STORAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")

_NAMESPACE = "split-harness:v1:"

# AgentCore Runtime requires runtimeSessionId to be at least 33 characters.
RUNTIME_SESSION_PREFIX = "tenant-"

# DynamoDB key conventions (coordination table and the demo SaaS table).
LEASE_SK = "LEASE"


class TenancyError(ValueError):
    """Raised when a tenant or request identifier is malformed."""


def storage_id_for(customer_id: str) -> str:
    """Return the 32-hex-character storage ID for a customer ID."""
    if not isinstance(customer_id, str) or not CUSTOMER_ID_RE.match(customer_id):
        raise TenancyError("invalid customer id")
    return hashlib.sha256((_NAMESPACE + customer_id).encode()).hexdigest()[:32]


def is_storage_id(value: object) -> bool:
    return isinstance(value, str) and bool(STORAGE_ID_RE.match(value))


def require_storage_id(value: object) -> str:
    if not is_storage_id(value):
        raise TenancyError("invalid storage id")
    return value  # type: ignore[return-value]


def require_request_id(value: object) -> str:
    if not isinstance(value, str) or not REQUEST_ID_RE.match(value):
        raise TenancyError("invalid request id")
    return value


def runtime_session_id(storage_id: str) -> str:
    """One stable logical AgentCore Runtime session per tenant (39 characters)."""
    return RUNTIME_SESSION_PREFIX + require_storage_id(storage_id)


def storage_id_from_runtime_session(session_id: str) -> str | None:
    if not isinstance(session_id, str) or not session_id.startswith(RUNTIME_SESSION_PREFIX):
        return None
    candidate = session_id[len(RUNTIME_SESSION_PREFIX):]
    return candidate if is_storage_id(candidate) else None


def fifo_deduplication_id(storage_id: str, request_id: str) -> str:
    """Tenant-qualified FIFO deduplication ID.

    A bare requestId collides across tenants when deduplication is queue-scoped: tenant B's
    turn would be silently dropped if tenant A used the same idempotency key within the
    five-minute window. The queue also uses message-group deduplication scope; this keeps the
    ID safe even if that setting changes.
    """
    return hashlib.sha256(f"{require_storage_id(storage_id)}:{require_request_id(request_id)}".encode()).hexdigest()


def tenant_pk(storage_id: str) -> str:
    return "TENANT#" + require_storage_id(storage_id)


OPERATION_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


def operation_key(storage_id: str, request_id: str, tool_use_id: str) -> str:
    """Stable key for one mutating tool call: the same turn retried gives the same key.

    Derived from requestId and toolUseId. The storage ID is folded in so a key can never name
    another tenant's operation, even though the ledger already lives in the tenant's own partition.
    """
    if not isinstance(tool_use_id, str) or not 1 <= len(tool_use_id) <= 128:
        raise TenancyError("invalid tool use id")
    material = f"{require_storage_id(storage_id)}:{require_request_id(request_id)}:{tool_use_id}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def is_operation_key(value: object) -> bool:
    return isinstance(value, str) and bool(OPERATION_KEY_RE.match(value))


def operation_sk(key: str) -> str:
    if not is_operation_key(key):
        raise TenancyError("invalid operation key")
    return "OP#" + key


def run_sk(key: str) -> str:
    """Coordination-table record of one execute_process call, keyed by its operation key."""
    if not is_operation_key(key):
        raise TenancyError("invalid operation key")
    return "RUN#" + key


def request_sk(request_id: str) -> str:
    return "REQ#" + require_request_id(request_id)


OPEN_PREFIX = "OPEN#"


def open_sk(request_id: str) -> str:
    """One of the tenant's open-request slots, held from enqueue until the turn ends."""
    return OPEN_PREFIX + require_request_id(request_id)


def input_hash(message: object, source: object) -> str:
    """Hash of what ingress accepted for one turn: the message and who sent it.

    Ingress stores it on the request record before it enqueues the turn. The worker recomputes it
    from the queued message and refuses a message that doesn't match, so a turn runs only as
    ingress accepted it.
    """
    return hashlib.sha256(json.dumps({"m": message, "s": source}, sort_keys=True).encode()).hexdigest()


class Layout:
    """S3 key layout in the shared tenant bucket.

    tenants/{sid}/harness/    Strands session: conversation and agent state (harness only)
    tenants/{sid}/workspace/  durable execution workspace (runtime read/write)
    tenants/{sid}/uploads/    validated user uploads (read-only to the runtime)
    transfer/{sid}/{rid}/     large script/result exchange; expired by a lifecycle rule
    """

    TENANTS = "tenants"
    TRANSFER = "transfer"

    @staticmethod
    def harness_prefix(storage_id: str) -> str:
        return f"tenants/{require_storage_id(storage_id)}/harness"

    @staticmethod
    def workspace_prefix(storage_id: str) -> str:
        return f"tenants/{require_storage_id(storage_id)}/workspace/"

    @staticmethod
    def uploads_prefix(storage_id: str) -> str:
        return f"tenants/{require_storage_id(storage_id)}/uploads/"

    @staticmethod
    def transfer_prefix(storage_id: str, request_id: str) -> str:
        return f"transfer/{require_storage_id(storage_id)}/{require_request_id(request_id)}/"


@dataclass(frozen=True)
class TenantContext:
    """Tenant context for one agent turn, derived from the authenticated request."""

    customer_id: str
    storage_id: str
    request_id: str

    @classmethod
    def from_customer(cls, customer_id: str, request_id: str) -> "TenantContext":
        return cls(customer_id, storage_id_for(customer_id), require_request_id(request_id))

    def validate(self) -> "TenantContext":
        require_storage_id(self.storage_id)
        require_request_id(self.request_id)
        if storage_id_for(self.customer_id) != self.storage_id:
            raise TenancyError("storage id does not match customer id")
        return self
