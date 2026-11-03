"""The acting user, read ONLY from the API Gateway authorizer context.

Never read userId or role from the request body, query string or headers.
"""


def claims(event):
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    identity = authorizer.get("lambda") or authorizer
    return {"user_id": identity.get("userId"), "role": identity.get("role")}
