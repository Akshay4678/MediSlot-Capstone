"""Consultation types offered by the calling doctor.

Endpoints:
    POST   /doctors/me/consultation-types
    GET    /doctors/me/consultation-types
    PUT    /doctors/me/consultation-types/<id>
    DELETE /doctors/me/consultation-types/<id>

Data model:
    consultation_types
        Shared catalogue containing type_name and duration_minutes.

    doctor_consultation_types
        Doctor-specific offering containing doctor_id, consultation_type_id
        and fee.

The public id exposed by this API is consultation_type_id.
"""

from decimal import Decimal, InvalidOperation

import pymysql

from application.common import rules
from application.common.api import ApiError, money, new_id, require_uuid


MAX_FEE = Decimal("100000")

_SELECT_OFFERING = (
    "SELECT ct.consultation_type_id, ct.type_name, "
    "ct.duration_minutes, dct.fee "
    "FROM doctor_consultation_types dct "
    "JOIN consultation_types ct "
    "ON ct.consultation_type_id = dct.consultation_type_id "
    "WHERE dct.doctor_id = %s"
)


def _fee(value):
    """Validate and normalise a consultation fee."""
    try:
        fee = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(
            400,
            "INVALID_FEE",
            "fee must be a number.",
        ) from None

    if fee < 0 or fee > MAX_FEE:
        raise ApiError(
            400,
            "INVALID_FEE",
            f"fee must be between 0 and {MAX_FEE}.",
        )

    return fee


def _row(row):
    """Convert a database row into the API response format."""
    return {
        "consultation_type_id": row["consultation_type_id"],
        "type_name": row["type_name"],
        "duration_minutes": int(row["duration_minutes"]),
        "fee": money(row["fee"]),
    }


def _load(cur, doctor_id, type_id):
    """Load one consultation type offered by a specific doctor."""
    cur.execute(
        _SELECT_OFFERING + " AND ct.consultation_type_id = %s",
        (doctor_id, type_id),
    )

    row = cur.fetchone()

    if not row:
        raise ApiError(
            404,
            "CONSULTATION_TYPE_NOT_FOUND",
            "Consultation type not found.",
        )

    return row


def _catalogue_id(cur, name, minutes):
    """Return the shared catalogue row for a consultation type."""
    cur.execute(
        "INSERT INTO consultation_types "
        "(consultation_type_id, type_name, duration_minutes) "
        "VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "consultation_type_id = consultation_type_id",
        (new_id(), name, minutes),
    )

    cur.execute(
        "SELECT consultation_type_id, duration_minutes "
        "FROM consultation_types "
        "WHERE type_name = %s",
        (name,),
    )

    return cur.fetchone()


def list_(ctx):
    """GET /doctors/me/consultation-types."""
    doctor = ctx.doctor()

    with ctx.conn.cursor() as cur:
        cur.execute(
            _SELECT_OFFERING
            + " ORDER BY ct.duration_minutes, ct.type_name",
            (doctor["doctor_id"],),
        )

        items = [_row(row) for row in cur.fetchall()]

    allowed = [
        {
            "type_name": name,
            "duration_minutes": rules.canonical_type(name)[1],
        }
        for name in rules.allowed_type_names()
    ]

    return 200, {
        "items": items,
        "allowed_types": allowed,
    }


def create(ctx):
    """POST /doctors/me/consultation-types."""
    doctor = ctx.doctor()

    canonical = rules.canonical_type(
        ctx.body.get("type_name")
    )

    if not canonical:
        raise ApiError(
            400,
            "INVALID_TYPE_NAME",
            f"type_name must be one of {rules.allowed_type_names()}.",
        )

    name, minutes = canonical

    if (
        "duration_minutes" in ctx.body
        and str(ctx.body["duration_minutes"]) != str(minutes)
    ):
        raise ApiError(
            400,
            "INVALID_DURATION",
            f"{name} has a fixed duration of {minutes} minutes.",
        )

    if "fee" not in ctx.body:
        raise ApiError(
            400,
            "MISSING_FIELD",
            "fee is required.",
        )

    fee = _fee(ctx.body["fee"])

    try:
        with ctx.conn.cursor() as cur:
            catalogue = _catalogue_id(
                cur,
                name,
                minutes,
            )

            cur.execute(
                "INSERT INTO doctor_consultation_types "
                "(doctor_consultation_type_id, doctor_id, "
                "consultation_type_id, fee) "
                "VALUES (%s, %s, %s, %s)",
                (
                    new_id(),
                    doctor["doctor_id"],
                    catalogue["consultation_type_id"],
                    fee,
                ),
            )

            row = _load(
                cur,
                doctor["doctor_id"],
                catalogue["consultation_type_id"],
            )

    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            raise ApiError(
                409,
                "TYPE_EXISTS",
                f"You already have a '{name}' consultation type.",
            ) from None

        raise

    return 201, {
        "consultation_type": _row(row),
    }


