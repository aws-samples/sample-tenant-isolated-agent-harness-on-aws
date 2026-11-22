# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Measured usage: parsed from stubbed CloudFormation, Lambda, Logs, CloudWatch and DynamoDB reads."""

import datetime as dt
import json

import pytest

from demo import measured

NOW = 1_792_400_000.0
ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:111122223333:runtime/split_harness_exec-AbCdEf1234"


def _report(billed_ms, mem_mb, init_ms=None):
    metrics = {"durationMs": billed_ms - 1, "billedDurationMs": billed_ms, "maxMemoryUsedMB": mem_mb}
    if init_ms:
        metrics["initDurationMs"] = init_ms
    return json.dumps({"type": "platform.report", "record": {"metrics": metrics}})


class Stub:
    def __init__(self):
        self.calls = []

    def list_stack_resources(self, **kwargs):
        self.calls.append(("cfn", kwargs))
        if "NextToken" not in kwargs:
            return {"StackResourceSummaries": [{"ResourceType": "AWS::S3::Bucket", "LogicalResourceId": "Tenant",
                                                "PhysicalResourceId": "b"}], "NextToken": "p2"}
        return {"StackResourceSummaries": [{"ResourceType": "AWS::Lambda::Function",
                                            "LogicalResourceId": "WorkerFunctionABC", "PhysicalResourceId": "worker-fn"}]}

    def get_function_configuration(self, FunctionName):
        assert FunctionName == "worker-fn"
        return {"MemorySize": 1024, "LoggingConfig": {"LogGroup": "worker-logs"}}

    def filter_log_events(self, **kwargs):
        assert kwargs["logGroupName"] == "worker-logs" and kwargs["filterPattern"] == '"platform.report"'
        if "nextToken" not in kwargs:
            return {"events": [{"message": _report(8000, 170, init_ms=650)}, {"message": "START RequestId: x"}],
                    "nextToken": "n2"}
        return {"events": [{"message": _report(3000, 260)},
                           {"message": "REPORT RequestId: y Duration: 3999.1 ms Billed Duration: 4000 ms "
                                       "Memory Size: 1024 MB Max Memory Used: 280 MB"}]}

    def get_metric_data(self, **kwargs):
        dims = {q["Id"]: {d["Name"]: d["Value"] for d in q["MetricStat"]["Metric"]["Dimensions"]}
                for q in kwargs["MetricDataQueries"]}
        assert dims["gb"]["Name"] == "split_harness_exec::DEFAULT" and dims["calls"]["Operation"] == "InvokeAgentRuntime"
        t = lambda minute: dt.datetime.fromtimestamp(NOW - 3600 + minute * 60, tz=dt.timezone.utc)  # noqa: E731
        return {"MetricDataResults": [
            # two windows: minutes 0-4 at 1 GB, then minutes 20-21
            {"Id": "gb", "Timestamps": [t(m) for m in (0, 1, 2, 3, 4, 20, 21)], "Values": [1 / 60] * 7},
            {"Id": "cpu", "Timestamps": [t(0), t(20)], "Values": [0.001, 0.002]},
            {"Id": "calls", "Timestamps": [t(0), t(20)], "Values": [1, 2]},
            {"Id": "sessions", "Timestamps": [t(0), t(20)], "Values": [1, 1]},
        ]}

    def scan(self, **kwargs):
        assert "resultText" not in kwargs["ProjectionExpression"] and "message" not in kwargs["ProjectionExpression"]
        # Only what the demo operator role may scan (MeasureCountTurns in infra/stack.py).
        assert kwargs["Select"] == "SPECIFIC_ATTRIBUTES"
        names = kwargs["ExpressionAttributeNames"]
        projected = {names.get(n.strip(), n.strip()) for n in kwargs["ProjectionExpression"].split(",")}
        assert projected <= set(measured.TURN_SCAN_ATTRIBUTES) and {"pk", "sk"} <= set(measured.TURN_SCAN_ATTRIBUTES)
        items = [
            {"state": {"S": "COMPLETED"}, "toolTrace": {"L": [{"S": "saas___list_jobs"}]}},
            {"state": {"S": "COMPLETED"}, "toolTrace": {"L": [{"S": "execute_process"}]}},
            {"state": {"S": "COMPLETED"}, "toolTrace": {"L": []}},
            {"state": {"S": "FAILED"}},
        ]
        if "ExclusiveStartKey" not in kwargs:
            return {"Items": items[:2], "LastEvaluatedKey": {"pk": {"S": "x"}}}
        return {"Items": items[2:]}


@pytest.fixture
def result():
    stub = Stub()
    return measured.measure(lambda name: stub, {"RuntimeArn": ARN, "CoordinationTable": "coord"}, "SplitHarness",
                            hours=2, now=NOW)


def test_worker_runs_from_platform_reports(result):
    worker = result["worker"]
    assert worker["runs"] == 3 and worker["coldStarts"] == 1 and worker["maxMemoryUsedMb"] == 280
    assert worker["billedSecondsMean"] == pytest.approx(5.0)
    assert worker["costPerRun"] == pytest.approx(5 * 0.0000133334 + 0.0000002, rel=1e-6)


def test_runtime_usage_per_minute(result):
    runtime = result["runtime"]
    assert runtime["billedWindows"] == 2 and runtime["billedMinutes"] == 7
    assert runtime["averageGbWhileBilled"] == pytest.approx(1.0)
    assert runtime["invocations"] == 3 and runtime["sessions"] == 2
    expected = 7 / 60 * 0.00945 + 0.003 * 0.0895
    assert runtime["cost"] == pytest.approx(expected, rel=1e-4)
    assert runtime["costPerSession"] == pytest.approx(expected / 2, rel=1e-4)


def test_turns_by_path(result):
    assert result["turns"] == {"completed": 3, "gatewayOnly": 1, "ranCode": 1, "modelOnly": 1, "notCompleted": 1}


@pytest.mark.parametrize("hours", [0, 25, "abc", "2.5", True, None])
def test_rejects_bad_windows(hours):
    with pytest.raises(measured.MeasureError):
        measured.parse_hours(hours)
