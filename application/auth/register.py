import re
import uuid
import logging
import bcrypt
import pymysql
from application.common.auth_utils import body_json, response, db_connection, email_value, valid_password

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, context):
    conn = None
    try:
        data = body_json(event)
        email = email_value(data.get("email"))
        password = data.get("password")
        role = str(data.get("role") or "").strip().upper()

        if not email or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return response(400, {"message": "A valid email is required."})
        if role not in {"PATIENT", "DOCTOR"}:
            return response(400, {"message": "role must be PATIENT or DOCTOR."})
        if not valid_password(password):
            return response(400, {"message": "Password must be 10-72 characters and contain a letter and a digit."})

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
        user_id = str(uuid.uuid4())
        conn = db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (user_id, email, password_hash, role, is_active)
                   VALUES (%s, %s, %s, %s, TRUE)""",
                (user_id, email, password_hash, role),
            )
        conn.commit()
        return response(201, {
            "message": "Account registered. Complete required profile details before using role-specific features.",
            "user": {"user_id": user_id, "email": email, "role": role},
        })
    except ValueError as exc:
        return response(400, {"message": str(exc)})
    except pymysql.err.IntegrityError as exc:
        if conn:
            conn.rollback()
        if exc.args and exc.args[0] == 1062:
            return response(409, {"message": "An account with this email already exists."})
        log.exception("Registration integrity error")
        return response(500, {"message": "Could not create account."})
    except Exception:
        if conn:
            conn.rollback()
        log.exception("Registration failed")
        return response(500, {"message": "Internal server error."})
    finally:
        if conn:
            conn.close()
