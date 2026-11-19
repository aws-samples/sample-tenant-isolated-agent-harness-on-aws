# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import hashlib
import json
import os
import signal
from pathlib import Path

import pytest

from runtime import server
from runtime.workspace_sync import WorkspaceError, safe_relative
from shared import tenancy


class ScopedFakeS3:
    """In-memory S3 that enforces the same prefixes the tenant-scoped credentials allow."""

    def __init__(self, bucket, allowed_prefixes):
        self.bucket, self.allowed = bucket, allowed_prefixes
        self.objects: dict[str, bytes] = {}

    def _check(self, bucket, key):
        assert bucket == self.bucket
        if not any(key.startswith(p) for p in self.allowed):
            raise PermissionError(f"AccessDenied: {key}")

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self._check(Bucket, Key)
        data = Body.read() if hasattr(Body, "read") else Body
        self.objects[Key] = data
        return {"ETag": '"' + hashlib.sha256(data).hexdigest()[:32] + '"'}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self._check(Bucket, Key)
        self.objects.pop(Key, None)

    def download_file(self, bucket, key, filename):
        self._check(bucket, key)
        with open(filename, "wb") as handle:
            handle.write(self.objects[key])

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        store = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                store._check(Bucket, Prefix)
                yield {"Contents": [{"Key": k, "Size": len(v), "ETag": '"' + hashlib.sha256(v).hexdigest()[:32] + '"'}
                                    for k, v in sorted(store.objects.items()) if k.startswith(Prefix)]}
        return Paginator()


def make_task(sid, script="echo ok", **overrides):
    rid = "req-00000001"
    task = {
        "version": 1, "customerStorageId": sid, "requestId": rid, "toolUseId": "tooluse_1",
        "script": {"kind": "inline", "text": script}, "timeoutSeconds": 20, "inlineLimitBytes": 256 * 1024,
        "outputSlot": {"key": tenancy.Layout.transfer_prefix(sid, rid) + "tooluse_1/result.json",
                       "putUrl": "https://example.invalid/put"},
        "inputs": [],
        "workspace": {"bucket": "tenant-bucket", "region": "ap-southeast-2",
                      "prefix": tenancy.Layout.workspace_prefix(sid), "uploadsPrefix": tenancy.Layout.uploads_prefix(sid),
                      "credentials": {"accessKeyId": "a", "secretAccessKey": "b", "sessionToken": "c"}},
    }
    task.update(overrides)
    return task


def test_task_must_match_the_runtime_session(tenants):
    a, b = tenants.values()
    server.validate_task(make_task(a), tenancy.runtime_session_id(a), strict=True)
    with pytest.raises(server.TaskError, match="does not match this runtime session") as err:
        server.validate_task(make_task(a), tenancy.runtime_session_id(b), strict=True)
    assert err.value.status == 403
    with pytest.raises(server.TaskError):
        server.validate_task(make_task(a), None, strict=True)  # strict: no session header, no task


def test_task_cannot_point_at_another_tenants_prefixes(tenants):
    a, b = tenants.values()
    session = tenancy.runtime_session_id(a)
    bad_workspace = make_task(a)
    bad_workspace["workspace"]["prefix"] = tenancy.Layout.workspace_prefix(b)
    bad_output = make_task(a)
    bad_output["outputSlot"]["key"] = tenancy.Layout.transfer_prefix(b, "req-00000001") + "x.json"
    for task in (bad_workspace, bad_output):
        with pytest.raises(server.TaskError) as err:
            server.validate_task(task, session, strict=True)
        assert err.value.status == 403


@pytest.mark.parametrize("key", ["uploads/../workspace/x", "workspace/x", "/etc/passwd", "uploads/a b"])
def test_input_keys_are_validated(tenants, key):
    a = tenants["anycompany-plumbing"]
    with pytest.raises(server.TaskError):
        server.validate_task(make_task(a, inputs=[key]), tenancy.runtime_session_id(a), strict=True)


@pytest.mark.parametrize("rel", ["../x", "/abs", "a//b", "a/./b", "a\\b", ""])
def test_unsafe_workspace_paths(rel):
    with pytest.raises(WorkspaceError):
        safe_relative(rel)


