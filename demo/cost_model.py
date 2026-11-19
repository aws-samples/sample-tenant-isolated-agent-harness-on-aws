# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Monthly cost per tenant of two designs: all on AgentCore, and the split harness.

Pure arithmetic with no AWS calls. At its defaults it gives the illustrative figures, $4.11
against $1.67 per tenant-month (59% less, 74% less before inference). Its assumptions follow,
and every one can be changed:

* one trades business is one tenant with one serialised agent session;
* events arrive evenly, 4,320 a 30-day month (one every 10 minutes);
* each event uses 2,000 Amazon Nova Lite input tokens and 300 output tokens;
* AgentCore Runtime v2 consumption pricing: memory is billed at 1 GB while busy and for
  ``reclaim_after_s`` idle seconds, then at the 128 MB floor until the idle timeout ends the
  session (or the next event arrives first);
* all on AgentCore: every event runs the agent loop in that tenant's microVM, 5 seconds and
  1 active vCPU-second;
* split: a shared 1 GB Arm Lambda runs the loop for 5 seconds, and only the ``code_share`` of
  events that run a script start a microVM (30 seconds, 5 active vCPU-seconds);
* Gateway calls, S3 state, the queue and the lease cost the same in both ($0.12).

Two optional corrections that the defaults leave out:

* ``count_script_time``: the defaults bill both designs as if each event took 5 seconds. A code
  event also waits for its script: the split's worker Lambda waits for the runtime (the call is
  synchronous), and the all-on microVM stays busy for the script. This adds the script's time to
  both designs.
* ``include_isolation``: the tenant isolation boundary in this repository, mostly KMS requests,
  added to the split only (docs/tenant-isolation.md, "Cost of the boundary").
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

MONTH_SECONDS = 30 * 24 * 3600

# name: (minimum, maximum) for every numeric input; booleans are checked separately.
BOUNDS: dict[str, tuple[float, float]] = {
    "events_per_month": (1, 1_000_000),
    "code_share": (0, 1),
    "input_tokens": (0, 1_000_000),
    "output_tokens": (0, 1_000_000),
    "price_input_per_m": (0, 1_000),
    "price_output_per_m": (0, 1_000),
    "runtime_gb_hour": (0, 10),
    "runtime_vcpu_hour": (0, 10),
    "memory_gb": (0.128, 16),
    "floor_gb": (0, 16),
    "reclaim_after_s": (0, 28_800),
    "idle_timeout_s": (1, 28_800),
    "allon_active_s": (0, 900),
    "allon_vcpu_s": (0, 3_600),
    "script_s": (0, 3_600),
    "script_vcpu_s": (0, 3_600),
    "lambda_gb": (0.128, 10.24),
    "lambda_s": (0, 900),
    "lambda_gb_second": (0, 1),
    "lambda_request_per_m": (0, 100),
    "other_per_tenant": (0, 1_000),
    "kms_requests_per_event": (0, 1_000),
    "kms_per_10k": (0, 100),
    "interceptor_calls_per_event": (0, 100),
    "interceptor_s": (0, 900),
    "interceptor_gb": (0.128, 10.24),
    "tenants": (1, 10_000_000),
}
FLAGS = ("count_script_time", "include_isolation")


class CostInputError(ValueError):
    pass


@dataclass(frozen=True)
class Inputs:
    events_per_month: float = 4320
    code_share: float = 0.10
    input_tokens: float = 2000
    output_tokens: float = 300
    price_input_per_m: float = 0.06        # Amazon Nova Lite, USD per million input tokens
    price_output_per_m: float = 0.24
    runtime_gb_hour: float = 0.0169        # AgentCore Runtime v2, consumption
    runtime_vcpu_hour: float = 0.1276
    memory_gb: float = 1.0
    floor_gb: float = 0.128
    reclaim_after_s: float = 120
    idle_timeout_s: float = 300
    allon_active_s: float = 5
    allon_vcpu_s: float = 1
    script_s: float = 30
    script_vcpu_s: float = 5
    lambda_gb: float = 1.0
    lambda_s: float = 5
    lambda_gb_second: float = 0.0000133334  # Arm
    lambda_request_per_m: float = 0.20
    other_per_tenant: float = 0.1205       # Gateway, S3 state, FIFO queue and lease: $0.12 rounded;
                                           # the fleet totals need the unrounded $0.1205
    kms_requests_per_event: float = 11
    kms_per_10k: float = 0.03
    interceptor_calls_per_event: float = 2.5
    interceptor_s: float = 0.05
    interceptor_gb: float = 0.5
    tenants: float = 10_000
    count_script_time: bool = False
    include_isolation: bool = False


