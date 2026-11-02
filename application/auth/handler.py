"""MediSlot authentication routes: register, login, refresh, logout, test-token.

Token model
  * Access token : JWT (HS256, 15 min by default). The user's CURRENT access token is
    tracked in TOKEN_ACTIVELIST (partition key user_id). Every login / refresh overwrites
    that single row, so the previous access token is rejected at once by the authorizer.
  * Refresh token: opaque random string. Only its SHA-256 hash is stored (REFRESH_TOKENS).
    Refresh ROTATES the token: the old one is consumed atomically and a new one is issued.
  * Logout       : deletes the user's active-list row and EVERY refresh token of that user.
  * Login responses never contain user details, only tokens.

Lifetimes and the bcrypt work factor come from SSM (auth/access-token-ttl-seconds,
auth/refresh-token-ttl-seconds, auth/bcrypt-work-factor).
"""

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

import bcrypt
import boto3
import pymysql
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

from application.authorizer.lambda_handler import get_jwt_secret, verify_access_token
from application.common import config
from application.common.http import request_body, request_method, request_path, response
from application.database.connection import get_connection

log = logging.getLogger()
log.setLevel(logging.INFO)

# Fail fast (seconds, not the Lambda timeout) if a VPC endpoint/route is missing.
_AWS_CFG = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})
ddb = boto3.resource("dynamodb", config=_AWS_CFG)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+[.][^@\s]+$")
MIN_BCRYPT_ROUNDS = 12


# ------------------------------------------------------------------ configuration
def access_ttl():
    return max(60, config.get_int("auth/access-token-ttl-seconds", 900))


def refresh_ttl():
    return max(3600, config.get_int("auth/refresh-token-ttl-seconds", 12 * 60 * 60))


def bcrypt_rounds():
    return max(MIN_BCRYPT_ROUNDS, config.get_int("auth/bcrypt-work-factor", MIN_BCRYPT_ROUNDS))


# ---------------------------------------------------------------------- helpers
def _json_error(status, code, message):
    return response(status, {"error": code, "message": message})


def _table(env_name):
    table_name = os.environ.get(env_name)
    if not table_name:
        raise RuntimeError(f"Missing required environment variable {env_name}")
    return ddb.Table(table_name)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(payload):
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":"), default=str).encode())
    signing_input = f"{header}.{body}".encode()
    signature = _b64(hmac.new(get_jwt_secret(), signing_input, hashlib.sha256).digest())
    return f"{header}.{body}.{signature}"


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _role_from_path(path):
    if "/doctors/" in path:
        return "DOCTOR"
    if "/patients/" in path:
        return "PATIENT"
    return None


def _bearer_token(event):
    headers = event.get("headers") or {}
    authorization = next((v for k, v in headers.items() if k.lower() == "authorization"), "") or ""
    if authorization.lower().startswith("bearer "):
        return authorization.split(None, 1)[1].strip()
    return None


# ----------------------------------------------------------------------- tokens
def _issue_access(user):
    """Create a signed access token. Returns (token, jti, exp)."""
    now = int(time.time())
    jti = str(uuid.uuid4())
    exp = now + access_ttl()
    token = _jwt({
        "sub": str(user["user_id"]),
        "role": str(user["role"]).lower(),
        "iat": now,
        "exp": exp,
        "jti": jti,
        "iss": os.environ.get("JWT_ISSUER", "medislot"),
    })
    return token, jti, exp


def _activate_jti(jti, user_id, expires_at):
    """Make this the user's ONE active access token (replaces any previous one)."""
    expires_at = int(expires_at)
    if expires_at <= int(time.time()):
        raise ValueError("Cannot activate an already-expired access token")
    result = _table("TOKEN_ACTIVELIST_TABLE").put_item(
        Item={"user_id": str(user_id), "jti": str(jti), "expires_at": expires_at, "ttl": expires_at},
        ReturnValues="ALL_OLD",
    )
    replaced = (result or {}).get("Attributes")
    if replaced:
        log.info("Replaced previous active access token jti=%s", replaced.get("jti"))


def _deactivate_user_token(user_id):
    _table("TOKEN_ACTIVELIST_TABLE").delete_item(Key={"user_id": str(user_id)})


