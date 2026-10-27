```python
from application.common.http import response, request_method
from application.common.auth_context import claims
from application.database.connection import get_connection


def lambda_handler(event, context):
    # Allow only GET requests
    if request_method(event) != "GET":
        return response(
            405,
            {"error": "METHOD_NOT_ALLOWED"},
        )

    # Allow access only to administrators
    identity = claims(event)

    if identity["role"] != "admin":
        return response(
            403,
            {"error": "ADMIN_REQUIRED"},
        )

    query_params = event.get("queryStringParameters") or {}

    start_date = query_params.get("from")
    end_date = query_params.get("to")

    # Validate the date range
    if bool(start_date) != bool(end_date):
        return response(
            400,
            {
                "error": "DATE_RANGE_REQUIRED",
                "message": "Supply both from and to as YYYY-MM-DD.",
            },
        )

    # Apply the date filter when dates are provided
    date_condition = ""
    values = []

    if start_date:
        date_condition = (
            " WHERE starts_at_utc >= %s "
            "AND starts_at_utc < DATE_ADD(%s, INTERVAL 1 DAY)"
        )

        values = [start_date, end_date]

    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            # Count appointments by status
            cursor.execute(
                "SELECT status, COUNT(*) AS appointment_count "
                "FROM appointments"
                + date_condition
                + " GROUP BY status",
                tuple(values),
            )

            appointments_by_status = cursor.fetchall()

            # Calculate the total number of appointments
            cursor.execute(
                "SELECT COUNT(*) AS total_appointments "
                "FROM appointments"
                + date_condition,
                tuple(values),
            )

            total = cursor.fetchone()

            # Retrieve the top 10 doctors by appointment count
            cursor.execute(
                """
                SELECT
                    d.doctor_id,
                    d.display_name,
                    COUNT(*) AS appointment_count
                FROM appointments a
                JOIN doctors d
                    ON d.doctor_id = a.doctor_id
                """
                + date_condition
                + " GROUP BY d.doctor_id, d.display_name"
                + " ORDER BY appointment_count DESC LIMIT 10",
                tuple(values),
            )

            top_doctors = cursor.fetchall()

        return response(
            200,
            {
                "total_appointments": total["total_appointments"],
                "by_status": appointments_by_status,
                "top_doctors": top_doctors,
                "from": start_date,
                "to": end_date,
            },
        )

    except Exception:
        print("Reporting query failed.")

        return response(
            500,
            {"error": "INTERNAL_SERVER_ERROR"},
        )

    finally:
        connection.close()
```