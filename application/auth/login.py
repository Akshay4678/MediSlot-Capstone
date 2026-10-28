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
    conn = None

    try:
        log.info("Login request received")

        # Parse and validate the request.
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

        # Connect to the database.
        log.info("Opening database connection")
        conn = db_connection()

        # Find the account.
        log.info("Looking up login account")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, email, password_hash, role, is_active
                FROM users
                WHERE email = %s
                LIMIT 1
                """,
                (email,),
            )
            user = cur.fetchone()

        # Avoid revealing whether an account exists.
        if not user or not user.get("is_active"):
            return response(
                401,
                {"message": "Invalid email or password"},
            )

        if requested_role and user["role"] != requested_role:
            return response(
                401,
                {"message": "Invalid email or password"},
            )

        # Verify the password.
        log.info("Verifying password")

        stored_hash = user.get("password_hash")

        if not isinstance(stored_hash, (str, bytes)):
            log.error("Account has an invalid password hash")
            return response(
                500,
                {"message": "Unable to authenticate account"},
            )

        if isinstance(stored_hash, str):
            stored_hash = stored_hash.encode("utf-8")

        try:
            password_matches = bcrypt.checkpw(
                password.encode("utf-8"),
                stored_hash,
            )
        except (ValueError, TypeError):
            log.exception("Password hash verification failed")
            password_matches = False

        if not password_matches:
            return response(
                401,
                {"message": "Invalid email or password"},
            )

        # Generate access and refresh tokens.
        log.info("Password verified; generating tokens")

        access_token, refresh_token, refresh_expires = create_tokens(user)

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
                    "role": user["role"],
                },
            },
        )

    except Exception:
        log.exception("Login failed")
        return response(
            500,
            {"message": "Internal server error"},
        )

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                log.exception("Failed to close database connection")