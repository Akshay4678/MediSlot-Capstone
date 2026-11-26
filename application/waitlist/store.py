"""DynamoDB waitlist and exclusive hold-offer storage."""
import os, time, uuid
from datetime import datetime, timezone
from botocore.config import Config
import boto3
from botocore.exceptions import ClientError


def _now_epoch():
    return int(time.time())


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


class WaitlistStore:
    def __init__(self, table, hold_table, max_size=5, hold_minutes=15, ddb_client=None):
        self.table = table
        self.hold_table = hold_table
        self.max_size = int(max_size)
        self.hold_minutes = int(hold_minutes)
        self.client = ddb_client or boto3.client("dynamodb", config=Config(connect_timeout=2, read_timeout=4, retries={"total_max_attempts": 2, "mode": "standard"}))

    @staticmethod
    def _key(slot_id, patient_id):
        return f"{slot_id}#{patient_id}"

    @staticmethod
    def _meta_key(slot_id):
        return f"{slot_id}#META"

    def join(self, slot_id, patient_id, doctor_id, consultation_type_id):
        """Join a slot exactly once, while atomically enforcing the per-slot cap.

        The waitlist row is updated rather than blindly inserted so a previously
        removed entry can re-join. An active WAITING/HELD entry fails the
        conditional update and is returned to the caller as an idempotent replay.
        """
        key = self._key(slot_id, patient_id)
        now = _iso_now()
        waitlist_id = str(uuid.uuid4())
        meta_key = self._meta_key(slot_id)
        meta_values = {
            ":zero": {"N": "0"},
            ":one": {"N": "1"},
            ":max": {"N": str(self.max_size)},
            ":slot": {"S": str(slot_id)},
            ":meta_type": {"S": "META"},
        }
        item_values = {
            ":waitlist_id": {"S": waitlist_id},
            ":slot": {"S": str(slot_id)},
            ":patient": {"S": str(patient_id)},
            ":doctor": {"S": str(doctor_id)},
            ":type": {"S": str(consultation_type_id)},
            ":joined": {"S": now},
            ":waiting": {"S": "WAITING"},
            ":removed": {"S": "REMOVED"},
        }
        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self.table.name,
                    "Key": {"waitlist_key": {"S": meta_key}},
                    "UpdateExpression": "SET #c = if_not_exists(#c, :zero) + :one, #slot = :slot, #type = :meta_type",
                    "ConditionExpression": "attribute_not_exists(#c) OR #c < :max",
                    "ExpressionAttributeNames": {"#c": "count", "#slot": "slot_id", "#type": "item_type"},
                    "ExpressionAttributeValues": meta_values,
                }},
                {"Update": {
                    "TableName": self.table.name,
                    "Key": {"waitlist_key": {"S": key}},
                    "UpdateExpression": "SET waitlist_id = :waitlist_id, slot_id = :slot, patient_id = :patient, doctor_id = :doctor, consultation_type_id = :type, joined_at = :joined, #s = :waiting REMOVE offer_id, offer_expires_at, removed_at",
                    "ConditionExpression": "attribute_not_exists(waitlist_key) OR #s = :removed",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": item_values,
                }},
            ])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "TransactionCanceledException":
                existing = self.get(slot_id, patient_id)
                if existing and existing.get("status") in ("WAITING", "HELD"):
                    return existing, False
                # A failed transaction with no active entry means the slot's
                # META row reached its configured maximum.
                raise RuntimeError("WAITLIST_FULL") from None
            raise

        item = {
            "waitlist_key": {"S": key},
            "waitlist_id": {"S": waitlist_id},
            "slot_id": {"S": str(slot_id)},
            "patient_id": {"S": str(patient_id)},
            "doctor_id": {"S": str(doctor_id)},
            "consultation_type_id": {"S": str(consultation_type_id)},
            "joined_at": {"S": now},
            "status": {"S": "WAITING"},
        }
        return {k: self._decode(v) for k, v in item.items()}, True

    def get(self, slot_id, patient_id):
        r = self.table.get_item(Key={"waitlist_key": self._key(slot_id, patient_id)}, ConsistentRead=True)
        return self._decode_item(r.get("Item"))

    def leave(self, slot_id, patient_id):
        key = self._key(slot_id, patient_id)
        item = self.table.get_item(Key={"waitlist_key": key}, ConsistentRead=True).get("Item")
        if not item or item.get("status", {}).get("S") != "WAITING":
            return False
        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": {"TableName": self.table.name, "Key": {"waitlist_key": {"S": self._meta_key(slot_id)}},
                             "UpdateExpression": "SET #c = if_not_exists(#c, :zero) - :one",
                             "ConditionExpression": "#c >= :one",
                             "ExpressionAttributeNames": {"#c": "count"}, "ExpressionAttributeValues": {":zero": {"N": "0"}, ":one": {"N": "1"}}}},
                {"Update": {"TableName": self.table.name, "Key": {"waitlist_key": {"S": key}},
                             "UpdateExpression": "SET #s = :s, removed_at = :now",
                             "ConditionExpression": "#s IN (:waiting, :held)",
                             "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": {":s": {"S": "REMOVED"}, ":now": {"S": _iso_now()}, ":waiting": {"S": "WAITING"}, ":held": {"S": "HELD"}}}},
            ])
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                return False
            raise

    def list_for_slot(self, slot_id, limit=50):
        r = self.table.query(IndexName="slot_id-joined_at-index", KeyConditionExpression="#slot = :slot",
                             ExpressionAttributeNames={"#slot": "slot_id"}, ExpressionAttributeValues={":slot": {"S": str(slot_id)}},
                             ScanIndexForward=True, Limit=int(limit))
        return [self._decode_item(i) for i in r.get("Items", []) if i.get("status", {}).get("S") in ("WAITING", "HELD")]

    def first_waiting(self, slot_id):
        rows = self.list_for_slot(slot_id, 50)
        return next((r for r in rows if r.get("status") == "WAITING"), None)

    def mark_held(self, slot_id, patient_id, offer_id, expires_at):
        return self.table.update_item(Key={"waitlist_key": self._key(slot_id, patient_id)},
            UpdateExpression="SET #s = :s, offer_id = :o, offer_expires_at = :e",
            ConditionExpression="#s = :waiting", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "HELD", ":o": str(offer_id), ":e": int(expires_at), ":waiting": "WAITING"}, ReturnValues="ALL_NEW")["Attributes"]

    def mark_waiting(self, slot_id, patient_id):
        self.table.update_item(Key={"waitlist_key": self._key(slot_id, patient_id)},
            UpdateExpression="SET #s = :s REMOVE offer_id, offer_expires_at",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "WAITING"})

    def mark_promoted(self, slot_id, patient_id):
        self.table.update_item(Key={"waitlist_key": self._key(slot_id, patient_id)},
            UpdateExpression="SET #s = :s, promoted_at = :now",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "PROMOTED", ":now": _iso_now()})

    def decrement_active_count(self, slot_id):
        self.table.update_item(Key={"waitlist_key": self._meta_key(slot_id)},
            UpdateExpression="SET #c = if_not_exists(#c, :zero) - :one",
            ConditionExpression="#c >= :one", ExpressionAttributeNames={"#c":"count"},
            ExpressionAttributeValues={":zero":{"N":"0"}, ":one":{"N":"1"}})

    def create_hold(self, slot_id, patient_id, waitlist_id):
        now = _now_epoch(); expires = now + self.hold_minutes * 60
        offer_id = str(uuid.uuid4())
        item = {"slot_id": str(slot_id), "offer_id": offer_id, "patient_id": str(patient_id), "waitlist_id": str(waitlist_id), "status": "LIVE", "created_at": now, "expires_at": expires, "ttl_at": expires + 86400}
        try:
            self.hold_table.put_item(Item=item, ConditionExpression="attribute_not_exists(slot_id) OR #s <> :live OR expires_at <= :now", ExpressionAttributeNames={"#s":"status"}, ExpressionAttributeValues={":live":"LIVE", ":now":now})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return item

    def get_hold(self, slot_id):
        return self.hold_table.get_item(Key={"slot_id": str(slot_id)}, ConsistentRead=True).get("Item")

    def get_hold_by_offer(self, offer_id):
        r = self.hold_table.query(IndexName="offer_id-index", KeyConditionExpression="offer_id = :o", ExpressionAttributeValues={":o": str(offer_id)}, ConsistentRead=False)
        return r.get("Items", [None])[0]

    def accept_hold(self, offer_id, patient_id):
        hold = self.get_hold_by_offer(offer_id)
        if not hold or hold.get("status") != "LIVE" or str(hold.get("patient_id")) != str(patient_id) or int(hold.get("expires_at", 0)) <= _now_epoch():
            return None
        self.hold_table.update_item(Key={"slot_id": hold["slot_id"]}, UpdateExpression="SET #s = :s", ConditionExpression="#s = :live AND patient_id = :p",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "ACCEPTED", ":live": "LIVE", ":p": str(patient_id)})
        return hold

    def expire_hold(self, slot_id, offer_id):
        try:
            self.hold_table.update_item(Key={"slot_id": str(slot_id)}, UpdateExpression="SET #s = :s",
                ConditionExpression="#s = :live AND offer_id = :o", ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "EXPIRED", ":live": "LIVE", ":o": str(offer_id)})
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException": return False
            raise

    @staticmethod
    def _decode(v):
        if not isinstance(v, dict): return v
        if "S" in v: return v["S"]
        if "N" in v: return int(v["N"]) if v["N"].isdigit() else float(v["N"])
        if "BOOL" in v: return v["BOOL"]
        return v

    @classmethod
    def _decode_item(cls, item):
        return {k: cls._decode(v) for k, v in (item or {}).items()}


def default_store():
    cfg = Config(connect_timeout=2, read_timeout=4, retries={"total_max_attempts": 2, "mode": "standard"})
    ddb = boto3.resource("dynamodb", config=cfg)
    return WaitlistStore(ddb.Table(os.environ["WAITLIST_TABLE_NAME"]), ddb.Table(os.environ["HOLD_OFFERS_TABLE_NAME"]), int(os.environ.get("WAITLIST_MAX_SIZE", "5")), int(os.environ.get("HOLD_OFFER_MINUTES", "15")))
