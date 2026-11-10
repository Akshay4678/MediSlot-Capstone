"""Patient-facing API for Milestone 4.

Routes are intentionally kept in one Lambda so API Gateway has a single patient-domain
integration. Identity is taken only from the authorizer context.

Implemented workflow:
* doctor search
* dated slot browsing
* idempotent booking with RDS row locking
* appointment listing/cancellation
* rescheduling (maximum two times, not inside six hours)
* waitlist join/leave
* 15-minute waitlist hold offers
* refund calculation/ledger entries
* event publication for appointment/slot-seat events
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr, Key

from application.common.api import ApiError
from application.common.auth_context import claims
from application.common.http import request_body, request_method, response
from application.common.rules import (
    booking_window_days, idempotency_ttl_hours, waitlist_max_size, hold_offer_minutes,
    max_reschedules, min_reschedule_hours, refund_bands,
)
from application.patient_api import discovery, profile as patient_profile
from application.database.connection import get_connection

log = logging.getLogger()
log.setLevel(logging.INFO)

AWS_CFG = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})
ddb = boto3.resource("dynamodb", config=AWS_CFG)
events = boto3.client("events", config=AWS_CFG)

IDEMPOTENCY_TABLE = os.environ.get("IDEMPOTENCY_TABLE_NAME")
WAITLIST_TABLE = os.environ.get("WAITLIST_TABLE_NAME")
HOLD_OFFERS_TABLE = os.environ.get("HOLD_OFFERS_TABLE_NAME")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")

ACTIVE_APPOINTMENT_STATUSES = ("CONFIRMED",)


def _table(name):
    if not name:
        raise RuntimeError("Required DynamoDB table environment variable is not configured.")
    return ddb.Table(name)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _publish(detail_type, detail):
    try:
        events.put_events(
            Entries=[{
                "Source": "medislot.appointments",
                "DetailType": detail_type,
                "Detail": json.dumps(detail, default=str),
                "EventBusName": EVENT_BUS_NAME,
            }]
        )
    except Exception:  # event publication must not undo a committed booking
        log.exception("Failed to publish %s event", detail_type)


def _error(status, code, message):
    return response(status, {"error": code, "message": message})


def _patient(conn, user_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT patient_id, first_name, last_name, phone, city FROM patients WHERE user_id = %s",
            (user_id,),
        )
        return cur.fetchone()


def _require_patient(conn, user_id):
    patient = _patient(conn, user_id)
    if not patient:
        raise ValueError("PATIENT_PROFILE_REQUIRED")
    return patient


def _with_connection(work):
    conn = get_connection()
    try:
        status, body = work(conn)
        return response(status, body)
    finally:
        conn.close()


def _doctor_search(event):
    q = event.get("queryStringParameters") or {}
    return _with_connection(lambda conn: discovery.search(conn, q))


def _doctor_profile(event):
    doctor_id = ((event.get("pathParameters") or {}).get("doctor_id") or "").strip()
    return _with_connection(lambda conn: discovery.doctor_profile(conn, doctor_id))


def _availability(event):
    q = event.get("queryStringParameters") or {}
    # The route is /doctors/{doctor_id}/slots, so the id arrives as a path parameter.
    doctor_id = (((event.get("pathParameters") or {}).get("doctor_id")) or q.get("doctor_id") or "").strip()
    return _with_connection(lambda conn: discovery.slots(conn, doctor_id, q))


def _profile_route(method, user_id, event):
    def work(conn):
        if method == "GET":
            return patient_profile.get(conn, user_id)
        if method == "POST":
            return patient_profile.create(conn, user_id, request_body(event))
        if method == "PUT":
            return patient_profile.update(conn, user_id, request_body(event))
        return patient_profile.delete(conn, user_id)
    return _with_connection(work)


def _idempotency_start(user_id, key):
    table = _table(IDEMPOTENCY_TABLE)
    now = _now()
    expires = int((now + timedelta(hours=idempotency_ttl_hours())).timestamp())
    item = {
        "idempotency_key": key,
        "user_id": user_id,
        "status": "PROCESSING",
        "created_at": _iso(now),
        "expires_at": expires,
    }
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(idempotency_key)")
        return None
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = table.get_item(Key={"idempotency_key": key}, ConsistentRead=True).get("Item")
        if existing and existing.get("user_id") != user_id:
            raise PermissionError("IDEMPOTENCY_KEY_OWNERSHIP")
        return existing


def _idempotency_complete(key, appointment_id):
    _table(IDEMPOTENCY_TABLE).update_item(
        Key={"idempotency_key": key},
        UpdateExpression="SET #s = :s, appointment_id = :a",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "COMPLETED", ":a": appointment_id},
    )


def _idempotency_delete(key):
    try:
        _table(IDEMPOTENCY_TABLE).delete_item(Key={"idempotency_key": key})
    except Exception:
        log.exception("Failed to clear idempotency record %s", key)


def _book(user_id, body, idempotency_key=None, hold_offer_id=None):
    required = ("doctor_id", "consultation_type_id", "slot_id")
    missing = [name for name in required if not body.get(name)]
    if missing:
        return _error(400, "MISSING_FIELDS", "Required fields are missing.",), None

    key = idempotency_key or body.get("idempotency_key")
    if not key:
        return _error(400, "MISSING_FIELD", "idempotency_key is required."), None
    key = str(key).strip()
    if len(key) > 128:
        return _error(400, "INVALID_FIELD", "idempotency_key must be at most 128 characters."), None

    existing = _idempotency_start(user_id, key)
    if existing:
        if existing.get("status") == "COMPLETED" and existing.get("appointment_id"):
            return response(200, {"appointment_id": existing["appointment_id"], "status": "CONFIRMED", "idempotent": True}), existing["appointment_id"]
        return _error(409, "REQUEST_IN_PROGRESS", "The same booking request is already being processed."), None

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            patient = _require_patient(conn, user_id)
            cur.execute(
                "SELECT s.slot_id, s.doctor_id, s.consultation_type_id, s.start_utc, s.end_utc, "
                "s.capacity, s.booked_count, s.status, dct.fee "
                "FROM materialised_slots s JOIN consultation_types ct ON ct.consultation_type_id = s.consultation_type_id "
                "JOIN doctor_consultation_types dct ON dct.doctor_id = s.doctor_id AND dct.consultation_type_id = s.consultation_type_id "
                "WHERE s.slot_id = %s FOR UPDATE",
                (str(body["slot_id"]),),
            )
            slot = cur.fetchone()
            if not slot:
                raise RuntimeError("SLOT_NOT_FOUND")
            if str(slot["doctor_id"]) != str(body["doctor_id"]) or str(slot["consultation_type_id"]) != str(body["consultation_type_id"]):
                raise RuntimeError("SLOT_MISMATCH")
            if slot["status"] != "OPEN" or int(slot["booked_count"]) >= int(slot["capacity"]):
                raise RuntimeError("SLOT_FULL")
            if _parse_utc(slot["start_utc"]) <= _now():
                raise RuntimeError("SLOT_IN_PAST")
            if _parse_utc(slot["start_utc"]) > _now() + timedelta(days=booking_window_days()):
                raise RuntimeError("BOOKING_WINDOW_EXCEEDED")

            appointment_id = str(uuid.uuid4())
            notes = str(body.get("notes") or "").strip()[:2000] or None
            cur.execute(
                "INSERT INTO appointments (appointment_id, patient_id, doctor_id, slot_id, consultation_type_id, status, notes) "
                "VALUES (%s, %s, %s, %s, %s, 'CONFIRMED', %s)",
                (appointment_id, patient["patient_id"], slot["doctor_id"], slot["slot_id"], slot["consultation_type_id"], notes),
            )
            new_count = int(slot["booked_count"]) + 1
            new_status = "FULL" if new_count >= int(slot["capacity"]) else "OPEN"
            cur.execute(
                "UPDATE materialised_slots SET booked_count = %s, status = %s WHERE slot_id = %s",
                (new_count, new_status, slot["slot_id"]),
            )
        conn.commit()

        _idempotency_complete(key, appointment_id)
        _publish("AppointmentCreated", {
            "appointment_id": appointment_id,
            "patient_id": patient["patient_id"],
            "doctor_id": slot["doctor_id"],
            "slot_id": slot["slot_id"],
            "starts_at_utc": _iso(_parse_utc(slot["start_utc"])),
        })
        return response(201, {"appointment_id": appointment_id, "status": "CONFIRMED", "idempotent": False}), appointment_id
    except ValueError as exc:
        conn.rollback()
        _idempotency_delete(key)
        if str(exc) == "PATIENT_PROFILE_REQUIRED":
            return _error(403, "PATIENT_PROFILE_REQUIRED", "Create your patient profile first."), None
        raise
    except RuntimeError as exc:
        conn.rollback()
        _idempotency_delete(key)
        mapping = {
            "SLOT_NOT_FOUND": (404, "SLOT_NOT_FOUND", "The selected slot was not found."),
            "SLOT_MISMATCH": (400, "SLOT_MISMATCH", "The selected slot does not belong to the requested doctor/consultation type."),
            "SLOT_FULL": (409, "SLOT_FULL", "The selected slot is no longer available."),
            "SLOT_IN_PAST": (409, "SLOT_IN_PAST", "The selected slot has already started."),
            "BOOKING_WINDOW_EXCEEDED": (400, "BOOKING_WINDOW_EXCEEDED", "The selected slot is outside the booking window."),
        }
        status, code, message = mapping.get(str(exc), (500, "BOOKING_FAILED", "The booking could not be completed."))
        return _error(status, code, message), None
    except Exception:
        conn.rollback()
        _idempotency_delete(key)
        log.exception("Booking failed")
        return _error(500, "INTERNAL_SERVER_ERROR", "The booking could not be completed."), None
    finally:
        conn.close()


def _appointments(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.appointment_id, a.status, a.notes, a.created_at, a.updated_at, "
                "a.doctor_id, a.consultation_type_id, a.slot_id, "
                "d.first_name AS doctor_first_name, d.last_name AS doctor_last_name, "
                "ct.type_name, ct.duration_minutes, dct.fee, s.`date`, s.start_time, s.end_time, s.start_utc, s.end_utc "
                "FROM appointments a "
                "JOIN patients p ON p.patient_id = a.patient_id "
                "JOIN doctors d ON d.doctor_id = a.doctor_id "
                "JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id "
                "JOIN doctor_consultation_types dct ON dct.doctor_id = a.doctor_id AND dct.consultation_type_id = a.consultation_type_id "
                "JOIN materialised_slots s ON s.slot_id = a.slot_id "
                "WHERE p.user_id = %s ORDER BY s.start_utc DESC LIMIT 200",
                (user_id,),
            )
            rows = cur.fetchall()
        return response(200, {"items": rows, "count": len(rows)})
    finally:
        conn.close()


def _refund_percentage(hours):
    full, seventy_five, fifty = refund_bands()
    if hours > full:
        return 100.0
    if hours >= seventy_five:
        return 75.0
    if hours >= fifty:
        return 50.0
    return 0.0


def _cancel(user_id, appointment_id, body):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id, a.status, "
                "s.start_utc, dct.fee FROM appointments a "
                "JOIN patients p ON p.patient_id = a.patient_id "
                "JOIN materialised_slots s ON s.slot_id = a.slot_id "
                "JOIN doctor_consultation_types dct ON dct.doctor_id = a.doctor_id AND dct.consultation_type_id = a.consultation_type_id "
                "WHERE a.appointment_id = %s AND p.user_id = %s FOR UPDATE",
                (appointment_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return _error(404, "APPOINTMENT_NOT_FOUND", "Appointment was not found.")
            if row["status"] != "CONFIRMED":
                return _error(409, "NOT_CANCELLABLE", "Only confirmed appointments can be cancelled.")

            hours = (_parse_utc(row["start_utc"]) - _now()).total_seconds() / 3600.0
            percentage = _refund_percentage(hours)
            refund_amount = round(float(row["fee"]) * percentage / 100.0, 2)
            reason = str(body.get("reason") or "Patient cancellation").strip()[:500]

            cur.execute(
                "UPDATE appointments SET status = 'CANCELLED_BY_PATIENT', notes = %s WHERE appointment_id = %s",
                (reason, appointment_id),
            )
            cur.execute(
                "UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0), "
                "status = IF(booked_count - 1 <= 0, 'OPEN', IF(booked_count - 1 >= capacity, 'FULL', 'OPEN')) "
                "WHERE slot_id = %s",
                (row["slot_id"],),
            )
            refund_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO refund_ledger (refund_id, appointment_id, refund_amount, refund_percentage, refund_status) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE refund_amount = VALUES(refund_amount), refund_percentage = VALUES(refund_percentage)",
                (refund_id, appointment_id, refund_amount, percentage, "CALCULATED" if percentage else "NOT_APPLICABLE"),
            )
        conn.commit()

        _publish("AppointmentCancelled", {
            "appointment_id": appointment_id,
            "slot_id": row["slot_id"],
            "refund_percentage": percentage,
            "refund_amount": refund_amount,
        })
        _publish("SlotSeatFreed", {"slot_id": row["slot_id"], "appointment_id": appointment_id})
        _dispatch_waitlist_offer(row["slot_id"])
        return response(200, {"message": "Appointment cancelled.", "refund": {"percentage": percentage, "amount": refund_amount}})
    except Exception:
        conn.rollback()
        log.exception("Cancellation failed")
        return _error(500, "INTERNAL_SERVER_ERROR", "The appointment could not be cancelled.")
    finally:
        conn.close()


def _reschedule(user_id, appointment_id, body):
    new_slot_id = str(body.get("slot_id") or "").strip()
    if not new_slot_id:
        return _error(400, "MISSING_FIELD", "slot_id is required.")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.appointment_id, a.doctor_id, a.patient_id, a.consultation_type_id, a.slot_id, a.status, "
                "COALESCE(a.reschedule_count, 0) AS reschedule_count, old.start_utc AS old_start "
                "FROM appointments a JOIN patients p ON p.patient_id = a.patient_id "
                "JOIN materialised_slots old ON old.slot_id = a.slot_id "
                "WHERE a.appointment_id = %s AND p.user_id = %s FOR UPDATE",
                (appointment_id, user_id),
            )
            appointment = cur.fetchone()
            if not appointment:
                return _error(404, "APPOINTMENT_NOT_FOUND", "Appointment was not found.")
            if appointment["status"] != "CONFIRMED":
                return _error(409, "NOT_RESCHEDULABLE", "Only confirmed appointments can be rescheduled.")
            if int(appointment["reschedule_count"]) >= max_reschedules():
                return _error(409, "RESCHEDULE_LIMIT", "An appointment can be rescheduled at most twice.")
            hours = (_parse_utc(appointment["old_start"]) - _now()).total_seconds() / 3600.0
            if hours < min_reschedule_hours():
                return _error(409, "RESCHEDULE_WINDOW", "Rescheduling is not allowed within 6 hours of the appointment.")
            if new_slot_id == appointment["slot_id"]:
                return _error(400, "SAME_SLOT", "Choose a different slot.")

            cur.execute(
                "SELECT slot_id, doctor_id, consultation_type_id, start_utc, capacity, booked_count, status "
                "FROM materialised_slots WHERE slot_id = %s FOR UPDATE",
                (new_slot_id,),
            )
            new_slot = cur.fetchone()
            if not new_slot:
                return _error(404, "SLOT_NOT_FOUND", "The new slot was not found.")
            if str(new_slot["doctor_id"]) != str(appointment["doctor_id"]):
                return _error(400, "DOCTOR_MISMATCH", "The new slot must belong to the same doctor.")
            if str(new_slot["consultation_type_id"]) != str(appointment["consultation_type_id"]):
                return _error(400, "TYPE_MISMATCH", "The new slot must use the same consultation type.")
            if new_slot["status"] != "OPEN" or int(new_slot["booked_count"]) >= int(new_slot["capacity"]):
                return _error(409, "SLOT_FULL", "The new slot is no longer available.")
            if _parse_utc(new_slot["start_utc"]) <= _now():
                return _error(409, "SLOT_IN_PAST", "The new slot has already started.")
            cur.execute(
                "UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0), "
                "status = IF(booked_count - 1 <= 0, 'OPEN', IF(booked_count - 1 >= capacity, 'FULL', 'OPEN')) "
                "WHERE slot_id = %s",
                (appointment["slot_id"],),
            )
            new_count = int(new_slot["booked_count"]) + 1
            new_status = "FULL" if new_count >= int(new_slot["capacity"]) else "OPEN"
            cur.execute(
                "UPDATE materialised_slots SET booked_count = %s, status = %s WHERE slot_id = %s",
                (new_count, new_status, new_slot_id),
            )
            cur.execute(
                "UPDATE appointments SET slot_id = %s, reschedule_count = COALESCE(reschedule_count, 0) + 1, "
                "notes = %s WHERE appointment_id = %s",
                (new_slot_id, str(body.get("notes") or "Rescheduled")[:2000], appointment_id),
            )
        conn.commit()
        _publish("AppointmentRescheduled", {"appointment_id": appointment_id, "old_slot_id": appointment["slot_id"], "new_slot_id": new_slot_id})
        return response(200, {"appointment_id": appointment_id, "status": "CONFIRMED", "slot_id": new_slot_id})
    except Exception:
        conn.rollback()
        log.exception("Reschedule failed")
        return _error(500, "INTERNAL_SERVER_ERROR", "The appointment could not be rescheduled.")
    finally:
        conn.close()


def _waitlist_join(user_id, body):
    slot_id = str(body.get("slot_id") or "").strip()
    if not slot_id:
        return _error(400, "MISSING_FIELD", "slot_id is required.")
    conn = get_connection()
    try:
        patient = _require_patient(conn, user_id)
        with conn.cursor() as cur:
            cur.execute("SELECT slot_id, status, capacity, booked_count FROM materialised_slots WHERE slot_id = %s", (slot_id,))
            slot = cur.fetchone()
            if not slot:
                return _error(404, "SLOT_NOT_FOUND", "The selected slot was not found.")
            if int(slot["booked_count"]) < int(slot["capacity"]) and slot["status"] == "OPEN":
                return _error(409, "SLOT_AVAILABLE", "The slot is available now; book it instead of joining the waitlist.")

        table = _table(WAITLIST_TABLE)
        existing = table.query(KeyConditionExpression=Key("slot_id").eq(slot_id))
        if any(item.get("patient_id") == patient["patient_id"] for item in existing.get("Items", [])):
            return _error(409, "ALREADY_WAITLISTED", "You are already on the waitlist for this slot.")
        if len(existing.get("Items", [])) >= waitlist_max_size():
            return _error(409, "WAITLIST_FULL", "The waitlist is full for this slot.")

        joined = _iso(_now())
        waitlist_id = str(uuid.uuid4())
        table.put_item(Item={"slot_id": slot_id, "created_at": joined, "patient_id": patient["patient_id"], "waitlist_id": waitlist_id})
        return response(201, {"waitlist_id": waitlist_id, "slot_id": slot_id, "joined_at": joined})
    except ValueError as exc:
        if str(exc) == "PATIENT_PROFILE_REQUIRED":
            return _error(403, "PATIENT_PROFILE_REQUIRED", "Create your patient profile first.")
        raise
    finally:
        conn.close()


def _waitlist_leave(user_id, waitlist_id):
    table = _table(WAITLIST_TABLE)
    scan = table.scan(FilterExpression=Attr("waitlist_id").eq(waitlist_id))
    items = scan.get("Items", [])
    if not items:
        return _error(404, "WAITLIST_NOT_FOUND", "Waitlist entry was not found.")
    item = items[0]
    conn = get_connection()
    try:
        patient = _require_patient(conn, user_id)
    finally:
        conn.close()
    if item.get("patient_id") != patient["patient_id"]:
        return _error(403, "FORBIDDEN", "You cannot remove another patient's waitlist entry.")
    table.delete_item(Key={"slot_id": item["slot_id"], "created_at": item["created_at"]})
    return response(200, {"message": "Removed from waitlist."})


def _waitlist_list(user_id):
    conn = get_connection()
    try:
        patient = _require_patient(conn, user_id)
    finally:
        conn.close()
    table = _table(WAITLIST_TABLE)
    scan = table.scan(FilterExpression=Attr("patient_id").eq(patient["patient_id"]))
    return response(200, {"items": scan.get("Items", [])})


def _hold_offer_accept(user_id, offer_id):
    table = _table(HOLD_OFFERS_TABLE)
    offer = table.get_item(Key={"offer_id": offer_id}, ConsistentRead=True).get("Item")
    if not offer:
        return _error(404, "OFFER_NOT_FOUND", "Hold offer was not found.")
    conn = get_connection()
    try:
        patient = _require_patient(conn, user_id)
    finally:
        conn.close()
    if offer.get("patient_id") != patient["patient_id"]:
        return _error(403, "FORBIDDEN", "This hold offer belongs to another patient.")
    if offer.get("status") != "PENDING":
        return _error(409, "OFFER_NOT_ACTIVE", "This hold offer is no longer active.")
    if int(offer.get("expires_at", 0)) <= int(_now().timestamp()):
        table.update_item(Key={"offer_id": offer_id}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "EXPIRED"})
        return _error(409, "OFFER_EXPIRED", "This hold offer has expired.")

    body = {"doctor_id": offer["doctor_id"], "consultation_type_id": offer["consultation_type_id"], "slot_id": offer["slot_id"], "idempotency_key": f"hold-{offer_id}"}
    result, appointment_id = _book(user_id, body, idempotency_key=body["idempotency_key"], hold_offer_id=offer_id)
    if appointment_id:
        table.update_item(Key={"offer_id": offer_id}, UpdateExpression="SET #s = :s, appointment_id = :a", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "ACCEPTED", ":a": appointment_id})
    return result


def _dispatch_waitlist_offer(slot_id):
    if not HOLD_OFFERS_TABLE or not WAITLIST_TABLE:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.doctor_id, s.consultation_type_id FROM materialised_slots s WHERE s.slot_id = %s",
                (slot_id,),
            )
            slot = cur.fetchone()
        if not slot:
            return
    finally:
        conn.close()

    waitlist = _table(WAITLIST_TABLE)
    entries = waitlist.query(KeyConditionExpression=Key("slot_id").eq(slot_id), ScanIndexForward=True, Limit=1).get("Items", [])
    if not entries:
        return
    entry = entries[0]
    offer_id = str(uuid.uuid4())
    expires = int((_now() + timedelta(minutes=hold_offer_minutes())).timestamp())
    _table(HOLD_OFFERS_TABLE).put_item(Item={
        "offer_id": offer_id,
        "slot_id": slot_id,
        "patient_id": entry["patient_id"],
        "waitlist_id": entry["waitlist_id"],
        "doctor_id": slot["doctor_id"],
        "consultation_type_id": slot["consultation_type_id"],
        "status": "PENDING",
        "created_at": _iso(_now()),
        "expires_at": expires,
    })
    # The patient now owns a time-limited offer rather than an active waitlist position.
    waitlist.delete_item(Key={"slot_id": entry["slot_id"], "created_at": entry["created_at"]})
    _publish("WaitlistHoldOffered", {"offer_id": offer_id, "slot_id": slot_id, "patient_id": entry["patient_id"], "expires_at": expires})


def _route(event, user_id, role):
    method = request_method(event)
    resource = event.get("resource") or ""
    path_params = event.get("pathParameters") or {}

    if method == "GET" and resource == "/doctors/search":
        return _doctor_search(event)
    if method == "GET" and resource == "/doctors/{doctor_id}/slots":
        return _availability(event)
    if method == "POST" and resource == "/bookings":
        body = request_body(event)
        result, _ = _book(user_id, body)
        return result
    if method == "GET" and resource == "/appointments":
        return _appointments(user_id)
    if method == "DELETE" and resource == "/appointments/{appointment_id}":
        return _cancel(user_id, path_params.get("appointment_id"), request_body(event))
    if method == "POST" and resource == "/appointments/{appointment_id}/reschedule":
        return _reschedule(user_id, path_params.get("appointment_id"), request_body(event))
    if method == "POST" and resource == "/waitlist":
        return _waitlist_join(user_id, request_body(event))
    if method == "GET" and resource == "/waitlist":
        return _waitlist_list(user_id)
    if method == "DELETE" and resource == "/waitlist/{waitlist_id}":
        return _waitlist_leave(user_id, path_params.get("waitlist_id"))
    if method == "POST" and resource == "/waitlist/offers/{offer_id}/accept":
        return _hold_offer_accept(user_id, path_params.get("offer_id"))
    if resource == "/patient/me" and method in ("GET", "POST", "PUT", "DELETE"):
        return _profile_route(method, user_id, event)
    if method == "GET" and resource == "/doctors/{doctor_id}":
        return _doctor_profile(event)
    return _error(404, "ROUTE_NOT_FOUND", "Unsupported patient route.")


def lambda_handler(event, context):
    identity = claims(event)
    user_id, role = identity.get("user_id"), identity.get("role")
    if not user_id:
        return _error(401, "UNAUTHORIZED", "A valid access token is required.")
    if role != "patient":
        return _error(403, "FORBIDDEN", "This endpoint is for patients only.")
    try:
        return _route(event, str(user_id), str(role))
    except ApiError as exc:
        return _error(exc.status, exc.code, exc.message)
    except ValueError as exc:
        if str(exc) == "PATIENT_PROFILE_REQUIRED":
            return _error(403, "PATIENT_PROFILE_REQUIRED", "Create your patient profile first.")
        if str(exc) == "INVALID_JSON":
            return _error(400, "INVALID_JSON", "Request body must be valid JSON.")
        log.exception("Patient API validation failed")
        return _error(400, "INVALID_REQUEST", str(exc))
    except PermissionError as exc:
        if str(exc) == "IDEMPOTENCY_KEY_OWNERSHIP":
            return _error(409, "IDEMPOTENCY_KEY_IN_USE", "That idempotency key belongs to another request.")
        return _error(403, "FORBIDDEN", "The requested operation is not permitted.")
    except Exception as exc:
        log.exception("Patient API failed")
        # In dev only, include the real cause so it shows on the web page without opening CloudWatch.
        detail = f" [{type(exc).__name__}: {exc}]" if os.environ.get("ENVIRONMENT_NAME") == "dev" else ""
        return _error(500, "INTERNAL_SERVER_ERROR", "The request could not be completed." + detail)