# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import base64
import json

import pytest

from shared import tenant_token
from tests.conftest import FakeKms

AUD = "split-harness-tools-abc123"


def _mint(kms, sid, now=1_000_000, ttl=600, aud=AUD):
    return tenant_token.mint(kms, "key", tenant_id=sid, request_id="req-00000001", audience=aud,
                             ttl_seconds=ttl, now=now)


def test_round_trip(kms, tenants):
    sid = tenants["anycompany-plumbing"]
    claims = tenant_token.verify(kms, "key", _mint(kms, sid), audience=AUD, now=1_000_100)
    assert claims["tid"] == sid and claims["rid"] == "req-00000001"
    assert kms.calls == ["GenerateMac", "VerifyMac"]


def test_tampered_tenant_is_rejected(kms, tenants):
    a, b = tenants.values()
    payload, mac = _mint(kms, a).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["tid"] = b
    forged = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=").decode()
    with pytest.raises(tenant_token.TokenError, match="not authentic"):
        tenant_token.verify(kms, "key", f"{forged}.{mac}", audience=AUD, now=1_000_100)


def test_token_from_another_key_is_rejected(tenants):
    token = _mint(FakeKms(), tenants["anycompany-plumbing"])
    with pytest.raises(tenant_token.TokenError, match="not authentic"):
        tenant_token.verify(FakeKms(), "key", token, audience=AUD, now=1_000_100)


@pytest.mark.parametrize("now,match", [(1_000_000 + 600 + 31, "expired"), (1_000_000 - 120, "window")])
def test_validity_window(kms, tenants, now, match):
    with pytest.raises(tenant_token.TokenError, match=match):
        tenant_token.verify(kms, "key", _mint(kms, tenants["anycompany-plumbing"]), audience=AUD, now=now)


def test_audience_is_bound(kms, tenants):
    token = _mint(kms, tenants["anycompany-plumbing"], aud="some-other-gateway")
    with pytest.raises(tenant_token.TokenError, match="audience"):
        tenant_token.verify(kms, "key", token, audience=AUD, now=1_000_100)


@pytest.mark.parametrize("token", [None, "", "abc", "a.b.c", "x" * 2000 + ".y"])
def test_malformed_tokens_fail_closed(kms, token):
    with pytest.raises(tenant_token.TokenError):
        tenant_token.verify(kms, "key", token, audience=AUD)


def test_mint_refuses_non_storage_ids(kms):
    with pytest.raises(ValueError):
        tenant_token.mint(kms, "key", tenant_id="anycompany-plumbing", request_id="req-00000001", audience=AUD,
                          ttl_seconds=60)
    with pytest.raises(ValueError):
        tenant_token.mint(kms, "key", tenant_id="0" * 32, request_id="req-00000001", audience=AUD,
                          ttl_seconds=7200)
