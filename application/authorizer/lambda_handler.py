import logging
import jwt
from application.common.auth_utils import get_bearer_token, decode_access_token, response

log = logging.getLogger()
log.setLevel(logging.INFO)


def _policy(principal_id, effect, resource, claims=None):
    context = {}
    if claims:
        # API Gateway authorizer context values must be scalar values.
        context = {"userId": str(claims["sub"]), "role": str(claims["role"])}
    return {
        "principalId": str(principal_id),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{
                "Action": "execute-api:Invoke",
                "Effect": effect,
                "Resource": resource,
            }],
        },
        "context": context,
    }


def lambda_handler(event, context):
    token = get_bearer_token(event)
    if not token:
        raise Exception("Unauthorized")
    try:
        claims = decode_access_token(token, check_denylist=True)
        method_arn = event.get("methodArn") or event.get("routeArn")
        if not method_arn:
            raise ValueError("API Gateway method ARN is missing.")
        return _policy(claims["sub"], "Allow", method_arn, claims)
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise Exception("Unauthorized")
    except Exception:
        # Fail closed if revocation lookup or secret retrieval fails.
        log.exception("Authorizer validation failed")
        raise Exception("Unauthorized")