def _store_refresh_token(user_id):
    """Create an opaque refresh token; only its hash is stored."""
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    expires_at = now + refresh_ttl()
    _table("REFRESH_TOKEN_TABLE").put_item(Item={
        "token_hash": _hash_token(token),
        "user_id": str(user_id),
        "created_at": now,
        "expires_at": expires_at,  # DynamoDB TTL attribute
    })
    return token, expires_at


def _delete_all_refresh_tokens(user_id):
    table = _table("REFRESH_TOKEN_TABLE")
    query_args = {
        "IndexName": "user_id-index",
        "KeyConditionExpression": Key("user_id").eq(str(user_id)),
        "ProjectionExpression": "token_hash",
    }
    while True:
        result = table.query(**query_args)
        for item in result.get("Items", []):
            table.delete_item(Key={"token_hash": item["token_hash"]})
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key


def _load_user_by_id(user_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, role, is_active FROM users WHERE user_id = %s LIMIT 1", (str(user_id),))
            return cur.fetchone()
    finally:
        conn.close()


# ----------------------------------------------------------------------- routes
def _register(body, path_role):
    email = str(body.get("email", "")).strip().lower()
    password = body.get("password")
    role = path_role or ""

    if role not in ("PATIENT", "DOCTOR"):
        return _json_error(403, "ROLE_NOT_ALLOWED", "Self-registration is only available for doctor and patient accounts.")
    if not EMAIL_RE.match(email) or len(email) > 254:
        return _json_error(400, "INVALID_EMAIL", "Provide a valid email address.")
    if (not isinstance(password, str) or len(password) < 10 or len(password.encode()) > 72
            or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password)):
        return _json_error(400, "WEAK_PASSWORD", "Password must be at least 10 characters and contain a letter and a digit.")

    if role == "DOCTOR":
        # Optional gate: when the SecureString auth/doctor-registration-code exists the
        # caller must present it. Errors other than "not found" fail closed.
        expected = config.get_optional_secret("auth/doctor-registration-code")
        if expected is not None and not hmac.compare_digest(
                str(body.get("registration_code", "")).encode(), expected.encode()):
            return _json_error(403, "INVALID_REGISTRATION_CODE", "A valid registration code is required for doctors.")

    first_name = str(body.get("first_name", "")).strip()
    last_name = str(body.get("last_name", "")).strip()
    if role == "PATIENT" and (not first_name or not last_name):
        return _json_error(400, "MISSING_NAME", "first_name and last_name are required.")

    user_id, patient_id = str(uuid.uuid4()), str(uuid.uuid4())
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=bcrypt_rounds())).decode()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE email = %s LIMIT 1", (email,))
            if cur.fetchone():
                return _json_error(409, "EMAIL_EXISTS", "An account with this email already exists.")
            cur.execute(
                "INSERT INTO users (user_id, email, password_hash, role, is_active) VALUES (%s, %s, %s, %s, TRUE)",
                (user_id, email, password_hash, role),
            )
            if role == "PATIENT":
                cur.execute(
                    "INSERT INTO patients (patient_id, user_id, first_name, last_name, phone) VALUES (%s, %s, %s, %s, %s)",
                    (patient_id, user_id, first_name, last_name, str(body.get("phone", "")).strip() or None),
                )
        conn.commit()
    except pymysql.err.IntegrityError as exc:  # concurrent duplicate registration
        conn.rollback()
        if exc.args and exc.args[0] == 1062:
            return _json_error(409, "EMAIL_EXISTS", "An account with this email already exists.")
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    payload = {"message": "Registration successful.", "user_id": user_id, "role": role.lower()}
    if role == "DOCTOR":
        payload["profile_complete"] = False  # completed through PUT /doctors/me
    return response(201, payload)


