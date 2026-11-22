# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only usage of the deployed stack, priced at Sydney list rates, for the page's Cost tab.

Three sources, all reads:

* the worker Lambda's ``platform.report`` log records: billed time and memory of every run;
* AgentCore Runtime metrics per minute: metered GB-hours and vCPU-hours, invocations, sessions;
* the coordination table: how many turns used only Gateway tools and how many ran code. The scan
  projects state, tool names and time only, never message or answer text.

This is demo traffic in a sandbox, not a load test: it checks the cost model's per-event
assumptions (worker seconds per turn, what a runtime window costs, whether routine turns start a
runtime), not the share of events that run code.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import time
from typing import Any, Callable

# Sydney on-demand rates from the AWS Price List API, October 2026.
RATES = {
    "lambdaGbSecond": 0.0000133334,  # Arm
    "lambdaRequest": 0.0000002,
    "runtimeVcpuHour": 0.0895,
    "runtimeGbHour": 0.00945,
}
MAX_HOURS = 24                       # one-minute metric resolution over the window
MAX_LOG_EVENTS = 5000
MAX_TURNS = 5000
MODEL_WORKER_SECONDS = 5             # the cost model's assumption per turn
# The coordination-table attributes the turn count reads. The demo operator role may scan these and
# nothing else (MeasureCountTurns in infra/stack.py), so the scan never returns reply text or errors.
TURN_SCAN_ATTRIBUTES = ("pk", "sk", "state", "toolTrace", "updatedAt")
REPORT_TEXT = re.compile(r"Billed Duration: (?P<billed>\d+) ms.*?Max Memory Used: (?P<mem>\d+) MB(?P<init>.*Init Duration)?")


class MeasureError(ValueError):
    pass


def parse_hours(value: Any) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        raise MeasureError(f"hours must be a whole number from 1 to {MAX_HOURS}") from None
    if isinstance(value, bool) or str(hours) != str(value).strip() or not 1 <= hours <= MAX_HOURS:
        raise MeasureError(f"hours must be a whole number from 1 to {MAX_HOURS}")
    return hours


def measure(client: Callable[[str], Any], outputs: dict, stack: str, hours: int = 24,
            now: float | None = None) -> dict:
    hours = parse_hours(hours)
    end = now if now is not None else time.time()
    start = end - hours * 3600
    function = _worker_function(client("cloudformation"), stack)
    config = client("lambda").get_function_configuration(FunctionName=function)
    log_group = (config.get("LoggingConfig") or {}).get("LogGroup") or f"/aws/lambda/{function}"
    memory_gb = config["MemorySize"] / 1024
    return {
        "window": {"hours": hours, "from": _iso(start), "to": _iso(end)},
        "rates": RATES,
        "worker": _worker_runs(client("logs"), log_group, start, end, memory_gb),
        "runtime": _runtime_usage(client("cloudwatch"), outputs["RuntimeArn"], start, end),
        "turns": _turns(client("dynamodb"), outputs["CoordinationTable"], start),
    }


def _worker_function(cloudformation: Any, stack: str) -> str:
    token = None
    while True:
        page = cloudformation.list_stack_resources(StackName=stack, **({"NextToken": token} if token else {}))
        for resource in page.get("StackResourceSummaries", []):
            if (resource.get("ResourceType") == "AWS::Lambda::Function"
                    and resource.get("LogicalResourceId", "").startswith("WorkerFunction")):
                return resource["PhysicalResourceId"]
        token = page.get("NextToken")
        if not token:
            raise MeasureError(f"no worker function in stack {stack}")


def _worker_runs(logs: Any, log_group: str, start: float, end: float, memory_gb: float) -> dict:
    billed, peak, cold = [], 0, 0
    token = None
    while len(billed) < MAX_LOG_EVENTS:
        page = logs.filter_log_events(logGroupName=log_group, startTime=int(start * 1000), endTime=int(end * 1000),
                                      filterPattern='"platform.report"', **({"nextToken": token} if token else {}))
        for event in page.get("events", []):
            run = _report(event.get("message", ""))
            if run:
                billed.append(run[0])
                peak = max(peak, run[1])
                cold += run[2]
        token = page.get("nextToken")
        if not token:
            break
    rate = memory_gb * RATES["lambdaGbSecond"]
    mean = statistics.mean(billed) if billed else None
    return {
        "runs": len(billed),
        "coldStarts": cold,
        "memoryGb": memory_gb,
        "billedSecondsMean": _r(mean),
        "billedSecondsMedian": _r(statistics.median(billed)) if billed else None,
        "billedSecondsMax": _r(max(billed)) if billed else None,
        "maxMemoryUsedMb": peak or None,
        "costPerRun": _r(mean * rate + RATES["lambdaRequest"], 10) if billed else None,
        "costPerRunModel": _r(MODEL_WORKER_SECONDS * rate + RATES["lambdaRequest"], 10),
    }


