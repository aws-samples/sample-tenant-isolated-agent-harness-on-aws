# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import pytest

from shared import tenancy


def test_storage_id_is_stable_hex_and_distinct(tenants):
    a, b = tenants["anycompany-plumbing"], tenants["example-corp-electrical"]
    assert tenancy.storage_id_for("anycompany-plumbing") == a
    assert tenancy.is_storage_id(a) and tenancy.is_storage_id(b) and a != b


@pytest.mark.parametrize("bad", ["", "a/b", "../x", "x" * 200, None, 5, "tenant id"])
def test_customer_id_validation(bad):
    with pytest.raises(tenancy.TenancyError):
        tenancy.storage_id_for(bad)


def test_runtime_session_id_meets_agentcore_minimum_and_round_trips(tenants):
    sid = tenants["anycompany-plumbing"]
    session = tenancy.runtime_session_id(sid)
    assert len(session) >= 33
    assert tenancy.storage_id_from_runtime_session(session) == sid
    assert tenancy.storage_id_from_runtime_session("tenant-zzz") is None
    assert tenancy.storage_id_from_runtime_session("other-" + sid) is None


def test_fifo_dedup_id_is_tenant_qualified(tenants):
    a, b = tenants.values()
    # Same client idempotency key in two tenants must not collide (would drop tenant B's turn).
    assert tenancy.fifo_deduplication_id(a, "req-00000001") != tenancy.fifo_deduplication_id(b, "req-00000001")
    assert len(tenancy.fifo_deduplication_id(a, "req-00000001")) <= 128


def test_layout_prefixes_are_tenant_scoped(tenants):
    sid = tenants["anycompany-plumbing"]
    assert tenancy.Layout.harness_prefix(sid) == f"tenants/{sid}/harness"
    assert tenancy.Layout.workspace_prefix(sid) == f"tenants/{sid}/workspace/"
    assert tenancy.Layout.uploads_prefix(sid) == f"tenants/{sid}/uploads/"
    assert tenancy.Layout.transfer_prefix(sid, "req-00000001") == f"transfer/{sid}/req-00000001/"
    with pytest.raises(tenancy.TenancyError):
        tenancy.Layout.workspace_prefix("../" + sid)


def test_tenant_context_validate_rejects_mismatched_storage_id(tenants):
    a, b = tenants.values()
    tenancy.TenantContext("anycompany-plumbing", a, "req-00000001").validate()
    with pytest.raises(tenancy.TenancyError):
        tenancy.TenantContext("anycompany-plumbing", b, "req-00000001").validate()
