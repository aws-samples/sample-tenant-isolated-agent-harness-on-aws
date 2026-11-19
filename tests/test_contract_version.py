# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Versioned runtime contract, for compatible rolling upgrades."""

import io
import json

import pytest

from runtime import server
from worker import execute_process as ep


def _response(body: dict) -> dict:
    return {"response": io.BytesIO(json.dumps(body).encode())}


def test_runtime_refuses_an_unknown_task_version():
    with pytest.raises(server.TaskError, match="unsupported task version"):
        server.validate_task({"version": 99}, None, strict=False)


def test_worker_reads_current_and_unversioned_responses():
    current = ep.read_runtime_response(_response({"version": 1, "status": "completed", "result": {"exitCode": 0}}),
                                       s3=None, bucket="b", expected_prefix="transfer/")
    legacy = ep.read_runtime_response(_response({"status": "completed", "result": {"exitCode": 0}}),
                                      s3=None, bucket="b", expected_prefix="transfer/")
    assert current["result"] == legacy["result"] == {"exitCode": 0}


def test_worker_refuses_a_response_version_it_does_not_know():
    with pytest.raises(ep.ExecuteError, match="not understood"):
        ep.read_runtime_response(_response({"version": 2, "status": "completed", "result": {}}),
                                 s3=None, bucket="b", expected_prefix="transfer/")


def test_both_sides_agree_on_the_current_version():
    assert ep.TASK_VERSION in server.SUPPORTED_TASK_VERSIONS
    assert server.RESPONSE_VERSION in ep.KNOWN_RESPONSE_VERSIONS
