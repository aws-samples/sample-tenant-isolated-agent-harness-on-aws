# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``execute_process``: one complete execution task per call to the tenant's isolated runtime.

The boundary carries a whole task (script, deadline, tenant workspace context, output slot)
rather than separate remote file and process objects. What changes for tenant isolation:

* The runtime gets no standing data access. Each task carries short-lived credentials for
  TenantRuntimeRole, minted with this tenant's ``TenantId`` session tag. They can read and
  write only ``tenants/{sid}/workspace/`` and read ``tenants/{sid}/uploads/``, and only through
  the runtime VPC's S3 gateway endpoint (``aws:SourceVpce``). A hostile script that steals them
  gets its own tenant's workspace, from inside the runtime VPC, for at most 15 minutes.
* Large scripts and results move through object-scoped presigned URLs under
  ``transfer/{sid}/{rid}/``, generated from the tenant-scoped harness session.
* The runtime session ID is derived from the tenant (``tenant-{sid}``), and the runtime refuses
  a task whose storage ID does not match the session it is running in.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from botocore.config import Config

from shared import tenancy
from shared.tenant_session import assume_tenant_credentials, wire_credentials

INLINE_SCRIPT_LIMIT = 48 * 1024
MAX_SCRIPT_BYTES = 5 * 1024 * 1024
RESULT_INLINE_LIMIT = 256 * 1024
MAX_TIMEOUT_SECONDS = 300
MAX_INPUTS = 20
PRESIGN_SECONDS = 900
# Versioned runtime contract, for compatible rolling upgrades. Bump TASK_VERSION only after
# every runtime in service accepts it; keep reading older response versions until they drain.
TASK_VERSION = 1
KNOWN_RESPONSE_VERSIONS = frozenset({1})
_INPUT_KEY = re.compile(r"^uploads/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")

DESCRIPTION = (
    "Run a self-contained POSIX shell script in this business's isolated execution environment. "
    "Use it only when the task needs code execution or files (for example normalising an uploaded "
    "supplier spreadsheet). Environment variables: $TRANSIENT_DIR is fast scratch space that persists "
    "between scripts and may hold earlier files, so work in a new subdirectory "
    "(work=$(mktemp -d \"$TRANSIENT_DIR/run.XXXXXX\")), don't read or glob files there that you didn't "
    "create, and remove it before the script ends; $HOME and $TMPDIR are new for every script; "
    "$WORKSPACE_DIR is the business's permanent workspace, so copy only final results there; "
    "$INPUTS_DIR holds any uploads you list in `inputs` (keys like uploads/prices.csv). They are shell "
    "variables: write \"$INPUTS_DIR/prices.csv\", never the bare name. Example: inputs "
    "[\"uploads/prices.csv\"] with the script awk -F, 'NR>1 {t+=$3} END {print t}' \"$INPUTS_DIR/prices.csv\". "
    "Scripts can't download anything or reach outside services."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "script": {"type": "string", "description": "POSIX sh script, read from stdin."},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS},
        "inputs": {"type": "array", "items": {"type": "string"},
                   "description": "Upload keys to place in INPUTS_DIR, for example uploads/prices.csv"},
    },
    "required": ["script"],
}


class ExecuteError(Exception):
    pass


# A script can change the workspace, so it must not run twice. When a turn dies mid-tool, Strands
# re-runs that message's tool calls with the same toolUseIds on redelivery. Each script call
# therefore records itself (coordination table, keyed by its operation key) just before it goes
# to the runtime and again when the runtime answers.
INTERRUPTED_RUN = ("Not run again: an earlier attempt of this turn sent this script to the runtime and was cut "
                   "off before its result was saved, so it may already have run. Check the workspace for its "
                   "output before running it again.")
ALREADY_RAN = ("Not run again: this script already ran in an earlier attempt of this turn, but its result was "
               "not saved. Check the workspace for its output.")


def _error(tool_use_id: str, message: str) -> dict:
    return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": message}]}


# Small models (Nova Lite in the live demo) write INPUTS_DIR/prices.csv instead of "$INPUTS_DIR/...".
# The shell then looks for a relative directory called INPUTS_DIR, the last command of a pipeline
# still exits 0, and the model sees an empty result. Refuse the script with the exact fix instead.
_BARE_DIRECTORY = re.compile(r"(?<![\w${])(INPUTS_DIR|WORKSPACE_DIR|TRANSIENT_DIR)(?=/)")


def bare_directory_names(script: str) -> list[str]:
    """Directory variables the script uses as literal path names, without the leading $."""
    return sorted(set(_BARE_DIRECTORY.findall(script)))


