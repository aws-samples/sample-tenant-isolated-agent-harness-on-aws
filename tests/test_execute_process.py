# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import datetime
import hashlib
import io
import json

import pytest

from shared import tenancy
from worker import execute_process as ep


class FakeS3:
    def __init__(self):
        self.objects, self.presigned = {}, []

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.presigned.append((op, Params["Key"], ExpiresIn))
        return f"https://bucket.s3.ap-southeast-2.amazonaws.com/{Params['Key']}?X-Amz-Signature=x"


CREDS = {"AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t",
         "Expiration": datetime.datetime(2026, 10, 19, 5, 0, tzinfo=datetime.timezone.utc)}


@pytest.fixture
def tenant():
    return tenancy.TenantContext.from_customer("anycompany-plumbing", "req-00000001")


def build(tenant, s3, script="echo hi", inputs=()):
    return ep.build_task(tenant=tenant, tool_use_id="tooluse/abc", script=script, timeout_seconds=30,
                         inputs=list(inputs), bucket="b", region="ap-southeast-2", s3=s3, runtime_credentials=CREDS)


def test_small_scripts_travel_inline_with_tenant_scoped_context(tenant):
    s3 = FakeS3()
    task = build(tenant, s3)
    assert task["script"] == {"kind": "inline", "text": "echo hi"}
    assert task["workspace"]["prefix"] == tenancy.Layout.workspace_prefix(tenant.storage_id)
    assert task["workspace"]["credentials"]["accessKeyId"] == "ASIA"
    assert task["outputSlot"]["key"].startswith(tenancy.Layout.transfer_prefix(tenant.storage_id, tenant.request_id))
    assert task["toolUseId"] == "tooluse_abc"
    assert [p[0] for p in s3.presigned] == ["put_object"]


def test_large_scripts_go_through_an_object_scoped_url(tenant):
    s3 = FakeS3()
    script = "echo x\n" * 10_000  # 70 KB, above the 48 KiB inline limit
    task = build(tenant, s3, script=script)
    assert task["script"]["kind"] == "s3"
    [key] = [k for k in s3.objects if k.endswith("script.sh")]
    assert key.startswith(tenancy.Layout.transfer_prefix(tenant.storage_id, tenant.request_id))
    assert task["script"]["sha256"] == hashlib.sha256(script.encode()).hexdigest()


@pytest.mark.parametrize("inputs", [["uploads/../harness/x"], ["harness/session.json"], ["uploads/a"] * 21])
def test_bad_inputs_are_refused_before_invoking(tenant, inputs):
    with pytest.raises(ep.ExecuteError):
        build(tenant, FakeS3(), inputs=inputs)


def test_artifacts_outside_this_requests_prefix_are_refused(tenant):
    s3 = FakeS3()
    other = tenancy.TenantContext.from_customer("example-corp-electrical", "req-00000001")
    response = {"response": io.BytesIO(json.dumps({"status": "completed", "artifact": {
        "key": tenancy.Layout.transfer_prefix(other.storage_id, other.request_id) + "x/result.json", "sha256": "0"}}).encode())}
    with pytest.raises(ep.ExecuteError, match="outside"):
        ep.read_runtime_response(response, s3=s3, bucket="b",
                                 expected_prefix=tenancy.Layout.transfer_prefix(tenant.storage_id, tenant.request_id))
