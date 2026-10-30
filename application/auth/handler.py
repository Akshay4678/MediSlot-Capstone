"""MediSlot authentication routes: register, login, refresh, logout."""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid

from functools import lru_cache

import boto3
import bcrypt
from botocore.config import Config
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

from application.common.http import (
    response,
    request_method,
    request_path,
    request_body,
)
from application.authorizer.lambda_handler import verify_access_token
from application.database.connection import get_connection


log = logging.getLogger()
log.setLevel(logging.INFO)

# Fail fast (seconds, not the Lambda timeout) if a VPC endpoint/route is missing,
# so the real error shows up in CloudWatch instead of an opaque 500 timeout.
_AWS_CFG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2})
ssm = boto3.client("ssm", config=_AWS_CFG)
ddb = boto3.resource("dynamodb", config=_AWS_CFG)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+[.][^@\s]+$")

ACCESS_TTL = 900
REFRESH_TTL = 7 * 24 * 60 * 60


@lru_cache(maxsize=1)
def _secret():
    """Retrieve the JWT signing secret from SSM Parameter Store (cached per cold start)."""
    parameter_name = os.environ["JWT_SECRET_PARAMETER_NAME"]

    parameter = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    return parameter["Parameter"]["Value"].encode()


def _b64(data):
    """Convert bytes to URL-safe Base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(payload):
    """Create a signed JWT using the HS256 algorithm."""
    header = _b64(
        json.dumps(
            {"alg": "HS256", "typ": "JWT"},
            separators=(",", ":"),
        ).encode()
    )

    body = _b64(
        json.dumps(
            payload,
            separators=(",", ":"),
            default=str,
        ).encode()
    )

    signing_data = f"{header}.{body}".encode()

    signature = _b64(
        hmac.new(
            _secret(),
            signing_data,
            hashlib.sha256,
        ).digest()
    )

    return f"{header}.{body}.{signature}"


def _issue_access(user, session_id):
    """Generate an access token bound to a refresh-token session.

    The session ID is the SHA-256 hash of the opaque refresh token. It lets the
    active list associate access tokens with a session without storing a JTI
    or TTL attribute in REFRESH_TOKENS.
    """
    current_time = int(time.time())
    jti = str(uuid.uuid4())
    expires_at = current_time + ACCESS_TTL

    token = _jwt({
        "sub": str(user["user_id"]),
        "role": str(user["role"]).lower(),
        "iat": current_time,
        "exp": expires_at,
        "jti": jti,
        "sid": str(session_id),
        "iss": os.environ.get("JWT_ISSUER", "medislot"),
    })

    return token, jti, expires_at


def _activate_jti(jti, user_id, expires_at, session_id):
    """Register an access token and associate it with its login session."""
    if not jti or not expires_at:
        return

    expires_at = int(expires_at)

    if expires_at <= int(time.time()):
        return

    _table("TOKEN_ACTIVELIST_TABLE").put_item(
        Item={"jti": str(jti), "user_id": str(user_id), "session_id": str(session_id), "expires_at": expires_at, "ttl": expires_at},
        ConditionExpression="attribute_not_exists(jti)",
    )


def _deactivate_jti(jti):
    """Remove an access token from the active list, making it unusable immediately."""
    if not jti:
        return
    _table("TOKEN_ACTIVELIST_TABLE").delete_item(Key={"jti": str(jti)})


def _hash_token(token):
    """Hash a refresh token before storing or looking it up."""
    return hashlib.sha256(token.encode()).hexdigest()


def _table(env_name):
    """Get a DynamoDB table using its environment variable."""
    table_name = os.environ.get(env_name)

    if not table_name:
        raise RuntimeError(
            f"Missing required environment variable {env_name}"
        )

    return ddb.Table(table_name)


def _json_error(status, code, message):
    """Return a consistent JSON error response."""
    return response(
        status,
        {
            "error": code,
            "message": message,
        },
    )


def _role_from_path(path):
    """/auth/doctors/... -> DOCTOR, /auth/patients/... -> PATIENT, else None."""
    if "/doctors/" in path:
        return "DOCTOR"
    if "/patients/" in path:
        return "PATIENT"
    return None


def _register(body, path_role=None):
    """Register a doctor or patient account.

    A role-specific URL (/auth/doctors/register) decides the role; the body
    "role" is only used by the generic /auth/register route.
    """
    email = str(body.get("email", "")).strip().lower()
    password = body.get("password")
    role = path_role or str(body.get("role", "PATIENT")).upper()

    if not EMAIL_RE.match(email):
        return _json_error(
            400,
            "INVALID_EMAIL",
            "Provide a valid email address.",
        )

    if (
        not isinstance(password, str)
        or len(password) < 10
        or len(password.encode()) > 72
        or not re.search(r"[A-Za-z]", password)
        or not re.search(r"\d", password)
    ):
        return _json_error(
            400,
            "WEAK_PASSWORD",
            "Password must be at least 10 characters and contain a letter and a digit.",
        )

    if role not in ("PATIENT", "DOCTOR"):
        return _json_error(
            403,
            "ROLE_NOT_ALLOWED",
            "Self-registration is only available for doctor and patient accounts.",
        )

    first_name = str(body.get("first_name", "")).strip()
    last_name = str(body.get("last_name", "")).strip()

    # Patients are created with their name. A doctor's name lives on the
    # doctors profile row, which needs a hospital and specialisation, so it is
    # filled in later through PUT /doctors/me (Milestone 3).
    if role == "PATIENT" and (not first_name or not last_name):
        return _json_error(
            400,
            "MISSING_NAME",
            "first_name and last_name are required.",
        )

    user_id = str(uuid.uuid4())
    patient_id = str(uuid.uuid4())

    password_hash = bcrypt.hashpw(
        password.encode(),
        bcrypt.gensalt(rounds=12),
    ).decode()

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            # Check whether this email is already registered.
            cur.execute(
                """
                SELECT user_id
                FROM users
                WHERE email = %s
                LIMIT 1
                """,
                (email,),
            )

            if cur.fetchone():
                return _json_error(
                    409,
                    "EMAIL_EXISTS",
                    "An account with this email already exists.",
                )

            # Create the user account.
            cur.execute(
                """
                INSERT INTO users (
                    user_id,
                    email,
                    password_hash,
                    role,
                    is_active,
                    email_verified
                )
                VALUES (%s, %s, %s, %s, TRUE, FALSE)
                """,
                (user_id, email, password_hash, role),
            )

            if role == "PATIENT":
                # Create the associated patient profile.
                cur.execute(
                    """
                    INSERT INTO patients (
                        patient_id,
                        user_id,
                        first_name,
                        last_name,
                        phone
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        patient_id,
                        user_id,
                        first_name,
                        last_name,
                        str(body.get("phone", "")).strip() or None,
                    ),
                )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    return response(
        201,
        {
            "message": "Registration successful.",
            "user_id": user_id,
            "role": role.lower(),
            **(
                {"profile_complete": False}
                if role == "DOCTOR"
                else {}
            ),
        },
    )