def _report(message: str) -> tuple[float, int, int] | None:
    """(billed seconds, max memory MB, 1 if cold start) from one platform.report record, JSON or text."""
    try:
        record = json.loads(message)
    except ValueError:
        match = REPORT_TEXT.search(message)
        if not match:
            return None
        return int(match["billed"]) / 1000, int(match["mem"]), 1 if match["init"] else 0
    if not isinstance(record, dict) or record.get("type") != "platform.report":
        return None
    metrics = (record.get("record") or {}).get("metrics") or {}
    if "billedDurationMs" not in metrics:
        return None
    return metrics["billedDurationMs"] / 1000, int(metrics.get("maxMemoryUsedMB") or 0), 1 if metrics.get("initDurationMs") else 0


def _runtime_usage(cloudwatch: Any, runtime_arn: str, start: float, end: float) -> dict:
    name = runtime_arn.rsplit("/", 1)[-1].rsplit("-", 1)[0] + "::DEFAULT"
    usage = [{"Name": "Resource", "Value": runtime_arn}, {"Name": "Service", "Value": "AgentCore.Runtime"},
             {"Name": "Name", "Value": name}]
    calls = [{"Name": "Resource", "Value": runtime_arn}, {"Name": "Operation", "Value": "InvokeAgentRuntime"},
             {"Name": "ComputeType", "Value": "MicroVM"}, {"Name": "Name", "Value": name}]
    queries = {"gb": ("MemoryUsed-GBHours", usage), "cpu": ("CPUUsed-vCPUHours", usage),
               "calls": ("Invocations", calls), "sessions": ("Sessions", calls)}
    series = _metric_series(cloudwatch, queries, start, end)
    gb, cpu = series["gb"], series["cpu"]
    minutes = sorted(t for t, value in gb.items() if value > 0)
    windows = 0
    for index, minute in enumerate(minutes):
        if index == 0 or minute - minutes[index - 1] > 60:
            windows += 1
    gb_hours, cpu_hours = sum(gb.values()), sum(cpu.values())
    sessions = sum(series["sessions"].values())
    cost = gb_hours * RATES["runtimeGbHour"] + cpu_hours * RATES["runtimeVcpuHour"]
    return {
        "invocations": int(sum(series["calls"].values())),
        "sessions": int(sessions),
        "billedWindows": windows,
        "billedMinutes": len(minutes),
        "gbHours": _r(gb_hours, 5),
        "vcpuHours": _r(cpu_hours, 6),
        "averageGbWhileBilled": _r(gb_hours / (len(minutes) / 60), 2) if minutes else None,
        "cost": _r(cost, 10),
        "costPerSession": _r(cost / sessions, 10) if sessions else None,
    }


def _metric_series(cloudwatch: Any, queries: dict, start: float, end: float) -> dict[str, dict[float, float]]:
    request = [{"Id": key, "ReturnData": True, "MetricStat": {
        "Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": metric, "Dimensions": dims},
        "Period": 60, "Stat": "Sum"}} for key, (metric, dims) in queries.items()]
    series: dict[str, dict[float, float]] = {key: {} for key in queries}
    token = None
    while True:
        page = cloudwatch.get_metric_data(MetricDataQueries=request, StartTime=_dt(start), EndTime=_dt(end),
                                          **({"NextToken": token} if token else {}))
        for result in page.get("MetricDataResults", []):
            for stamp, value in zip(result.get("Timestamps", []), result.get("Values", [])):
                series[result["Id"]][_epoch(stamp)] = series[result["Id"]].get(_epoch(stamp), 0.0) + value
        token = page.get("NextToken")
        if not token:
            return series


def _turns(dynamodb: Any, table: str, since: float) -> dict:
    counts = {"completed": 0, "gatewayOnly": 0, "ranCode": 0, "modelOnly": 0, "notCompleted": 0}
    kwargs = {"TableName": table, "Select": "SPECIFIC_ATTRIBUTES",
              "FilterExpression": "begins_with(sk, :req) AND updatedAt >= :since",
              "ProjectionExpression": "#st, toolTrace, updatedAt", "ExpressionAttributeNames": {"#st": "state"},
              "ExpressionAttributeValues": {":req": {"S": "REQ#"}, ":since": {"N": str(int(since))}}}
    seen = 0
    while seen < MAX_TURNS:
        page = dynamodb.scan(**kwargs)
        for item in page.get("Items", []):
            seen += 1
            if item.get("state", {}).get("S") != "COMPLETED":
                counts["notCompleted"] += 1
                continue
            counts["completed"] += 1
            trace = _strings(item.get("toolTrace"))
            key = "ranCode" if "execute_process" in trace else "gatewayOnly" if trace else "modelOnly"
            counts[key] += 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return counts


def _strings(value: Any) -> list[str]:
    if not value:
        return []
    if "SS" in value:
        return list(value["SS"])
    return [entry.get("S", "") for entry in value.get("L", [])]


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _dt(epoch: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)


def _epoch(stamp: Any) -> float:
    return stamp.timestamp() if hasattr(stamp, "timestamp") else float(stamp)


def _iso(epoch: float) -> str:
    return _dt(epoch).isoformat(timespec="seconds")
