"""Blackout (leave) dates: POST/GET /doctors/me/blackouts, DELETE /doctors/me/blackouts/{id}."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pymysql

from application.common.api import ApiError, as_date, clean_text, new_id, parse_date, require_uuid
from application.slot_materialiser import core as slots


def _today(doctor):
    return datetime.now(timezone.utc).astimezone(ZoneInfo(doctor["timezone"])).date()


def _row(r):
    return {"blackout_id": r["blackout_id"], "blackout_date": as_date(r["blackout_date"]).isoformat(),
            "reason": r["reason"] or ""}


def list_(ctx):
    doctor = ctx.doctor()
    with ctx.conn.cursor() as cur:
        cur.execute(
            "SELECT blackout_id, blackout_date, reason FROM blackout_dates "
            "WHERE doctor_id = %s AND blackout_date >= %s ORDER BY blackout_date",
            (doctor["doctor_id"], _today(doctor)))
        return 200, {"items": [_row(r) for r in cur.fetchall()]}


BOOKING_CHOICES = ("keep", "cancel")


def _booked_appointments(conn, doctor_id, day, now_utc):
    """Active (CONFIRMED) appointments that have not started yet on a doctor-local date."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.appointment_id, a.slot_id, a.consultation_type_id, s.start_time, dct.fee "
            "FROM appointments a "
            "JOIN materialised_slots s ON s.slot_id = a.slot_id "
            "JOIN doctor_consultation_types dct ON dct.doctor_id = a.doctor_id "
            "     AND dct.consultation_type_id = a.consultation_type_id "
            "WHERE a.doctor_id = %s AND s.`date` = %s AND a.status = 'CONFIRMED' AND s.start_utc > %s "
            "ORDER BY s.start_utc",
            (doctor_id, day, now_utc))
        return cur.fetchall()


def _cancel_appointments(conn, appointments, reason):
    """Doctor-initiated cancellation: status, 100% refund ledger row, seats released."""
    note = ("Cancelled by doctor: " + (reason or "doctor unavailable"))[:500]
    for a in appointments:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE appointments SET status = 'CANCELLED_BY_DOCTOR', notes = %s "
                "WHERE appointment_id = %s AND status = 'CONFIRMED'",
                (note, a["appointment_id"]))
            if not (cur.rowcount or 0):
                continue
            cur.execute(
                "UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0) WHERE slot_id = %s",
                (a["slot_id"],))
            cur.execute(
                "INSERT INTO refund_ledger (refund_id, appointment_id, refund_amount, refund_percentage, refund_status) "
                "VALUES (%s, %s, %s, 100, 'CALCULATED') "
                "ON DUPLICATE KEY UPDATE refund_amount = VALUES(refund_amount), refund_percentage = 100",
                (new_id(), a["appointment_id"], money_value(a["fee"])))


def money_value(fee):
    return round(float(fee), 2)


def create(ctx):
    doctor = ctx.doctor()
    day = parse_date(ctx.body.get("blackout_date"), "blackout_date")
    if day < _today(doctor):
        raise ApiError(400, "DATE_IN_PAST", "blackout_date cannot be in the past.")
    reason = clean_text(ctx.body.get("reason"), "reason", 255)

    # What should happen to patients who already booked that day? The doctor must decide.
    choice = ctx.body.get("existing_bookings")
    choice = str(choice).strip().lower() if choice not in (None, "") else ""
    if choice and choice not in BOOKING_CHOICES:
        raise ApiError(400, "INVALID_FIELD", "existing_bookings must be 'keep' or 'cancel'.")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    booked = _booked_appointments(ctx.conn, doctor["doctor_id"], day, now_utc)
    if booked and not choice:
        raise ApiError(
            409, "BLACKOUT_HAS_BOOKINGS",
            f"{len(booked)} appointment(s) are already booked on {day.isoformat()}. "
            "Choose whether to keep them or cancel them (patients are refunded in full) before blocking the day.")

    blackout_id = new_id()
    try:
        with ctx.conn.cursor() as cur:
            cur.execute("INSERT INTO blackout_dates (blackout_id, doctor_id, blackout_date, reason) VALUES (%s, %s, %s, %s)",
                        (blackout_id, doctor["doctor_id"], day, reason))
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            raise ApiError(409, "BLACKOUT_EXISTS", "That date is already blacked out.") from None
        raise

    cancelled = 0
    if booked and choice == "cancel":
        _cancel_appointments(ctx.conn, booked, reason)
        cancelled = len(booked)
    blocked, with_bookings = slots.block_date(ctx.conn, doctor["doctor_id"], day)
    return 201, {"blackout": {"blackout_id": blackout_id, "blackout_date": day.isoformat(), "reason": reason or ""},
                 "slots_blocked": blocked, "slots_with_bookings": with_bookings,
                 "appointments_kept": len(booked) - cancelled, "appointments_cancelled": cancelled}


def delete(ctx):
    doctor = ctx.doctor()
    blackout_id = require_uuid(ctx.params.get("id"), "id")
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT blackout_date FROM blackout_dates WHERE blackout_id = %s AND doctor_id = %s",
                    (blackout_id, doctor["doctor_id"]))
        row = cur.fetchone()
        if not row:
            raise ApiError(404, "BLACKOUT_NOT_FOUND", "Blackout date not found.")
        cur.execute("DELETE FROM blackout_dates WHERE blackout_id = %s", (blackout_id,))
    summary = slots.unblock_date(ctx.conn, doctor["doctor_id"], doctor["timezone"], as_date(row["blackout_date"]))
    return 200, {"message": "Blackout removed.", "slots_reopened": summary["reopened"], "slots_created": summary["inserted"]}
