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


def create(ctx):
    doctor = ctx.doctor()
    day = parse_date(ctx.body.get("blackout_date"), "blackout_date")
    if day < _today(doctor):
        raise ApiError(400, "DATE_IN_PAST", "blackout_date cannot be in the past.")
    reason = clean_text(ctx.body.get("reason"), "reason", 255)
    blackout_id = new_id()
    try:
        with ctx.conn.cursor() as cur:
            cur.execute("INSERT INTO blackout_dates (blackout_id, doctor_id, blackout_date, reason) VALUES (%s, %s, %s, %s)",
                        (blackout_id, doctor["doctor_id"], day, reason))
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            raise ApiError(409, "BLACKOUT_EXISTS", "That date is already blacked out.") from None
        raise
    blocked, with_bookings = slots.block_date(ctx.conn, doctor["doctor_id"], day)
    return 201, {"blackout": {"blackout_id": blackout_id, "blackout_date": day.isoformat(), "reason": reason or ""},
                 "slots_blocked": blocked, "slots_with_bookings": with_bookings}


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
