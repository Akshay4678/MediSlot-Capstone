"""Milestone 6 worker Lambda: Step Functions tasks, waitlist sweeper/promoter, reminders, no-show and notification consumer."""
import json, os, time, uuid, logging
from urllib.parse import unquote
import boto3
from botocore.config import Config
from application.database.connection import get_connection
from application.booking.slot_store import SlotStore
from application.booking.events import EventPublisher
from application.booking.metrics import Metrics
from application.waitlist.store import default_store as default_waitlist_store
from application.waitlist import service as waitlist_service
from application.common import rules

log = logging.getLogger(); log.setLevel(logging.INFO)
_cfg = Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})
_ddb = None
_slots = None
_waitlist = None
_events = None
_metrics = None
_sns = None


def _deps():
    global _ddb, _slots, _waitlist, _events, _metrics, _sns
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", config=_cfg)
        _slots = SlotStore(_ddb.Table(os.environ["SLOT_CAPACITY_TABLE_NAME"]), rules.booking_max_attempts(), rules.booking_backoff_ms())
        _waitlist = default_waitlist_store()
        _events = EventPublisher(boto3.client("events", config=_cfg), os.environ.get("EVENT_BUS_NAME", "default"))
        _metrics = Metrics(boto3.client("cloudwatch", config=_cfg), environment=os.environ.get("ENVIRONMENT_NAME", "dev"))
        _sns = boto3.client("sns", config=_cfg)
    return _slots, _waitlist, _events, _metrics, _sns


class WorkerDeps:
    def __init__(self):
        self.slots, self.waitlist, self.events, self.metrics, _ = _deps()
        self.now = lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc).replace(tzinfo=None)


def _db_slot(cur, slot_id):
    cur.execute("SELECT slot_id, doctor_id, consultation_type_id, status, booked_count, capacity, fee, start_utc, end_utc, date, start_time, end_time, timezone FROM materialised_slots WHERE slot_id = %s FOR UPDATE", (slot_id,))
    return cur.fetchone()


def claim_target(conn, data):
    patient_id, appointment_id = data["patient_id"], data["appointment_id"]
    target_slot_id = unquote(str(data.get("target_slot_id") or "").strip())
    if not target_slot_id:
        raise ValueError("INVALID_TARGET_SLOT")
    with conn.cursor() as cur:
        cur.execute("SELECT appointment_id, patient_id, doctor_id, slot_id, status, starts_at_utc, ends_at_utc, fee_amount FROM appointments WHERE appointment_id = %s AND patient_id = %s FOR UPDATE", (appointment_id, patient_id))
        appt = cur.fetchone()
        if not appt or appt["status"] != "CONFIRMED": raise ValueError("APPOINTMENT_NOT_RESCHEDULABLE")
        cur.execute("SELECT slot_id, doctor_id, consultation_type_id, status, capacity, booked_count, start_utc, end_utc FROM materialised_slots WHERE slot_id = %s", (target_slot_id,))
        target = cur.fetchone()
        if not target or target["status"] in ("BLOCKED", "CLOSED") or int(target["booked_count"]) >= int(target["capacity"]): raise ValueError("TARGET_SLOT_UNAVAILABLE")
        cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND status = 'CONFIRMED' AND appointment_id <> %s AND starts_at_utc < %s AND ends_at_utc > %s", (patient_id, appointment_id, target["end_utc"], target["start_utc"]))
        if int(cur.fetchone()["n"]) > 0: raise ValueError("TIME_CONFLICT")
    reservation = _deps()[0].reserve(target_slot_id, int(target["capacity"]))
    return {"reservation": {"slot_id": target_slot_id, "booked_count": reservation.booked_count, "conflicts": reservation.conflicts}, "original_slot_id": appt["slot_id"], "target_slot_id": target_slot_id}


