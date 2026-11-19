# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cost model reproduces the README's figures and keeps its corrections explicit."""

import pytest

from demo import cost_model


def test_defaults_reproduce_the_illustrative_model():
    result = cost_model.compute()
    all_on, split = result["perTenant"]["allOn"], result["perTenant"]["split"]
    assert round(all_on["inference"], 2) == round(split["inference"], 2) == 0.83
    assert round(all_on["runtime"], 2) == 3.16
    assert round(split["runtime"], 2) == 0.43
    assert round(split["worker"], 2) == 0.29
    assert round(all_on["total"], 2) == 4.11
    assert round(split["total"], 2) == 1.67
    assert round(result["saving"]["percent"]) == 59
    assert round(result["saving"]["percentBeforeInference"]) == 74
    assert result["microvm"]["allOnWindows"] == 4320 and result["microvm"]["splitWindows"] == pytest.approx(432)


def test_fleet_totals_at_100k_tenants():
    fleet = cost_model.compute({"tenants": 100_000})["fleet"]
    assert fleet["allOnMonthly"] * 12 / 1e6 == pytest.approx(4.93, abs=0.01)
    assert fleet["splitMonthly"] * 12 / 1e6 == pytest.approx(2.00, abs=0.01)
    assert fleet["savingYearly"] / 1e6 == pytest.approx(2.93, abs=0.01)


def test_counting_script_time_adds_to_both_designs():
    base = cost_model.compute()["perTenant"]
    counted = cost_model.compute({"count_script_time": True})["perTenant"]
    assert round(counted["split"]["workerWait"], 2) == 0.17       # 432 x 30 s of 1 GB Lambda
    # All on AgentCore, a code event keeps the microVM busy 30 s longer and adds the script's 5 vCPU-s.
    assert round(counted["allOn"]["total"] - base["allOn"]["total"], 2) == 0.14
    assert counted["split"]["runtime"] == pytest.approx(base["split"]["runtime"])  # already in the defaults
    assert round(counted["allOn"]["total"], 2) == 4.24 and round(counted["split"]["total"], 2) == 1.84


def test_isolation_boundary_adds_fifteen_cents_to_the_split_only():
    base = cost_model.compute()["perTenant"]
    isolated = cost_model.compute({"include_isolation": True})["perTenant"]
    assert round(isolated["split"]["isolation"], 2) == 0.15
    assert isolated["allOn"]["total"] == pytest.approx(base["allOn"]["total"])


def test_both_corrections_together():
    result = cost_model.compute({"count_script_time": True, "include_isolation": True})
    assert round(result["perTenant"]["allOn"]["total"], 2) == 4.24
    assert round(result["perTenant"]["split"]["total"], 2) == 1.99
    assert round(result["saving"]["percent"]) == 53


def test_break_even_code_share():
    # The split stays cheaper until about two events in three run code.
    assert cost_model.compute()["breakEvenCodeShare"] == pytest.approx(0.67, abs=0.01)
    assert cost_model.compute({"count_script_time": True})["breakEvenCodeShare"] == pytest.approx(0.62, abs=0.01)
    # If nothing ever ran code, there would be no runtime cost to save on in the split.
    assert cost_model.compute({"code_share": 0.0})["saving"]["percent"] > 59


def test_events_closer_than_the_idle_timeout_keep_one_microvm_alive():
    result = cost_model.compute({"events_per_month": 43_200})  # one a minute
    assert result["microvm"]["allOnHours"] == pytest.approx(720)  # the whole month, not 43,200 windows
    ceiling = 720 * 1.0 * 0.0169 + 43_200 * (1 + 0.1 * 5) / 3600 * 0.1276
    assert result["perTenant"]["allOn"]["runtime"] <= ceiling + 1e-9


@pytest.mark.parametrize("overrides,message", [
    ({"surprise": 1}, "unknown inputs"),
    ({"code_share": 1.5}, "between"),
    ({"events_per_month": "4320"}, "must be a number"),
    ({"events_per_month": True}, "must be a number"),
    ({"events_per_month": float("nan")}, "must be a number"),
    ({"include_isolation": "yes"}, "true or false"),
    ({"floor_gb": 2.0, "memory_gb": 1.0}, "cannot exceed"),
])
def test_rejects_bad_inputs(overrides, message):
    with pytest.raises(cost_model.CostInputError, match=message):
        cost_model.compute(overrides)
