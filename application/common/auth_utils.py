"""Shared authentication utilities for MediSlot Lambda functions."""

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


# ---------------------------------------------------------------------
# AWS CLIENT CONFIGURATION
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# HTTP RESPONSE
# ---------------------------------------------------------------------

def response(status, body):
    """Create a response compatible with API Gateway HTTP API."""

    return {
        "statusCode": int(status),
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


# ---------------------------------------------------------------------
# REQUEST BODY
# ---------------------------------------------------------------------

def body_json(event):
    """Parse a JSON object from an API Gateway request."""

    raw = event.get("body") or "{}"

    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must contain valid encoded JSON.") from exc

    if isinstance(raw, dict):
        value = raw
    elif isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
    else:
        raise ValueError("Request body must be valid JSON.")

    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object.")

    return value


# ---------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# ---------------------------------------------------------------------

def required_env(name):
    """Return a required environment variable."""

    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


# ---------------------------------------------------------------------
# JWT SIGNING SECRET
# ---------------------------------------------------------------------

@lru_cache(maxsize=1)
def jwt_secret():
    """Retrieve and cache the JWT signing secret from SSM Parameter Store."""

    parameter_name = required_env("JWT_SECRET_PARAMETER_NAME")

    log.info("JWT: retrieving signing secret from SSM")

    try:
        result = _ssm.get_parameter(
            Name=parameter_name,
            WithDecryption=True,
        )

        value = result["Parameter"]["Value"]

    except Exception:
        log.exception("JWT: failed to retrieve signing secret from SSM")
        raise

    if not isinstance(value, str) or len(value) < 32:
        raise RuntimeError(
            "JWT signing secret must be a string of at least 32 characters."
        )

    log.info("JWT: signing secret retrieved successfully")

    return value


# ---------------------------------------------------------------------
# DATABASE CONFIGURATION
# ---------------------------------------------------------------------

@lru_cache(maxsize=1)
def db_config():
    """Retrieve and cache the RDS connection configuration."""

    secret_arn = required_env("DATABASE_SECRET_ARN")
    environment_host = required_env("DB_HOST")
    environment_database = required_env("DB_NAME")

    log.info("Database: retrieving credentials from Secrets Manager")

    try:
        result = _secrets.get_secret_value(
            SecretId=secret_arn
        )

    except Exception:
        log.exception(
            "Database: failed to retrieve credentials from Secrets Manager"
        )
        raise

    secret_string = result.get("SecretString")

    if not secret_string:
        raise RuntimeError(
            "Database secret does not contain SecretString."
        )

    try:
        secret = json.loads(secret_string)

    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            "Database secret must contain valid JSON."
        ) from exc

    username = secret.get("username")
    password = secret.get("password")

    if not isinstance(username, str) or not username:
        raise RuntimeError(
            "Database secret is missing a valid username."
        )

    if not isinstance(password, str) or not password:
        raise RuntimeError(
            "Database secret is missing a valid password."
        )

    database_name = (
        secret.get("dbname")
        or secret.get("database")
        or environment_database
    )

    host = secret.get("host") or environment_host

    try:
        port = int(
            secret.get("port", os.environ.get("DB_PORT", "3306"))
        )

    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "Database port must be a valid integer."
        ) from exc

    log.info("Database: credentials retrieved successfully")

    return {
        "host": host,
        "user": username,
        "password": password,
        "database": database_name,
        "port": port,
        "connect_timeout": 5,
        "read_timeout": 8,
        "write_timeout": 8,
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
        "ssl": {"check_hostname": True},
    }


def db_connection():
    """Create an RDS MySQL connection."""

    config = db_config()

    log.info("Database: connecting to RDS")

    try:
        connection = pymysql.connect(**config)

    except Exception:
        log.exception("Database: RDS connection failed")
        raise

    log.info("Database: RDS connection established")

    return connection


# ---------------------------------------------------------------------
# DYNAMODB TABLES
# ---------------------------------------------------------------------

@lru_cache(maxsize=8)
def _table(table_name):
    """Return a cached DynamoDB table resource."""

    return _dynamodb.Table(table_name)


def refresh_table():
    """Return the refresh-token DynamoDB table."""

    return _table(required_env("REFRESH_TOKEN_TABLE"))


def denylist_table():
    """Return the access-token denylist DynamoDB table."""

    return _table(required_env("TOKEN_DENYLIST_TABLE"))


