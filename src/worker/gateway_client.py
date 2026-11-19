# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Gateway client for the shared harness, and model-safe tool wrappers.

Gateway calls go straight from the worker to the IAM-authenticated Gateway. They are signed
with the turn's tenant-scoped session (only TenantHarnessRole may call ``InvokeGateway``) and
carry the KMS-signed tenant context in ``X-Tenant-Context``. The REQUEST interceptor verifies
that header and injects ``tenantId`` into the tool arguments.

"Model-safe" means two things here:
  * tool names are sanitised to the model's tool-name rules (Gateway names look like
    ``target___tool``), with a stable alias -> Gateway name map; and
  * tenant-controlled fields (``tenantId``) are removed from the schema the model sees and
    stripped from any arguments the model sends anyway. The interceptor overwrites them too.

This is a deliberately small JSON-RPC client (tools/list, tools/call over HTTPS POST) so the
worker has no background event loop or MCP session to manage inside Lambda.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from shared import tenancy, tenant_token

RESERVED_ARGUMENTS = frozenset({"tenantId", "operationKey"})
# Tools with side effects. Each call carries an operation key derived from requestId and
# toolUseId, so a retried turn repeats the same key and the tool returns its stored outcome.
MUTATING_TOOLS = frozenset({"create_quote"})
# A mutating call that times out or gets a 5xx may still have gone through, and the model would
# retry it under a new toolUseId, so a new operation key. The client retries it itself instead,
# with the same arguments and so the same key: the tool returns its stored outcome.
MUTATING_ATTEMPTS = 3
SIGNING_SERVICE = "bedrock-agentcore"
# Must be one of the gateway's protocolConfiguration.mcp.supportedVersions.
# Must be a version the Gateway lists as supported: by default AgentCore Gateway accepts only
# 2025-03-26 and rejects others with JSON-RPC -32600 "Unsupported protocol version" (observed on
# the deployed stack). Change it together with the Gateway's protocol configuration.
MCP_PROTOCOL_VERSION = "2025-03-26"
_ALIAS_INVALID = re.compile(r"[^A-Za-z0-9_-]")
_TOOLS_CACHE_SECONDS = 300
_tools_cache: dict[str, tuple[float, list[dict]]] = {}
_tools_cache_lock = threading.Lock()


class GatewayError(Exception):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        # Timeouts, network errors, 429 and 5xx: the call may or may not have reached the tool.
        self.transient = transient


_MIN_CALL_SECONDS = 1.0  # no call starts with less time than this left in the turn


def _backoff(attempt: int) -> float:
    """Seconds before retry number `attempt`: exponential from 0.5 s, capped, with full jitter."""
    return random.uniform(0, min(4.0, 0.5 * 2 ** (attempt - 1)))


def _default_opener(request: urllib.request.Request, timeout: float):
    # Bandit B310: GatewayClient accepts only an https URL (checked in __init__).
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  # nosec B310


def parse_response(raw: bytes, content_type: str, request_id: int) -> dict:
    """Parse a JSON or SSE (text/event-stream) JSON-RPC response."""
    if "text/event-stream" in (content_type or ""):
        found: dict | None = None
        for line in raw.decode(errors="replace").splitlines():
            if line.startswith("data:"):
                try:
                    message = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    found = message
        if found is None:
            raise GatewayError("no JSON-RPC response in event stream")
        return found
    message = json.loads(raw or b"{}")
    if not isinstance(message, dict):
        raise GatewayError("unexpected JSON-RPC response")
    return message


class GatewayClient:
    def __init__(self, url: str, session: Any, region: str, tenant_context: str,
                 timeout: float = 60.0, opener: Callable | None = None, deadline: float | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not url.startswith("https://"):
            raise ValueError("Gateway URL must be https")
        self._url = url if url.rstrip("/").endswith("/mcp") else url.rstrip("/") + "/mcp"
        self._session, self._region, self._token = session, region, tenant_context
        self._timeout, self._opener = timeout, opener or _default_opener
        self._deadline, self._sleep = deadline, sleep
        self._next_id = 0

    @property
    def url(self) -> str:
        return self._url

    def _call_timeout(self) -> float:
        """The configured timeout, cut to the time left before the turn's deadline."""
        if self._deadline is None:
            return self._timeout
        left = self._deadline - time.time()
        if left < _MIN_CALL_SECONDS:
            raise GatewayError("no time left in this turn for a Gateway call")
        return min(self._timeout, left)

    def signed_request(self, body: dict) -> urllib.request.Request:
        data = json.dumps(body).encode()
        request = AWSRequest(method="POST", url=self._url, data=data, headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            tenant_token.HEADER_NAME: self._token,
        })
        credentials = self._session.get_credentials().get_frozen_credentials()
        SigV4Auth(credentials, SIGNING_SERVICE, self._region).add_auth(request)
        prepared = request.prepare()
        return urllib.request.Request(prepared.url, data=data, method="POST", headers=dict(prepared.headers))

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params
        timeout = self._call_timeout()
        try:
            with self._opener(self.signed_request(body), timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as err:
            detail = err.read()[:500].decode(errors="replace")
            raise GatewayError(f"Gateway HTTP {err.code}: {detail}",
                               transient=err.code == 429 or err.code >= 500) from err
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as err:
            raise GatewayError(f"Gateway unreachable: {type(err).__name__}: {err}", transient=True) from err
        message = parse_response(raw, content_type, self._next_id)
        if "error" in message:
            raise GatewayError(str((message["error"] or {}).get("message", "Gateway error")))
        return message.get("result") or {}

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(50):
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict, *, retry: bool = False) -> dict:
        """Call one tool. With retry (mutating tools, which carry an operation key), a transient
        failure is retried with the same arguments, a bounded number of times and never past the
        turn's deadline. Read-only calls are not retried: the model can simply call them again."""
        attempts = MUTATING_ATTEMPTS if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                return self.rpc("tools/call", {"name": name, "arguments": arguments})
            except GatewayError as err:
                delay = _backoff(attempt)
                out_of_time = (self._deadline is not None
                               and time.time() + delay + _MIN_CALL_SECONDS > self._deadline)
                if not err.transient or attempt == attempts or out_of_time:
                    raise
                self._sleep(delay)
        raise AssertionError("unreachable")


