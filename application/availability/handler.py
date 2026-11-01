"""Doctor day-view endpoint using the approved MediSlot ERD.

Route:
    GET /doctor/schedule?date=YYYY-MM-DD

The authenticated doctor's identity is taken from API Gateway authorizer context.
The endpoint reads relational schedule/blackout data from RDS, materialised slots
from DynamoDB, and appointment/patient details from RDS.
"""

import logging
import os
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
from boto3.dynamodb.conditions import Attr

from application.common.http import response, request_method
from application.database.connection import get_connection

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def _authorizer_context(event):
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    return authorizer.get("lambda") or authorizer.get("claims") or authorizer


def _identity(event):
    """Return the authenticated user id and role; never trust query/body identity."""
    context = _authorizer_context(event)
    user_id = context.get("userId") or context.get("user_id") or context.get("sub")
    role = str(context.get("role") or "").strip().lower()

    if user_id in (None, ""):
        raise PermissionError("Authenticated user identity is missing.")

    try:
        user_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Authorizer user id must be a BIGINT.") from exc

    if role not in ("doctor", "admin"):
        raise PermissionError("Only doctors or administrators can access this endpoint.")

    return user_id, role


def _parse_target_date(event):
    params = event.get("queryStringParameters") or {}
    raw_date = str(params.get("date") or "").strip()

    if not raw_date:
        raise ValueError("date is required in YYYY-MM-DD format.")

    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD format.") from exc


def _resolve_doctor(cursor, user_id, role, event):
    """Doctors can only read their own schedule; admins may supply doctor_id."""
    params = event.get("queryStringParameters") or {}

    if role == "admin" and params.get("doctor_id"):
        try:
            doctor_id = int(params["doctor_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError("doctor_id must be a positive integer.") from exc

        cursor.execute(
            """
            SELECT doctor_id, timezone, is_active
            FROM doctors
            WHERE doctor_id = %s
            LIMIT 1
            """,
            (doctor_id,),
        )
    else:
        cursor.execute(
            """
            SELECT doctor_id, timezone, is_active
            FROM doctors
            WHERE user_id = %s
            LIMIT 1
            """,
            (user_id,),
        )

    doctor = cursor.fetchone()
    if not doctor:
        return None

    if not doctor.get("is_active"):
        raise PermissionError("The doctor profile is inactive.")

    return doctor


def _scan_slots(table, doctor_id, target_date):
    """Read one doctor's slots for a date.

    The approved ERD defines slot_id as the DynamoDB primary key but does not
    define a doctor/date GSI. A paginated Scan is therefore used here so this
    file works with the table definition currently shown in the project.
    For larger production datasets, add a GSI (doctor_id HASH, date RANGE)
    and replace this scan with Query.
    """
    scan_args = {
        "FilterExpression": (
            Attr("doctor_id").eq(int(doctor_id))
            & Attr("date").eq(target_date.isoformat())
        )
    }
    items = []

    while True:
        page = table.scan(**scan_args)
        items.extend(page.get("Items", []))

        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break

        scan_args["ExclusiveStartKey"] = last_key

    items.sort(key=lambda item: str(item.get("start_time", "")))
    return items


def _time_value(value):
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value))


def _slot_utc_values(slot, target_date, zone):
    """Return UTC ISO strings for the slot's local wall-clock times."""
    start_local = datetime.combine(target_date, _time_value(slot["start_time"]))
    end_local = datetime.combine(target_date, _time_value(slot["end_time"]))

    start_aware = start_local.replace(tzinfo=zone, fold=0)
    end_aware = end_local.replace(tzinfo=zone, fold=0)

    # Reject nonexistent wall times in a DST spring-forward gap.
    start_roundtrip = (
        start_aware.astimezone(timezone.utc)
        .astimezone(zone)
        .replace(tzinfo=None)
    )
    end_roundtrip = (
        end_aware.astimezone(timezone.utc)
        .astimezone(zone)
        .replace(tzinfo=None)
    )

    start_utc = (
        start_aware.astimezone(timezone.utc).isoformat()
        if start_roundtrip == start_local
        else None
    )
    end_utc = (
        end_aware.astimezone(timezone.utc).isoformat()
        if end_roundtrip == end_local
        else None
    )

    return start_utc, end_utc


def _appointment_details(cursor, doctor_id, slot_ids):
    """Fetch appointment and patient details for the selected DynamoDB slots."""
    if not slot_ids:
        return {}

    appointments_by_slot = {}

    # Keep each IN clause below conservative MySQL parameter limits.
    for offset in range(0, len(slot_ids), 400):
        chunk = slot_ids[offset : offset + 400]
        placeholders = ", ".join(["%s"] * len(chunk))

        cursor.execute(
            f"""
            SELECT
                a.appointment_id,
                a.patient_id,
                a.doctor_id,
                a.slot_id,
                a.consultation_type_id,
                a.status,
                a.notes,
                a.created_at,
                a.updated_at,
                p.first_name AS patient_first_name,
                p.last_name AS patient_last_name,
                p.phone AS patient_phone
            FROM appointments a
            JOIN patients p ON p.patient_id = a.patient_id
            WHERE a.doctor_id = %s
              AND a.slot_id IN ({placeholders})
              AND a.status IN ('PENDING', 'CONFIRMED')
            ORDER BY a.created_at
            """,
            (doctor_id, *chunk),
        )

        for row in cursor.fetchall():
            appointments_by_slot.setdefault(str(row["slot_id"]), []).append(row)

    return appointments_by_slot


