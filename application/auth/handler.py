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
    """Generate an access token for an authenticated user."""
    current_time = int(time.time())

    return _jwt({
        "sub": str(user["user_id"]),
        "role": str(user["role"]).lower(),
        "iat": current_time,
        "exp": current_time + ACCESS_TTL,
        "jti": str(uuid.uuid4()),
        "iss": os.environ.get("JWT_ISSUER", "medislot"),
    })


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


def _register(body):
    """Register a new patient account."""
    email = str(body.get("email", "")).strip().lower()
    password = body.get("password")
    role = str(body.get("role", "PATIENT")).upper()

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

    # Only patients can register themselves.
    if role != "PATIENT":
        return _json_error(
            403,
            "ROLE_NOT_ALLOWED",
            "Self-registration is only available for patient accounts.",
        )

    first_name = str(body.get("first_name", "")).strip()
    last_name = str(body.get("last_name", "")).strip()

    if not first_name or not last_name:
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
                VALUES (%s, %s, %s, 'PATIENT', TRUE, FALSE)
                """,
                (user_id, email, password_hash),
            )

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
            "role": "patient",
        },
    )


def _login(body):
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

    access_token = _issue_access(user)
    refresh_token = secrets.token_urlsafe(48)

    current_time = int(time.time())

    # Store only the hash of the refresh token in DynamoDB.
    _table("REFRESH_TOKEN_TABLE").put_item(
        Item={
            "token_hash": _hash_token(refresh_token),
            "user_id": str(user["user_id"]),
            "role": str(user["role"]).lower(),
            "created_at": current_time,
            "expires_at": current_time + REFRESH_TTL,
            "ttl": current_time + REFRESH_TTL,
            "revoked": False,
        }
    )

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


def _refresh(body):
    """Validate a refresh token and issue new tokens."""
    refresh_token = body.get("refresh_token")

    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(
            400,
            "MISSING_REFRESH_TOKEN",
            "refresh_token is required.",
        )

    refresh_table = _table("REFRESH_TOKEN_TABLE")
    token_hash = _hash_token(refresh_token)

    result = refresh_table.get_item(
        Key={"token_hash": token_hash},
        ConsistentRead=True,
    )

    token_record = result.get("Item")
    current_time = int(time.time())

    if (
        not token_record
        or token_record.get("revoked")
        or int(token_record.get("expires_at", 0)) <= current_time
    ):
        return _json_error(
            401,
            "INVALID_REFRESH_TOKEN",
            "Refresh token is invalid, expired, or revoked.",
        )

    # The refresh token is NOT rotated: it stays valid, and revocable via
    # logout, until its own 7-day expiry. Each call only mints a fresh
    # short-lived access token.
    new_access_token = _jwt({
        "sub": str(token_record["user_id"]),
        "role": str(token_record["role"]).lower(),
        "iat": current_time,
        "exp": current_time + ACCESS_TTL,
        "jti": str(uuid.uuid4()),
        "iss": os.environ.get("JWT_ISSUER", "medislot"),
    })

    return response(
        200,
        {
            "access_token": new_access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_expires_in": int(token_record["expires_at"]) - current_time,
        },
    )


def _logout(event, body):
    """Revoke a refresh token and optionally denylist an access token."""
    refresh_token = body.get("refresh_token")

    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(
            400,
            "MISSING_REFRESH_TOKEN",
            "refresh_token is required.",
        )

    try:
        _table("REFRESH_TOKEN_TABLE").update_item(
            Key={"token_hash": _hash_token(refresh_token)},
            UpdateExpression="SET revoked = :r",
            ExpressionAttributeValues={":r": True},
            ConditionExpression="attribute_exists(token_hash)",
        )
    except ClientError as exc:
        # Token already gone/revoked: logout is idempotent, keep going.
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    # If an access token is supplied, denylist its JTI until it expires.
    headers = event.get("headers", {})

    authorization = (
        headers.get("authorization")
        or headers.get("Authorization", "")
    )

    if authorization.lower().startswith("bearer "):
        token_parts = authorization.split()[1].split(".")

        if len(token_parts) == 3:
            try:
                encoded_payload = token_parts[1]
                padding = "=" * (-len(encoded_payload) % 4)

                payload = json.loads(
                    base64.urlsafe_b64decode(
                        encoded_payload + padding
                    )
                )

                token_id = payload.get("jti")
                expires_at = int(payload.get("exp", 0))

                if token_id and expires_at > int(time.time()):
                    _table("TOKEN_DENYLIST_TABLE").put_item(
                        Item={
                            "jti": token_id,
                            "expires_at": expires_at,
                            "ttl": expires_at,
                        }
                    )

            except Exception:
                pass

    return response(
        200,
        {"message": "Logged out successfully."},
    )


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
        if method != "POST":
            return response(
                405,
                {"error": "METHOD_NOT_ALLOWED"},
            )

        if path.endswith("/auth/register") or path.endswith("/register"):
            return _register(body)

        if path.endswith("/auth/login") or path.endswith("/login"):
            return _login(body)

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