def parse(overrides: dict | None) -> Inputs:
    """Defaults with ``overrides`` applied; rejects unknown names, wrong types and out-of-range values."""
    overrides = dict(overrides or {})
    known = {f.name for f in fields(Inputs)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise CostInputError(f"unknown inputs: {', '.join(unknown)}")
    for name, value in overrides.items():
        if name in FLAGS:
            if not isinstance(value, bool):
                raise CostInputError(f"{name} must be true or false")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CostInputError(f"{name} must be a number")
        low, high = BOUNDS[name]
        if not low <= value <= high:
            raise CostInputError(f"{name} must be between {low:g} and {high:g}")
        overrides[name] = float(value)
    inputs = Inputs(**overrides)
    if inputs.floor_gb > inputs.memory_gb:
        raise CostInputError("floor_gb cannot exceed memory_gb")
    return inputs


def _window(i: Inputs, busy_s: float, vcpu_s: float, events: float) -> tuple[float, float]:
    """Cost of one microVM window and the windows' total billed seconds per month.

    A window is the busy time plus the idle time until the next event or the idle timeout,
    whichever comes first. Idle memory stays at ``memory_gb`` for ``reclaim_after_s`` seconds,
    then drops to ``floor_gb``. Events are spread evenly over the month.
    """
    if events <= 0:
        return 0.0, 0.0
    busy = min(busy_s, MONTH_SECONDS / events)
    idle = max(0.0, min(MONTH_SECONDS / events - busy, i.idle_timeout_s))
    full_idle = min(idle, i.reclaim_after_s)
    floor_idle = idle - full_idle
    gb_seconds = (busy + full_idle) * i.memory_gb + floor_idle * i.floor_gb
    cost = gb_seconds / 3600 * i.runtime_gb_hour + vcpu_s / 3600 * i.runtime_vcpu_hour
    return cost, (busy + idle) * events


def _designs(i: Inputs) -> dict:
    events = i.events_per_month
    code_events = events * i.code_share
    inference = events * (i.input_tokens * i.price_input_per_m + i.output_tokens * i.price_output_per_m) / 1e6
    lambda_second = i.lambda_gb * i.lambda_gb_second

    # All on AgentCore: the loop runs in the tenant's microVM for every event.
    routine_events = events - code_events if i.count_script_time else events
    allon_cost, allon_seconds = _window(i, i.allon_active_s, i.allon_vcpu_s, routine_events)
    allon_runtime = allon_cost * routine_events
    if i.count_script_time and code_events:
        code_cost, code_seconds = _window(i, i.allon_active_s + i.script_s, i.allon_vcpu_s + i.script_vcpu_s,
                                          code_events)
        allon_runtime += code_cost * code_events
        allon_seconds += code_seconds
    allon_runtime = min(allon_runtime, _always_on(i))  # one microVM per tenant cannot bill past the month

    # Split: shared Lambda for every event; a microVM only for code events.
    split_cost, split_seconds = _window(i, i.script_s, i.script_vcpu_s, code_events)
    split_runtime = split_cost * code_events
    worker = events * (i.lambda_s * lambda_second + i.lambda_request_per_m / 1e6)
    worker_wait = code_events * i.script_s * lambda_second if i.count_script_time else 0.0
    isolation = 0.0
    if i.include_isolation:
        isolation = events * (i.kms_requests_per_event * i.kms_per_10k / 10_000
                              + i.interceptor_calls_per_event
                              * (i.interceptor_s * i.interceptor_gb * i.lambda_gb_second + i.lambda_request_per_m / 1e6))

    all_on = {"inference": inference, "runtime": allon_runtime, "worker": 0.0, "workerWait": 0.0,
              "other": i.other_per_tenant, "isolation": 0.0}
    split = {"inference": inference, "runtime": split_runtime, "worker": worker, "workerWait": worker_wait,
             "other": i.other_per_tenant, "isolation": isolation}
    all_on["total"] = sum(all_on.values())
    split["total"] = sum(split.values())
    return {"allOn": all_on, "split": split,
            "microvm": {"allOnWindows": events, "splitWindows": code_events,
                        "allOnHours": min(allon_seconds, MONTH_SECONDS) / 3600, "splitHours": split_seconds / 3600}}


def _always_on(i: Inputs) -> float:
    """A microVM held busy at full memory for the whole month: the ceiling for one tenant."""
    return (MONTH_SECONDS / 3600 * i.memory_gb * i.runtime_gb_hour
            + i.events_per_month * (i.allon_vcpu_s + i.code_share * i.script_vcpu_s) / 3600 * i.runtime_vcpu_hour)


def break_even_code_share(i: Inputs) -> float | None:
    """The share of events that run code at which the split stops being cheaper, or None if it never does."""
    def gap(share: float) -> float:
        d = _designs(Inputs(**{**asdict(i), "code_share": share}))
        return d["split"]["total"] - d["allOn"]["total"]

    if gap(1.0) < 0:
        return None
    if gap(0.0) >= 0:
        return 0.0
    low, high = 0.0, 1.0
    for _ in range(60):
        mid = (low + high) / 2
        low, high = (mid, high) if gap(mid) < 0 else (low, mid)
    return high


def compute(overrides: dict | None = None) -> dict:
    """Both designs per tenant and across ``tenants``, with the saving and the break-even code share."""
    i = parse(overrides)
    d = _designs(i)
    all_on, split = d["allOn"], d["split"]
    saving = all_on["total"] - split["total"]
    before_inference = all_on["total"] - all_on["inference"]
    return {
        "inputs": asdict(i),
        "perTenant": {"allOn": _rounded(all_on), "split": _rounded(split)},
        "saving": {
            "perTenant": round(saving, 4),
            "percent": round(100 * saving / all_on["total"], 2) if all_on["total"] else 0.0,
            "percentBeforeInference": round(100 * saving / before_inference, 2) if before_inference else 0.0,
        },
        "fleet": {
            "tenants": i.tenants,
            "allOnMonthly": round(all_on["total"] * i.tenants, 2),
            "splitMonthly": round(split["total"] * i.tenants, 2),
            "savingMonthly": round(saving * i.tenants, 2),
            "savingYearly": round(saving * i.tenants * 12, 2),
        },
        "microvm": _rounded(d["microvm"]),
        "breakEvenCodeShare": _maybe_round(break_even_code_share(i)),
    }


def _rounded(values: dict) -> dict:
    return {key: round(value, 6) for key, value in values.items()}


def _maybe_round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
