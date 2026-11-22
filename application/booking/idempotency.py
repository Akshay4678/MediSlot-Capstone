"""Idempotency store (DynamoDB Idempotency table, partition key `idempotency_key`).

A booking request may carry an Idempotency-Key. The key is namespaced with the caller
("<user_id>#<key>") so two users can never collide or probe each other's keys.

    begin()     -> NEW      first time: an IN_PROGRESS record is written with a conditional put,
                            so of two concurrent requests with the same key exactly one proceeds
                -> REPLAY   the original request finished: its stored response is returned unchanged
                -> 409      the same key is still being processed
                -> 422      the same key was used with a DIFFERENT request body
    complete()  stores the finished response (status code + body) for replay
    abandon()   removes the record when the request failed, so the client may retry with the same key

Records expire through DynamoDB TTL (`expires_at`, SSM rules/idempotency-ttl-hours, default 24 h).
Because TTL deletion is lazy, an expired record is also treated as absent here. A record that is
IN_PROGRESS longer than `lock_seconds` (the Lambda died) can be taken over.
As a last line of defence the appointments table has UNIQUE(patient_id, idempotency_key).
"""

import hashlib
import json

from application.booking.errors import is_conditional_failure
from application.common.api import ApiError

NEW, REPLAY = "NEW", "REPLAY"


def request_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, table, ttl_hours=24, lock_seconds=30, clock=None):
        self.table, self.ttl_hours, self.lock_seconds = table, int(ttl_hours), int(lock_seconds)
        self._clock = clock

    def _now(self):
        import time
        return int(self._clock() if self._clock else time.time())

    @staticmethod
    def storage_key(user_id, key):
        return f"{user_id}#{key}"

    def begin(self, user_id, key, payload_hash):
        pk, now = self.storage_key(user_id, key), self._now()
        for _ in range(3):
            try:
                self.table.put_item(
                    Item={"idempotency_key": pk, "request_hash": payload_hash, "state": "IN_PROGRESS",
                          "lock_until": now + self.lock_seconds, "created_at": now,
                          "expires_at": now + self.ttl_hours * 3600},
                    ConditionExpression=("attribute_not_exists(#pk) OR #exp < :now "
                                         "OR (#st = :ip AND #lock < :now)"),
                    ExpressionAttributeNames={"#pk": "idempotency_key", "#exp": "expires_at",
                                              "#st": "state", "#lock": "lock_until"},
                    ExpressionAttributeValues={":now": now, ":ip": "IN_PROGRESS"},
                )
                return NEW, None
            except Exception as exc:  # noqa: BLE001
                if not is_conditional_failure(exc):
                    raise
            item = self.table.get_item(Key={"idempotency_key": pk}, ConsistentRead=True).get("Item")
            if not item:
                continue  # deleted between the two calls: try to claim it again
            if item.get("request_hash") != payload_hash:
                raise ApiError(422, "IDEMPOTENCY_KEY_REUSED",
                               "This Idempotency-Key was already used with a different request.")
            if item.get("state") == "COMPLETED":
                return REPLAY, {"status": int(item["status_code"]), "body": json.loads(item["response"])}
            raise ApiError(409, "REQUEST_IN_PROGRESS", "A request with this Idempotency-Key is still being processed.")
        raise ApiError(409, "REQUEST_IN_PROGRESS", "A request with this Idempotency-Key is still being processed.")

    def complete(self, user_id, key, status_code, body):
        self.table.update_item(
            Key={"idempotency_key": self.storage_key(user_id, key)},
            UpdateExpression="SET #st = :done, #code = :code, #resp = :resp",
            ExpressionAttributeNames={"#st": "state", "#code": "status_code", "#resp": "response"},
            ExpressionAttributeValues={":done": "COMPLETED", ":code": int(status_code),
                                       ":resp": json.dumps(body, default=str)},
        )

    def abandon(self, user_id, key):
        try:
            self.table.delete_item(
                Key={"idempotency_key": self.storage_key(user_id, key)},
                ConditionExpression="#st = :ip",
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={":ip": "IN_PROGRESS"},
            )
        except Exception as exc:  # noqa: BLE001
            if not is_conditional_failure(exc):
                raise
