"""Seat counter for each slot with OPTIMISTIC CONCURRENCY CONTROL (DynamoDB conditional writes).

One item per slot in the SlotCapacity table:

    slot_id (partition key) | booked_count (N) | version (N)

A booking never takes a lock. It reads the item, then tries ONE conditional write:

    SET booked_count = booked_count + 1, version = version + 1
    IF  version = <the version I read>  AND  booked_count < <capacity>

If another booking changed the item first, `version` no longer matches, DynamoDB rejects the write
(ConditionalCheckFailedException) and this booking re-reads and retries with exponential backoff
plus jitter, at most `max_attempts` times. Every rejected write is one "conflict retry" and is
reported to the BookingConflictRetries metric. Because the capacity is part of the same condition,
the counter can never go above capacity, however many bookings run at once.

The capacity itself is NOT stored here: the caller passes the slot's capacity from RDS every time,
so a doctor changing a slot's capacity never leaves a stale copy behind.
"""

import random
import time

from application.booking.errors import ContentionExceeded, SlotFull, is_conditional_failure

# Attribute names are always passed through ExpressionAttributeNames (#bc = booked_count, #ver = version,
# #sid = slot_id) so that no name can ever clash with a DynamoDB reserved word.


class Reservation:
    """Result of a successful seat reservation."""

    def __init__(self, slot_id, booked_count, version, conflicts):
        self.slot_id, self.booked_count, self.version, self.conflicts = slot_id, booked_count, version, conflicts


class SlotStore:
    def __init__(self, table, max_attempts=6, backoff_ms=25, sleep=time.sleep, rng=random.random):
        self.table = table
        self.max_attempts, self.backoff_ms = int(max_attempts), int(backoff_ms)
        self._sleep, self._rng = sleep, rng

    # ------------------------------------------------------------------ reading
    def read(self, slot_id):
        """(booked_count, version, exists) using a strongly consistent read."""
        item = self.table.get_item(Key={"slot_id": slot_id}, ConsistentRead=True).get("Item")
        if not item:
            return 0, 0, False
        return int(item.get("booked_count", 0)), int(item.get("version", 0)), True

    def _delay(self, attempt):
        """Exponential backoff with full jitter: random between 0 and base * 2^(attempt-1), capped at 1 s."""
        ceiling = min(1.0, (self.backoff_ms / 1000.0) * (2 ** (attempt - 1)))
        return self._rng() * ceiling

    # --------------------------------------------------------------- reserving
    def reserve(self, slot_id, capacity):
        """Take one seat. Raises SlotFull or ContentionExceeded; returns a Reservation."""
        capacity = int(capacity)
        conflicts = 0
        for attempt in range(1, self.max_attempts + 1):
            booked, version, exists = self.read(slot_id)
            if booked >= capacity:
                full = SlotFull(slot_id)
                full.conflicts = conflicts
                raise full
            try:
                if exists:
                    self.table.update_item(
                        Key={"slot_id": slot_id},
                        UpdateExpression="SET #bc = #bc + :one, #ver = #ver + :one",
                        ConditionExpression="#ver = :v AND #bc < :cap",
                        ExpressionAttributeNames={"#bc": "booked_count", "#ver": "version"},
                        ExpressionAttributeValues={":one": 1, ":v": version, ":cap": capacity},
                    )
                else:
                    self.table.put_item(
                        Item={"slot_id": slot_id, "booked_count": 1, "version": 1},
                        ConditionExpression="attribute_not_exists(#sid)",
                        ExpressionAttributeNames={"#sid": "slot_id"},
                    )
                return Reservation(slot_id, booked + 1, version + 1, conflicts)
            except Exception as exc:  # noqa: BLE001 - only the conditional failure is expected here
                if not is_conditional_failure(exc):
                    raise
                conflicts += 1
                if attempt == self.max_attempts:
                    err = ContentionExceeded(attempt)
                    err.conflicts = conflicts
                    raise err from None
                self._sleep(self._delay(attempt))
        raise ContentionExceeded(self.max_attempts)  # pragma: no cover - loop always returns or raises

    # ----------------------------------------------------------------- releasing
    def release(self, slot_id, count=1):
        """Give seats back (cancellation, or a booking that failed after reserving). Never goes below zero.

        Subtracting is commutative, so it needs no retry; it still bumps `version` so that a booking
        that read the old counter is forced to re-read.
        """
        count = int(count)
        if count <= 0:
            return
        try:
            self.table.update_item(
                Key={"slot_id": slot_id},
                UpdateExpression="SET #bc = #bc - :n, #ver = #ver + :one",
                ConditionExpression="#bc >= :n",
                ExpressionAttributeNames={"#bc": "booked_count", "#ver": "version"},
                ExpressionAttributeValues={":n": count, ":one": 1},
            )
        except Exception as exc:  # noqa: BLE001
            if not is_conditional_failure(exc):
                raise
            # The counter was already lower than expected (for example a manual repair): clamp to zero.
            self.table.update_item(
                Key={"slot_id": slot_id},
                UpdateExpression="SET #bc = :zero, #ver = if_not_exists(#ver, :zero) + :one",
                ExpressionAttributeNames={"#bc": "booked_count", "#ver": "version"},
                ExpressionAttributeValues={":zero": 0, ":one": 1},
            )
