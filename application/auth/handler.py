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


def _issue_access(user):
    """Generate an access token. Returns (token, jti, exp).

    The jti and exp are returned so the caller can remember the latest token
    of this login and denylist it when it is replaced or on logout.
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
        "iss": os.environ.get("JWT_ISSUER", "medislot"),
    })

    return token, jti, expires_at


def _activate_jti(jti, user_id, expires_at):
    """Register an access token as active until its JWT expiry."""
    expires_at = int(expires_at)
    if expires_at <= int(time.time()):
        raise ValueError("Cannot activate an already-expired access token")
    _table("TOKEN_ACTIVELIST_TABLE").put_item(
        Item={
            "jti": str(jti),
            "user_id": str(user_id),
            "expires_at": expires_at,
            "ttl": expires_at,
        }
    )


def _deactivate_jti(jti):
    """Remove an access token from the active list immediately."""
    if jti:
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
    """Verify credentials, then store refresh metadata and an active access jti."""
    email = str(body.get("email", "")).strip().lower()
    password = body.get("password")
    if not email or not isinstance(password, str):
        return _json_error(400, "MISSING_CREDENTIALS", "email and password are required.")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, email, password_hash, role, is_active FROM users WHERE email = %s LIMIT 1",
                (email,),
            )
            user = cur.fetchone()
    finally:
        conn.close()

    if (not user or not user.get("is_active")
            or (path_role and str(user["role"]).upper() != path_role)
            or not bcrypt.checkpw(password.encode(), str(user["password_hash"]).encode())):
        return _json_error(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")

    access_token, access_jti, access_exp = _issue_access(user)
    refresh_token = secrets.token_urlsafe(48)
    now = int(time.time())

    # Refresh table follows the data model: no JTI, role, revoked flag, or TTL.
    _table("REFRESH_TOKEN_TABLE").put_item(Item={
        "token_hash": _hash_token(refresh_token),
        "user_id": str(user["user_id"]),
        "created_at": now,
        "expires_at": now + REFRESH_TTL,
    })
    _activate_jti(access_jti, user["user_id"], access_exp)

    return response(200, {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": refresh_token,
        "refresh_expires_in": REFRESH_TTL,
        "user": {"user_id": str(user["user_id"]), "email": user["email"], "role": str(user["role"]).lower()},
    })


def _load_user_by_id(user_id):
    """Fetch the user's current role from RDS; role is not stored in REFRESH_TOKENS."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, email, role, is_active FROM users WHERE user_id = %s LIMIT 1",
                (str(user_id),),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _refresh(body):
    """Validate the refresh record and issue a new active access token.

    The data model intentionally does not associate a JTI with a refresh record.
    Therefore an older access token remains active until it expires (15 minutes),
    unless that specific token is explicitly removed from TOKEN_ACTIVELIST.
    """
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(400, "MISSING_REFRESH_TOKEN", "refresh_token is required.")

    table = _table("REFRESH_TOKEN_TABLE")
    token_hash = _hash_token(refresh_token)
    record = table.get_item(Key={"token_hash": token_hash}, ConsistentRead=True).get("Item")
    now = int(time.time())
    if (not record or record.get("revoked_at") is not None
            or int(record.get("expires_at", 0)) <= now):
        return _json_error(401, "INVALID_REFRESH_TOKEN", "Refresh token is invalid, expired, or revoked.")

    user = _load_user_by_id(record["user_id"])
    if not user or not user.get("is_active"):
        return _json_error(401, "INACTIVE_ACCOUNT", "This account is not active.")

    token, jti, exp = _issue_access(user)
    _activate_jti(jti, user["user_id"], exp)
    return response(200, {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_expires_in": max(0, int(record["expires_at"]) - now),
    })


def _logout(event, body):
    """Revoke the refresh token and remove the presented access token from active list."""
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(400, "MISSING_REFRESH_TOKEN", "refresh_token is required.")

    bearer = _bearer_token(event)
    if not bearer:
        return _json_error(400, "MISSING_ACCESS_TOKEN", "Send the current access token as a Bearer token to log out.")
    try:
        claims = verify_access_token(bearer, check_active=False)
    except (ValueError, KeyError, TypeError):
        return _json_error(401, "INVALID_ACCESS_TOKEN", "A valid signed access token is required for logout.")

    table = _table("REFRESH_TOKEN_TABLE")
    key = {"token_hash": _hash_token(refresh_token)}
    record = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if not record or int(record.get("expires_at", 0)) <= int(time.time()):
        return response(200, {"message": "Logged out successfully."})
    if str(record.get("user_id")) != str(claims.get("sub")):
        return _json_error(403, "TOKEN_USER_MISMATCH", "The access token and refresh token belong to different users.")

    now = int(time.time())
    if record.get("revoked_at") is None:
        table.update_item(
            Key=key,
            UpdateExpression="SET revoked_at = :now",
            ConditionExpression="attribute_exists(token_hash) AND attribute_not_exists(revoked_at)",
            ExpressionAttributeValues={":now": now},
        )
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

    # A small protected route for mentor/demo testing. API Gateway must attach
    # the Lambda authorizer to GET /auth/test-access for this to be meaningful.
    if method == "GET" and path.rstrip("/").endswith("/auth/test-access"):
        request_context = event.get("requestContext") or {}
        authorizer = request_context.get("authorizer") or {}
        identity = authorizer.get("lambda") or authorizer.get("claims") or authorizer
        user_id = identity.get("userId") or identity.get("user_id") or identity.get("sub")
        role = identity.get("role")
        if not user_id or not role:
            return _json_error(401, "UNAUTHORIZED", "A valid access token is required.")
        return response(200, {"message": "Access token is valid.", "user_id": str(user_id), "role": str(role)})

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