def _login(body, path_role):
    email = str(body.get("email", "")).strip().lower()
    password = body.get("password")
    if not email or not isinstance(password, str) or not password:
        return _json_error(400, "MISSING_CREDENTIALS", "email and password are required.")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, password_hash, role, is_active FROM users WHERE email = %s LIMIT 1", (email,))
            user = cur.fetchone()
    finally:
        conn.close()

    if (not user or not user.get("is_active")
            or (path_role and str(user["role"]).upper() != path_role)
            or not bcrypt.checkpw(password.encode(), str(user["password_hash"]).encode())):
        return _json_error(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")

    access_token, jti, exp = _issue_access(user)
    refresh_token, refresh_exp = _store_refresh_token(user["user_id"])
    _activate_jti(jti, user["user_id"], exp)  # replaces the previous active token

    # Tokens only: no user details are returned by login.
    return response(200, {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": access_ttl(),
        "refresh_token": refresh_token,
        "refresh_expires_in": refresh_exp - int(time.time()),
    })


def _refresh(body):
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return _json_error(400, "MISSING_REFRESH_TOKEN", "refresh_token is required.")

    invalid = _json_error(401, "INVALID_REFRESH_TOKEN", "Refresh token is invalid, expired, or revoked.")
    # Consume the old token atomically: a replay or a race finds nothing to delete.
    try:
        old = _table("REFRESH_TOKEN_TABLE").delete_item(
            Key={"token_hash": _hash_token(refresh_token)},
            ConditionExpression="attribute_exists(token_hash) AND expires_at > :now",
            ExpressionAttributeValues={":now": int(time.time())},
            ReturnValues="ALL_OLD",
        ).get("Attributes")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return invalid
        raise
    if not old:
        return invalid

    user = _load_user_by_id(old["user_id"])
    if not user or not user.get("is_active"):
        return _json_error(401, "INACTIVE_ACCOUNT", "This account is not active.")

    access_token, jti, exp = _issue_access(user)
    new_refresh, refresh_exp = _store_refresh_token(user["user_id"])
    _activate_jti(jti, user["user_id"], exp)  # replaces the previous active token
    return response(200, {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": access_ttl(),
        "refresh_token": new_refresh,
        "refresh_expires_in": refresh_exp - int(time.time()),
    })


def _logout(event, body):
    bearer = _bearer_token(event)
    if not bearer:
        return _json_error(401, "MISSING_ACCESS_TOKEN", "Send an access token as a Bearer token to log out.")
    try:
        # The active-list row may already be gone; logout must still clean everything up.
        claims = verify_access_token(bearer, check_active=False)
    except (ValueError, KeyError, TypeError):
        return _json_error(401, "INVALID_ACCESS_TOKEN", "A valid, unexpired signed access token is required for logout.")

    user_id = str(claims["sub"])
    _deactivate_user_token(user_id)
    # Strongly consistent delete of the presented token first (the GSI query below is
    # eventually consistent), then every other refresh token of this user.
    presented = body.get("refresh_token")
    if isinstance(presented, str) and presented:
        _table("REFRESH_TOKEN_TABLE").delete_item(Key={"token_hash": _hash_token(presented)})
    _delete_all_refresh_tokens(user_id)
    return response(200, {"message": "Logged out successfully."})


def _test_token(event):
    """Protected demo route: proves the authorizer context reaches the handler."""
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    identity = authorizer.get("lambda") or authorizer
    user_id, role = identity.get("userId"), identity.get("role")
    if not user_id or not role:
        return _json_error(401, "UNAUTHORIZED", "A valid access token is required.")
    return response(200, {"message": "Access token is valid.", "user_id": str(user_id), "role": str(role)})


def lambda_handler(event, context):
    method, path = request_method(event), request_path(event).rstrip("/")

    if method == "GET" and (path.endswith("/auth/test-token") or path.endswith("/auth/test-access")):
        return _test_token(event)

    try:
        body = request_body(event) if method in ("POST", "PUT", "PATCH") else {}
    except ValueError as exc:
        return _json_error(400, "INVALID_JSON", str(exc))

    try:
        if method != "POST":
            return response(405, {"error": "METHOD_NOT_ALLOWED"})
        if path.endswith("/register"):
            return _register(body, _role_from_path(path + "/"))
        if path.endswith("/login"):
            return _login(body, _role_from_path(path + "/"))
        if path.endswith("/auth/refresh"):
            return _refresh(body)
        if path.endswith("/auth/logout"):
            return _logout(event, body)
        return response(404, {"error": "ROUTE_NOT_FOUND"})
    except Exception:  # noqa: BLE001 - details stay in CloudWatch, never in the response
        log.exception("Authentication request failed: %s %s", method, path)
        return response(500, {"error": "INTERNAL_SERVER_ERROR", "message": "Authentication service failed."})
