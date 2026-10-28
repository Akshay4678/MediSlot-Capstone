"""Shared authentication utilities for MediSlot."""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from functools import lru_cache

import boto3
import jwt
import pymysql
from botocore.config import Config


log = logging.getLogger()
log.setLevel(logging.INFO)


# Short AWS SDK timeouts help identify network connectivity problems.
# They do not guarantee that every operation will finish within this time.
AWS_CONFIG = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={
        "total_max_attempts": 1,
        "mode": "standard",
    },
)

_ssm = boto3.client("ssm", config=AWS_CONFIG)

_secrets = boto3.client(
    "secretsmanager",
    config=AWS_CONFIG,
)

_dynamodb = boto3.resource(
    "dynamodb",
    config=AWS_CONFIG,
)


# ---------------------------------------------------------
# HTTP RESPONSE
# ---------------------------------------------------------

def response(status, body):
    """Build an API Gateway-compatible JSON response."""
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


# ---------------------------------------------------------
# REQUEST BODY
# ---------------------------------------------------------

def body_json(event):
    """Parse and validate a JSON request body."""

    raw = event.get("body") or "{}"

    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception as exc:
            raise ValueError(
                "Request body contains invalid Base64 data."
            ) from exc

    if isinstance(raw, dict):
        value = raw
    else:
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError(
                "Request body must be valid JSON."
            ) from exc

    if not isinstance(value, dict):
        raise ValueError(
            "Request body must be a JSON object."
        )

    return value


# ---------------------------------------------------------
# ENVIRONMENT VARIABLES
# ---------------------------------------------------------

def required_env(name):
    """Return a required environment variable."""
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


# ---------------------------------------------------------
# JWT SIGNING SECRET
# ---------------------------------------------------------

@lru_cache(maxsize=1)
def jwt_secret():
    """Retrieve and cache the JWT signing secret from SSM."""

    parameter_name = required_env(
        "JWT_SECRET_PARAMETER_NAME"
    )

    log.info("Retrieving JWT signing secret from SSM")

    try:
        result = _ssm.get_parameter(
            Name=parameter_name,
            WithDecryption=True,
        )

        value = result["Parameter"]["Value"]

    except Exception:
        log.exception(
            "Failed to retrieve JWT signing secret from SSM"
        )
        raise

    if not isinstance(value, str) or len(value) < 32:
        raise RuntimeError(
            "JWT signing secret must be at least 32 characters."
        )

    log.info("JWT signing secret retrieved successfully")

    return value


# ---------------------------------------------------------
# DATABASE CONFIGURATION
# ---------------------------------------------------------

@lru_cache(maxsize=1)
def db_config():
    """Retrieve and cache the RDS connection configuration."""

    secret_arn = required_env(
        "DATABASE_SECRET_ARN"
    )

    log.info(
        "Retrieving database credentials from Secrets Manager"
    )

    try:
        result = _secrets.get_secret_value(
            SecretId=secret_arn
        )

        secret_string = result.get("SecretString")

        if not secret_string:
            raise RuntimeError(
                "Database secret does not contain SecretString."
            )

        secret = json.loads(secret_string)

        username = secret["username"]
        password = secret["password"]

        if not isinstance(username, str):
            raise RuntimeError(
                "Database username in the secret must be a string."
            )

        if not isinstance(password, str):
            raise RuntimeError(
                "Database password in the secret must be a string."
            )

        host = (
            secret.get("host")
            or required_env("DB_HOST")
        )

        database = (
            secret.get("dbname")
            or secret.get("database")
            or required_env("DB_NAME")
        )

        port = int(
            secret.get(
                "port",
                os.environ.get("DB_PORT", "3306"),
            )
        )

    except Exception:
        log.exception(
            "Failed to load or validate database configuration"
        )
        raise

    log.info(
        "Database configuration retrieved successfully"
    )

    return {
        "host": host,
        "user": username,
        "password": password,
        "database": database,
        "port": port,
        "connect_timeout": 5,
        "read_timeout": 10,
        "write_timeout": 10,
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
        "ssl": {"check_hostname": True},
    }


def db_connection():
    """Create a connection to the MediSlot RDS database."""

    log.info("Opening RDS database connection")

    try:
        connection = pymysql.connect(
            **db_config()
        )

        log.info("RDS connection established")

        return connection

    except Exception:
        log.exception(
            "Failed to establish RDS database connection"
        )
        raise


# ---------------------------------------------------------
# DYNAMODB TABLES
# ---------------------------------------------------------

def refresh_table():
    """Return the refresh-token DynamoDB table."""

    table_name = required_env(
        "REFRESH_TOKEN_TABLE"
    )

    return _dynamodb.Table(table_name)


def denylist_table():
    """Return the access-token denylist DynamoDB table."""

    table_name = required_env(
        "TOKEN_DENYLIST_TABLE"
    )

    return _dynamodb.Table(table_name)


