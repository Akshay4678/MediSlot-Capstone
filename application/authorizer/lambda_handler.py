"""
MediSlot API Gateway Lambda Authorizer

Verifies JWT access tokens and requires a matching active-list entry.

Required environment variables:
    JWT_SECRET_PARAMETER_NAME
    TOKEN_ACTIVELIST_TABLE
    JWT_ISSUER (optional; defaults to "medislot")

DynamoDB:
    Token active-list table partition key: jti (String)
"""

import base64
import hashlib
import hmac
import json
import os
import time
from functools import lru_cache

import boto3
from botocore.config import Config


# AWS clients
_cfg = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2})
ssm = boto3.client("ssm", config=_cfg)
dynamodb = boto3.resource("dynamodb", config=_cfg)


def decode_base64(value):
    """Decode a URL-safe Base64 string."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@lru_cache(maxsize=1)
def get_jwt_secret():
    """Read the JWT signing secret from AWS Systems Manager Parameter Store."""
    parameter_name = os.environ["JWT_SECRET_PARAMETER_NAME"]

    parameter = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True
    )

    return parameter["Parameter"]["Value"].encode()


def is_token_active(token_id, subject=None):
    """Return True only when the JTI exists and has not expired in the active list."""
    table_name = os.environ.get("TOKEN_ACTIVELIST_TABLE")
    if not table_name:
        raise RuntimeError("TOKEN_ACTIVELIST_TABLE is not configured")
    item = dynamodb.Table(table_name).get_item(
        Key={"jti": token_id}, ConsistentRead=True
    ).get("Item")
    if not item or int(item.get("expires_at", 0)) <= int(time.time()):
        return False
    if subject is not None and str(item.get("user_id")) != str(subject):
        return False
    return True


def verify_access_token(token, check_active=True):
    """Verify the JWT signature, expiry, issuer, and required claims."""

    # A JWT must contain three parts.
    token_parts = token.split(".")

    if len(token_parts) != 3:
        raise ValueError("Malformed JWT")

    header_part, payload_part, signature_part = token_parts

    # Re-create the signature using our stored secret.
    message = f"{header_part}.{payload_part}".encode()

    expected_signature = hmac.new(
        get_jwt_secret(),
        message,
        hashlib.sha256
    ).digest()

    supplied_signature = decode_base64(signature_part)

    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise ValueError("Invalid signature")

    # Decode the JWT header and payload.
    header = json.loads(decode_base64(header_part))
    claims = json.loads(decode_base64(payload_part))

    # Only HS256-signed tokens are accepted.
    if header.get("alg") != "HS256":
        raise ValueError("Unsupported JWT algorithm")

    # Check token timestamps.
    current_time = int(time.time())
    expiry_time = int(claims.get("exp", 0))
    issued_time = int(claims.get("iat", 0))

    if expiry_time <= current_time or issued_time > current_time + 60:
        raise ValueError("Expired or invalid token time")

    # Check who issued the token.
    expected_issuer = os.environ.get("JWT_ISSUER", "medislot")

    if claims.get("iss") != expected_issuer:
        raise ValueError("Invalid issuer")

    # Check that all required claims are present and valid.
    user_id = claims.get("sub")
    user_role = claims.get("role")
    token_id = claims.get("jti")

    allowed_roles = ("patient", "doctor", "admin")

    if not user_id or user_role not in allowed_roles or not token_id:
        raise ValueError("Required claims missing")

    # Normal API authorization requires the JTI to exist in TOKEN_ACTIVELIST.
    # Logout can set check_active=False after verifying the signature and claims,
    # so it can remove the current JTI from the list.
    if check_active and not is_token_active(token_id, user_id):
        raise ValueError("Token is not active")

    return claims


def _deny(event):
    """Reject the request in the format the API type expects.

    HTTP API (payload 2.0, simple responses): raising an exception makes API
    Gateway return 500, so return isAuthorized=False instead.
    REST API: raising "Unauthorized" is the documented way to return 401.
    """
    if event.get("version") == "2.0":
        return {"isAuthorized": False}
    raise Exception("Unauthorized")


def lambda_handler(event, context):
    """Entry point called by API Gateway."""

    # Read the Authorization header.
    headers = event.get("headers") or {}

    authorization = (
        headers.get("authorization")
        or headers.get("Authorization")
        or ""
    )

    # The request must contain a Bearer token.
    if not authorization.lower().startswith("bearer "):
        return _deny(event)

    access_token = authorization.split(None, 1)[1].strip()

    # Validate the token.
    try:
        claims = verify_access_token(access_token)

    except Exception as error:
        print(f"Authorizer rejected token: {type(error).__name__}: {error}")
        return _deny(event)

    user_id = str(claims["sub"])
    user_role = str(claims["role"])
    token_id = str(claims["jti"])

    # HTTP API using the simple Lambda authorizer response format.
    if event.get("version") == "2.0":
        return {
            "isAuthorized": True,
            "context": {
                "userId": user_id,
                "role": user_role,
                "jti": token_id
            }
        }

    # REST API or IAM-policy authorizer response format.
    method_arn = event.get("methodArn") or event.get("routeArn", "*")

    if "/" in method_arn:
        resource_arn = method_arn.rsplit("/", 2)[0] + "/*/*"
    else:
        resource_arn = "*"

    return {
        "principalId": user_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": resource_arn
                }
            ]
        },
        "context": {
            "userId": user_id,
            "role": user_role,
            "jti": token_id
        }
    }