def _login(body, path_role=None):
    """Verify credentials and issue access and refresh tokens."""
    email = str(body.get("email", "")).strip().lower()
    password = body.get("password")

    if not email or not isinstance(password, str):
        return _json_error(
            400,
            "MISSING_CREDENTIALS",
            "email and password are required.",
        )

    conn = get_connection()

    try:
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

    finally:
        conn.close()

    # Reject invalid credentials or inactive accounts.
    if (
        not user
        or not user.get("is_active")
        or (path_role and str(user["role"]).upper() != path_role)
        or not bcrypt.checkpw(
            password.encode(),
            str(user["password_hash"]).encode(),
        )
    ):
        return _json_error(
            401,
            "INVALID_CREDENTIALS",
            "Email or password is incorrect.",
        )

    refresh_token = secrets.token_urlsafe(48)
    session_id = _hash_token(refresh_token)
    access_token, access_jti, access_exp = _issue_access(user, session_id)

    current_time = int(time.time())

    # REFRESH_TOKENS stores only the fields in the data model. Its expires_at
    # attribute is configured as DynamoDB TTL; no separate ttl/current_jti/role
    # attribute is stored in this table.
    _activate_jti(access_jti, user["user_id"], access_exp, session_id)
    try:
        _table("REFRESH_TOKEN_TABLE").put_item(
            Item={
                "token_hash": session_id,
                "user_id": str(user["user_id"]),
                "created_at": current_time,
                "expires_at": current_time + REFRESH_TTL,
            },
            ConditionExpression="attribute_not_exists(token_hash)",
        )
    except Exception:
        _deactivate_jti(access_jti)
        raise

    return response(
        200,
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh_token,
            "refresh_expires_in": REFRESH_TTL,
            "user": {
                "user_id": str(user["user_id"]),
                "email": user["email"],
                "role": str(user["role"]).lower(),
            },
        },
    )