# ---------------------------------------------------------
# TIME AND TOKEN HASHING
# ---------------------------------------------------------

def utc_epoch():
    """Return the current Unix timestamp in seconds."""
    return int(time.time())


def token_hash(token):
    """Return a SHA-256 hash of a token."""
    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------
# ACCESS TOKEN
# ---------------------------------------------------------

def create_access_token(user):
    """Create a signed JWT access token."""

    now = utc_epoch()

    payload = {
        "sub": str(user["user_id"]),
        "role": str(user["role"]).upper(),
        "iat": now,
        "exp": now + 900,
        "jti": secrets.token_urlsafe(18),
    }

    log.info("Signing access token")

    token = jwt.encode(
        payload,
        jwt_secret(),
        algorithm="HS256",
    )

    log.info("Access token created successfully")

    return token


# ---------------------------------------------------------
# ACCESS AND REFRESH TOKENS
# ---------------------------------------------------------

def create_tokens(user):
    """Create an access token and persist a refresh token."""

    now = utc_epoch()

    log.info("Starting access-token generation")

    access_token = create_access_token(user)

    log.info("Access-token generation completed")

    refresh_token = secrets.token_urlsafe(48)

    refresh_expires = now + (7 * 24 * 60 * 60)

    log.info(
        "Writing refresh-token record to DynamoDB"
    )

    try:
        refresh_table().put_item(
            Item={
                "token_hash": token_hash(refresh_token),
                "user_id": str(user["user_id"]),
                "role": str(user["role"]).upper(),
                "created_at": now,
                "expires_at": refresh_expires,
            },
            ConditionExpression=(
                "attribute_not_exists(token_hash)"
            ),
        )

    except Exception:
        log.exception(
            "Failed to store refresh-token record in DynamoDB"
        )
        raise

    log.info(
        "Refresh-token record stored successfully"
    )

    return (
        access_token,
        refresh_token,
        refresh_expires,
    )


# ---------------------------------------------------------
# BEARER TOKEN
# ---------------------------------------------------------

def get_bearer_token(event):
    """Extract a Bearer token from request headers."""

    headers = event.get("headers") or {}

    authorization = (
        headers.get("authorization")
        or headers.get("Authorization")
        or ""
    )

    if not isinstance(authorization, str):
        return None

    if not authorization.lower().startswith("bearer "):
        return None

    token = authorization[7:].strip()

    return token or None


# ---------------------------------------------------------
# ACCESS TOKEN VALIDATION
# ---------------------------------------------------------

def decode_access_token(token, check_denylist=True):
    """Decode and validate a JWT access token."""

    claims = jwt.decode(
        token,
        jwt_secret(),
        algorithms=["HS256"],
        options={
            "require": [
                "sub",
                "role",
                "iat",
                "exp",
                "jti",
            ]
        },
    )

    if claims.get("role") not in {
        "PATIENT",
        "DOCTOR",
        "ADMIN",
    }:
        raise jwt.InvalidTokenError(
            "Invalid role"
        )

    if check_denylist:
        log.info(
            "Checking access-token denylist in DynamoDB"
        )

        result = denylist_table().get_item(
            Key={
                "jti": claims["jti"],
            },
            ConsistentRead=True,
        )

        item = result.get("Item")

        if (
            item
            and int(item.get("expires_at", 0)) > utc_epoch()
        ):
            raise jwt.InvalidTokenError(
                "Token revoked"
            )

    return claims


# ---------------------------------------------------------
# LOGOUT / TOKEN REVOCATION
# ---------------------------------------------------------

def save_jti_to_denylist(jti, expiry, user_id):
    """Store a token identifier in the denylist."""

    now = utc_epoch()
    expiry = int(expiry)

    if expiry <= now:
        return

    log.info(
        "Writing revoked access-token identifier to DynamoDB"
    )

    denylist_table().put_item(
        Item={
            "jti": str(jti),
            "user_id": str(user_id),
            "expires_at": expiry,
            "revoked_at": now,
        }
    )

    log.info(
        "Access-token identifier stored in denylist"
    )


# ---------------------------------------------------------
# EMAIL AND PASSWORD VALIDATION
# ---------------------------------------------------------

def email_value(value):
    """Normalize an email address."""
    return str(value or "").strip().lower()


def valid_password(password):
    """Validate the project's password requirements."""

    return (
        isinstance(password, str)
        and 10 <= len(password) <= 72
        and any(ch.isalpha() for ch in password)
        and any(ch.isdigit() for ch in password)
    )


# ---------------------------------------------------------
# GENERIC SERVER ERROR
# ---------------------------------------------------------

def generic_server_error(
    message="Authentication request failed"
):
    """Log the active exception and return a generic response."""

    log.exception(message)

    return response(
        500,
        {"message": "Internal server error"},
    )