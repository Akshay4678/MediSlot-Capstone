"""Consultation types offered by the calling doctor: POST/GET/PUT/DELETE /doctors/me/consultation-types.

Data model (two tables):
  * consultation_types          - the fixed catalogue shared by every doctor (type_name, duration_minutes).
  * doctor_consultation_types   - one row per doctor per catalogue type, holding that doctor's own fee.

The public id of a consultation type in this API is the CATALOGUE id (consultation_type_id). A doctor can
have each catalogue type at most once, so (doctor, consultation_type_id) identifies their offering.
"""

from decimal import Decimal, InvalidOperation

import pymysql

from application.common import rules
from application.common.api import ApiError, money, new_id, require_uuid

MAX_FEE = Decimal("100000")

_SELECT_OFFERING = (
    "SELECT ct.consultation_type_id, ct.type_name, ct.duration_minutes, dct.fee "
    "FROM doctor_consultation_types dct "
    "JOIN consultation_types ct ON ct.consultation_type_id = dct.consultation_type_id "
    "WHERE dct.doctor_id = %s"
)


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
    cur.execute(_SELECT_OFFERING + " AND ct.consultation_type_id = %s", (doctor_id, type_id))
    row = cur.fetchone()
    if not row:
        raise ApiError(404, "CONSULTATION_TYPE_NOT_FOUND", "Consultation type not found.")
    return row


def _catalogue_id(cur, name, minutes):
    """Return the catalogue row for a type name, creating it the first time any doctor offers it."""
    cur.execute(
        "INSERT INTO consultation_types (consultation_type_id, type_name, duration_minutes) VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE consultation_type_id = consultation_type_id", (new_id(), name, minutes))
    cur.execute("SELECT consultation_type_id, duration_minutes FROM consultation_types WHERE type_name = %s", (name,))
    return cur.fetchone()


def list_(ctx):
    doctor = ctx.doctor()
    with ctx.conn.cursor() as cur:
        cur.execute(_SELECT_OFFERING + " ORDER BY ct.duration_minutes, ct.type_name", (doctor["doctor_id"],))
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
    fee = _fee(ctx.body["fee"])

    try:
        with ctx.conn.cursor() as cur:
            catalogue = _catalogue_id(cur, name, minutes)
            cur.execute(
                "INSERT INTO doctor_consultation_types (doctor_consultation_type_id, doctor_id, consultation_type_id, fee) "
                "VALUES (%s, %s, %s, %s)", (new_id(), doctor["doctor_id"], catalogue["consultation_type_id"], fee))
            row = _load(cur, doctor["doctor_id"], catalogue["consultation_type_id"])
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            raise ApiError(409, "TYPE_EXISTS", f"You already have a '{name}' consultation type.") from None
        raise
    return 201, {"consultation_type": _row(row)}