# ---------------------------------------------------------------------
# TIME AND TOKEN HASHING
# ---------------------------------------------------------------------

def utc_epoch():
    """Return the current Unix timestamp in seconds."""

    return int(time.time())


def token_hash(token):
    """Hash a token before storing it in DynamoDB."""

    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------
# ACCESS TOKEN GENERATION
# ---------------------------------------------------------------------

def create_access_token(user):
    """Create a signed JWT access token valid for 15 minutes."""

    log.info("JWT: creating access token")

    now = utc_epoch()

    payload = {
        "sub": str(user["user_id"]),
        "role": str(user["role"]).upper(),
        "iat": now,
        "exp": now + 900,
        "jti": secrets.token_urlsafe(18),
    }

    token = jwt.encode(
        payload,
        jwt_secret(),
        algorithm="HS256",
    )

    log.info("JWT: access token created successfully")

    return token


# ---------------------------------------------------------------------
# ACCESS AND REFRESH TOKEN GENERATION
# ---------------------------------------------------------------------

def create_tokens(user):
    """Create an access token and persist a hashed refresh token."""

    now = utc_epoch()

    log.info("Tokens: generating access token")

    access_token = create_access_token(user)

    log.info("Tokens: access token generated")

    refresh_token = secrets.token_urlsafe(48)

    expires = now + (7 * 24 * 60 * 60)

    item = {
        "token_hash": token_hash(refresh_token),
        "user_id": str(user["user_id"]),
        "role": str(user["role"]).upper(),
        "created_at": now,
        "expires_at": expires,
    }

    log.info("Tokens: storing refresh-token record in DynamoDB")

    try:
        refresh_table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(token_hash)",
        )

    except Exception:
        log.exception(
            "Tokens: failed to store refresh-token record in DynamoDB"
        )
        raise

    log.info("Tokens: refresh-token record stored successfully")

    return access_token, refresh_token, expires


# ---------------------------------------------------------------------
# BEARER TOKEN EXTRACTION
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# ACCESS TOKEN VALIDATION
# ---------------------------------------------------------------------

def decode_access_token(token, check_denylist=True):
    """Validate a JWT and optionally check whether it has been revoked."""

    if not isinstance(token, str) or not token:
        raise jwt.InvalidTokenError("Missing access token")

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
        raise jwt.InvalidTokenError("Invalid role")

    if check_denylist:
        log.info("JWT: checking access-token denylist")

        try:
            result = denylist_table().get_item(
                Key={"jti": claims["jti"]},
                ConsistentRead=True,
            )

        except Exception:
            log.exception(
                "JWT: failed to check access-token denylist"
            )
            raise

        item = result.get("Item")

        if item and int(item.get("expires_at", 0)) > utc_epoch():
            raise jwt.InvalidTokenError("Token revoked")

    return claims


# ---------------------------------------------------------------------
# ACCESS TOKEN REVOCATION
# ---------------------------------------------------------------------

def save_jti_to_denylist(jti, expiry, user_id):
    """Store an access-token ID in the denylist until it expires."""

    now = utc_epoch()
    expiry = int(expiry)

    if expiry <= now:
        return

    log.info("JWT: saving revoked access-token ID to DynamoDB")

    try:
        denylist_table().put_item(
            Item={
                "jti": str(jti),
                "user_id": str(user_id),
                "expires_at": expiry,
                "revoked_at": now,
            }
        )

    except Exception:
        log.exception(
            "JWT: failed to save revoked access-token ID"
        )
        raise

    log.info("JWT: revoked access-token ID saved successfully")


# ---------------------------------------------------------------------
# INPUT HELPERS
# ---------------------------------------------------------------------

def email_value(value):
    """Normalize an email address."""

    return str(value or "").strip().lower()


def valid_password(password):
    """Check the project's existing password length and composition rules."""

    return (
        isinstance(password, str)
        and 10 <= len(password) <= 72
        and any(character.isalpha() for character in password)
        and any(character.isdigit() for character in password)
    )


# ---------------------------------------------------------------------
# STANDARD ERROR RESPONSE
# ---------------------------------------------------------------------

def generic_server_error(message="Authentication request failed"):
    """Log the current exception and return a generic HTTP 500 response."""

    log.exception(message)

    return response(
        500,
        {"message": "Internal server error"},
    )
