
"""Read-only doctor, hospital and specialisation catalogue endpoints.
Uses the schema.sql columns in this MediSlot project.
Supports GET /doctors, /doctors/{doctor_id}, /hospitals, /specialisations.
"""

from application.common.http import response, request_method
from application.database.connection import get_connection


def _query(sql, params=(), one=False):
    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

            if one:
                return cursor.fetchone()

            return cursor.fetchall()

    finally:
        connection.close()


def _int(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(value)))

    except (TypeError, ValueError):
        return default


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method")
        or event.get("httpMethod", "GET")
    ).upper()

    if method != "GET":
        return response(
            405,
            {
                "error": "METHOD_NOT_ALLOWED",
                "message": "Only GET is supported.",
            },
        )

    path = (
        event.get("rawPath")
        or event.get("path")
        or "/"
    ).rstrip("/") or "/"

    query_params = event.get("queryStringParameters") or {}

    try:
        # Get hospitals
        if path.endswith("/hospitals"):
            limit = _int(query_params.get("limit"), 50, 1, 100)

            city = (query_params.get("city") or "").strip()
            search = (query_params.get("search") or "").strip()

            conditions = ["is_active = TRUE"]
            values = []

            if city:
                conditions.append("city LIKE %s")
                values.append(f"%{city}%")

            if search:
                conditions.append("(name LIKE %s OR city LIKE %s)")
                values.extend([
                    f"%{search}%",
                    f"%{search}%",
                ])

            values.append(limit)

            sql = (
                "SELECT hospital_id,name,address_line1,address_line2,"
                "city,state,postal_code,country,phone,email "
                "FROM hospitals WHERE "
                + " AND ".join(conditions)
                + " ORDER BY name LIMIT %s"
            )

            hospitals = _query(sql, tuple(values))

            return response(
                200,
                {
                    "items": hospitals,
                    "count": len(hospitals),
                },
            )

        # Get specialisations
        if (
            path.endswith("/specialisations")
            or path.endswith("/specializations")
        ):
            limit = _int(query_params.get("limit"), 100, 1, 200)

            sql = (
                "SELECT specialisation_id,name,description "
                "FROM specialisations "
                "WHERE is_active=TRUE "
                "ORDER BY name LIMIT %s"
            )

            specialisations = _query(sql, (limit,))

            return response(
                200,
                {
                    "items": specialisations,
                    "count": len(specialisations),
                },
            )

        # Get all doctors or a specific doctor
        marker = "/doctors"
        marker_index = path.rfind(marker)

        if marker_index < 0:
            return response(404, {"error": "ROUTE_NOT_FOUND"})

        doctor_path = path[marker_index + len(marker):].strip("/")

        base_sql = (
            "SELECT d.doctor_id,d.user_id,d.first_name,d.last_name,d.display_name,"
            "d.qualifications,d.years_experience,d.bio,d.phone,d.consultation_fee,"
            "h.hospital_id,h.name AS hospital_name,h.city AS hospital_city,"
            "s.specialisation_id,s.name AS specialisation "
            "FROM doctors d JOIN hospitals h ON h.hospital_id=d.hospital_id "
            "JOIN specialisations s ON s.specialisation_id=d.specialisation_id "
            "JOIN users u ON u.user_id=d.user_id "
            "WHERE d.is_active=TRUE AND h.is_active=TRUE "
            "AND s.is_active=TRUE AND u.is_active=TRUE"
        )

        # Get one doctor by ID
        if doctor_path:
            doctor_id = doctor_path.split("/")[0]

            doctor = _query(
                base_sql + " AND d.doctor_id=%s LIMIT 1",
                (doctor_id,),
                one=True,
            )

            if not doctor:
                return response(404, {"error": "DOCTOR_NOT_FOUND"})

            return response(200, {"doctor": doctor})

        # Apply filters when retrieving the doctor list
        conditions = []
        values = []

        search = (query_params.get("search") or "").strip()
        city = (query_params.get("city") or "").strip()
        hospital_id = (query_params.get("hospital_id") or "").strip()
        specialisation_id = (
            query_params.get("specialisation_id") or ""
        ).strip()

        if search:
            conditions.append(
                "(d.first_name LIKE %s OR d.last_name LIKE %s "
                "OR d.display_name LIKE %s)"
            )

            values.extend([f"%{search}%"] * 3)

        if city:
            conditions.append("h.city LIKE %s")
            values.append(f"%{city}%")

        if hospital_id:
            conditions.append("d.hospital_id=%s")
            values.append(hospital_id)

        if specialisation_id:
            conditions.append("d.specialisation_id=%s")
            values.append(specialisation_id)

        sql = base_sql

        if conditions:
            sql += " AND " + " AND ".join(conditions)

        limit = _int(query_params.get("limit"), 50, 1, 100)
        offset = _int(query_params.get("offset"), 0, 0, 100000)

        sql += " ORDER BY d.last_name,d.first_name LIMIT %s OFFSET %s"

        values.extend([limit, offset])

        doctors = _query(sql, tuple(values))

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
        print(
            "Doctor/catalogue query failed; "
            "inspect CloudWatch logs for the exception."
        )

        return response(
            500,
            {
                "error": "INTERNAL_SERVER_ERROR",
                "message": "Could not retrieve catalogue data.",
            },
        )