def cached_tool_list(gateway: GatewayClient) -> list[dict]:
    """Tool definitions are the same for every tenant, so cache them per warm container."""
    now = time.time()
    with _tools_cache_lock:
        hit = _tools_cache.get(gateway.url)
        if hit and now - hit[0] < _TOOLS_CACHE_SECONDS:
            return hit[1]
    tools = gateway.list_tools()
    with _tools_cache_lock:
        _tools_cache[gateway.url] = (now, tools)
    return tools


def alias_for(gateway_name: str) -> str:
    alias = _ALIAS_INVALID.sub("_", gateway_name.replace("___", "__"))
    if len(alias) > 64:
        alias = alias[:55] + "_" + hashlib.sha256(gateway_name.encode()).hexdigest()[:8]
    return alias


def strip_reserved_schema(schema: dict | None) -> dict:
    schema = json.loads(json.dumps(schema or {"type": "object", "properties": {}}))
    properties = schema.get("properties") or {}
    for name in RESERVED_ARGUMENTS:
        properties.pop(name, None)
    schema["properties"] = properties
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r not in RESERVED_ARGUMENTS]
    schema.setdefault("type", "object")
    return schema


def strip_reserved_arguments(arguments: dict | None) -> dict:
    return {k: v for k, v in (arguments or {}).items() if k not in RESERVED_ARGUMENTS}


def _tool_result(tool_use_id: str, result: dict) -> dict:
    content = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            content.append({"text": block.get("text", "")})
        else:
            content.append({"text": json.dumps(block)})
    if not content and "structuredContent" in result:
        content.append({"text": json.dumps(result["structuredContent"])})
    return {"toolUseId": tool_use_id, "status": "error" if result.get("isError") else "success",
            "content": content or [{"text": ""}]}


def model_safe_tools(gateway: GatewayClient, trace: list[str],
                     tool_list: list[dict] | None = None, tenant: Any = None,
                     allow_mutating: bool = True) -> list[Any]:
    """Wrap each Gateway tool as a Strands tool with a model-safe name and schema.

    ``tenant`` (a TenantContext) is needed for mutating tools: their operation key is built
    from the turn's storage ID, requestId and the call's toolUseId.

    ``allow_mutating`` False drops the mutating tools entirely (event turns, by default), so the
    model is not even offered them.
    """
    from strands.tools.tools import PythonAgentTool  # imported lazily: keeps unit tests light

    wrapped = []
    for tool in tool_list if tool_list is not None else cached_tool_list(gateway):
        gateway_name = tool["name"]
        alias = alias_for(gateway_name)
        mutating = gateway_name.split("___", 1)[-1] in MUTATING_TOOLS
        if mutating and not allow_mutating:
            continue
        spec = {"name": alias, "description": tool.get("description") or gateway_name,
                "inputSchema": {"json": strip_reserved_schema(tool.get("inputSchema"))}}

        def call(tool_use: dict, _name: str = gateway_name, _mutating: bool = mutating, **_: Any) -> dict:
            trace.append(_name)
            arguments = strip_reserved_arguments(tool_use.get("input"))
            if _mutating:
                if tenant is None:
                    return {"toolUseId": tool_use["toolUseId"], "status": "error",
                            "content": [{"text": "mutating tool called without a turn context"}]}
                arguments["operationKey"] = tenancy.operation_key(
                    tenant.storage_id, tenant.request_id, tool_use["toolUseId"])
            try:
                result = gateway.call_tool(_name, arguments, retry=_mutating)
            except GatewayError as err:
                text = str(err)
                if _mutating and err.transient:
                    text += (" It was retried with the same operation key and may already have been "
                             "applied. Check before calling it again.")
                return {"toolUseId": tool_use["toolUseId"], "status": "error", "content": [{"text": text}]}
            return _tool_result(tool_use["toolUseId"], result)

        wrapped.append(PythonAgentTool(alias, spec, call))
    return wrapped