def test_run_task_persists_workspace_to_the_tenant_prefix_only(tmp_path, tenants):
    a = tenants["anycompany-plumbing"]
    s3 = ScopedFakeS3("tenant-bucket", [tenancy.Layout.workspace_prefix(a), tenancy.Layout.uploads_prefix(a)])
    s3.objects[tenancy.Layout.uploads_prefix(a) + "prices.csv"] = b"sku,price\nA1,10\n"
    s3.objects[tenancy.Layout.workspace_prefix(a) + "existing.txt"] = b"from an earlier turn"
    script = ('cat "$WORKSPACE_DIR/existing.txt"; '
              'wc -l < "$INPUTS_DIR/prices.csv" > "$WORKSPACE_DIR/line_count.txt"; '
              'mkdir -p "$WORKSPACE_DIR/out" && echo done > "$WORKSPACE_DIR/out/report.txt"; '
              'echo scratch > "$TRANSIENT_DIR/tmp.txt"; env | grep -c AWS_ || true')
    result = server.run_task(make_task(a, script=script, inputs=["uploads/prices.csv"]),
                             session_root=tmp_path, s3_factory=lambda ws: s3)
    run = result["result"]
    assert run["exitCode"] == 0 and run["stdout"].startswith("from an earlier turn")
    assert run["stdout"].strip().endswith("0")  # no AWS_* variables leak into scripts
    keys = set(s3.objects)
    assert tenancy.Layout.workspace_prefix(a) + "line_count.txt" in keys
    assert tenancy.Layout.workspace_prefix(a) + "out/report.txt" in keys
    assert not any("tmp.txt" in k for k in keys)  # transient files stay in the session
    assert run["workspace"] == {"pulled": 1, "uploaded": 2, "deleted": 0}

    # A second run in the same session pulls nothing new and sees the earlier outputs.
    again = server.run_task(make_task(a, script='cat "$WORKSPACE_DIR/out/report.txt"'),
                            session_root=tmp_path, s3_factory=lambda ws: s3)["result"]
    assert again["stdout"].strip() == "done" and again["workspace"]["pulled"] == 0


def test_a_compromised_workspace_listing_cannot_reach_other_tenants(tmp_path, tenants):
    a, b = tenants.values()
    s3 = ScopedFakeS3("tenant-bucket", [tenancy.Layout.workspace_prefix(b)])  # credentials for B only
    with pytest.raises(PermissionError):
        server.run_task(make_task(a), session_root=tmp_path, s3_factory=lambda ws: s3)


def test_timeouts_kill_the_process_group(tmp_path, tenants):
    a = tenants["anycompany-plumbing"]
    s3 = ScopedFakeS3("tenant-bucket", [tenancy.Layout.workspace_prefix(a)])
    result = server.run_task(make_task(a, script="sleep 30 & sleep 30", timeoutSeconds=1),
                             session_root=tmp_path, s3_factory=lambda ws: s3)["result"]
    assert result["timedOut"] is True and result["durationMs"] < 10_000


def test_output_is_bounded_and_large_results_use_the_output_slot(tmp_path, tenants):
    a = tenants["anycompany-plumbing"]
    s3 = ScopedFakeS3("tenant-bucket", [tenancy.Layout.workspace_prefix(a)])
    uploaded = {}
    result = server.run_task(make_task(a, script="yes 0123456789 | head -c 200000", inlineLimitBytes=1024),
                             session_root=tmp_path, s3_factory=lambda ws: s3,
                             http_put=lambda url, data: uploaded.update(url=url, data=data))
    assert "artifact" in result and uploaded["url"] == "https://example.invalid/put"
    body = json.loads(uploaded["data"])
    assert body["stdoutBytes"] == 200000 and len(body["stdout"]) == server.PREVIEW_BYTES and body["outputTruncated"]
    assert result["artifact"]["sha256"] == hashlib.sha256(uploaded["data"]).hexdigest()


def test_s3_script_source_is_digest_checked(tmp_path, tenants):
    a = tenants["anycompany-plumbing"]
    script = b"echo from-s3"
    good = {"kind": "s3", "getUrl": "https://example.invalid/get", "bytes": len(script),
            "sha256": hashlib.sha256(script).hexdigest()}
    assert server.load_script(good, lambda url: script) == script
    with pytest.raises(server.TaskError, match="digest"):
        server.load_script(good, lambda url: b"echo tampered")


