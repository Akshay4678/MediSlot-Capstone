"""MediSlot login Lambda handler."""

import logging
import time

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
    conn = None
    stage = "initialization"
    start_time = time.monotonic()

    try:
        log.info("LOGIN_DEBUG: Login request received")

        # 1. Parse and validate the request.
        stage = "request_validation"
        log.info("LOGIN_DEBUG: Parsing request body")

        data = body_json(event)
        email = email_value(data.get("email"))
        password = data.get("password")
        requested_role = str(data.get("role") or "").strip().upper()

        if not email or not isinstance(password, str) or not password:
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

        log.info("LOGIN_DEBUG: Request validation completed")

        # 2. Connect to RDS.
        stage = "database_connection"
        log.info("LOGIN_DEBUG: Opening database connection")

        conn = db_connection()

        log.info(
            "LOGIN_DEBUG: Database connection established in %.2f seconds",
            time.monotonic() - start_time,
        )

        # 3. Retrieve the user from the users table.
        stage = "user_lookup"
        query_start = time.monotonic()

        log.info("LOGIN_DEBUG: Executing user lookup query")

        with conn.cursor() as cur:
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

            user = cur.fetchone()

        log.info(
            "LOGIN_DEBUG: User lookup completed in %.2f seconds; "
            "user_found=%s",
            time.monotonic() - query_start,
            user is not None,
        )

        # Do not reveal whether the account exists or is inactive.
        if not user or not user.get("is_active"):
            log.warning("LOGIN_DEBUG: Login rejected during account validation")

            return response(
                401,
                {"message": "Invalid email or password"},
            )

        if requested_role and str(user["role"]).upper() != requested_role:
            log.warning("LOGIN_DEBUG: Requested role does not match account role")

            return response(
                401,
                {"message": "Invalid email or password"},
            )

        # 4. Verify the password hash.
        stage = "password_verification"

        stored_hash = user.get("password_hash")

        if isinstance(stored_hash, str):
            stored_hash = stored_hash.encode("utf-8")

        if not isinstance(stored_hash, bytes):
            log.error("LOGIN_DEBUG: Stored password hash is missing or invalid")

            return response(
                500,
                {"message": "Unable to authenticate account"},
            )

        log.info("LOGIN_DEBUG: Starting bcrypt password verification")

        password_start = time.monotonic()

        try:
            password_matches = bcrypt.checkpw(
                password.encode("utf-8"),
                stored_hash,
            )
        except (ValueError, TypeError):
            log.exception("LOGIN_DEBUG: Password hash verification failed")
            password_matches = False

        log.info(
            "LOGIN_DEBUG: Password verification finished in %.2f seconds",
            time.monotonic() - password_start,
        )

        if not password_matches:
            log.info("LOGIN_DEBUG: Login rejected due to invalid credentials")

            return response(
                401,
                {"message": "Invalid email or password"},
            )

        log.info("LOGIN_DEBUG: Password verified successfully")

        # 5. Generate access and refresh tokens.
        stage = "token_generation"

        log.info("LOGIN_DEBUG: Starting token generation")

        token_start = time.monotonic()

        access_token, refresh_token, refresh_expires = create_tokens(user)

        log.info(
            "LOGIN_DEBUG: Token generation completed in %.2f seconds",
            time.monotonic() - token_start,
        )

        # 6. Return the successful login response.
        stage = "response"

        result = response(
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
                    "role": str(user["role"]).upper(),
                },
            },
        )

        log.info(
            "LOGIN_DEBUG: Login completed successfully in %.2f seconds",
            time.monotonic() - start_time,
        )

        return result

    except Exception:
        # The full traceback is recorded in CloudWatch for diagnosis.
        log.exception(
            "LOGIN_ERROR: Login failed at stage '%s'",
            stage,
        )

        return response(
            500,
            {"message": "Internal server error"},
        )

    finally:
        if conn is not None:
            try:
                conn.close()
                log.info("LOGIN_DEBUG: Database connection closed")
            except Exception:
                log.exception("LOGIN_ERROR: Failed to close database connection")