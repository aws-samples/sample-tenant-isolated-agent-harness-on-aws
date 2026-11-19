# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Gateway REQUEST interceptor: verify the tenant context, then bind tool calls to it.

Configured with ``passRequestHeaders: true`` so the ``X-Tenant-Context`` header reaches this
function. On ``tools/call`` it overwrites ``arguments.tenantId`` with the verified tenant, so a
Gateway target always receives a tenant that came from the authenticated request and never
from model output. Anything it cannot verify is rejected (fail closed) before a target runs.

IAM role: ``kms:VerifyMac`` on the tenant-context key, nothing else. It cannot mint tokens.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any

import boto3

from shared import tenant_token

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

TENANT_ARGUMENT = "tenantId"
_kms: Any = None
_verified: "OrderedDict[str, dict]" = OrderedDict()
_lock = threading.Lock()
_CACHE_MAX = 512


def _kms_client() -> Any:
    global _kms
    if _kms is None:
        _kms = boto3.client("kms")
    return _kms


def _verify(token: str | None) -> dict:
    """Verify with KMS once per token; warm containers reuse the result until it expires."""
    now = time.time()
    if token:
        with _lock:
            cached = _verified.get(token)
            if cached and cached["exp"] > now:
                _verified.move_to_end(token)
                return cached
    claims = tenant_token.verify(_kms_client(), os.environ["TENANT_TOKEN_KEY_ID"], token,
                                 audience=os.environ["TOKEN_AUDIENCE"], now=now)
    with _lock:
        _verified[token] = claims  # type: ignore[index]
        while len(_verified) > _CACHE_MAX:
            _verified.popitem(last=False)
    return claims


def _deny(body: dict, reason: str) -> dict:
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": {
        "statusCode": 403,
        "body": {"jsonrpc": "2.0", "id": body.get("id"),
                 "error": {"code": -32001, "message": f"tenant context rejected: {reason}"}},
    }}}


def handler(event: dict, context: Any = None) -> dict:
    request = ((event or {}).get("mcp") or {}).get("gatewayRequest") or {}
    body = request.get("body") or {}
    headers = {str(k).lower(): v for k, v in (request.get("headers") or {}).items()}
    try:
        claims = _verify(headers.get(tenant_token.HEADER_NAME.lower()))
    except tenant_token.TokenError as err:
        LOG.warning(json.dumps({"event": "tenant_context_rejected", "reason": str(err),
                                "method": body.get("method")}))
        return _deny(body, str(err))

    if body.get("method") == "tools/call":
        params = body.get("params") or {}
        arguments = dict(params.get("arguments") or {})
        supplied = arguments.get(TENANT_ARGUMENT)
        if supplied is not None and supplied != claims["tid"]:
            LOG.warning(json.dumps({"event": "tenant_argument_overridden", "tool": params.get("name"),
                                    "storageId": claims["tid"]}))
        arguments[TENANT_ARGUMENT] = claims["tid"]
        params["arguments"] = arguments
        body["params"] = params
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {"body": body}}}
