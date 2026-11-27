"""DynamoDB storage for the waitlist (Milestone 6).

Two tables (created in infrastructure/cloudformation/booking-tables.yaml)

WaitlistTable   one item per waiting patient per slot
    partition key  slot_id
    sort key       patient_id            -> a patient can be in a slot's queue ONCE: the conditional
                                            put_item (attribute_not_exists) is the atomic duplicate check
    joined_at      ISO time with microseconds
    expire_time    DynamoDB TTL attribute: epoch seconds of the slot's END. When the slot is over nobody needs the
                   patient's waitlist row any more, so DynamoDB deletes it (TTL deletion can lag by up to a couple of
                   days, so reads also ignore rows whose expire_time has passed)
    status         WAITING | OFFERED     (a patient who left, accepted or lost an offer has no item)
    GSI order-index    slot_id + joined_at   -> the queue, ordered by join time (first in, first offered)
    GSI patient-index  patient_id + slot_id  -> "my waitlists" and the at-most-5 rule

HoldOffersTable one item per hold offer
    partition key  offer_id
    slot_id, patient_id, created_at, expires_at (epoch seconds), expires_at_utc (ISO text)
    status         LIVE | ACCEPTED | EXPIRED | CANCELLED
    purge_at       DynamoDB TTL attribute: the item is deleted days after it ended (TTL only cleans up;
                   the LIVE -> EXPIRED transition is done by the sweeper, because TTL cannot drive state)
    GSI slot-index     slot_id + created_at
    GSI patient-index  patient_id + created_at
    GSI status-index   status + expires_at  -> "LIVE offers that are due", for the expiry sweeper

Every state change is a CONDITIONAL write (status must still be what the caller saw), so two workers can
never both win the same transition: that is what makes exclusivity checkable.
"""

import calendar

from application.booking.errors import is_conditional_failure

PURGE_AFTER_SECONDS = 7 * 24 * 3600

WAITING, OFFERED = "WAITING", "OFFERED"
LIVE, ACCEPTED, EXPIRED, CANCELLED = "LIVE", "ACCEPTED", "EXPIRED", "CANCELLED"


def epoch(naive_utc):
    return calendar.timegm(naive_utc.timetuple())


def iso(naive_utc):
    return naive_utc.isoformat(timespec="microseconds")


def _clean(item):
    """Numbers come back from boto3 as Decimal; offers and entries are used as plain ints and strings."""
    out = dict(item)
    if "expire_time" in out:
        out["expire_time"] = int(out["expire_time"])
    if "expires_at" in out:
        out["expires_at"] = int(out["expires_at"])
    return out


def _alive(items, now):
    """Drop rows whose TTL has passed but that DynamoDB has not physically deleted yet."""
    if now is None:
        return items
    cutoff = epoch(now)
    return [i for i in items if int(i.get("expire_time", cutoff + 1)) > cutoff]


def query_all(table, **kwargs):
    """Every item of a query: a result page is not necessarily the whole answer, so follow LastEvaluatedKey."""
    items, start = [], None
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.query(**kwargs)
        items += [_clean(i) for i in page.get("Items", [])]
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