def commit_reschedule(conn, data):
    appointment_id, patient_id = data["appointment_id"], data["patient_id"]
    original_slot_id = unquote(str(data.get("original_slot_id") or "").strip())
    target_slot_id = unquote(str(data.get("target_slot_id") or "").strip())
    if not original_slot_id or not target_slot_id:
        raise ValueError("INVALID_RESCHEDULE_SLOTS")
    with conn.cursor() as cur:
        cur.execute("SELECT doctor_id, consultation_type_id, fee_amount FROM materialised_slots WHERE slot_id = %s", (target_slot_id,))
        target = cur.fetchone()
        if not target: raise ValueError("TARGET_SLOT_NOT_FOUND")
        cur.execute("SELECT start_utc, end_utc, date, start_time, end_time, timezone FROM materialised_slots WHERE slot_id = %s", (target_slot_id,))
        target_times = cur.fetchone()
        cur.execute("UPDATE appointments SET slot_id = %s, doctor_id = %s, consultation_type_id = %s, starts_at_utc = %s, ends_at_utc = %s, local_date = %s, local_start = %s, local_end = %s, timezone = %s WHERE appointment_id = %s AND patient_id = %s AND status = 'CONFIRMED'", (target_slot_id, target["doctor_id"], target["consultation_type_id"], target_times["start_utc"], target_times["end_utc"], target_times["date"], target_times["start_time"], target_times["end_time"], target_times["timezone"], appointment_id, patient_id))
        if cur.rowcount != 1: raise ValueError("APPOINTMENT_NOT_RESCHEDULABLE")
        cur.execute("UPDATE materialised_slots SET booked_count = booked_count + 1 WHERE slot_id = %s AND status = 'OPEN' AND booked_count < capacity", (target_slot_id,))
        if cur.rowcount != 1: raise ValueError("TARGET_SLOT_UNAVAILABLE")
        cur.execute("UPDATE materialised_slots SET status = 'FULL' WHERE slot_id = %s AND booked_count >= capacity", (target_slot_id,))
        cur.execute("UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0), status = CASE WHEN status = 'FULL' THEN 'OPEN' ELSE status END WHERE slot_id = %s", (original_slot_id,))
        conn.commit()
    # This is deliberately outside the RDS transaction: if it fails, Step Functions enters compensation.
    # Set FORCE_RELEASE_FAILURE=1 during the mentor's forced-failure test to exercise the compensation branch.
    if os.environ.get("FORCE_RELEASE_FAILURE") == "1":
        raise RuntimeError("FORCED_RELEASE_FAILURE")
    _deps()[0].release(original_slot_id)
    _deps()[2].publish("AppointmentRescheduled", {"event_id": appointment_id + ":rescheduled", "appointment_id": appointment_id, "patient_id": patient_id, "from_slot_id": original_slot_id, "to_slot_id": target_slot_id})
    _deps()[2].publish("SlotSeatFreed", {"event_id": appointment_id + ":seat-freed", "slot_id": original_slot_id, "doctor_id": target["doctor_id"], "seats_freed": 1})
    _deps()[3].put("ReschedulesConfirmed", 1)
    return {"status": "RESCHEDULED", "appointment_id": appointment_id, "from_slot_id": original_slot_id, "to_slot_id": target_slot_id}


def compensate(conn, data):
    appointment_id, patient_id = data["appointment_id"], data["patient_id"]
    original_slot_id = unquote(str(data.get("original_slot_id") or "").strip())
    target_slot_id = unquote(str(data.get("target_slot_id") or "").strip())
    with conn.cursor() as cur:
        cur.execute("SELECT start_utc, end_utc, date, start_time, end_time, timezone, doctor_id, consultation_type_id, fee FROM materialised_slots WHERE slot_id = %s", (original_slot_id,))
        original = cur.fetchone()
        if original:
            cur.execute("UPDATE appointments SET slot_id=%s, doctor_id=%s, consultation_type_id=%s, starts_at_utc=%s, ends_at_utc=%s, local_date=%s, local_start=%s, local_end=%s, timezone=%s WHERE appointment_id=%s AND patient_id=%s AND status='CONFIRMED'", (original_slot_id, original["doctor_id"], original["consultation_type_id"], original["start_utc"], original["end_utc"], original["date"], original["start_time"], original["end_time"], original["timezone"], appointment_id, patient_id))
            cur.execute("UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0), status = CASE WHEN status='FULL' THEN 'OPEN' ELSE status END WHERE slot_id = %s", (target_slot_id,))
            cur.execute("UPDATE materialised_slots SET booked_count = booked_count + 1, status = CASE WHEN booked_count + 1 >= capacity THEN 'FULL' ELSE 'OPEN' END WHERE slot_id = %s AND booked_count < capacity", (original_slot_id,))
            conn.commit()
    _deps()[0].release(target_slot_id)
    _deps()[3].put("ReschedulesCompensated", 1)
    return {"status": "COMPENSATED", "appointment_id": appointment_id}


def no_show_sweep(conn):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(minutes=15)
    with conn.cursor() as cur:
        cur.execute("SELECT appointment_id, patient_id, doctor_id, slot_id FROM appointments WHERE status='CONFIRMED' AND starts_at_utc < %s", (cutoff,))
        rows = cur.fetchall()
        for row in rows:
            cur.execute("UPDATE appointments SET status='NO_SHOW', completed_at=%s WHERE appointment_id=%s AND status='CONFIRMED'", (now, row["appointment_id"]))
            if cur.rowcount == 1:
                cur.execute("UPDATE materialised_slots SET booked_count=GREATEST(booked_count-1,0), status=CASE WHEN status='FULL' THEN 'OPEN' ELSE status END WHERE slot_id=%s", (row["slot_id"],))
    conn.commit()
    for row in rows:
        _deps()[0].release(row["slot_id"])
        _deps()[2].publish("AppointmentCancelled", {"event_id": row["appointment_id"] + ":no-show", **row, "reason": "NO_SHOW"})
        _deps()[2].publish("SlotSeatFreed", {"event_id": row["appointment_id"] + ":no-show-seat", "slot_id": row["slot_id"], "seats_freed": 1})
    return {"marked": len(rows)}


