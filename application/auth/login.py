"""MediSlot login Lambda handler."""

import logging

import bcrypt

from application.common.auth_utils import (
    body_json,
    response,
    db_connection,
    email_value,
    create_tokens,
)


log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Authenticate a user and issue access and refresh tokens."""

    conn = None

    try:
        log.info("Login request received")

        # -------------------------------------------------
        # 1. Parse the incoming request
        # -------------------------------------------------

        try:
            data = body_json(event)

        except ValueError:
            log.warning(
                "Login request rejected because its body is invalid"
            )

            return response(
                400,
                {"message": "Request body must be valid JSON."},
            )

        email = email_value(
            data.get("email")
        )

        password = data.get("password")

        requested_role = str(
            data.get("role") or ""
        ).strip().upper()

        if (
            not email
            or not isinstance(password, str)
            or not password
        ):
            return response(
                400,
                {"message": "email and password are required"},
            )

        if requested_role and requested_role not in {
            "PATIENT",
            "DOCTOR",
            "ADMIN",
        }:
            return response(
                400,
                {"message": "Invalid role"},
            )

        # -------------------------------------------------
        # 2. Connect to RDS
        # -------------------------------------------------

        log.info("Opening database connection")

        conn = db_connection()

        log.info(
            "Database connection returned to login handler"
        )

        # -------------------------------------------------
        # 3. Find the account in the users table
        # -------------------------------------------------

        log.info("Opening database cursor")

        with conn.cursor() as cur:
            log.info("Executing users table query")

            cur.execute(
                """
                SELECT
                    user_id,
                    email,
                    password_hash,
                    role,
                    is_active
                FROM users
                WHERE email = %s
                LIMIT 1
                """,
                (email,),
            )

            log.info("Users table query completed")

            user = cur.fetchone()

            log.info(
                "User lookup completed; account_found=%s",
                user is not None,
            )

        # Use the same authentication response for an unknown,
        # inactive, or incorrectly authenticated account.
        if not user or not user.get("is_active"):
            log.info(
                "Login rejected because the account is unavailable"
            )

            return response(
                401,
                {"message": "Invalid email or password"},
            )

        # -------------------------------------------------
        # 4. Validate the requested role
        # -------------------------------------------------

        actual_role = str(
            user.get("role") or ""
        ).upper()

        if requested_role and actual_role != requested_role:
            log.info(
                "Login rejected because the requested role did not match"
            )

            return response(
                401,
                {"message": "Invalid email or password"},
            )

        # -------------------------------------------------
        # 5. Verify the stored password hash
        # -------------------------------------------------

        log.info("Starting bcrypt password verification")

        stored_hash = user.get("password_hash")

        if isinstance(stored_hash, str):
            stored_hash = stored_hash.encode("utf-8")

        if not isinstance(stored_hash, bytes):
            log.error(
                "The account has an invalid password hash type"
            )

            return response(
                500,
                {"message": "Unable to authenticate account"},
            )

        try:
            password_matches = bcrypt.checkpw(
                password.encode("utf-8"),
                stored_hash,
            )

        except (ValueError, TypeError):
            log.exception(
                "Password hash verification failed"
            )

            password_matches = False

        log.info(
            "Bcrypt password verification completed; matched=%s",
            password_matches,
        )

        if not password_matches:
            return response(
                401,
                {"message": "Invalid email or password"},
            )

        # -------------------------------------------------
        # 6. Generate access and refresh tokens
        # -------------------------------------------------

        log.info(
            "Starting access and refresh token generation"
        )

        access_token, refresh_token, refresh_expires = (
            create_tokens(user)
        )

        log.info(
            "Access and refresh token generation completed"
        )

        # -------------------------------------------------
        # 7. Return successful login response
        # -------------------------------------------------

        log.info("Login completed successfully")

        return response(
            200,
            {
                "message": "Login successful",
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_token": refresh_token,
                "refresh_token_expires_at": refresh_expires,
                "user": {
                    "user_id": str(user["user_id"]),
                    "email": user["email"],
                    "role": actual_role,
                },
            },
        )

    except Exception:
        # Full traceback goes to CloudWatch, not the API response.
        log.exception("Login failed")

        return response(
            500,
            {"message": "Internal server error"},
        )

    finally:
        # Close the connection on success, failure, or early return.
        if conn is not None:
            try:
                conn.close()

                log.info(
                    "Database connection closed"
                )

            except Exception:
                log.exception(
                    "Failed to close database connection"
                )