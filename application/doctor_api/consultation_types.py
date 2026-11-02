"""Consultation types owned by the calling doctor: POST/GET/PUT/DELETE /doctors/me/consultation-types."""

from decimal import Decimal, InvalidOperation

import pymysql

from application.common import rules
from application.common.api import ApiError, money, new_id, require_uuid

MAX_FEE = Decimal("100000")


def _fee(value):
    try:
        fee = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ApiError(400, "INVALID_FEE", "fee must be a number.") from None
    if fee < 0 or fee > MAX_FEE:
        raise ApiError(400, "INVALID_FEE", f"fee must be between 0 and {MAX_FEE}.")
    return fee


def _row(row):
    return {"consultation_type_id": row["consultation_type_id"], "type_name": row["type_name"],
            "duration_minutes": int(row["duration_minutes"]), "fee": money(row["fee"])}


def _load(cur, doctor_id, type_id):
    cur.execute(
        "SELECT consultation_type_id, type_name, duration_minutes, fee FROM consultation_types "
        "WHERE consultation_type_id = %s AND doctor_id = %s", (type_id, doctor_id))
    row = cur.fetchone()
    if not row:
        raise ApiError(404, "CONSULTATION_TYPE_NOT_FOUND", "Consultation type not found.")
    return row


def list_(ctx):
    doctor = ctx.doctor()
    with ctx.conn.cursor() as cur:
        cur.execute(
            "SELECT consultation_type_id, type_name, duration_minutes, fee FROM consultation_types "
            "WHERE doctor_id = %s ORDER BY duration_minutes, type_name", (doctor["doctor_id"],))
        items = [_row(r) for r in cur.fetchall()]
    allowed = [{"type_name": n, "duration_minutes": rules.canonical_type(n)[1]} for n in rules.allowed_type_names()]
    return 200, {"items": items, "allowed_types": allowed}


def create(ctx):
    doctor = ctx.doctor()
    canonical = rules.canonical_type(ctx.body.get("type_name"))
    if not canonical:
        raise ApiError(400, "INVALID_TYPE_NAME", f"type_name must be one of {rules.allowed_type_names()}.")
    name, minutes = canonical
    if "duration_minutes" in ctx.body and str(ctx.body["duration_minutes"]) != str(minutes):
        raise ApiError(400, "INVALID_DURATION", f"{name} has a fixed duration of {minutes} minutes.")
    if "fee" not in ctx.body:
        raise ApiError(400, "MISSING_FIELD", "fee is required.")
    fee, type_id = _fee(ctx.body["fee"]), new_id()

    try:
        with ctx.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO consultation_types (consultation_type_id, doctor_id, type_name, duration_minutes, fee) "
                "VALUES (%s, %s, %s, %s, %s)", (type_id, doctor["doctor_id"], name, minutes, fee))
            row = _load(cur, doctor["doctor_id"], type_id)
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            raise ApiError(409, "TYPE_EXISTS", f"You already have a '{name}' consultation type.") from None
        raise
    return 201, {"consultation_type": _row(row)}


def update(ctx):
    doctor = ctx.doctor()
    type_id = require_uuid(ctx.params.get("id"), "id")
    body = ctx.body
    with ctx.conn.cursor() as cur:
        row = _load(cur, doctor["doctor_id"], type_id)
        if "type_name" in body and str(body["type_name"]).strip().lower() != row["type_name"].lower():
            raise ApiError(400, "TYPE_NAME_IMMUTABLE", "type_name cannot be changed; create a new type instead.")
        if "duration_minutes" in body and str(body["duration_minutes"]) != str(row["duration_minutes"]):
            raise ApiError(400, "INVALID_DURATION", "Consultation durations are fixed by the platform rules.")
        if "fee" not in body:
            raise ApiError(400, "NOTHING_TO_UPDATE", "Only fee can be updated.")
        cur.execute("UPDATE consultation_types SET fee = %s WHERE consultation_type_id = %s", (_fee(body["fee"]), type_id))
        row = _load(cur, doctor["doctor_id"], type_id)
    return 200, {"consultation_type": _row(row)}


def delete(ctx):
    doctor = ctx.doctor()
    type_id = require_uuid(ctx.params.get("id"), "id")
    try:
        with ctx.conn.cursor() as cur:
            _load(cur, doctor["doctor_id"], type_id)
            cur.execute("DELETE FROM consultation_types WHERE consultation_type_id = %s", (type_id,))
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] in (1451, 1217):
            raise ApiError(409, "TYPE_IN_USE", "This consultation type is used by an availability pattern, slot or appointment.") from None
        raise
    return 200, {"message": "Consultation type deleted."}
