"""Notification consumer: the eight notices, exactly-once dispatch and per-message failure reporting."""

import json
from datetime import datetime

from fake_dynamo import FakeTable

from application.booking import events
from application.notification import consumer
from application.notification.dispatch_log import DispatchLog

CLOCK = lambda: datetime(2026, 11, 26, 12, 0)  # noqa: E731


class Sns:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def publish(self, **kw):
        if self.fail:
            raise RuntimeError("sns down")
        self.sent.append(kw)


class Metrics:
    def __init__(self):
        self.puts = []

    def put(self, name, value, dims=None):
        self.puts.append((name, value, dims))


def dispatcher(sns=None):
    sns = sns or Sns()
    return consumer.Dispatcher(DispatchLog(FakeTable("dispatch_id"), CLOCK), sns,
                               {"PATIENT": "arn:p", "DOCTOR": "arn:d"}, Metrics()), sns


def test_every_event_produces_the_documented_notices():
    base = {"appointment_id": "a1", "starts_at_utc": "2026-12-01T09:00:00Z", "offer_id": "o1"}
    cases = [
        (events.APPOINTMENT_BOOKED, base, {"PATIENT", "DOCTOR"}),
        (events.APPOINTMENT_RESCHEDULED, base, {"PATIENT", "DOCTOR"}),
        (events.APPOINTMENT_CANCELLED, dict(base, cancelled_by="patient"), {"PATIENT", "DOCTOR"}),
        (events.APPOINTMENT_CANCELLED, dict(base, cancelled_by="doctor"), {"PATIENT"}),
        (events.WAITLIST_OFFER_ISSUED, base, {"PATIENT"}),
        (events.WAITLIST_OFFER_EXPIRED, base, {"PATIENT"}),
        (events.REMINDER_DUE, dict(base, reminder_type="24H"), {"PATIENT"}),
        (events.REMINDER_DUE, dict(base, reminder_type="2H"), {"PATIENT"}),
    ]
    for detail_type, detail, who in cases:
        notices = consumer.plan(detail_type, detail)
        assert {n.recipient for n in notices} == who, (detail_type, detail)
    assert consumer.plan("Unknown", base) == []


def test_reminder_intervals_have_distinct_dispatch_ids():
    a = consumer.plan(events.REMINDER_DUE, {"appointment_id": "a", "reminder_type": "24H"})[0]
    b = consumer.plan(events.REMINDER_DUE, {"appointment_id": "a", "reminder_type": "2H"})[0]
    assert a.dispatch_id != b.dispatch_id


def test_the_same_event_delivered_twice_is_sent_once():
    d, sns = dispatcher()
    envelope = {"detail-type": events.APPOINTMENT_BOOKED, "detail": {"appointment_id": "a1"}}
    assert d.handle_envelope(envelope) == ["sent", "sent"]
    assert d.handle_envelope(envelope) == ["duplicate", "duplicate"]
    assert len(sns.sent) == 2
    assert {s["TopicArn"] for s in sns.sent} == {"arn:p", "arn:d"}


def test_only_the_failed_message_of_a_batch_is_reported():
    d, _ = dispatcher()
    good = {"messageId": "m1", "body": json.dumps({"detail-type": events.APPOINTMENT_BOOKED, "detail": {"appointment_id": "a"}})}
    bad = {"messageId": "m2", "body": "not json"}
    assert consumer.lambda_handler({"Records": [good, bad]}, None, dispatcher=d) == {"batchItemFailures": [{"itemIdentifier": "m2"}]}


def test_a_failed_publish_can_be_retried_after_the_lease():
    d, _ = dispatcher(Sns(fail=True))
    envelope = {"detail-type": events.WAITLIST_OFFER_EXPIRED, "detail": {"offer_id": "o"}}
    try:
        d.handle_envelope(envelope)
        raise AssertionError("expected failure")
    except RuntimeError:
        pass
    # the claim is still SENDING: a retry inside the lease is refused rather than double-sent
    try:
        d.handle_envelope(envelope)
        raise AssertionError("expected busy")
    except RuntimeError as exc:
        assert "another worker" in str(exc)
