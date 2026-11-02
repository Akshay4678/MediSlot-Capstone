
import uuid
import os
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

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

    # Idempotency records remain in the ERD-defined DynamoDB table.
    idempotency_table_name = os.environ.get("IDEMPOTENCY_TABLE_NAME")
    if not idempotency_table_name:
        return response(500, {"error": "SERVER_CONFIGURATION_ERROR"})

    idempotency_table = boto3.resource("dynamodb").Table(idempotency_table_name)
    idempotency_reserved = False
    try:
        idempotency_table.put_item(
            Item={
                "idempotency_key": str(body["idempotency_key"]),
                "action": "BOOK_APPOINTMENT",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": int(time.time()) + 3600,
                "status": "PROCESSING",
            },
            ConditionExpression=Attr("idempotency_key").not_exists(),
        )
        idempotency_reserved = True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            previous = idempotency_table.get_item(
                Key={"idempotency_key": str(body["idempotency_key"])}
            ).get("Item")
            if previous and previous.get("appointment_id"):
                return response(200, {
                    "message": "Idempotent booking result",
                    "appointment_id": int(previous["appointment_id"]),
                    "status": previous.get("status", "PENDING"),
                })
            return response(409, {"error": "IDEMPOTENT_REQUEST_IN_PROGRESS"})
        raise

    def release_idempotency_reservation():
        """Release a PROCESSING key when no appointment was committed."""
        if idempotency_reserved:
            try:
                idempotency_table.delete_item(
                    Key={"idempotency_key": str(body["idempotency_key"])},
                    ConditionExpression=Attr("status").eq("PROCESSING"),
                )
            except Exception:
                print("Could not release the idempotency reservation; inspect CloudWatch logs.")

    # Connect to the RDS database.
    connection = None
    database_committed = False

    try:
        connection = get_connection()
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
                connection.rollback()
                release_idempotency_reservation()
                return response(
                    403,
                    {"error": "PATIENT_PROFILE_REQUIRED"},
                )

            # Idempotency is checked/reserved in DynamoDB above; it is not an
            # appointments column in the approved relational ERD.

            # Validate the shared consultation type against the doctor's active pattern.
            cursor.execute(
                """
                SELECT d.is_active AS doctor_active
                FROM doctors d
                JOIN availability_patterns ap
                    ON ap.doctor_id = d.doctor_id
                WHERE d.doctor_id = %s
                  AND ap.consultation_type_id = %s
                  AND ap.is_active = TRUE
                LIMIT 1
                """,
                (int(body["doctor_id"]), int(body["consultation_type_id"])),
            )

            consultation = cursor.fetchone()

            if not consultation or not consultation["doctor_active"]:
                connection.rollback()
                return response(400, {"error": "INVALID_CONSULTATION_TYPE"})

            # Reserve slot capacity in RDS. FOR UPDATE serializes concurrent
            # bookings for this slot and prevents overbooking.
            cursor.execute(
                """
                SELECT slot_id, doctor_id, consultation_type_id, capacity,
                       booked_count, status
                FROM materialised_slots
                WHERE slot_id = %s
                FOR UPDATE
                """,
                (str(body["slot_id"]),),
            )
            slot = cursor.fetchone()

            if not slot:
                connection.rollback()
                return response(404, {"error": "SLOT_NOT_FOUND"})

            if (
                int(slot["doctor_id"]) != int(body["doctor_id"])
                or int(slot["consultation_type_id"]) != int(body["consultation_type_id"])
            ):
                connection.rollback()
                return response(400, {"error": "SLOT_DETAILS_MISMATCH"})

            if slot["status"] == "UNAVAILABLE" or int(slot["booked_count"]) >= int(slot["capacity"]):
                connection.rollback()
                return response(409, {"error": "SLOT_FULL_OR_UNAVAILABLE"})

            cursor.execute(
                """
                UPDATE materialised_slots
                SET booked_count = booked_count + 1
                WHERE slot_id = %s AND booked_count < capacity
                  AND status <> 'UNAVAILABLE'
                """,
                (str(body["slot_id"]),),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return response(409, {"error": "SLOT_CAPACITY_EXHAUSTED"})

            cursor.execute(
                """UPDATE materialised_slots
                   SET status = CASE WHEN booked_count >= capacity THEN 'FULL' ELSE 'AVAILABLE' END
                   WHERE slot_id = %s""",
                (str(body["slot_id"]),),
            )

            # Insert the appointment using the BIGINT AUTO_INCREMENT key
            # and columns defined by application/database/schema.sql.
            cursor.execute(
                """
                INSERT INTO appointments (
                    patient_id,
                    doctor_id,
                    consultation_type_id,
                    slot_id,
                    status,
                    notes
                )
                VALUES (%s, %s, %s, %s, 'PENDING', %s)
                """,
                (
                    patient["patient_id"],
                    int(body["doctor_id"]),
                    int(body["consultation_type_id"]),
                    str(body["slot_id"]),
                    body.get("notes"),
                ),
            )
            appointment_id = int(cursor.lastrowid)

        # Save the appointment in the database.
        connection.commit()
        database_committed = True

        idempotency_table.update_item(
            Key={"idempotency_key": str(body["idempotency_key"])},
            UpdateExpression="SET appointment_id = :appointment_id, #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":appointment_id": appointment_id,
                ":status": "PENDING",
            },
        )

        return response(201, {
            "appointment_id": appointment_id,
            "status": "PENDING",
            "message": (
                "Appointment created pending capacity/workflow confirmation."
            ),
        })

    except Exception:
        if connection:
            connection.rollback()
        if not database_committed:
            release_idempotency_reservation()

        print("Booking failed; inspect CloudWatch logs.")

        return response(
            500,
            {"error": "INTERNAL_SERVER_ERROR"},
        )

    finally:
        # Always close the database connection.
        if connection:
            connection.close()
