"""Notification consumer: EventBridge -> SQS -> this Lambda -> SNS (Milestone 6).

The eight notifications of the frozen rule set
    1 AppointmentBooked                         patient + doctor
    2 AppointmentRescheduled                    patient + doctor
    3 AppointmentCancelled (cancelled_by patient)  patient + doctor
    4 AppointmentCancelled (cancelled_by doctor)   patient
    5 WaitlistOfferIssued                       patient
    6 WaitlistOfferExpired                      patient
    7 ReminderDue 24H                           patient
    8 ReminderDue 2H                            patient

Flow per message: build the notices -> claim each one in the DynamoDB dispatch log (exactly once) -> publish
to the SNS topic of that recipient type -> mark SENT. A message that fails goes back to the queue (SQS visibility
timeout) and, after maxReceiveCount, to this consumer's own dead-letter queue. Only the failed messages of a
batch are retried (ReportBatchItemFailures). Real e-mail or SMS delivery is out of scope: SNS is the end point
and the audit queue subscribed to the topics lets the messages be inspected.
"""

import json
import logging
import os
from collections import namedtuple

from application.booking import events
from application.notification import dispatch_log as dl

log = logging.getLogger()
log.setLevel(logging.INFO)

PATIENT, DOCTOR = "PATIENT", "DOCTOR"
Notice = namedtuple("Notice", "dispatch_id recipient event subject text")


def _when(detail):
    return str(detail.get("starts_at_utc") or "").replace("T", " ").replace("Z", " UTC").strip()


def plan(detail_type, detail):
    """The notices that one event produces (pure function, unit-tested)."""
    appt = detail.get("appointment_id")
    when = _when(detail)
    if detail_type == events.APPOINTMENT_BOOKED:
        text = f"Appointment {appt} is confirmed for {when}."
        return [Notice(f"{appt}#BOOKED#{r}", r, detail_type, "Appointment booked", text) for r in (PATIENT, DOCTOR)]
    if detail_type == events.APPOINTMENT_RESCHEDULED:
        n = detail.get("reschedule_count", 1)
        text = f"Appointment {appt} was moved to {when}."
        return [Notice(f"{appt}#RESCHEDULED{n}#{r}", r, detail_type, "Appointment rescheduled", text)
                for r in (PATIENT, DOCTOR)]
    if detail_type == events.APPOINTMENT_CANCELLED:
        refund = detail.get("refund") or {}
        money = f" Refund: {refund.get('amount')} ({refund.get('percentage')}%)." if refund else ""
        if detail.get("cancelled_by") == "doctor":
            text = f"Appointment {appt} was cancelled by the doctor.{money}"
            return [Notice(f"{appt}#CANCELLED#{PATIENT}", PATIENT, detail_type, "Appointment cancelled by the doctor", text)]
        text = f"Appointment {appt} was cancelled by the patient.{money}"
        return [Notice(f"{appt}#CANCELLED#{r}", r, detail_type, "Appointment cancelled", text) for r in (PATIENT, DOCTOR)]
    if detail_type == events.WAITLIST_OFFER_ISSUED:
        offer = detail.get("offer_id")
        text = (f"A seat is held for you until {detail.get('expires_at_utc')}. "
                f"Accept offer {offer} before then or it passes to the next patient.")
        return [Notice(f"{offer}#OFFER_ISSUED#{PATIENT}", PATIENT, detail_type, "A seat is held for you", text)]
    if detail_type == events.WAITLIST_OFFER_EXPIRED:
        offer = detail.get("offer_id")
        return [Notice(f"{offer}#OFFER_EXPIRED#{PATIENT}", PATIENT, detail_type, "Your waitlist offer expired",
                       f"Offer {offer} expired and the seat was passed to the next patient.")]
    if detail_type == events.REMINDER_DUE:
        interval = str(detail.get("reminder_type") or "").upper()
        label = "24 hours" if interval == "24H" else "2 hours"
        return [Notice(f"{appt}#REMINDER_{interval}#{PATIENT}", PATIENT, detail_type, f"Reminder: appointment in {label}",
                       f"Your appointment {appt} starts at {when}.")]
    return []


class Dispatcher:
    def __init__(self, log_, sns, topics, metrics):
        self.dispatch_log, self.sns, self.topics, self.metrics = log_, sns, topics, metrics

    def deliver(self, notice, detail):
        """Returns "sent" or "duplicate". Raises when the message must be retried."""
        state = self.dispatch_log.claim(notice.dispatch_id, event=notice.event, recipient=notice.recipient)
        if state == dl.DUPLICATE:
            return "duplicate"
        if state == dl.BUSY:
            raise RuntimeError(f"{notice.dispatch_id} is being sent by another worker")
        attributes = {"event": {"DataType": "String", "StringValue": notice.event},
                      "recipient": {"DataType": "String", "StringValue": notice.recipient},
                      "dispatch_id": {"DataType": "String", "StringValue": notice.dispatch_id}}
        # The e-mail subscription of each person filters on recipient_id (see subscriptions.py): only that person gets it.
        recipient_id = detail.get("patient_id" if notice.recipient == PATIENT else "doctor_id")
        if recipient_id:
            attributes["recipient_id"] = {"DataType": "String", "StringValue": str(recipient_id)}
        self.sns.publish(TopicArn=self.topics[notice.recipient], Subject=notice.subject[:100],
                         Message=notice.text, MessageAttributes=attributes)
        self.dispatch_log.mark_sent(notice.dispatch_id)
        self.metrics.put("NotificationsDispatched", 1, {"Event": notice.event, "Recipient": notice.recipient})
        return "sent"

    def handle_envelope(self, envelope):
        detail = envelope.get("detail") or {}
        results = [self.deliver(n, detail) for n in plan(envelope.get("detail-type"), detail)]
        return results


_cached = None


def default_dispatcher():
    global _cached
    if _cached is None:
        import boto3
        from application.booking.metrics import Metrics
        from application.booking.deps import _utcnow
        ddb = boto3.resource("dynamodb")
        _cached = Dispatcher(
            dl.DispatchLog(ddb.Table(os.environ["DISPATCH_LOG_TABLE_NAME"]), _utcnow), boto3.client("sns"),
            {PATIENT: os.environ["PATIENT_TOPIC_ARN"], DOCTOR: os.environ["DOCTOR_TOPIC_ARN"]},
            Metrics(boto3.client("cloudwatch"), environment=os.environ.get("ENVIRONMENT_NAME", "dev")))
    return _cached


def lambda_handler(event, context, dispatcher=None):
    dispatcher = dispatcher or default_dispatcher()
    failures = []
    for record in event.get("Records", []):
        try:
            dispatcher.handle_envelope(json.loads(record["body"]))
        except Exception:  # noqa: BLE001 - the message returns to the queue, then to the DLQ
            log.exception("notification failed for message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