def test_every_script_gets_a_new_home_and_tmpdir(tmp_path, tenants):
    a = tenants["anycompany-plumbing"]
    s3 = ScopedFakeS3("tenant-bucket", [tenancy.Layout.workspace_prefix(a)])

    def run(script):
        return server.run_task(make_task(a, script=script), session_root=tmp_path, s3_factory=lambda ws: s3)["result"]

    first = run('echo "$HOME|$TMPDIR"; echo "output = /dev/null" > "$HOME/.curlrc"; '
                'echo planted > "$TMPDIR/left.txt"; echo kept > "$TRANSIENT_DIR/kept.txt"')
    home, tmpdir = first["stdout"].splitlines()[0].split("|")
    second = run('echo "$HOME|$TMPDIR"; ls -A "$HOME" "$TMPDIR"; cat "$TRANSIENT_DIR/kept.txt"')
    lines = second["stdout"].splitlines()

    assert set(lines[0].split("|")).isdisjoint({home, tmpdir})  # new folders for the second script
    assert ".curlrc" not in second["stdout"] and "left.txt" not in second["stdout"]
    assert lines[-1] == "kept"  # TRANSIENT_DIR persists, as the model is told
    for folder in (home, tmpdir):
        assert not Path(folder).exists()  # deleted once its script ended
        assert not Path(folder).is_relative_to(tmp_path / "scratch")  # never inside TRANSIENT_DIR


def test_folders_a_crashed_task_left_behind_are_removed(tmp_path, tenants):
    a = tenants["anycompany-plumbing"]
    s3 = ScopedFakeS3("tenant-bucket", [tenancy.Layout.workspace_prefix(a)])
    leftover = tmp_path / ".tasks" / "task-crashed" / "home"
    leftover.mkdir(parents=True)
    (leftover / ".profile").write_text("export PATH=/tmp/evil:$PATH")
    server.run_task(make_task(a), session_root=tmp_path, s3_factory=lambda ws: s3)
    assert not leftover.parent.exists()


def test_process_listing_reads_owner_state_and_start_time(tmp_path):
    def proc(pid, state, started, comm="sh"):
        (tmp_path / str(pid)).mkdir()
        fields = [state] + ["0"] * 18 + [started, "0", "0"]
        (tmp_path / str(pid) / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))

    proc(10, "S", "111")
    proc(11, "Z", "112")  # a zombie has already exited
    proc(12, "R", "113", comm="odd) name")  # comm may hold spaces and parentheses
    (tmp_path / "self").mkdir()
    assert server._processes(tmp_path) == {10: "111", 12: "113"}
    assert server._processes(tmp_path, uid=os.getuid() + 1) == {}  # other users' processes are never listed
    assert server._processes(tmp_path / "missing") == {}  # no /proc (macOS): nothing is stopped


class FakeProcesses:
    def __init__(self, table, unkillable=()):
        self.table, self.signals, self.unkillable = dict(table), [], set(unkillable)

    def list(self):
        return dict(self.table)

    def signal(self, pid, sig):
        self.signals.append((pid, sig))
        if pid not in self.unkillable:
            self.table.pop(pid, None)


def _reaper(fake):
    return server.StrayReaper(fake.list, fake.signal, collect_children=lambda: None, grace_seconds=0)


def test_only_processes_started_after_the_server_are_stopped():
    fake = FakeProcesses({1: "10", 40: "20"})
    reaper = _reaper(fake)
    fake.table.update({41: "30", 42: "31", os.getpid(): "5"})
    assert reaper.reap() == []
    assert fake.signals == [(41, signal.SIGKILL), (42, signal.SIGKILL)]  # never the startup processes or itself
    assert set(fake.table) == {1, 40, os.getpid()}


def test_a_reused_pid_does_not_pass_as_a_startup_process():
    fake = FakeProcesses({40: "20"})
    reaper = _reaper(fake)
    fake.table[40] = "99"  # the startup process exited and a script's process got its PID
    reaper.reap()
    assert fake.signals == [(40, signal.SIGKILL)]


def test_a_process_that_survives_is_reported():
    fake = FakeProcesses({}, unkillable={50})  # e.g. stuck in uninterruptible sleep
    reaper = _reaper(fake)
    fake.table[50] = "70"
    assert reaper.reap() == [50]


@pytest.mark.parametrize("url", ["http://bucket.example/key", "file:///etc/passwd"])
def test_presigned_urls_must_use_https(url):
    with pytest.raises(server.TaskError, match="https"):
        server._http_get(url)
    with pytest.raises(server.TaskError, match="https"):
        server._http_put(url, b"{}")
