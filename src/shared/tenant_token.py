# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Signed tenant context for Gateway tool calls.

The harness Lambda mints one token per agent turn with AWS KMS ``GenerateMac`` (HMAC_SHA_256)
and sends it to AgentCore Gateway in the ``X-Tenant-Context`` header. A Gateway REQUEST
interceptor verifies it with ``VerifyMac`` and writes the verified tenant ID into the tool
arguments. The model never sees or controls the tenant ID.

Key separation: the harness role holds only ``kms:GenerateMac`` and the interceptor role holds
only ``kms:VerifyMac``, so the verifier cannot forge tokens and no secret ever leaves KMS.
HMAC keys count against the shared symmetric KMS request quota (10,000+ requests per second
per Region), not the much lower asymmetric signing quota.

Token format: ``base64url(canonical JSON claims) "." base64url(mac)``
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any

from shared.tenancy import is_storage_id, REQUEST_ID_RE

MAC_ALGORITHM = "HMAC_SHA_256"
HEADER_NAME = "X-Tenant-Context"
TOKEN_VERSION = 1
MAX_TOKEN_CHARS = 1024
MAX_TTL_SECONDS = 3600
CLOCK_LEEWAY_SECONDS = 30


class TokenError(Exception):
    """The tenant context token is missing, malformed, expired or not authentic."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise TokenError("token is not base64url") from exc


def mint(kms: Any, key_id: str, *, tenant_id: str, request_id: str, audience: str,
         ttl_seconds: int, now: float | None = None) -> str:
    if not is_storage_id(tenant_id):
        raise ValueError("tenant_id must be a storage id")
    if not REQUEST_ID_RE.match(request_id):
        raise ValueError("invalid request id")
    if not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError("ttl out of range")
    issued = int(now if now is not None else time.time())
    claims = {"v": TOKEN_VERSION, "tid": tenant_id, "rid": request_id, "aud": audience,
              "iat": issued, "exp": issued + ttl_seconds}
    payload = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    mac = kms.generate_mac(KeyId=key_id, MacAlgorithm=MAC_ALGORITHM, Message=payload.encode())["Mac"]
    return f"{payload}.{_b64url(mac)}"


def verify(kms: Any, key_id: str, token: str | None, *, audience: str,
           now: float | None = None) -> dict:
    """Return the verified claims or raise TokenError. Fails closed on any doubt."""
    if not token or not isinstance(token, str):
        raise TokenError("missing tenant context")
    if len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
        raise TokenError("malformed tenant context")
    payload, mac_text = token.split(".")
    mac = _b64url_decode(mac_text)
    try:
        result = kms.verify_mac(KeyId=key_id, MacAlgorithm=MAC_ALGORITHM,
                                Message=payload.encode(), Mac=mac)
    except Exception as exc:  # KMSInvalidMacException and friends: never authentic
        raise TokenError("tenant context is not authentic") from exc
    if not result.get("MacValid"):
        raise TokenError("tenant context is not authentic")
    try:
        claims = json.loads(_b64url_decode(payload))
    except ValueError as exc:
        raise TokenError("tenant context payload is not JSON") from exc
    current = now if now is not None else time.time()
    if claims.get("v") != TOKEN_VERSION:
        raise TokenError("unsupported tenant context version")
    if not is_storage_id(claims.get("tid")):
        raise TokenError("tenant context has no valid tenant")
    if not isinstance(claims.get("rid"), str) or not REQUEST_ID_RE.match(claims["rid"]):
        raise TokenError("tenant context has no valid request")
    if claims.get("aud") != audience:
        raise TokenError("tenant context audience mismatch")
    iat, exp = claims.get("iat"), claims.get("exp")
    if not isinstance(iat, int) or not isinstance(exp, int):
        raise TokenError("tenant context has no validity window")
    if exp - iat > MAX_TTL_SECONDS or iat > current + CLOCK_LEEWAY_SECONDS:
        raise TokenError("tenant context validity window rejected")
    if exp < current - CLOCK_LEEWAY_SECONDS:
        raise TokenError("tenant context expired")
    return claims