def _deactivate_session_tokens(session_id):
    """Deactivate all access tokens associated with one refresh-token session."""
    if not session_id:
        return
    table = _table("TOKEN_ACTIVELIST_TABLE")
    query_args = {
        "IndexName": "session_id-index",
        "KeyConditionExpression": Key("session_id").eq(str(session_id)),
    }
    while True:
        result = table.query(**query_args)
        for item in result.get("Items", []):
            _deactivate_jti(item.get("jti"))
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key


def _user_for_refresh(user_id):
    """Load the current role/status from MySQL; role is not duplicated in DynamoDB."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, role, is_active FROM users WHERE user_id = %s LIMIT 1",
                (str(user_id),),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _refresh(body):
    """Rotate a refresh token and replace access tokens for its session."""
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(400, "MISSING_REFRESH_TOKEN", "refresh_token is required.")

    table = _table("REFRESH_TOKEN_TABLE")
    old_hash = _hash_token(refresh_token)
    current_time = int(time.time())
    record = table.get_item(Key={"token_hash": old_hash}, ConsistentRead=True).get("Item")

    if (
        not record
        or record.get("revoked_at") is not None
        or int(record.get("expires_at", 0)) <= current_time
    ):
        return _json_error(401, "INVALID_REFRESH_TOKEN", "Refresh token is invalid, expired, or revoked.")

    user = _user_for_refresh(record["user_id"])
    if not user or not user.get("is_active"):
        return _json_error(401, "INVALID_REFRESH_TOKEN", "The account is unavailable.")

    # Rotate the refresh token. Conditional revocation below ensures only one
    # concurrent request can successfully consume the old refresh token.
    new_refresh_token = secrets.token_urlsafe(48)
    new_hash = _hash_token(new_refresh_token)
    new_token, new_jti, new_exp = _issue_access(user, new_hash)
    _activate_jti(new_jti, record["user_id"], new_exp, new_hash)

    try:
        table.put_item(
            Item={
                "token_hash": new_hash,
                "user_id": str(record["user_id"]),
                "created_at": current_time,
                "expires_at": current_time + REFRESH_TTL,
            },
            ConditionExpression="attribute_not_exists(token_hash)",
        )
        table.update_item(
            Key={"token_hash": old_hash},
            UpdateExpression="SET revoked_at = :now",
            ConditionExpression=(
                "attribute_exists(token_hash) AND attribute_not_exists(revoked_at) "
                "AND expires_at > :now"
            ),
            ExpressionAttributeValues={":now": current_time},
        )
    except ClientError as exc:
        _deactivate_jti(new_jti)
        # If the new refresh record was created but the old token was already
        # consumed by another request, remove the unreturned candidate record.
        try:
            table.delete_item(Key={"token_hash": new_hash})
        except Exception:
            log.exception("Could not clean up losing refresh candidate")
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _json_error(401, "INVALID_REFRESH_TOKEN", "Refresh token was already used or revoked.")
        raise
    except Exception:
        _deactivate_jti(new_jti)
        try:
            table.delete_item(Key={"token_hash": new_hash})
        except Exception:
            log.exception("Could not clean up failed refresh candidate")
        raise

    # Remove old session access tokens only after the refresh-token rotation
    # succeeds. The new access token is tied to new_hash, so it remains active.
    _deactivate_session_tokens(old_hash)

    return response(200, {
        "access_token": new_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": new_refresh_token,
        "refresh_expires_in": REFRESH_TTL,
    })


def _logout(event, body):
    """Revoke the supplied refresh token and deactivate its session's access tokens."""
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(400, "MISSING_REFRESH_TOKEN", "refresh_token is required.")

    table = _table("REFRESH_TOKEN_TABLE")
    session_id = _hash_token(refresh_token)
    now = int(time.time())
    record = table.get_item(Key={"token_hash": session_id}, ConsistentRead=True).get("Item")

    if record and record.get("revoked_at") is None:
        table.update_item(
            Key={"token_hash": session_id},
            UpdateExpression="SET revoked_at = :now",
            ConditionExpression="attribute_exists(token_hash) AND attribute_not_exists(revoked_at)",
            ExpressionAttributeValues={":now": now},
        )
        _deactivate_session_tokens(session_id)

    # Also deactivate the bearer token if it belongs to this user/session.
    bearer = _bearer_token(event)
    if bearer:
        try:
            claims = verify_access_token(bearer)
        except ValueError:
            claims = None
        if claims and claims.get("sid") == session_id:
            _deactivate_jti(claims.get("jti"))

    return response(200, {"message": "Logged out successfully."})


