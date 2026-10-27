```python
import uuid
from datetime import datetime, timezone

from application.common.http import (
    response,
    request_method,
    request_body,
)
from application.common.auth_context import claims
from application.database.connection import get_connection


def lambda_handler(event, context):
    # Allow only POST requests
    if request_method(event) != "POST":
        return response(
            405,
            {"error": "METHOD_NOT_ALLOWED"},
        )

    # Validate the user's identity and role
    identity = claims(event)

    if (
        not identity["user_id"]
        or identity["role"] not in ("patient", "admin")
    ):
        return response(
            401,
            {"error": "UNAUTHORIZED"},
        )

    # Read the request body
    try:
        body = request_body(event)

    except ValueError as error:
        return response(
            400,
            {
                "error": "INVALID_JSON",
                "message": str(error),
            },
        )

    # Get the appointment ID from the path or request body
    path_params = event.get("pathParameters") or {}

    appointment_id = (
        path_params.get("appointment_id")
        or body.get("appointment_id")
    )

    required_fields = (
        "new_slot_id",
        "starts_at_utc",
        "ends_at_utc",
        "timezone_name",
    )

    missing_fields = [
        field
        for field in required_fields
        if not body.get(field)
    ]

    if not appointment_id or missing_fields:
        return response(
            400,
            {
                "error": "MISSING_FIELDS",
                "fields": (
                    ["appointment_id"] if not appointment_id else []
                ) + missing_fields,
            },
        )

    # Validate the appointment start and end times
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
        return response(
            400,
            {"error": "INVALID_TIME"},
        )

    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            # Lock the appointment and retrieve its details
            if identity["role"] == "patient":
                cursor.execute(
                    """
                    SELECT a.*, p.user_id
                    FROM appointments a
                    JOIN patients p
                        ON p.patient_id = a.patient_id
                    WHERE a.appointment_id = %s
                    FOR UPDATE
                    """,
                    (appointment_id,),
                )

            else:
                cursor.execute(
                    """
                    SELECT *
                    FROM appointments
                    WHERE appointment_id = %s
                    FOR UPDATE
                    """,
                    (appointment_id,),
                )

            appointment = cursor.fetchone()

            # Verify that the appointment exists and belongs to the patient
            if (
                not appointment
                or (
                    identity["role"] == "patient"
                    and str(appointment["user_id"]) != identity["user_id"]
                )
            ):
                connection.rollback()

                return response(
                    404,
                    {"error": "APPOINTMENT_NOT_FOUND"},
                )

            # Check whether the appointment can be rescheduled
            if (
                appointment["status"] not in ("PENDING", "CONFIRMED")
                or int(appointment["reschedule_count"]) >= 2
            ):
                connection.rollback()

                return response(
                    409,
                    {"error": "NOT_RESCHEDULABLE"},
                )

            # Capacity reservation and compensation are not configured yet.
            # Rescheduling remains disabled until the DynamoDB workflow is connected.
            connection.rollback()

            return response(
                501,
                {
                    "error": "CAPACITY_WORKFLOW_NOT_CONFIGURED",
                    "message": (
                        "Connect the DynamoDB conditional capacity "
                        "reservation and compensation workflow before "
                        "enabling rescheduling."
                    ),
                },
            )

    except Exception:
        connection.rollback()

        print(
            "Reschedule lookup failed; "
            "inspect CloudWatch logs."
        )

        return response(
            500,
            {"error": "INTERNAL_SERVER_ERROR"},
        )

    finally:
        connection.close()
```