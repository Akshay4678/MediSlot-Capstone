"""Authentication endpoints. Connect this to your existing auth/token design."""
from application.common.http import response, request_method, request_body, not_implemented

def lambda_handler(event, context):
    method = request_method(event)
    try:
        body = request_body(event) if method in ("POST", "PUT", "PATCH") else {}
    except ValueError as exc:
        return response(400, {"error": "INVALID_JSON", "message": str(exc)})

    if method == "POST":
        # Do not issue tokens until password hashing, user lookup, and JWT signing
        # use the project's existing auth implementation and secret configuration.
        return not_implemented("Login/register")
    return response(405, {"error": "METHOD_NOT_ALLOWED"})