def _build_schedule_slots(slots, appointments_by_slot, target_date, zone):
    output = []

    for slot in slots:
        slot_id = str(slot.get("slot_id", ""))
        appointments = appointments_by_slot.get(slot_id, [])

        start_utc, end_utc = _slot_utc_values(slot, target_date, zone)

        output.append(
            {
                "slot_id": slot_id,
                "availability_id": slot.get("availability_id"),
                "doctor_id": slot.get("doctor_id"),
                "consultation_type_id": slot.get("consultation_type_id"),
                "date": slot.get("date"),
                "start_time": slot.get("start_time"),
                "end_time": slot.get("end_time"),
                "start_utc": start_utc,
                "end_utc": end_utc,
                "capacity": int(slot.get("capacity", 1)),
                "booked_count": int(slot.get("booked_count", len(appointments))),
                "remaining_capacity": max(
                    0,
                    int(slot.get("capacity", 1))
                    - int(slot.get("booked_count", len(appointments))),
                ),
                "status": slot.get("status", "AVAILABLE"),
                "appointments": appointments,
            }
        )

    return output


def lambda_handler(event, context):
    """Return a doctor's schedule, active patterns, blackouts and materialised slots."""
    if request_method(event) != "GET":
        return response(405, {"error": "METHOD_NOT_ALLOWED"})

    connection = None

    try:
        user_id, role = _identity(event)
        target_date = _parse_target_date(event)
        table_name = os.environ.get("MATERIALISED_SLOTS_TABLE")

        if not table_name:
            log.error("MATERIALISED_SLOTS_TABLE environment variable is missing.")
            return response(500, {"error": "SERVER_CONFIGURATION_ERROR"})

        connection = get_connection()

        with connection.cursor() as cursor:
            doctor = _resolve_doctor(cursor, user_id, role, event)

            if not doctor:
                return response(
                    404,
                    {
                        "error": "DOCTOR_PROFILE_NOT_FOUND",
                        "message": "No doctor profile is associated with this account.",
                    },
                )

            doctor_id = int(doctor["doctor_id"])

            try:
                doctor_timezone = ZoneInfo(
                    str(doctor.get("timezone") or "Asia/Kolkata")
                )
            except ZoneInfoNotFoundError as exc:
                log.exception("Invalid timezone configured for doctor_id=%s", doctor_id)
                raise ValueError("Doctor profile contains an invalid IANA timezone.") from exc

            day_of_week = target_date.isoweekday()

            cursor.execute(
                """
                SELECT
                    ap.availability_id,
                    ap.doctor_id,
                    ap.consultation_type_id,
                    ap.day_of_week,
                    ap.start_time,
                    ap.end_time,
                    ap.is_active,
                    ap.capacity,
                    ct.type_name AS consultation_type,
                    ct.duration_minutes,
                    ct.fee
                FROM availability_patterns ap
                JOIN consultation_types ct
                  ON ct.consultation_type_id = ap.consultation_type_id
                WHERE ap.doctor_id = %s
                  AND ap.day_of_week = %s
                  AND ap.is_active = TRUE
                ORDER BY ap.start_time
                """,
                (doctor_id, day_of_week),
            )
            availability_patterns = cursor.fetchall()

            cursor.execute(
                """
                SELECT blackout_id, doctor_id, blackout_date, reason
                FROM blackout_dates
                WHERE doctor_id = %s
                  AND blackout_date = %s
                ORDER BY blackout_id
                """,
                (doctor_id, target_date),
            )
            blackout_dates = cursor.fetchall()

            # The ERD's blackout_dates table blocks the whole local date.
            if blackout_dates:
                return response(
                    200,
                    {
                        "doctor_id": doctor_id,
                        "date": target_date.isoformat(),
                        "timezone": str(doctor_timezone),
                        "schedule_windows": availability_patterns,
                        "blackout_dates": blackout_dates,
                        "slots": [],
                        "count": 0,
                        "message": "This doctor has declared a blackout for the selected date.",
                    },
                )

            dynamodb_table = boto3.resource("dynamodb").Table(table_name)
            slots = _scan_slots(dynamodb_table, doctor_id, target_date)
            slot_ids = [
                str(item["slot_id"])
                for item in slots
                if item.get("slot_id") not in (None, "")
            ]
            appointments_by_slot = _appointment_details(
                cursor, doctor_id, slot_ids
            )

            schedule_slots = _build_schedule_slots(
                slots,
                appointments_by_slot,
                target_date,
                doctor_timezone,
            )

        return response(
            200,
            {
                "doctor_id": doctor_id,
                "date": target_date.isoformat(),
                "timezone": str(doctor_timezone),
                "day_of_week": day_of_week,
                "schedule_windows": availability_patterns,
                "blackout_dates": blackout_dates,
                "slots": schedule_slots,
                "count": len(schedule_slots),
                "availability_source": (
                    "DynamoDB materialised slots; appointments and patient details "
                    "are joined from RDS"
                ),
            },
        )

    except PermissionError as exc:
        return response(403, {"error": "FORBIDDEN", "message": str(exc)})
    except ValueError as exc:
        return response(400, {"error": "INVALID_REQUEST", "message": str(exc)})
    except Exception:
        log.exception("Doctor schedule query failed.")
        return response(500, {"error": "INTERNAL_SERVER_ERROR"})
    finally:
        if connection:
            connection.close()
