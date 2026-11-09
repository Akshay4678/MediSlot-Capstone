"""MediSlot API Gateway Lambda Authorizer (REST API, REQUEST type, no result cache).

Outcomes
  * missing / malformed / tampered / expired / replaced / logged-out token
        -> raise Exception("Unauthorized")  -> API Gateway answers 401
  * valid token, wrong role for the resource -> explicit Deny policy -> 403
  * valid token, allowed role -> Allow policy; userId, role and jti are passed in the
    authorizer context. Handlers read the acting user ONLY from that context.

A token is valid only if (1) its HS256 signature, expiry, issuer and claims check out
and (2) its jti is the ONE entry in the TOKEN_ACTIVELIST table for that user. Issuing a
new access token (login / refresh) overwrites that entry, so the previous token stops
working immediately; logout deletes it.

Required environment variables: JWT_SECRET_PARAMETER_NAME, TOKEN_ACTIVELIST_TABLE,
JWT_ISSUER (optional, default "medislot").
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

_cfg = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})
ssm = boto3.client("ssm", config=_cfg)
dynamodb = boto3.resource("dynamodb", config=_cfg)

ALLOWED_ROLES = ("patient", "doctor")

# Resource patterns (after /<stage>/<METHOD>/) that a role may NOT call.
DENIED_FOR_ROLE = {
    "patient": ["doctors/me", "doctors/me/*"],
    "doctor": ["patients/me", "patients/me/*", "appointments", "appointments/*",
               "slots/*", "waitlist-offers/*"],
}


def decode_base64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@lru_cache(maxsize=1)
def get_jwt_secret():
    """JWT signing secret from SSM Parameter Store (read once per cold start; failures are not cached)."""
    name = os.environ.get("JWT_SECRET_PARAMETER_NAME")
    if not name:
        log.error("JWT_SECRET_PARAMETER_NAME is not set on this Lambda function.")
        raise RuntimeError("JWT_SECRET_PARAMETER_NAME is not configured")
    try:
        parameter = ssm.get_parameter(Name=name, WithDecryption=True)
    except ClientError as error:
        # Name the parameter so the fix is obvious from CloudWatch (never log the value).
        code = error.response.get("Error", {}).get("Code", "Unknown")
        log.error("Cannot read the JWT signing secret from SSM parameter %s (%s). "
                  "Create the SecureString parameter or correct JWT_SECRET_PARAMETER_NAME.", name, code)
        raise
    return parameter["Parameter"]["Value"].encode()


def is_token_active(token_id, subject=None):
    """True only if this jti is the user's current entry in the active list."""
    table_name = os.environ.get("TOKEN_ACTIVELIST_TABLE")
    if not table_name:
        raise RuntimeError("TOKEN_ACTIVELIST_TABLE is not configured")
    if not subject:
        return False
    item = dynamodb.Table(table_name).get_item(Key={"user_id": str(subject)}, ConsistentRead=True).get("Item")
    if not item or int(item.get("expires_at", 0)) <= int(time.time()):
        return False
    return hmac.compare_digest(str(item.get("jti", "")), str(token_id))


def verify_access_token(token, check_active=True):
    """Verify signature, algorithm, expiry, issuer and claims; return the claims."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT")
    header_part, payload_part, signature_part = parts

    expected = hmac.new(get_jwt_secret(), f"{header_part}.{payload_part}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, decode_base64(signature_part)):
        raise ValueError("Invalid signature")

    header = json.loads(decode_base64(header_part))
    claims = json.loads(decode_base64(payload_part))
    if header.get("alg") != "HS256":
        raise ValueError("Unsupported JWT algorithm")

    now = int(time.time())
    if int(claims.get("exp", 0)) <= now or int(claims.get("iat", 0)) > now + 60:
        raise ValueError("Expired or invalid token time")
    if claims.get("iss") != os.environ.get("JWT_ISSUER", "medislot"):
        raise ValueError("Invalid issuer")

    user_id, role, token_id = claims.get("sub"), claims.get("role"), claims.get("jti")
    if not user_id or role not in ALLOWED_ROLES or not token_id:
        raise ValueError("Required claims missing")

    if check_active and not is_token_active(token_id, user_id):
        raise ValueError("Token is not active")
    return claims


def _token_from_event(event):
    """TOKEN authorizers send authorizationToken; REQUEST authorizers send headers."""
    value = event.get("authorizationToken")
    if not value:
        headers = event.get("headers") or {}
        value = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    if not isinstance(value, str) or not value.lower().startswith("bearer "):
        return None
    return value.split(None, 1)[1].strip() or None


def build_policy(claims, method_arn):
    # arn:aws:execute-api:region:account:apiId/stage/METHOD/path...
    arn_prefix, _, rest = method_arn.partition("/")
    base = f"{arn_prefix}/{rest.split('/', 1)[0]}"
    role = str(claims["role"])

    statements = [{"Effect": "Allow", "Action": "execute-api:Invoke", "Resource": f"{base}/*/*"}]
    denied = [f"{base}/*/{pattern}" for pattern in DENIED_FOR_ROLE[role]]
    if denied:
        statements.append({"Effect": "Deny", "Action": "execute-api:Invoke", "Resource": denied})

    return {
        "principalId": str(claims["sub"]),
        "policyDocument": {"Version": "2012-10-17", "Statement": statements},
        "context": {"userId": str(claims["sub"]), "role": role, "jti": str(claims["jti"])},
    }


def lambda_handler(event, context):
    token = _token_from_event(event)
    if not token:
        raise Exception("Unauthorized")  # -> 401
    try:
        claims = verify_access_token(token)
    except (ValueError, KeyError, TypeError) as error:  # a bad token: log the type only, never the token
        log.info("Authorizer rejected token: %s", type(error).__name__)
        raise Exception("Unauthorized")  # -> 401
    except Exception:  # noqa: BLE001 - AWS/config failure, not a bad token: fail closed but make it visible
        log.exception("Authorizer could not verify the token because a dependency failed")
        raise Exception("Unauthorized")  # -> 401
    return build_policy(claims, event["methodArn"])