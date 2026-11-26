"""Exactly-once dispatch log (DynamoDB conditional write).

One item per notification, keyed `<appointmentId>#<interval>#<recipient>` (for example
`3f2a...#REMINDER_24H#PATIENT`). Sending is: claim -> publish to SNS -> mark SENT.

    claim()   conditional put_item (attribute_not_exists): only ONE caller can create the item.
    DUPLICATE the item exists and is SENT          -> skip, it was delivered already
    BUSY      the item exists, SENDING, lease live -> another worker is on it: retry the message later
    CLAIMED   we own it (new, or a SENDING item whose worker died and whose lease ran out)

If SNS fails, the item stays SENDING; once its lease (60 s) is over the retry claims it again with a
conditional update on the old claimed_at, so a stale claim is taken by exactly one retrier.
"""

import calendar

from application.booking.errors import is_conditional_failure

CLAIMED, DUPLICATE, BUSY = "CLAIMED", "DUPLICATE", "BUSY"
LEASE_SECONDS = 60
KEEP_SECONDS = 30 * 24 * 3600


class DispatchLog:
    def __init__(self, table, clock):
        self.table, self.clock = table, clock  # clock() -> naive UTC datetime

    def _now(self):
        return calendar.timegm(self.clock().timetuple())

    def claim(self, dispatch_id, **attributes):
        now = self._now()
        item = {"dispatch_id": dispatch_id, "status": "SENDING", "claimed_at": now, "expires_at": now + KEEP_SECONDS}
        item.update(attributes)
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(#d)",
                                ExpressionAttributeNames={"#d": "dispatch_id"})
            return CLAIMED
        except Exception as exc:  # noqa: BLE001
            if not is_conditional_failure(exc):
                raise
        existing = self.table.get_item(Key={"dispatch_id": dispatch_id}, ConsistentRead=True).get("Item")
        if not existing or existing.get("status") == "SENT":
            return DUPLICATE if existing else self.claim(dispatch_id, **attributes)
        claimed_at = int(existing.get("claimed_at", 0))
        if now - claimed_at < LEASE_SECONDS:
            return BUSY
        try:  # the previous worker died: take over, but only one retrier can
            self.table.update_item(
                Key={"dispatch_id": dispatch_id}, UpdateExpression="SET #c = :new",
                ConditionExpression="#st = :sending AND #c = :old",
                ExpressionAttributeNames={"#c": "claimed_at", "#st": "status"},
                ExpressionAttributeValues={":new": now, ":old": claimed_at, ":sending": "SENDING"})
            return CLAIMED
        except Exception as exc:  # noqa: BLE001
            if is_conditional_failure(exc):
                return BUSY
            raise

    def mark_sent(self, dispatch_id):
        self.table.update_item(Key={"dispatch_id": dispatch_id}, UpdateExpression="SET #st = :sent",
                               ExpressionAttributeNames={"#st": "status"}, ExpressionAttributeValues={":sent": "SENT"})

    def release(self, dispatch_id):
        """Give the claim back (the thing we were going to send could not be sent at all)."""
        self.table.delete_item(Key={"dispatch_id": dispatch_id})