def build_task(*, tenant: tenancy.TenantContext, tool_use_id: str, script: str, timeout_seconds: int,
               inputs: list[str], bucket: str, region: str, s3: Any, runtime_credentials: dict) -> dict:
    script_bytes = script.encode()
    if len(script_bytes) > MAX_SCRIPT_BYTES:
        raise ExecuteError("script exceeds 5 MiB")
    if len(inputs) > MAX_INPUTS or any(not isinstance(k, str) or not _INPUT_KEY.match(k) or ".." in k for k in inputs):
        raise ExecuteError("inputs must be upload keys like uploads/<file>")
    safe_tool_id = re.sub(r"[^A-Za-z0-9_-]", "_", tool_use_id)[:64] or "tool"
    transfer = tenancy.Layout.transfer_prefix(tenant.storage_id, tenant.request_id) + safe_tool_id + "/"

    if len(script_bytes) <= INLINE_SCRIPT_LIMIT:
        source: dict[str, Any] = {"kind": "inline", "text": script}
    else:
        key = transfer + "script.sh"
        s3.put_object(Bucket=bucket, Key=key, Body=script_bytes, ContentType="text/x-shellscript")
        source = {"kind": "s3", "bytes": len(script_bytes), "sha256": hashlib.sha256(script_bytes).hexdigest(),
                  "getUrl": s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                                      ExpiresIn=PRESIGN_SECONDS)}
    result_key = transfer + "result.json"
    return {
        "version": TASK_VERSION,
        "customerStorageId": tenant.storage_id,
        "requestId": tenant.request_id,
        "toolUseId": safe_tool_id,
        "script": source,
        "timeoutSeconds": timeout_seconds,
        "inlineLimitBytes": RESULT_INLINE_LIMIT,
        "outputSlot": {"key": result_key, "putUrl": s3.generate_presigned_url(
            "put_object", Params={"Bucket": bucket, "Key": result_key, "ContentType": "application/json"},
            ExpiresIn=PRESIGN_SECONDS)},
        "inputs": inputs,
        "workspace": {
            "bucket": bucket, "region": region,
            "prefix": tenancy.Layout.workspace_prefix(tenant.storage_id),
            "uploadsPrefix": tenancy.Layout.uploads_prefix(tenant.storage_id),
            "credentials": wire_credentials(runtime_credentials),
        },
    }


def read_runtime_response(response: dict, *, s3: Any, bucket: str, expected_prefix: str) -> dict:
    body = json.loads(response["response"].read())
    # Rolling upgrades: an old runtime still on warm compute sends no version, which means 1.
    version = body.get("version", 1)
    if version not in KNOWN_RESPONSE_VERSIONS:
        raise ExecuteError(f"runtime response version {version!r} is not understood by this worker")
    artifact = body.get("artifact")
    if artifact:
        key = artifact.get("key", "")
        if not key.startswith(expected_prefix):
            raise ExecuteError("runtime returned an artifact outside this request's transfer prefix")
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if hashlib.sha256(data).hexdigest() != artifact.get("sha256"):
            raise ExecuteError("runtime artifact digest mismatch")
        body = {"status": body.get("status"), "result": json.loads(data)}
    return body


def make_execute_process_tool(*, tenant: tenancy.TenantContext, harness_session: Any, sts: Any, agentcore: Any,
                              runtime_arn: str, runtime_role_arn: str, bucket: str, region: str,
                              deadline: float, trace: list[str], runs: Any) -> Any:
    """``runs`` keeps the per-call run records (worker.coordination.Coordination)."""
    from strands.tools.tools import PythonAgentTool

    s3 = harness_session.client("s3", region_name=region,
                                config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    spec = {"name": "execute_process", "description": DESCRIPTION, "inputSchema": {"json": INPUT_SCHEMA}}

    def run(tool_use: dict, **_: Any) -> dict:
        tool_use_id = tool_use["toolUseId"]
        args = tool_use.get("input") or {}
        script = args.get("script")
        if not isinstance(script, str) or not script.strip():
            return _error(tool_use_id, "script is required")
        bare = bare_directory_names(script)
        if bare:
            fixes = ", ".join(f'"${name}/..."' for name in bare)
            return _error(tool_use_id, f"Not run: the script uses {', '.join(bare)} as a literal folder name. "
                                       f"They are environment variables, so write {fixes}. For example: awk -F, "
                                       "'NR>1 {t+=$3} END {print t}' \"$INPUTS_DIR/prices.csv\"")
        remaining = int(deadline - time.time()) - 30
        if remaining < 5:
            return _error(tool_use_id, "not enough time left in this turn to run a script")
        timeout = max(1, min(int(args.get("timeout_seconds") or 60), MAX_TIMEOUT_SECONDS, remaining))
        trace.append("execute_process")
        try:
            credentials = assume_tenant_credentials(sts, runtime_role_arn, storage_id=tenant.storage_id,
                                                    purpose="runtime", request_id=tenant.request_id)
            task = build_task(tenant=tenant, tool_use_id=tool_use_id, script=script, timeout_seconds=timeout,
                              inputs=list(args.get("inputs") or []), bucket=bucket, region=region, s3=s3,
                              runtime_credentials=credentials)
            op_key = tenancy.operation_key(tenant.storage_id, tenant.request_id, tool_use_id)
            earlier = runs.start_run(tenant.storage_id, op_key)
            if earlier is not None:
                return _error(tool_use_id, ALREADY_RAN if earlier == "COMPLETED" else INTERRUPTED_RUN)
            response = agentcore.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=tenancy.runtime_session_id(tenant.storage_id),
                qualifier="DEFAULT",
                contentType="application/json",
                accept="application/json",
                payload=json.dumps(task).encode(),
            )
            runs.finish_run(tenant.storage_id, op_key)
            body = read_runtime_response(response, s3=s3, bucket=bucket,
                                         expected_prefix=tenancy.Layout.transfer_prefix(tenant.storage_id, tenant.request_id))
        except ExecuteError as err:
            return _error(tool_use_id, str(err))
        if body.get("status") != "completed":
            return _error(tool_use_id, json.dumps(body)[:4000])
        return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": json.dumps(body["result"])[:16000]}]}

    return PythonAgentTool("execute_process", spec, run)


def agentcore_client(session: Any, region: str) -> Any:
    # No automatic retries: a retried invoke can run a script twice. If the worker dies mid-script,
    # the redelivered turn finds the call's run record and reports it to the model as possibly run
    # rather than running it again.
    return session.client("bedrock-agentcore", region_name=region, config=Config(
        read_timeout=MAX_TIMEOUT_SECONDS + 120, connect_timeout=10,
        retries={"mode": "standard", "total_max_attempts": 1}))
