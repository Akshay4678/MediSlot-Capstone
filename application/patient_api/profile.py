"""Patient profile: create, read, update, delete (POST/GET/PUT/DELETE /patient/me).

The patient is always identified by the user id the authorizer put in the request context, never by
anything in the URL, query string or body, so one patient can never read or change another's profile.
"""

import re
from datetime import date, datetime, timezone

from application.common.api import ApiError, clean_text, new_id, parse_date

COLUMNS = ("patient_id", "first_name", "last_name", "date_of_birth", "gender", "phone", "address", "city",
           "emergency_contact")
GENDERS = {"male": "Male", "female": "Female", "other": "Other", "prefer not to say": "Prefer not to say"}
PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{5,17}[0-9]$")
EDITABLE = ("first_name", "last_name", "date_of_birth", "gender", "phone", "address", "city", "emergency_contact")


def _view(row):
    out = {c: row.get(c) for c in COLUMNS}
    if isinstance(out["date_of_birth"], (date, datetime)):
        out["date_of_birth"] = out["date_of_birth"].isoformat()[:10]
    return out


def _fetch(conn, user_id):
    with conn.cursor() as cur:
        cur.execute("SELECT " + ", ".join(COLUMNS) + " FROM patients WHERE user_id = %s", (user_id,))
        return cur.fetchone()


def validate(body, creating):
    """Return the cleaned values for the fields present in body (all required names when creating)."""
    if not isinstance(body, dict):
        raise ApiError(400, "INVALID_FIELD", "Request body must be a JSON object.")
    values = {}
    for field, limit in (("first_name", 100), ("last_name", 100)):
        if creating or field in body:
            values[field] = clean_text(body.get(field), field, limit, required=True)
    if "date_of_birth" in body:
        raw = body["date_of_birth"]
        if raw in (None, ""):
            values["date_of_birth"] = None
        else:
            dob = parse_date(raw, "date_of_birth")
            if dob > date.today() or dob.year < 1900:
                raise ApiError(400, "INVALID_FIELD", "date_of_birth must be a real past date.")
            values["date_of_birth"] = dob
    if "gender" in body:
        raw = clean_text(body["gender"], "gender", 30)
        if raw is not None and raw.lower() not in GENDERS:
            raise ApiError(400, "INVALID_FIELD", "gender must be Male, Female, Other or Prefer not to say.")
        values["gender"] = GENDERS[raw.lower()] if raw else None
    if "phone" in body:
        phone = clean_text(body["phone"], "phone", 30)
        if phone is not None and not PHONE_RE.match(phone):
            raise ApiError(400, "INVALID_PHONE", "phone must be 7-19 digits, optionally starting with + (spaces and - allowed).")
        values["phone"] = phone
    for field, limit in (("address", 500), ("city", 100), ("emergency_contact", 150)):
        if field in body:
            values[field] = clean_text(body[field], field, limit)
    return values


def get(conn, user_id):
    row = _fetch(conn, user_id)
    if not row:
        raise ApiError(404, "PATIENT_NOT_FOUND", "Patient profile was not found.")
    return 200, {"patient": _view(row)}


def create(conn, user_id, body):
    values = validate(body, creating=True)
    if _fetch(conn, user_id):
        raise ApiError(409, "PROFILE_EXISTS", "Your patient profile already exists; use PUT to change it.")
    columns = ["patient_id", "user_id"] + list(values)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO patients ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
            [new_id(), user_id] + list(values.values()))
    conn.commit()
    return 201, {"patient": _view(_fetch(conn, user_id))}


def update(conn, user_id, body):
    if not _fetch(conn, user_id):
        raise ApiError(404, "PATIENT_NOT_FOUND", "Patient profile was not found.")
    values = validate(body, creating=False)
    unknown = [k for k in body if k not in EDITABLE]
    if unknown:
        raise ApiError(400, "UNKNOWN_FIELD", "These fields cannot be changed here: " + ", ".join(sorted(unknown)) + ".")
    if not values:
        raise ApiError(400, "NO_CHANGES", "Send at least one field to update.")
    with conn.cursor() as cur:
        cur.execute("UPDATE patients SET " + ", ".join(f"{k} = %s" for k in values) + " WHERE user_id = %s",
                    list(values.values()) + [user_id])
    conn.commit()
    return 200, {"patient": _view(_fetch(conn, user_id))}


def delete(conn, user_id, now_utc=None):
    """Close the account: personal details are erased and login is disabled.

    The row itself stays (appointments and refunds reference it), but nothing identifying remains.
    Refused while the patient still has upcoming confirmed appointments.
    """
    row = _fetch(conn, user_id)
    if not row:
        raise ApiError(404, "PATIENT_NOT_FOUND", "Patient profile was not found.")
    now_utc = (now_utc or datetime.now(timezone.utc)).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM appointments a JOIN materialised_slots s ON s.slot_id = a.slot_id "
            "WHERE a.patient_id = %s AND a.status = 'CONFIRMED' AND s.start_utc > %s",
            (row["patient_id"], now_utc))
        found = cur.fetchone()
        if int(found["n"] if isinstance(found, dict) else found[0]):
            raise ApiError(409, "HAS_ACTIVE_APPOINTMENTS",
                           "Cancel your upcoming appointments before deleting your profile.")
        cur.execute(
            "UPDATE patients SET first_name = 'Deleted', last_name = 'Patient', date_of_birth = NULL, gender = NULL, "
            "phone = NULL, address = NULL, city = NULL, emergency_contact = NULL WHERE user_id = %s", (user_id,))
        cur.execute("UPDATE users SET is_active = FALSE WHERE user_id = %s", (user_id,))
    conn.commit()
    return 200, {"message": "Your profile was deleted and your account is closed."}
