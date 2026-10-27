import logging
import bcrypt
from application.common.auth_utils import body_json, response, db_connection, email_value, create_tokens

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, context):
    conn = None
    try:
        data = body_json(event)
        email = email_value(data.get("email"))
        password = data.get("password")
        requested_role = str(data.get("role") or "").strip().upper()

        if not email or not isinstance(password, str) or not password:
            return response(400, {"message": "email and password are required."})
        if requested_role and requested_role not in {"PATIENT", "DOCTOR", "ADMIN"}:
            return response(400, {"message": "Invalid role."})

        conn = db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT user_id, email, password_hash, role, is_active
                   FROM users WHERE email = %s LIMIT 1""",
                (email,),
            )
            user = cur.fetchone()

        # Same response for unknown email and incorrect password.
        if not user or not user.get("is_active") or not bcrypt.checkpw(
            password.encode("utf-8"), user["password_hash"].encode("utf-8")
        ):
            return response(401, {"message": "Invalid email or password."})
        if requested_role and user["role"].upper() != requested_role:
            return response(401, {"message": "Invalid email or password."})

        access_token, refresh_token, refresh_expires = create_tokens(user)
        return response(200, {
            "message": "Login successful.",
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 900,
            "refresh_token": refresh_token,
            "refresh_token_expires_at": refresh_expires,
            "user": {"user_id": user["user_id"], "email": user["email"], "role": user["role"]},
        })
    except ValueError as exc:
        return response(400, {"message": str(exc)})
    except Exception:
        log.exception("Login failed")
        return response(500, {"message": "Internal server error."})
    finally:
        if conn:
            conn.close()
