
import uuid
from datetime import datetime

from application.common.http import (
    response,
    request_method,
    request_body,
)
from application.common.auth_context import claims
from application.database.connection import get_connection


def lambda_handler(event, context):

    # Accept only POST requests.
    if request_method(event) != "POST":
        return response(405, {"error": "METHOD_NOT_ALLOWED"})

    # Check whether the request belongs to an authenticated patient.
    identity = claims(event)

    if not identity["user_id"] or identity["role"] != "patient":
        return response(401, {
            "error": "UNAUTHORIZED",
            "message": "A patient access token is required.",
        })

    # Read the appointment details from the request body.
    try:
        body = request_body(event)

    except ValueError as error:
        return response(400, {
            "error": "INVALID_JSON",
            "message": str(error),
        })

    # Check that all required fields are provided.
    required_fields = (
        "doctor_id",
        "consultation_type_id",
        "slot_id",
        "starts_at_utc",
        "ends_at_utc",
        "timezone_name",
        "idempotency_key",
    )

    missing_fields = [
        field
        for field in required_fields
        if not body.get(field)
    ]

    if missing_fields:
        return response(400, {
            "error": "MISSING_FIELDS",
            "fields": missing_fields,
        })

    # Validate the appointment start and end times.
    try:
        start_time = datetime.fromisoformat(
            str(body["starts_at_utc"]).replace("Z", "+00:00")
        )

        end_time = datetime.fromisoformat(
            str(body["ends_at_utc"]).replace("Z", "+00:00")
        )

        if end_time <= start_time:
            raise ValueError()

    except ValueError:
        return response(400, {
            "error": "INVALID_TIME",
            "message": (
                "starts_at_utc and ends_at_utc must be valid "
                "ISO timestamps and end must be later."
            ),
        })

    # Connect to the RDS database.
    connection = get_connection()

    try:
        with connection.cursor() as cursor:

            # Find the patient profile associated with the logged-in user.
            cursor.execute(
                """
                SELECT patient_id
                FROM patients
                WHERE user_id = %s
                """,
                (identity["user_id"],),
            )

            patient = cursor.fetchone()

            if not patient:
                return response(
                    403,
                    {"error": "PATIENT_PROFILE_REQUIRED"},
                )

            # Check whether this booking request was already processed.
            cursor.execute(
                """
                SELECT appointment_id, status
                FROM appointments
                WHERE idempotency_key = %s
                """,
                (str(body["idempotency_key"]),),
            )

            previous_booking = cursor.fetchone()

            if previous_booking:
                connection.rollback()

                return response(200, {
                    "message": "Idempotent booking result",
                    "appointment": previous_booking,
                })

            # Validate the consultation type and its associated doctor.
            cursor.execute(
                """
                SELECT
                    ct.doctor_id,
                    ct.duration_minutes,
                    ct.capacity,
                    ct.is_active,
                    d.is_active AS doctor_active
                FROM consultation_types ct
                JOIN doctors d
                    ON d.doctor_id = ct.doctor_id
                WHERE ct.consultation_type_id = %s
                """,
                (str(body["consultation_type_id"]),),
            )

            consultation = cursor.fetchone()

            if (
                not consultation
                or not consultation["is_active"]
                or not consultation["doctor_active"]
                or str(consultation["doctor_id"]) != str(body["doctor_id"])
            ):
                connection.rollback()

                return response(
                    400,
                    {"error": "INVALID_CONSULTATION_TYPE"},
                )

            # IMPORTANT:
            # Slot capacity must be reserved through a DynamoDB
            # conditional write before inserting the appointment.
            # This reservation has not yet been connected in this code.

            appointment_id = str(uuid.uuid4())

            # Insert the appointment into RDS with PENDING status.
            cursor.execute(
                """
                INSERT INTO appointments (
                    appointment_id,
                    patient_id,
                    doctor_id,
                    consultation_type_id,
                    slot_id,
                    starts_at_utc,
                    ends_at_utc,
                    timezone_name,
                    status,
                    idempotency_key
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, 'PENDING', %s
                )
                """,
                (
                    appointment_id,
                    patient["patient_id"],
                    str(body["doctor_id"]),
                    str(body["consultation_type_id"]),
                    str(body["slot_id"]),
                    start_time.astimezone(
                        __import__("datetime").timezone.utc
                    ).replace(tzinfo=None),
                    end_time.astimezone(
                        __import__("datetime").timezone.utc
                    ).replace(tzinfo=None),
                    str(body["timezone_name"]),
                    str(body["idempotency_key"]),
                ),
            )

        # Save the appointment in the database.
        connection.commit()

        return response(201, {
            "appointment_id": appointment_id,
            "status": "PENDING",
            "message": (
                "Appointment created pending capacity/workflow confirmation."
            ),
        })

    except Exception:
        connection.rollback()

        print("Booking failed; inspect CloudWatch logs.")

        return response(
            500,
            {"error": "INTERNAL_SERVER_ERROR"},
        )

    finally:
        # Always close the database connection.
        connection.close()
