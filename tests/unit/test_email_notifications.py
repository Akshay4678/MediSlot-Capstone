"""Each person's notifications go to the e-mail address they registered with (SNS e-mail + filter policy)."""

import json

from booking_support import SCHEMA, seed
from fake_dynamo import FakeTable
from sqlite_mysql import Conn
from test_notifications import CLOCK, Metrics, Sns

from application.booking import events
from application.notification import consumer, subscriptions
from application.notification.dispatch_log import DispatchLog


class SnsClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def subscribe(self, **kw):
        if self.fail:
            raise RuntimeError("sns down")
        self.calls.append(kw)


def test_subscribe_uses_the_registered_email_and_a_recipient_filter(monkeypatch):
    monkeypatch.setenv("PATIENT_TOPIC_ARN", "arn:patients")
    client = SnsClient()
    assert subscriptions.ensure_subscribed("PATIENT", "p-1", "pat@example.com", client) is True
    call = client.calls[0]
    assert call["TopicArn"] == "arn:patients" and call["Protocol"] == "email" and call["Endpoint"] == "pat@example.com"
    assert json.loads(call["Attributes"]["FilterPolicy"]) == {"recipient_id": ["p-1"]}
    assert call["Attributes"]["FilterPolicyScope"] == "MessageAttributes"


def test_a_missing_topic_email_or_an_sns_failure_never_breaks_the_caller(monkeypatch):
    monkeypatch.delenv("PATIENT_TOPIC_ARN", raising=False)
    assert subscriptions.ensure_subscribed("PATIENT", "p-1", "a@b.c", SnsClient()) is False
    monkeypatch.setenv("PATIENT_TOPIC_ARN", "arn:patients")
    assert subscriptions.ensure_subscribed("PATIENT", "p-1", "", SnsClient()) is False
    assert subscriptions.ensure_subscribed("PATIENT", "p-1", "a@b.c", SnsClient(fail=True)) is False


def test_backfill_subscribes_every_active_patient_and_doctor_with_their_own_email(monkeypatch):
    monkeypatch.setenv("PATIENT_TOPIC_ARN", "arn:patients")
    monkeypatch.setenv("DOCTOR_TOPIC_ARN", "arn:doctors")
    conn = Conn(SCHEMA)
    seed(conn.db, patients=2)
    client = SnsClient()
    assert subscriptions.subscribe_all(conn, client) == {"patients": 2, "doctors": 2}
    sent = {(c["TopicArn"], c["Endpoint"]) for c in client.calls}
    assert ("arn:patients", "p1@x") in sent and ("arn:doctors", "d1@x") in sent and ("arn:doctors", "d2@x") in sent


def test_the_consumer_addresses_each_notice_to_the_right_person_and_sends_readable_text():
    sns = Sns()
    d = consumer.Dispatcher(DispatchLog(FakeTable("dispatch_id"), CLOCK), sns, {"PATIENT": "arn:p", "DOCTOR": "arn:d"}, Metrics())
    d.handle_envelope({"detail-type": events.APPOINTMENT_BOOKED,
                       "detail": {"appointment_id": "a1", "patient_id": "p-1", "doctor_id": "d-9", "starts_at_utc": "2026-12-01T09:00:00Z"}})
    by_topic = {s["TopicArn"]: s for s in sns.sent}
    assert by_topic["arn:p"]["MessageAttributes"]["recipient_id"]["StringValue"] == "p-1"
    assert by_topic["arn:d"]["MessageAttributes"]["recipient_id"]["StringValue"] == "d-9"
    assert by_topic["arn:p"]["Message"].startswith("Appointment a1 is confirmed")  # plain text, not JSON


def test_creating_a_patient_profile_subscribes_the_registered_email(monkeypatch):
    from application.patient_api import profile
    monkeypatch.setenv("PATIENT_TOPIC_ARN", "arn:patients")
    conn = Conn(SCHEMA)
    conn.db.execute("INSERT INTO users VALUES ('u-new','new@example.com',1)")
    conn.db.commit()
    client = SnsClient()
    monkeypatch.setattr(subscriptions, "_client", lambda: client)
    status, body = profile.create(conn, "u-new", {"first_name": "Nina", "last_name": "N", "date_of_birth": "1990-05-04"})
    assert status == 201
    assert [(c["Endpoint"], json.loads(c["Attributes"]["FilterPolicy"])["recipient_id"][0]) for c in client.calls] == [
        ("new@example.com", body["patient"]["patient_id"])]
