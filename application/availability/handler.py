from datetime import datetime, timezone

from application.common.http import response, request_method
from application.database.connection import get_connection


def lambda_handler(event, context):

    # Allow only GET requests.
    if request_method(event) != "GET":
        return response(405, {
            "error": "METHOD_NOT_ALLOWED"
        })

    # Read the query parameters.
    query_params = event.get("queryStringParameters") or {}

    doctor_id = (query_params.get("doctor_id") or "").strip()
    date = (query_params.get("date") or "").strip()

    # Both doctor_id and date are required.
    if not doctor_id or not date:
        return response(400, {
            "error": "MISSING_PARAMETERS",
            "message": "doctor_id and date (YYYY-MM-DD) are required."
        })

    # Validate the date format.
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()

    except ValueError:
        return response(400, {
            "error": "INVALID_DATE",
            "message": "date must use YYYY-MM-DD."
        })

    # Connect to the database.
    connection = get_connection()

    try:
        with connection.cursor() as cursor:

            # Get the doctor's active availability patterns for the requested date.
            cursor.execute(
                """
                SELECT
                    ap.availability_pattern_id,
                    ap.consultation_type_id,
                    ap.weekday,
                    ap.start_local_time,
                    ap.end_local_time,
                    ap.timezone_name,
                    ap.effective_from,
                    ap.effective_until,
                    ct.name AS consultation_type,
                    ct.duration_minutes,
                    ct.fee,
                    ct.capacity
                FROM availability_patterns ap
                JOIN consultation_types ct
                    ON ct.consultation_type_id = ap.consultation_type_id
                WHERE ap.doctor_id = %s
                    AND ap.active = TRUE
                    AND ct.is_active = TRUE
                    AND ap.weekday = %s
                    AND ap.effective_from <= %s
                    AND (
                        ap.effective_until IS NULL
                        OR ap.effective_until >= %s
                    )
                ORDER BY ap.start_local_time
                """,
                (
                    doctor_id,
                    target_date.isoweekday(),
                    target_date,
                    target_date
                )
            )

            availability_patterns = cursor.fetchall()

            # Get any blackout periods for the doctor on this date.
            cursor.execute(
                """
                SELECT
                    start_local_time,
                    end_local_time,
                    reason
                FROM blackout_dates
                WHERE doctor_id = %s
                    AND local_date = %s
                """,
                (doctor_id, target_date)
            )

            blackout_windows = cursor.fetchall()

            # Get existing pending or confirmed appointments for this doctor.
            cursor.execute(
                """
                SELECT
                    slot_id,
                    starts_at_utc,
                    ends_at_utc,
                    status
                FROM appointments
                WHERE doctor_id = %s
                    AND DATE(starts_at_utc) = %s
                    AND status IN ('PENDING', 'CONFIRMED')
                """,
                (doctor_id, target_date)
            )

            booked_appointments = cursor.fetchall()

        # Return the schedule, blackout periods, and existing appointments.
        # These schedule windows do not guarantee that slots are available.
        return response(200, {
            "doctor_id": doctor_id,
            "date": date,
            "timezone_note": (
                "Times in patterns are local to each pattern's timezone."
            ),
            "schedule_windows": availability_patterns,
            "blackout_windows": blackout_windows,
            "appointments": booked_appointments,
            "availability_source": (
                "schedule plus RDS appointments; verify DynamoDB slot "
                "capacity before booking"
            )
        })

    except Exception:
        print("Availability query failed; inspect CloudWatch logs.")

        return response(500, {
            "error": "INTERNAL_SERVER_ERROR"
        })

    finally:
        # Always close the database connection.
        connection.close()