def _bearer_token(event):
    """Return the Bearer token from the Authorization header, if any."""
    headers = event.get("headers") or {}
    authorization = (
        headers.get("authorization")
        or headers.get("Authorization")
        or ""
    )

    if authorization.lower().startswith("bearer "):
        return authorization.split(None, 1)[1].strip()

    return None


def lambda_handler(event, context):
    """Route incoming authentication requests."""
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod", "GET")
    ).upper()

    path = event.get("rawPath") or event.get("path") or "/"

    try:
        body = (
            request_body(event)
            if method in ("POST", "PUT", "PATCH")
            else {}
        )

    except ValueError as exc:
        return _json_error(
            400,
            "INVALID_JSON",
            str(exc),
        )

    try:
        # Protected diagnostic endpoint used to demonstrate access-token
        # validation. API Gateway also invokes the Lambda authorizer on this route.
        if method == "GET" and path.endswith("/auth/test-token"):
            bearer = _bearer_token(event)
            if not bearer:
                return _json_error(401, "MISSING_ACCESS_TOKEN", "Send Authorization: Bearer <access_token>.")
            try:
                claims = verify_access_token(bearer)
            except Exception:
                return _json_error(401, "INVALID_ACCESS_TOKEN", "Access token is invalid, expired, or inactive.")
            return response(200, {
                "message": "Access token is valid.",
                "authenticated": True,
                "user": {
                    "user_id": str(claims["sub"]),
                    "role": str(claims["role"]),
                    "jti": str(claims["jti"]),
                },
                "expires_at": int(claims["exp"]),
            })

        if method != "POST":
            return response(
                405,
                {"error": "METHOD_NOT_ALLOWED"},
            )

        if path.endswith("/auth/register") or path.endswith("/register"):
            return _register(body, _role_from_path(path))

        if path.endswith("/auth/login") or path.endswith("/login"):
            return _login(body, _role_from_path(path))

        if path.endswith("/auth/refresh") or path.endswith("/refresh"):
            return _refresh(body)

        if path.endswith("/auth/logout") or path.endswith("/logout"):
            return _logout(event, body)

        return response(
            404,
            {"error": "ROUTE_NOT_FOUND"},
        )

    except Exception:
        # Detailed errors stay in CloudWatch logs, not in the client response.
        log.exception("Authentication request failed: %s %s", method, path)

        return response(
            500,
            {
                "error": "INTERNAL_SERVER_ERROR",
                "message": "Authentication service failed.",
            },
        )