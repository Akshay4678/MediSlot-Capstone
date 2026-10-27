import os
import json
import time
import hashlib
import secrets
import logging
from functools import lru_cache

import boto3
import jwt
import pymysql

log = logging.getLogger()
log.setLevel(logging.INFO)

_ssm = boto3.client("ssm")
_secrets = boto3.client("secretsmanager")
_dynamodb = boto3.resource("dynamodb")


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=str),
    }


def body_json(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    if isinstance(raw, dict):
        value = raw
    else:
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            raise ValueError("Request body must be valid JSON.")
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object.")
    return value


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@lru_cache(maxsize=1)
def jwt_secret():
    name = required_env("JWT_SECRET_PARAMETER_NAME")
    value = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    if len(value) < 32:
        raise RuntimeError("JWT signing secret must be at least 32 characters.")
    return value


@lru_cache(maxsize=1)
def db_config():
    secret_arn = required_env("DATABASE_SECRET_ARN")
    secret = json.loads(_secrets.get_secret_value(SecretId=secret_arn)["SecretString"])
    return {
        "host": secret.get("host") or required_env("DB_HOST"),
        "user": secret["username"],
        "password": secret["password"],
        "database": secret.get("dbname") or secret.get("database") or required_env("DB_NAME"),
        "port": int(secret.get("port", os.environ.get("DB_PORT", "3306"))),
        "connect_timeout": 5,
        "read_timeout": 8,
        "write_timeout": 8,
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
        "ssl": {"check_hostname": True},
    }


def db_connection():
    return pymysql.connect(**db_config())


def refresh_table():
    return _dynamodb.Table(required_env("REFRESH_TOKEN_TABLE"))


def denylist_table():
    return _dynamodb.Table(required_env("TOKEN_DENYLIST_TABLE"))


def utc_epoch():
    return int(time.time())


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(user):
    now = utc_epoch()
    payload = {
        "sub": str(user["user_id"]),
        "role": str(user["role"]).upper(),
        "iat": now,
        "exp": now + 900,
        "jti": secrets.token_urlsafe(18),
    }
    return jwt.encode(payload, jwt_secret(), algorithm="HS256")


def create_tokens(user):
    now = utc_epoch()
    access_token = create_access_token(user)
    refresh_token = secrets.token_urlsafe(48)
    expires = now + 7 * 24 * 60 * 60
    refresh_table().put_item(
        Item={
            "token_hash": token_hash(refresh_token),
            "user_id": str(user["user_id"]),
            "role": str(user["role"]).upper(),
            "created_at": now,
            "expires_at": expires,
        },
        ConditionExpression="attribute_not_exists(token_hash)",
    )
    return access_token, refresh_token, expires


def get_bearer_token(event):
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip() or None


def decode_access_token(token, check_denylist=True):
    claims = jwt.decode(
        token,
        jwt_secret(),
        algorithms=["HS256"],
        options={"require": ["sub", "role", "iat", "exp", "jti"]},
    )
    if claims.get("role") not in {"PATIENT", "DOCTOR", "ADMIN"}:
        raise jwt.InvalidTokenError("Invalid role")
    if check_denylist:
        item = denylist_table().get_item(Key={"jti": claims["jti"]}, ConsistentRead=True).get("Item")
        if item and int(item.get("expires_at", 0)) > utc_epoch():
            raise jwt.InvalidTokenError("Token revoked")
    return claims


def save_jti_to_denylist(jti, expiry, user_id):
    now = utc_epoch()
    if int(expiry) > now:
        denylist_table().put_item(Item={
            "jti": str(jti), "user_id": str(user_id),
            "expires_at": int(expiry), "revoked_at": now,
        })


def email_value(value):
    return str(value or "").strip().lower()


def valid_password(password):
    return (
        isinstance(password, str)
        and 10 <= len(password) <= 72
        and any(ch.isalpha() for ch in password)
        and any(ch.isdigit() for ch in password)
    )


def generic_server_error(message="Authentication request failed"):
    log.exception(message)
    return response(500, {"message": "Internal server error"})
