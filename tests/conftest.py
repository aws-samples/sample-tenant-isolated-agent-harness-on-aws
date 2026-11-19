# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import hashlib
import hmac
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("AWS_REGION", "ap-southeast-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")
os.environ.setdefault("SPLIT_HARNESS_ALLOW_PLACEHOLDER_ASSETS", "1")


class InvalidMac(Exception):
    """Stands in for KMS.Client.exceptions.KMSInvalidMacException."""


class FakeKms:
    """KMS GenerateMac/VerifyMac with a local HMAC key. Records which operations ran."""

    def __init__(self, key: bytes | None = None):
        self._key = key or os.urandom(32)
        self.calls: list[str] = []

    def generate_mac(self, KeyId, MacAlgorithm, Message):  # noqa: N803 - boto3 casing
        assert MacAlgorithm == "HMAC_SHA_256"
        self.calls.append("GenerateMac")
        return {"Mac": hmac.new(self._key, Message, hashlib.sha256).digest(), "KeyId": KeyId,
                "MacAlgorithm": MacAlgorithm}

    def verify_mac(self, KeyId, MacAlgorithm, Message, Mac):  # noqa: N803
        self.calls.append("VerifyMac")
        if not hmac.compare_digest(hmac.new(self._key, Message, hashlib.sha256).digest(), Mac):
            raise InvalidMac("KMSInvalidMacException")
        return {"MacValid": True, "KeyId": KeyId, "MacAlgorithm": MacAlgorithm}


@pytest.fixture
def kms():
    return FakeKms()


@pytest.fixture
def tenants():
    from shared import tenancy
    return {name: tenancy.storage_id_for(name) for name in ("anycompany-plumbing", "example-corp-electrical")}
