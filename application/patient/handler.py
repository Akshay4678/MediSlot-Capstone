```python
from application.common.http import (
    response,
    request_method,
    request_body,
)
from application.common.auth_context import claims
from application.database.connection import get_connection


def lambda_handler(event, context):
    identity = claims(event)

    if not identity["user_id"]:
        return response(401, {"error": "UNAUTHORIZED"})

    method = request_method(event)

    # Retrieve the patient's profile
    if method == "GET":
        connection = get_connection()

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        p.patient_id,
                        p.first_name,
                        p.last_name,
                        p.date_of_birth,
                        p.phone,
                        p.gender,
                        u.email
                    FROM patients p
                    JOIN users u
                        ON u.user_id = p.user_id
                    WHERE p.user_id = %s
                    """,
                    (identity["user_id"],),
                )

                patient = cursor.fetchone()

            if patient:
                return response(200, {"patient": patient})

            return response(404, {"error": "PATIENT_NOT_FOUND"})

        except Exception:
            print("Patient profile query failed.")

            return response(
                500,
                {"error": "INTERNAL_SERVER_ERROR"},
            )

        finally:
            connection.close()

    # Update the patient's profile
    if method in ("PATCH", "PUT"):
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

        allowed_fields = {
            "first_name",
            "last_name",
            "phone",
            "gender",
            "date_of_birth",
        }

        if not body or set(body) - allowed_fields:
            return response(
                400,
                {
                    "error": "INVALID_FIELDS",
                    "allowed_fields": sorted(allowed_fields),
                },
            )

        # Build the UPDATE query using the supplied fields
        update_fields = []
        values = []

        for field, value in body.items():
            update_fields.append(f"{field}=%s")
            values.append(value)

        values.append(identity["user_id"])

        connection = get_connection()

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE patients SET "
                    + ", ".join(update_fields)
                    + " WHERE user_id=%s",
                    tuple(values),
                )

                if not cursor.rowcount:
                    connection.rollback()

                    return response(
                        404,
                        {"error": "PATIENT_NOT_FOUND"},
                    )

            connection.commit()

            return response(
                200,
                {"message": "Patient profile updated."},
            )

        except Exception:
            connection.rollback()

            return response(
                500,
                {"error": "PROFILE_UPDATE_FAILED"},
            )

        finally:
            connection.close()

    return response(
        405,
        {"error": "METHOD_NOT_ALLOWED"},
    )
```