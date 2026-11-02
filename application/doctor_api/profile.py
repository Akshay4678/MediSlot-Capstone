"""GET /doctors/me and PUT /doctors/me (create-or-update the doctor's own profile)."""

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pymysql

from application.common.api import ApiError, clean_text, new_id, require_uuid
from application.doctor_api import photo
from application.slot_materialiser import core as slots

LICENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/\-]{4,49}$")
REQUIRED_ON_CREATE = ("first_name", "last_name", "licence_number", "hospital_id", "specialisation_id", "timezone")

PROFILE_SQL = (
    "SELECT d.doctor_id, d.first_name, d.last_name, d.licence_number, d.experience_years, d.qualifications, "
    "       d.photo_url, d.timezone, d.is_active, d.hospital_id, h.hospital_name, h.address AS hospital_address, "
    "       h.city AS hospital_city, d.specialisation_id, s.specialisation_name "
    "FROM doctors d "
    "JOIN hospitals h ON h.hospital_id = d.hospital_id "
    "JOIN specialisations s ON s.specialisation_id = d.specialisation_id "
    "WHERE d.user_id = %s"
)


def load_profile(ctx):
    with ctx.conn.cursor() as cur:
        cur.execute(PROFILE_SQL, (ctx.user_id,))
        row = cur.fetchone()
    if not row:
        return None
    key = row.pop("photo_url")
    row["photo_key"] = key
    row["photo_url"] = photo.view_url(key)
    row["is_active"] = bool(row["is_active"])
    row["qualifications"] = row["qualifications"] or ""
    return row


def get(ctx):
    profile = load_profile(ctx)
    if not profile:
        raise ApiError(404, "PROFILE_NOT_FOUND", "Doctor profile not created yet. Create it with PUT /doctors/me.")
    return 200, {"profile": profile}


def _validate(body, creating):
    values = {}
    if creating:
        missing = [f for f in REQUIRED_ON_CREATE if body.get(f) in (None, "")]
        if missing:
            raise ApiError(400, "MISSING_FIELD", f"Required to create a profile: {', '.join(missing)}.")

    if "first_name" in body:
        values["first_name"] = clean_text(body["first_name"], "first_name", 100, required=True)
    if "last_name" in body:
        values["last_name"] = clean_text(body["last_name"], "last_name", 100, required=True)
    if "licence_number" in body:
        licence = clean_text(body["licence_number"], "licence_number", 50, required=True).upper()
        if not LICENCE_RE.match(licence):
            raise ApiError(400, "INVALID_LICENCE", "licence_number must be 5-50 letters, digits, '/' or '-'.")
        values["licence_number"] = licence
    if "hospital_id" in body:
        values["hospital_id"] = require_uuid(body["hospital_id"], "hospital_id")
    if "specialisation_id" in body:
        values["specialisation_id"] = require_uuid(body["specialisation_id"], "specialisation_id")
    if "experience_years" in body:
        try:
            years = int(body["experience_years"])
        except (TypeError, ValueError):
            raise ApiError(400, "INVALID_FIELD", "experience_years must be a whole number.") from None
        if not 0 <= years <= 70:
            raise ApiError(400, "INVALID_FIELD", "experience_years must be between 0 and 70.")
        values["experience_years"] = years
    if "qualifications" in body:
        values["qualifications"] = clean_text(body["qualifications"], "qualifications", 2000)
    if "timezone" in body:
        name = clean_text(body["timezone"], "timezone", 64, required=True)
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ApiError(400, "INVALID_TIMEZONE", "timezone must be an IANA identifier such as Asia/Kolkata.") from None
        values["timezone"] = name
    return values


def _check_references(cur, values):
    if "hospital_id" in values:
        cur.execute("SELECT 1 AS ok FROM hospitals WHERE hospital_id = %s AND is_active = TRUE", (values["hospital_id"],))
        if not cur.fetchone():
            raise ApiError(400, "UNKNOWN_HOSPITAL", "hospital_id does not exist.")
    if "specialisation_id" in values:
        cur.execute("SELECT 1 AS ok FROM specialisations WHERE specialisation_id = %s", (values["specialisation_id"],))
        if not cur.fetchone():
            raise ApiError(400, "UNKNOWN_SPECIALISATION", "specialisation_id does not exist.")


def put(ctx):
    body = ctx.body
    existing = ctx.find_doctor()
    values = _validate(body, creating=existing is None)
    if not values:
        raise ApiError(400, "NOTHING_TO_UPDATE", "No updatable profile fields were supplied.")

    try:
        with ctx.conn.cursor() as cur:
            _check_references(cur, values)
            if existing is None:
                doctor_id = new_id()
                columns = ["doctor_id", "user_id"] + list(values)
                params = [doctor_id, ctx.user_id] + list(values.values())
                cur.execute(
                    f"INSERT INTO doctors ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})", params)
                status = 201
            else:
                doctor_id = existing["doctor_id"]
                assignments = ", ".join(f"{column} = %s" for column in values)
                cur.execute(f"UPDATE doctors SET {assignments} WHERE doctor_id = %s", list(values.values()) + [doctor_id])
                status = 200
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            raise ApiError(409, "LICENCE_EXISTS", "This licence number is already registered.") from None
        raise

    # A timezone change moves every UTC instant: rebuild the not-yet-booked future slots.
    if existing is not None and "timezone" in values and values["timezone"] != existing["timezone"]:
        slots.rebuild_future_slots(ctx.conn, doctor_id, values["timezone"])
    return status, {"profile": load_profile(ctx)}
