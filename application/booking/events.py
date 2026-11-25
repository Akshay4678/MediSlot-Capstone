"""Domain events published to EventBridge (source medislot.appointments)."""
import json, logging, uuid
log = logging.getLogger()
SOURCE = "medislot.appointments"
APPOINTMENT_BOOKED = "AppointmentBooked"
APPOINTMENT_CANCELLED = "AppointmentCancelled"
APPOINTMENT_RESCHEDULED = "AppointmentRescheduled"
SLOT_SEAT_FREED = "SlotSeatFreed"
WAITLIST_JOINED = "WaitlistJoined"
WAITLIST_LEFT = "WaitlistLeft"
WAITLIST_HOLD_OFFERED = "WaitlistHoldOffered"
WAITLIST_OFFER_ACCEPTED = "WaitlistOfferAccepted"
OFFERS_EXPIRED = "OffersExpired"
NOTIFICATION_EVENT_TYPES = (
    APPOINTMENT_BOOKED, APPOINTMENT_CANCELLED, APPOINTMENT_RESCHEDULED,
    WAITLIST_JOINED, WAITLIST_LEFT, WAITLIST_HOLD_OFFERED,
    WAITLIST_OFFER_ACCEPTED, OFFERS_EXPIRED,
)

class EventPublisher:
    def __init__(self, client, bus_name="default"):
        self.client, self.bus_name = client, bus_name
    def publish(self, detail_type, detail):
        try:
            payload = dict(detail or {})
            payload.setdefault("event_id", str(uuid.uuid4()))
            result = self.client.put_events(Entries=[{"Source": SOURCE, "DetailType": detail_type, "EventBusName": self.bus_name, "Detail": json.dumps(payload, default=str)}])
            if result.get("FailedEntryCount"):
                log.error("EventBridge rejected %s: %s", detail_type, result.get("Entries")); return False
            return True
        except Exception:
            log.exception("Could not publish %s", detail_type); return False

class NullPublisher:
    def publish(self, detail_type, detail): return False