def reminder_dispatch(conn):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    windows = [(24, "24h"), (2, "2h")]
    sent = 0
    for hours, label in windows:
        lo, hi = now + timedelta(hours=hours) - timedelta(minutes=2), now + timedelta(hours=hours) + timedelta(minutes=2)
        with conn.cursor() as cur:
            cur.execute("SELECT appointment_id, patient_id, doctor_id, slot_id, starts_at_utc FROM appointments WHERE status='CONFIRMED' AND starts_at_utc >= %s AND starts_at_utc < %s", (lo, hi))
            for row in cur.fetchall():
                dispatch_id = f"{row['appointment_id']}:reminder:{label}"
                table = _ddb.Table(os.environ["NOTIFICATION_DISPATCH_TABLE_NAME"])
                try:
                    table.put_item(Item={"dispatch_id": dispatch_id, "state": "PROCESSING", "expires_at": int(time.time()) + 7*24*3600}, ConditionExpression="attribute_not_exists(dispatch_id)")
                except Exception as exc:
                    from botocore.exceptions import ClientError
                    if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                        continue
                    raise
                _deps()[4].publish(TopicArn=os.environ["NOTIFICATION_TOPIC_ARN"], Message=json.dumps({"event_type":"AppointmentReminder", "detail":{**row, "reminder":label}}, default=str), Subject="MediSlot appointment reminder")
                table.update_item(Key={"dispatch_id": dispatch_id}, UpdateExpression="SET #s=:s, dispatched_at=:t", ExpressionAttributeNames={"#s":"state"}, ExpressionAttributeValues={":s":"COMPLETED", ":t":int(time.time())})
                sent += 1
    if sent: _deps()[3].put("NotificationsDispatched", sent)
    return {"dispatched": sent}


def handle_sqs(records):
    _, _, _, metrics, sns = _deps()
    dispatch_table = _ddb.Table(os.environ["NOTIFICATION_DISPATCH_TABLE_NAME"])
    topic = os.environ["NOTIFICATION_TOPIC_ARN"]
    sent = 0
    for record in records:
        body = json.loads(record["body"])
        detail = body.get("detail", body)
        event_id = detail.get("event_id") or body.get("id") or str(uuid.uuid4())
        now = int(time.time())
        try:
            dispatch_table.put_item(Item={"dispatch_id": event_id, "state": "PROCESSING", "expires_at": now + 7*24*3600}, ConditionExpression="attribute_not_exists(dispatch_id)")
        except Exception as exc:
            from botocore.exceptions import ClientError
            if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise
        message = json.dumps({"event_type": body.get("detail-type", body.get("detail_type", "Unknown")), "detail": detail}, default=str)
        sns.publish(TopicArn=topic, Message=message, Subject="MediSlot appointment update")
        dispatch_table.update_item(Key={"dispatch_id": event_id}, UpdateExpression="SET #s=:s, dispatched_at=:t", ExpressionAttributeNames={"#s":"state"}, ExpressionAttributeValues={":s":"COMPLETED", ":t": now})
        sent += 1
    if sent: metrics.put("NotificationsDispatched", sent)
    return {"sent": sent}


def lambda_handler(event, context):
    action = event.get("action")
    if action == "reschedule_claim":
        with get_connection() as conn: return claim_target(conn, event)
    if action == "reschedule_commit":
        with get_connection() as conn: return commit_reschedule(conn, event)
    if action == "reschedule_compensate":
        with get_connection() as conn: return compensate(conn, event)
    if action == "waitlist_promote":
        with get_connection() as conn: return {"offer": waitlist_service.promote_first(conn, WorkerDeps(), event["slot_id"])}
    if action == "hold_expiry_sweep":
        with get_connection() as conn: return {"expired": waitlist_service.expire_offers(conn, WorkerDeps())}
    if action == "no_show_sweep":
        with get_connection() as conn: return no_show_sweep(conn)
    if action == "reminder_dispatch":
        with get_connection() as conn: return reminder_dispatch(conn)
    if action == "notification_consume":
        return handle_sqs(event.get("Records", []))
    raise ValueError(f"Unknown workflow action: {action}")
