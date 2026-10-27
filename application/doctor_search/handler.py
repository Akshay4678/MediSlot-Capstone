
from application.common.http import response, request_method
from application.database.connection import get_connection


def lambda_handler(event, context):
    # Allow only GET requests
    if request_method(event) != "GET":
        return response(405, {"error": "METHOD_NOT_ALLOWED"})

    query_params = event.get("queryStringParameters") or {}

    search = (query_params.get("search") or "").strip()
    city = (query_params.get("city") or "").strip()
    specialisation = (query_params.get("specialisation") or "").strip()

    # Read pagination parameters
    try:
        limit = max(1, min(100, int(query_params.get("limit", 50))))
    except ValueError:
        limit = 50

    try:
        offset = max(0, min(100000, int(query_params.get("offset", 0))))
    except ValueError:
        offset = 0

    # Build the SQL filters
    conditions = [
        "d.is_active=TRUE",
        "h.is_active=TRUE",
        "s.is_active=TRUE",
        "u.is_active=TRUE",
    ]

    values = []

    if search:
        conditions.append(
            "(d.first_name LIKE %s OR d.last_name LIKE %s "
            "OR d.display_name LIKE %s OR h.name LIKE %s)"
        )
        values.extend([f"%{search}%"] * 4)

    if city:
        conditions.append("h.city LIKE %s")
        values.append(f"%{city}%")

    if specialisation:
        conditions.append("s.name LIKE %s")
        values.append(f"%{specialisation}%")

    # Prepare the doctor search query
    sql = (
        """
        SELECT
            d.doctor_id,
            d.first_name,
            d.last_name,
            d.display_name,
            d.qualifications,
            d.years_experience,
            d.bio,
            d.consultation_fee,
            h.hospital_id,
            h.name AS hospital_name,
            h.city,
            s.specialisation_id,
            s.name AS specialisation
        FROM doctors d
        JOIN hospitals h
            ON h.hospital_id = d.hospital_id
        JOIN specialisations s
            ON s.specialisation_id = d.specialisation_id
        JOIN users u
            ON u.user_id = d.user_id
        WHERE
        """
        + " AND ".join(conditions)
        + " ORDER BY d.last_name,d.first_name LIMIT %s OFFSET %s"
    )

    # Add pagination values to the query parameters
    values.extend([limit, offset])

    connection = None

    try:
        connection = get_connection()

        with connection.cursor() as cursor:
            cursor.execute(sql, tuple(values))
            doctors = cursor.fetchall()

        return response(
            200,
            {
                "items": doctors,
                "count": len(doctors),
                "limit": limit,
                "offset": offset,
            },
        )

    except Exception:
        print("Doctor search failed; inspect CloudWatch logs.")

        return response(
            500,
            {"error": "INTERNAL_SERVER_ERROR"},
        )

    finally:
        if connection:
            connection.close()
