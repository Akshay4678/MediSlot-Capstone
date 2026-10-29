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


def _deny_jti(jti, expires_at):
    """Put an access token's jti on the denylist until the token expires.

    The entry's TTL equals the token's own expiry, so DynamoDB removes it once
    the token would be rejected anyway. Already-expired tokens are skipped.
    """
    if not jti or not expires_at:
        return

    expires_at = int(expires_at)

    if expires_at <= int(time.time()):
        return

    _table("TOKEN_DENYLIST_TABLE").put_item(
        Item={"jti": jti, "expires_at": expires_at, "ttl": expires_at}
    )


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

    access_token, access_jti, access_exp = _issue_access(user)
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
            # The newest access token issued for this login. It is denylisted
            # when replaced at refresh time and when the user logs out.
            "current_jti": access_jti,
            "current_jti_exp": access_exp,
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


MAX_REFRESH_ATTEMPTS = 3


def _refresh(body):
    """Issue a new access token and denylist the one it replaces.

    At most ONE access token per login is valid at any moment: the newest.
    """
    refresh_token = body.get("refresh_token")

    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(
            400,
            "MISSING_REFRESH_TOKEN",
            "refresh_token is required.",
        )

    table = _table("REFRESH_TOKEN_TABLE")
    token_hash = _hash_token(refresh_token)

    for _attempt in range(MAX_REFRESH_ATTEMPTS):
        record = table.get_item(
            Key={"token_hash": token_hash},
            ConsistentRead=True,
        ).get("Item")

        current_time = int(time.time())

        if (
            not record
            or record.get("revoked")
            or int(record.get("expires_at", 0)) <= current_time
        ):
            return _json_error(
                401,
                "INVALID_REFRESH_TOKEN",
                "Refresh token is invalid, expired, or revoked.",
            )

        old_jti = record.get("current_jti")
        old_exp = record.get("current_jti_exp")

        new_token, new_jti, new_exp = _issue_access(
            {"user_id": record["user_id"], "role": record["role"]}
        )

        # 1. Denylist the previous access token FIRST. If this write fails the
        #    request fails and the old token is simply still the valid one.
        _deny_jti(old_jti, old_exp)

        # 2. Record the new token as "current", but only if nobody refreshed
        #    (or logged out) since we read the record. Optimistic concurrency:
        #    two simultaneous refreshes can never both end up with a live token.
        if old_jti:
            condition = "attribute_exists(token_hash) AND current_jti = :old"
            values = {":n": new_jti, ":e": new_exp, ":old": old_jti}
        else:
            # Record created before this feature existed.
            condition = (
                "attribute_exists(token_hash) "
                "AND attribute_not_exists(current_jti)"
            )
            values = {":n": new_jti, ":e": new_exp}

        try:
            table.update_item(
                Key={"token_hash": token_hash},
                UpdateExpression="SET current_jti = :n, current_jti_exp = :e",
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Lost the race (or logged out meanwhile): the new token was never
            # returned to anyone, so discard it and re-read.
            continue

        return response(
            200,
            {
                "access_token": new_token,
                "token_type": "Bearer",
                "expires_in": ACCESS_TTL,
                "refresh_expires_in": int(record["expires_at"]) - current_time,
            },
        )

    return _json_error(
        409,
        "REFRESH_CONFLICT",
        "Concurrent refresh detected. Please retry.",
    )


def _logout(event, body):
    """Log out: delete the refresh token and denylist the latest access token.

    The refresh token is required: it is the only way to find the newest
    access token of this login (earlier ones were denylisted at refresh time).
    Failures are not swallowed, so a failed revocation never reports success.
    """
    refresh_token = body.get("refresh_token")

    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(
            400,
            "MISSING_REFRESH_TOKEN",
            "refresh_token is required.",
        )

    table = _table("REFRESH_TOKEN_TABLE")
    key = {"token_hash": _hash_token(refresh_token)}

    # 1. Denylist the newest access token recorded for this login.
    record = table.get_item(Key=key, ConsistentRead=True).get("Item")
    denied_jti = None

    if record:
        denied_jti = record.get("current_jti")
        _deny_jti(denied_jti, record.get("current_jti_exp"))

    # 2. Delete the refresh token. ALL_OLD returns the record as it was at the
    #    instant of deletion; if a refresh slipped in between steps 1 and 2,
    #    its newer token is caught here. After deletion no refresh can succeed.
    deleted = table.delete_item(Key=key, ReturnValues="ALL_OLD").get("Attributes")

    if deleted and deleted.get("current_jti") != denied_jti:
        _deny_jti(deleted.get("current_jti"), deleted.get("current_jti_exp"))

    # 3. If a (valid) access token was presented, denylist it too.
    bearer = _bearer_token(event)

    if bearer:
        try:
            claims = verify_access_token(bearer)
        except ValueError:
            claims = None  # expired, forged or already denylisted

        if claims:
            _deny_jti(claims["jti"], claims["exp"])

    return response(
        200,
        {"message": "Logged out successfully."},
    )


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