# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tenant-scoped AWS credentials from STS session tags (attribute-based access control).

One IAM role per access pattern serves every tenant. The caller assumes the role with a
``TenantId`` session tag; the role's policies reference ``${aws:PrincipalTag/TenantId}`` in
S3 key ARNs, KMS encryption-context conditions and DynamoDB leading keys. The number of IAM
roles therefore stays constant as the tenant count grows (no per-tenant roles, so no
roles-per-account quota), and a session minted for tenant A cannot touch tenant B's keys even
if application code builds the wrong key.

The role session name embeds the storage ID, so CloudTrail data events attribute every S3 or
Gateway call to a tenant.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

import boto3

from shared.tenancy import require_request_id, require_storage_id

TENANT_TAG = "TenantId"
MIN_DURATION_SECONDS = 900  # STS minimum
REFRESH_MARGIN_SECONDS = 300


def session_name(purpose: str, storage_id: str, request_id: str | None = None) -> str:
    """RoleSessionName: max 64 chars of [\\w+=,.@-]."""
    suffix = f"-{require_request_id(request_id)[:8]}" if request_id else ""
    return f"{purpose[:8]}-{require_storage_id(storage_id)}{suffix}"[:64]


def assume_tenant_credentials(sts: Any, role_arn: str, *, storage_id: str, purpose: str,
                              request_id: str | None = None,
                              duration_seconds: int = MIN_DURATION_SECONDS) -> dict:
    """Return raw STS credentials scoped to one tenant."""
    response = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name(purpose, storage_id, request_id),
        DurationSeconds=max(duration_seconds, MIN_DURATION_SECONDS),
        Tags=[{"Key": TENANT_TAG, "Value": require_storage_id(storage_id)}],
    )
    return response["Credentials"]


def boto3_session(credentials: dict, region: str) -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


def tenant_boto3_session(sts: Any, role_arn: str, *, storage_id: str, purpose: str,
                         region: str, request_id: str | None = None) -> boto3.Session:
    return boto3_session(
        assume_tenant_credentials(sts, role_arn, storage_id=storage_id, purpose=purpose,
                                  request_id=request_id),
        region,
    )


def wire_credentials(credentials: dict) -> dict:
    """JSON-safe form for handing credentials to the isolated runtime."""
    expiration = credentials["Expiration"]
    return {
        "accessKeyId": credentials["AccessKeyId"],
        "secretAccessKey": credentials["SecretAccessKey"],
        "sessionToken": credentials["SessionToken"],
        "expiration": expiration.isoformat() if hasattr(expiration, "isoformat") else str(expiration),
    }


class TenantSessionCache:
    """Small LRU of tenant sessions for high-rate callers (the Gateway tools Lambda).

    Keyed strictly by storage ID. Entries are refreshed five minutes before expiry, which
    keeps AssumeRole well under the 600 requests-per-second STS account quota.
    """

    def __init__(self, sts: Any, role_arn: str, purpose: str, region: str, max_entries: int = 256):
        self._sts, self._role_arn, self._purpose, self._region = sts, role_arn, purpose, region
        self._max = max_entries
        self._entries: OrderedDict[str, tuple[float, boto3.Session]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, storage_id: str) -> boto3.Session:
        require_storage_id(storage_id)
        now = time.time()
        with self._lock:
            entry = self._entries.get(storage_id)
            if entry and entry[0] - now > REFRESH_MARGIN_SECONDS:
                self._entries.move_to_end(storage_id)
                return entry[1]
        credentials = assume_tenant_credentials(self._sts, self._role_arn, storage_id=storage_id,
                                                purpose=self._purpose)
        expiration = credentials["Expiration"]
        expires_at = expiration.timestamp() if hasattr(expiration, "timestamp") else now + MIN_DURATION_SECONDS
        session = boto3_session(credentials, self._region)
        with self._lock:
            self._entries[storage_id] = (expires_at, session)
            self._entries.move_to_end(storage_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return session