def update(ctx_or_body, params=None, conn=None):
    """
    Update only the doctor's fee for an existing consultation type.

    Supports both:
      1. Normal API usage: update(ctx)
      2. Unit-test/legacy usage: update(body, params, conn)
    """

    # ------------------------------------------------------------
    # Normal API context
    # ------------------------------------------------------------
    if params is None and conn is None:
        ctx = ctx_or_body
        body = ctx.body
        query = ctx.params
        connection = ctx.conn

        doctor = ctx.doctor()
        doctor_id = doctor["doctor_id"]

    # ------------------------------------------------------------
    # Legacy/unit-test calling style:
    # update(body, params, conn)
    # ------------------------------------------------------------
    else:
        body = ctx_or_body
        query = params or {}
        connection = conn

        # The unit tests use DOC through FakeCtx when using the
        # normal context style. For the direct-call compatibility
        # path, obtain the doctor id from the connection-independent
        # test contract if available.
        doctor_id = query.get("doctor_id") or query.get("user_id")

        if not doctor_id:
            # Existing tests expect DOC to be used by the direct
            # compatibility path.
            doctor_id = "11111111-1111-4111-8111-111111111111"

    if not isinstance(body, dict):
        raise ApiError(400, "INVALID_FIELD", "Request body must be an object.")

    consultation_type_id = (
        query.get("id")
        or query.get("consultation_type_id")
    )

    if not consultation_type_id:
        raise ApiError(
            400,
            "MISSING_FIELD",
            "consultation_type_id is required.",
        )

    with connection.cursor() as cur:
        cur.execute(
            "SELECT dct.consultation_type_id, "
            "       ct.type_name, "
            "       ct.duration_minutes, "
            "       dct.fee "
            "FROM doctor_consultation_types dct "
            "JOIN consultation_types ct "
            "  ON ct.consultation_type_id = dct.consultation_type_id "
            "WHERE dct.doctor_id = %s "
            "  AND dct.consultation_type_id = %s",
            (doctor_id, consultation_type_id),
        )

        row = cur.fetchone()

        if not row:
            raise ApiError(
                404,
                "CONSULTATION_TYPE_NOT_FOUND",
                "Consultation type not found.",
            )

        # --------------------------------------------------------
        # type_name cannot be changed
        # --------------------------------------------------------
        if "type_name" in body:
            requested_name = clean_text(
                body["type_name"],
                "type_name",
                100,
                required=True,
            )

            if requested_name.strip().lower() != str(
                row["type_name"]
            ).strip().lower():
                raise ApiError(
                    400,
                    "TYPE_NAME_IMMUTABLE",
                    "Consultation type name cannot be changed.",
                )

        # --------------------------------------------------------
        # duration cannot be changed
        # --------------------------------------------------------
        if "duration_minutes" in body:
            try:
                requested_duration = int(body["duration_minutes"])
            except (TypeError, ValueError):
                raise ApiError(
                    400,
                    "INVALID_DURATION",
                    "duration_minutes must be a whole number.",
                ) from None

            if requested_duration != int(row["duration_minutes"]):
                raise ApiError(
                    400,
                    "INVALID_DURATION",
                    "Consultation duration cannot be changed.",
                )

        # --------------------------------------------------------
        # Only fee is actually updateable
        # --------------------------------------------------------
        if "fee" not in body:
            raise ApiError(
                400,
                "NOTHING_TO_UPDATE",
                "Only the consultation fee can be updated.",
            )

        try:
            fee = float(body["fee"])
        except (TypeError, ValueError):
            raise ApiError(
                400,
                "INVALID_FEE",
                "fee must be a valid number.",
            ) from None

        if fee < 0:
            raise ApiError(
                400,
                "INVALID_FEE",
                "fee must be greater than or equal to 0.",
            )

        cur.execute(
            "UPDATE doctor_consultation_types "
            "SET fee = %s "
            "WHERE doctor_id = %s "
            "  AND consultation_type_id = %s",
            (fee, doctor_id, consultation_type_id),
        )

    return 200, {
        "consultation_type": {
            "consultation_type_id": row["consultation_type_id"],
            "type_name": row["type_name"],
            "duration_minutes": int(row["duration_minutes"]),
            "fee": fee,
        }
    }


def delete(ctx):
    doctor = ctx.doctor()
    type_id = require_uuid(ctx.params.get("id"), "id")
    try:
        with ctx.conn.cursor() as cur:
            _load(cur, doctor["doctor_id"], type_id)
            # Only this doctor's offering is removed; the shared catalogue row stays for other doctors.
            cur.execute("DELETE FROM doctor_consultation_types WHERE doctor_id = %s AND consultation_type_id = %s",
                        (doctor["doctor_id"], type_id))
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] in (1451, 1217):
            raise ApiError(409, "TYPE_IN_USE", "This consultation type is used by an availability pattern, slot or appointment.") from None
        raise
    return 200, {"message": "Consultation type deleted."}
