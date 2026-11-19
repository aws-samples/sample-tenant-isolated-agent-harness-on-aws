# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Isolated execution runtime for AgentCore Runtime (HTTP protocol, direct code deploy).

One self-contained script per invocation. No model loop, no Gateway credentials and no
standing data access: the execution role can only write logs and read this code package. The
task carries short-lived, tenant-scoped workspace credentials (see worker/execute_process.py).

Checks that make this the tenant boundary rather than a convention:
* The task's ``customerStorageId`` must match the AgentCore session this microVM serves
  (``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`` = ``tenant-{sid}``). A harness bug that
  routes tenant A's task into tenant B's session is refused here.
* Workspace and uploads prefixes must be exactly this tenant's; output keys must sit under
  this request's transfer prefix.
* Scripts get a minimal environment: no AWS credentials, no inherited variables.
* Processes a script leaves running are stopped after its task, and no task starts while any
  survive (see StrayReaper).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from runtime.workspace_sync import WorkspaceError, WorkspaceSync, safe_join, safe_relative
from shared import tenancy

LOG = logging.getLogger("runtime")
logging.basicConfig(level=logging.INFO)

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_SCRIPT_BYTES = 5 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 300
MAX_INLINE_RESULT = 4 * 1024 * 1024
PREVIEW_BYTES = 32 * 1024
MAX_INPUTS = 20
# Versioned contract with the worker. A new runtime keeps accepting the previous task version
# while old workers drain, so either side of a rolling deployment can talk to the other.
SUPPORTED_TASK_VERSIONS = frozenset({1})
RESPONSE_VERSION = 1
_INPUT_KEY = re.compile(r"^uploads/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_TOOL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

BUSY = threading.Event()
LOCK = threading.Lock()


class TaskError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _require(condition: bool, message: str, status: int = 400) -> None:
    if not condition:
        raise TaskError(message, status)


def validate_task(task: Any, session_id: str | None, strict: bool) -> dict:
    _require(isinstance(task, dict) and task.get("version") in SUPPORTED_TASK_VERSIONS,
             f"unsupported task version; supported {sorted(SUPPORTED_TASK_VERSIONS)}")
    try:
        sid = tenancy.require_storage_id(task.get("customerStorageId"))
        rid = tenancy.require_request_id(task.get("requestId"))
    except tenancy.TenancyError as err:
        raise TaskError(str(err)) from err
    if session_id is not None or strict:
        _require(tenancy.storage_id_from_runtime_session(session_id or "") == sid,
                 "task tenant does not match this runtime session", 403)
    _require(isinstance(task.get("toolUseId"), str) and bool(_TOOL_ID.match(task["toolUseId"])), "invalid toolUseId")
    timeout = task.get("timeoutSeconds")
    _require(isinstance(timeout, int) and 1 <= timeout <= MAX_TIMEOUT_SECONDS, "invalid timeout")
    limit = task.get("inlineLimitBytes")
    _require(isinstance(limit, int) and 1 <= limit <= MAX_INLINE_RESULT, "invalid inline limit")

    script = task.get("script") or {}
    if script.get("kind") == "inline":
        _require(isinstance(script.get("text"), str) and len(script["text"].encode()) <= MAX_SCRIPT_BYTES, "invalid script")
    elif script.get("kind") == "s3":
        _require(str(script.get("getUrl", "")).startswith("https://"), "script URL must be https")
        _require(isinstance(script.get("bytes"), int) and 0 < script["bytes"] <= MAX_SCRIPT_BYTES, "invalid script size")
        _require(bool(_SHA256.match(str(script.get("sha256", "")))), "invalid script digest")
    else:
        raise TaskError("invalid script source")

    workspace = task.get("workspace") or {}
    _require(workspace.get("prefix") == tenancy.Layout.workspace_prefix(sid), "workspace prefix is not this tenant's", 403)
    _require(workspace.get("uploadsPrefix") == tenancy.Layout.uploads_prefix(sid), "uploads prefix is not this tenant's", 403)
    _require(isinstance(workspace.get("bucket"), str) and isinstance(workspace.get("region"), str), "invalid workspace")
    creds = workspace.get("credentials") or {}
    _require(all(isinstance(creds.get(k), str) for k in ("accessKeyId", "secretAccessKey", "sessionToken")),
             "workspace credentials missing")

    inputs = task.get("inputs") or []
    _require(isinstance(inputs, list) and len(inputs) <= MAX_INPUTS, "too many inputs")
    for key in inputs:
        _require(isinstance(key, str) and bool(_INPUT_KEY.match(key)) and ".." not in key, "invalid input key")

    slot = task.get("outputSlot") or {}
    _require(str(slot.get("putUrl", "")).startswith("https://"), "output URL must be https")
    _require(str(slot.get("key", "")).startswith(tenancy.Layout.transfer_prefix(sid, rid)),
             "output key is not under this request's transfer prefix", 403)
    return task


class _Capture(threading.Thread):
    """Read a pipe to EOF, keeping only the first `limit` bytes (bounded memory)."""

    def __init__(self, pipe: Any, limit: int):
        super().__init__(daemon=True)
        self._pipe, self._limit = pipe, limit
        self.kept, self.total = bytearray(), 0

    def run(self) -> None:
        for chunk in iter(lambda: self._pipe.read(65536), b""):
            self.total += len(chunk)
            room = self._limit - len(self.kept)
            if room > 0:
                self.kept.extend(chunk[:room])


def execute(script_path: Path, *, cwd: Path, env: dict, timeout: int) -> dict:
    started = time.monotonic()
    proc = subprocess.Popen(["/bin/sh", str(script_path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=str(cwd), env=env, start_new_session=True)
    out, err = _Capture(proc.stdout, PREVIEW_BYTES), _Capture(proc.stderr, PREVIEW_BYTES)
    out.start()
    err.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    out.join(5)
    err.join(5)
    return {"exitCode": proc.returncode, "timedOut": timed_out,
            "stdout": out.kept.decode(errors="replace"), "stderr": err.kept.decode(errors="replace"),
            "stdoutBytes": out.total, "stderrBytes": err.total,
            "outputTruncated": out.total > PREVIEW_BYTES or err.total > PREVIEW_BYTES,
            "durationMs": int((time.monotonic() - started) * 1000)}


def _processes(proc_root: Path = Path("/proc"), uid: int | None = None) -> dict[int, str]:
    """This user's live processes as {pid: start time}, read from /proc. Zombies have already
    exited and are left out. Empty where there is no /proc, so nothing is stopped off Linux."""
    uid = os.getuid() if uid is None else uid
    found: dict[int, str] = {}
    try:
        entries = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return found
    for entry in entries:
        try:
            if entry.stat().st_uid != uid:
                continue
            stat = (entry / "stat").read_text()
        except OSError:
            continue  # it exited while we looked
        # After "pid (comm) " the state is the first field and the start time the twentieth.
        fields = stat[stat.rindex(")") + 2:].split()
        if fields[0] not in ("Z", "X"):
            found[int(entry.name)] = fields[19]
    return found


def _collect_children() -> None:
    """Collect our exited children so stopped strays don't stay as zombies (when the server is
    PID 1, orphaned processes are re-parented to it)."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


class StrayReaper:
    """Stops processes a script left running after its task.

    A script runs in its own session and a timeout signals that process group, but the script can
    start a process in a new session (setsid, a self-daemonising tool) that outlives the task and
    watches or changes the next one. AgentCore may run helpers of its own as this user, so only
    processes that started after the server are stopped: the baseline is every process of this
    user when the server started, matched on PID and start time so a reused PID doesn't pass as
    a startup process. A helper of this user that AgentCore starts later would be stopped too.
    """

    def __init__(self, list_processes: Callable[[], dict[int, str]] = _processes,
                 send_signal: Callable[[int, int], None] = os.kill,
                 collect_children: Callable[[], None] = _collect_children, grace_seconds: float = 2.0):
        self._list, self._signal, self._collect = list_processes, send_signal, collect_children
        self._grace = grace_seconds
        self._baseline = set(self._list().items())
        self._own_pid = os.getpid()

    def strays(self) -> list[int]:
        return sorted(pid for pid, started in self._list().items()
                      if (pid, started) not in self._baseline and pid != self._own_pid)

    def reap(self) -> list[int]:
        """Stop every stray. Returns the PIDs still alive after the grace period."""
        for pid in self.strays():
            try:
                self._signal(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + self._grace
        while True:
            self._collect()
            left = self.strays()
            if not left or time.monotonic() >= deadline:
                return left
            time.sleep(0.05)


def _require_https(url: str) -> str:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise TaskError("presigned URLs must use https")
    return url


def _http_get(url: str) -> bytes:
    # Bandit B310: _require_https allows only https.
    with urllib.request.urlopen(_require_https(url), timeout=60) as response:  # noqa: S310  # nosec B310
        return response.read(MAX_SCRIPT_BYTES + 1)


def _http_put(url: str, data: bytes) -> None:
    request = urllib.request.Request(_require_https(url), data=data, method="PUT",
                                     headers={"Content-Type": "application/json"})
    # Bandit B310: _require_https allows only https.
    with urllib.request.urlopen(request, timeout=60):  # noqa: S310  # nosec B310
        pass


def load_script(source: dict, http_get: Callable[[str], bytes]) -> bytes:
    if source["kind"] == "inline":
        return source["text"].encode()
    data = http_get(source["getUrl"])
    if len(data) != source["bytes"] or hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise TaskError("script does not match its declared size and digest")
    return data


def default_s3(workspace: dict) -> Any:
    import boto3
    from botocore.config import Config
    creds = workspace["credentials"]
    return boto3.client("s3", region_name=workspace["region"], aws_access_key_id=creds["accessKeyId"],
                        aws_secret_access_key=creds["secretAccessKey"], aws_session_token=creds["sessionToken"],
                        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"},
                                      retries={"mode": "standard", "max_attempts": 3}))


def run_task(task: dict, *, session_root: Path, s3_factory: Callable[[dict], Any] = default_s3,
             http_get: Callable[[str], bytes] = _http_get, http_put: Callable[[str, bytes], None] = _http_put) -> dict:
    workspace = task["workspace"]
    scratch, workspace_dir = session_root / "scratch", session_root / "workspace"
    inputs_dir, state_dir = session_root / "inputs", session_root / ".state"
    tasks_dir = session_root / ".tasks"
    # Inputs, and any per-task folders a crashed task left behind, start empty.
    for leftover in (inputs_dir, tasks_dir):
        shutil.rmtree(leftover, ignore_errors=True)
    for path in (scratch, workspace_dir, inputs_dir, state_dir, tasks_dir):
        path.mkdir(parents=True, exist_ok=True)

    s3 = s3_factory(workspace)
    sync = WorkspaceSync(s3, workspace["bucket"], workspace["prefix"], workspace_dir, state_dir)
    try:
        pulled = sync.pull()
        for key in task.get("inputs") or []:
            rel = safe_relative(key[len("uploads/"):])
            destination = safe_join(inputs_dir, rel)
            destination.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(workspace["bucket"], workspace["uploadsPrefix"] + rel, str(destination))
    except WorkspaceError as err:
        raise TaskError(str(err)) from err

    script_path = state_dir / f"run-{task['toolUseId']}.sh"
    script_path.write_bytes(load_script(task["script"], http_get))
    # HOME and TMPDIR are new for every script and deleted after it, so nothing one script leaves
    # there (a ~/.curlrc, a Python usercustomize.py, shell start-up files) reaches the next one.
    # TRANSIENT_DIR stays persistent for the session; the model is told it may hold earlier files.
    task_dir = Path(tempfile.mkdtemp(prefix="task-", dir=tasks_dir))
    home, tmp = task_dir / "home", task_dir / "tmp"
    try:
        home.mkdir()
        tmp.mkdir()
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(home),
               "TMPDIR": str(tmp), "TRANSIENT_DIR": str(scratch),
               "WORKSPACE_DIR": str(workspace_dir), "INPUTS_DIR": str(inputs_dir)}
        outcome = execute(script_path, cwd=scratch, env=env, timeout=task["timeoutSeconds"])
    finally:
        script_path.unlink(missing_ok=True)
        shutil.rmtree(task_dir, ignore_errors=True)
    try:
        pushed = sync.push()
    except WorkspaceError as err:
        raise TaskError(str(err)) from err

    result = {**outcome, "workspace": {"pulled": pulled, **pushed}}
    encoded = json.dumps(result).encode()
    if len(encoded) <= task["inlineLimitBytes"]:
        return {"version": RESPONSE_VERSION, "status": "completed", "result": result}
    http_put(task["outputSlot"]["putUrl"], encoded)
    return {"version": RESPONSE_VERSION, "status": "completed",
            "artifact": {"key": task["outputSlot"]["key"], "bytes": len(encoded),
                         "sha256": hashlib.sha256(encoded).hexdigest()}}


class Handler(BaseHTTPRequestHandler):
    server_version = "split-harness-runtime/1"
    session_root = Path(os.environ.get("SESSION_ROOT", "/mnt/session"))
    strict = os.environ.get("STRICT_SESSION_BINDING", "true").lower() == "true"

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] == "/ping":
            # No time_of_last_update: a timestamp that moves on every ping keeps the session alive.
            return self._send(200, {"status": "HealthyBusy" if BUSY.is_set() else "Healthy"})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/invocations":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_REQUEST_BYTES:
            return self._send(413, {"status": "failed", "error": "request size"})
        try:
            task = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send(400, {"status": "failed", "error": "body is not JSON"})
        if not LOCK.acquire(blocking=False):
            return self._send(409, {"status": "failed", "error": "busy"})
        BUSY.set()
        started = time.time()
        reaper = getattr(self.server, "reaper", None)
        try:
            # Nothing an earlier task left running may watch this one.
            if reaper is not None and reaper.reap():
                raise TaskError("processes from an earlier task are still running", 503)
            validated = validate_task(task, self.headers.get(SESSION_HEADER), self.strict)
            try:
                result = run_task(validated, session_root=self.session_root)
            finally:
                left = reaper.reap() if reaper is not None else []
                if left:
                    LOG.warning(json.dumps({"event": "stray_processes_survived", "count": len(left)}))
            LOG.info(json.dumps({"event": "task_completed", "requestId": validated["requestId"],
                                 "exitCode": (result.get("result") or {}).get("exitCode"),
                                 "seconds": round(time.time() - started, 2)}))
            self._send(200, result)
        except TaskError as err:
            LOG.warning(json.dumps({"event": "task_rejected", "reason": str(err)}))
            self._send(err.status, {"status": "failed", "error": str(err)})
        except Exception as err:  # noqa: BLE001 - never echo task content (it holds credentials)
            LOG.exception("task failed")
            self._send(500, {"status": "failed", "error": type(err).__name__})
        finally:
            BUSY.clear()
            LOCK.release()

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug(fmt, *args)


def serve(port: int = 8080) -> None:
    # Loopback unless told otherwise. The AgentCore Runtime configuration sets
    # RUNTIME_BIND_HOST=0.0.0.0 because the service contract requires it inside the microVM; no
    # port is published there and the only way in is InvokeAgentRuntime with SigV4.
    host = os.environ.get("RUNTIME_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("RUNTIME_PORT", port))
    httpd = ThreadingHTTPServer((host, port), Handler)
    # The baseline for stopping stray processes: everything this user runs when the server starts.
    httpd.reaper = StrayReaper()
    httpd.serve_forever()
