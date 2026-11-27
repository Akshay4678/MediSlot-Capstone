"""Domain events published to EventBridge (source medislot.appointments).

Detail types: AppointmentBooked, AppointmentCancelled, AppointmentRescheduled, SlotSeatFreed,
WaitlistOfferIssued, WaitlistOfferExpired (source medislot.appointments) and ReminderDue (also published
with this source by the reminder dispatcher), AppointmentNoShow.
Publishing is best effort AFTER the database commit: a failure is logged, never raised, because the
appointment (RDS) is the system of record and the booking has already succeeded.
"""

import json
import logging

log = logging.getLogger()

SOURCE = "medislot.appointments"
APPOINTMENT_BOOKED = "AppointmentBooked"
APPOINTMENT_CANCELLED = "AppointmentCancelled"
SLOT_SEAT_FREED = "SlotSeatFreed"
APPOINTMENT_RESCHEDULED = "AppointmentRescheduled"
WAITLIST_OFFER_ISSUED = "WaitlistOfferIssued"
WAITLIST_OFFER_EXPIRED = "WaitlistOfferExpired"
REMINDER_DUE = "ReminderDue"
APPOINTMENT_NO_SHOW = "AppointmentNoShow"
APPOINTMENT_COMPLETED = "AppointmentCompleted"


class EventPublisher:
    def __init__(self, client, bus_name="default"):
        self.client, self.bus_name = client, bus_name

    def publish(self, detail_type, detail):
        try:
            result = self.client.put_events(Entries=[{
                "Source": SOURCE, "DetailType": detail_type, "EventBusName": self.bus_name,
                "Detail": json.dumps(detail, default=str)}])
            if result.get("FailedEntryCount"):
                log.error("EventBridge rejected %s: %s", detail_type, result.get("Entries"))
                return False
            return True
        except Exception:  # noqa: BLE001 - never fail a committed booking because of notifications
            log.exception("Could not publish %s", detail_type)
            return False


class NullPublisher:
    def publish(self, detail_type, detail):
        return False