class WaitlistStore:
    def __init__(self, queue_table, offers_table):
        self.queue, self.offers = queue_table, offers_table

    # ------------------------------------------------------------------ the queue
    def join(self, slot_id, patient_id, entry_id, now, expire_time=None):
        """Add the patient to the end of the queue. Returns False if they are already in it.
        expire_time: naive UTC end of the slot; it becomes the row's TTL attribute."""
        item = {"slot_id": slot_id, "patient_id": patient_id, "entry_id": entry_id,
                "joined_at": iso(now), "status": WAITING}
        if expire_time is not None:
            item["expire_time"] = epoch(expire_time)
        try:
            self.queue.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#sid)",
                ExpressionAttributeNames={"#sid": "slot_id"})
            return True
        except Exception as exc:  # noqa: BLE001
            if is_conditional_failure(exc):
                return False
            raise

    def get_entry(self, slot_id, patient_id):
        item = self.queue.get_item(Key={"slot_id": slot_id, "patient_id": patient_id}, ConsistentRead=True).get("Item")
        return dict(item) if item else None

    def entries_for_patient(self, patient_id, now=None):
        items = query_all(
            self.queue, IndexName="patient-index", KeyConditionExpression="#p = :p",
            ExpressionAttributeNames={"#p": "patient_id"}, ExpressionAttributeValues={":p": patient_id})
        return _alive(items, now)

    def entries_for_slot(self, slot_id, now=None):
        """The whole queue of a slot in join order (waiting and offered)."""
        items = query_all(
            self.queue, IndexName="order-index", KeyConditionExpression="#s = :s",
            ExpressionAttributeNames={"#s": "slot_id"}, ExpressionAttributeValues={":s": slot_id})
        return _alive(items, now)

    def waiting(self, slot_id):
        return [e for e in self.entries_for_slot(slot_id) if e.get("status") == WAITING]

    def _set_entry_status(self, slot_id, patient_id, expected, new):
        try:
            self.queue.update_item(
                Key={"slot_id": slot_id, "patient_id": patient_id},
                UpdateExpression="SET #st = :new", ConditionExpression="#st = :old",
                ExpressionAttributeNames={"#st": "status"}, ExpressionAttributeValues={":new": new, ":old": expected})
            return True
        except Exception as exc:  # noqa: BLE001
            if is_conditional_failure(exc):
                return False
            raise

    def claim(self, slot_id, patient_id):
        """WAITING -> OFFERED. Only one caller can win; the loser gets False and tries the next patient."""
        return self._set_entry_status(slot_id, patient_id, WAITING, OFFERED)

    def unclaim(self, slot_id, patient_id):
        return self._set_entry_status(slot_id, patient_id, OFFERED, WAITING)

    def remove(self, slot_id, patient_id):
        self.queue.delete_item(Key={"slot_id": slot_id, "patient_id": patient_id})

    # ------------------------------------------------------------------ offers
    def create_offer(self, offer_id, slot_id, patient_id, now, expires_at):
        item = {"offer_id": offer_id, "slot_id": slot_id, "patient_id": patient_id, "status": LIVE,
                "created_at": iso(now), "expires_at": epoch(expires_at), "expires_at_utc": iso(expires_at),
                "purge_at": epoch(expires_at) + PURGE_AFTER_SECONDS}
        self.offers.put_item(Item=item, ConditionExpression="attribute_not_exists(#o)",
                             ExpressionAttributeNames={"#o": "offer_id"})
        return item

    def get_offer(self, offer_id):
        item = self.offers.get_item(Key={"offer_id": offer_id}, ConsistentRead=True).get("Item")
        return _clean(item) if item else None

    def transition(self, offer_id, expected, new, not_after=None, not_before=None):
        """Conditional status change. not_after: only while expires_at > that epoch (accept);
        not_before: only once expires_at <= that epoch (the sweeper). Returns True when THIS caller won."""
        condition, names, values = "#st = :old", {"#st": "status"}, {":old": expected, ":new": new}
        if not_after is not None:
            condition += " AND #e > :t"
            names["#e"], values[":t"] = "expires_at", int(not_after)
        if not_before is not None:
            condition += " AND #e <= :t"
            names["#e"], values[":t"] = "expires_at", int(not_before)
        try:
            self.offers.update_item(Key={"offer_id": offer_id}, UpdateExpression="SET #st = :new",
                                    ConditionExpression=condition, ExpressionAttributeNames=names,
                                    ExpressionAttributeValues=values)
            return True
        except Exception as exc:  # noqa: BLE001
            if is_conditional_failure(exc):
                return False
            raise

    def _query_offers(self, index, key_name, key_value, extra=None):
        names, values = {"#k": key_name}, {":k": key_value}
        kwargs = {"IndexName": index, "KeyConditionExpression": "#k = :k"}
        if extra:
            kwargs["KeyConditionExpression"] += " AND " + extra[0]
            names.update(extra[1])
            values.update(extra[2])
        kwargs["ExpressionAttributeNames"], kwargs["ExpressionAttributeValues"] = names, values
        return query_all(self.offers, **kwargs)

    def live_offers_for_slot(self, slot_id):
        return [o for o in self._query_offers("slot-index", "slot_id", slot_id) if o.get("status") == LIVE]

    def live_offer_for(self, slot_id, patient_id):
        for offer in self.live_offers_for_slot(slot_id):
            if offer["patient_id"] == patient_id:
                return offer
        return None

    def due_offers(self, now):
        """LIVE offers whose hold has run out (expires_at <= now), oldest first."""
        return self._query_offers("status-index", "status", LIVE, ("#e <= :now", {"#e": "expires_at"}, {":now": epoch(now)}))

    # ------------------------------------------------------------------ closing a slot
    def close_slot(self, slot_id, now=None):
        """The doctor cancelled the slot: cancel every live offer, empty the queue. Returns how many seats were held."""
        held = 0
        for offer in self.live_offers_for_slot(slot_id):
            if self.transition(offer["offer_id"], LIVE, CANCELLED):
                held += 1
        for entry in self.entries_for_slot(slot_id):
            self.remove(slot_id, entry["patient_id"])
        return held