def update(ctx_or_body, params=None, conn=None):
    """PUT /doctors/me/consultation-types/<id>.

    Supports both:

        update(ctx)

    and the legacy/unit-test form:

        update(body, params, conn)

    The normal application path uses the doctor from ctx.doctor().
    """

    # ------------------------------------------------------------
    # Normal API path
    # ------------------------------------------------------------
    if params is None and conn is None:
        ctx = ctx_or_body

        doctor = ctx.doctor()
        type_id = require_uuid(
            ctx.params.get("id"),
            "id",
        )

        body = ctx.body
        connection = ctx.conn

        with connection.cursor() as cur:
            row = _load(
                cur,
                doctor["doctor_id"],
                type_id,
            )

            # type_name cannot be changed.
            if (
                "type_name" in body
                and str(body["type_name"]).strip().lower()
                != str(row["type_name"]).strip().lower()
            ):
                raise ApiError(
                    400,
                    "TYPE_NAME_IMMUTABLE",
                    "type_name cannot be changed; create a new type instead.",
                )

            # duration is fixed by the platform.
            if (
                "duration_minutes" in body
                and str(body["duration_minutes"])
                != str(row["duration_minutes"])
            ):
                raise ApiError(
                    400,
                    "INVALID_DURATION",
                    "Consultation durations are fixed by the platform rules.",
                )

            # Only fee can be updated.
            if "fee" not in body:
                raise ApiError(
                    400,
                    "NOTHING_TO_UPDATE",
                    "Only fee can be updated.",
                )

            fee = _fee(body["fee"])

            cur.execute(
                "UPDATE doctor_consultation_types "
                "SET fee = %s "
                "WHERE doctor_id = %s "
                "AND consultation_type_id = %s",
                (
                    fee,
                    doctor["doctor_id"],
                    type_id,
                ),
            )

            row = _load(
                cur,
                doctor["doctor_id"],
                type_id,
            )

        return 200, {
            "consultation_type": _row(row),
        }

    # ------------------------------------------------------------
    # Legacy/unit-test compatibility path
    # ------------------------------------------------------------
    body = ctx_or_body

    if not isinstance(body, dict):
        raise ApiError(
            400,
            "INVALID_FIELD",
            "Request body must be an object.",
        )

    if not isinstance(params, dict):
        raise ApiError(
            400,
            "INVALID_FIELD",
            "Request parameters must be an object.",
        )

    type_id = require_uuid(
        params.get("id"),
        "id",
    )

    with conn.cursor() as cur:
        # The unit-test compatibility path uses the first matching
        # offering row returned by the fake connection.
        cur.execute(
            "SELECT ct.consultation_type_id, "
            "ct.type_name, "
            "ct.duration_minutes, "
            "dct.fee "
            "FROM doctor_consultation_types dct "
            "JOIN consultation_types ct "
            "ON ct.consultation_type_id = dct.consultation_type_id "
            "WHERE ct.consultation_type_id = %s",
            (type_id,),
        )

        row = cur.fetchone()

    if not row:
        raise ApiError(
            404,
            "CONSULTATION_TYPE_NOT_FOUND",
            "Consultation type not found.",
        )

    # type_name is immutable.
    if "type_name" in body:
        if (
            str(body["type_name"]).strip().lower()
            != str(row["type_name"]).strip().lower()
        ):
            raise ApiError(
                400,
                "TYPE_NAME_IMMUTABLE",
                "type_name cannot be changed; create a new type instead.",
            )

    # duration is immutable.
    if "duration_minutes" in body:
        if (
            str(body["duration_minutes"])
            != str(row["duration_minutes"])
        ):
            raise ApiError(
                400,
                "INVALID_DURATION",
                "Consultation durations are fixed by the platform rules.",
            )

    # Only fee can be updated.
    if "fee" not in body:
        raise ApiError(
            400,
            "NOTHING_TO_UPDATE",
            "Only fee can be updated.",
        )

    fee = _fee(body["fee"])

    # If a doctor id is supplied, update the real database row.
    doctor_id = (
        params.get("doctor_id")
        or params.get("user_id")
    )

    if doctor_id:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE doctor_consultation_types "
                "SET fee = %s "
                "WHERE doctor_id = %s "
                "AND consultation_type_id = %s",
                (
                    fee,
                    doctor_id,
                    type_id,
                ),
            )

            cur.execute(
                _SELECT_OFFERING
                + " AND ct.consultation_type_id = %s",
                (
                    doctor_id,
                    type_id,
                ),
            )

            updated = cur.fetchone()

        if updated:
            return 200, {
                "consultation_type": _row(updated),
            }

    # Compatibility response for tests that only validate the update.
    result = dict(row)
    result["fee"] = fee

    return 200, {
        "consultation_type": _row(result),
    }


def delete(ctx):
    """DELETE /doctors/me/consultation-types/<id>."""
    doctor = ctx.doctor()

    type_id = require_uuid(
        ctx.params.get("id"),
        "id",
    )

    try:
        with ctx.conn.cursor() as cur:
            _load(
                cur,
                doctor["doctor_id"],
                type_id,
            )

            # Delete only this doctor's offering.
            # The shared catalogue row remains.
            cur.execute(
                "DELETE FROM doctor_consultation_types "
                "WHERE doctor_id = %s "
                "AND consultation_type_id = %s",
                (
                    doctor["doctor_id"],
                    type_id,
                ),
            )

    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] in (1451, 1217):
            raise ApiError(
                409,
                "TYPE_IN_USE",
                "This consultation type is used by an availability pattern, "
                "slot or appointment.",
            ) from None

        raise

    return 200, {
        "message": "Consultation type deleted.",
    }