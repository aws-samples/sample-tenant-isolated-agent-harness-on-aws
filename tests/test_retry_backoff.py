# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A failed turn must not block its tenant's FIFO lane for the queue's 60-minute visibility timeout."""

from worker import handler as wh

ARN = "arn:aws:sqs:ap-southeast-2:123456789012:SplitHarness-TurnQueue.fifo"
URL = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/SplitHarness-TurnQueue.fifo"


class FakeSqs:
    def __init__(self, fail: bool = False):
        self.calls, self.fail = [], fail

    def change_message_visibility(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("throttled")


def _record(receive_count: str) -> dict:
    return {"messageId": "m1", "receiptHandle": "rh-1", "eventSourceARN": ARN, "body": "{}",
            "attributes": {"ApproximateReceiveCount": receive_count}}


def test_failed_turn_is_reported_and_retried_after_a_short_backoff(monkeypatch):
    sqs = FakeSqs()
    monkeypatch.setitem(wh._clients, "sqs", sqs)
    monkeypatch.setattr(wh, "process_record", lambda record, context: (_ for _ in ()).throw(RuntimeError("boom")))
    first = wh.handler({"Records": [_record("1")]}, None)
    second = wh.handler({"Records": [_record("2")]}, None)
    assert first == second == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert [c["VisibilityTimeout"] for c in sqs.calls] == [30, 120]
    assert sqs.calls[0] == {"QueueUrl": URL, "ReceiptHandle": "rh-1", "VisibilityTimeout": 30}


def test_backoff_failure_still_reports_the_turn(monkeypatch):
    monkeypatch.setitem(wh._clients, "sqs", FakeSqs(fail=True))
    monkeypatch.setattr(wh, "process_record", lambda record, context: (_ for _ in ()).throw(RuntimeError("boom")))
    assert wh.handler({"Records": [_record("1")]}, None) == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_successful_turn_leaves_visibility_alone(monkeypatch):
    sqs = FakeSqs()
    monkeypatch.setitem(wh._clients, "sqs", sqs)
    monkeypatch.setattr(wh, "process_record", lambda record, context: None)
    assert wh.handler({"Records": [_record("1")]}, None) == {"batchItemFailures": []}
    assert sqs.calls == []
