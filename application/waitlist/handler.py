from application.common.http import response, request_method, request_body, not_implemented

def lambda_handler(event, context):
    method = request_method(event)
    if method not in ("GET", "POST", "DELETE"):
        return response(405, {"error": "METHOD_NOT_ALLOWED"})
    if method == "POST":
        try:
            body = request_body(event)
        except ValueError as exc:
            return response(400, {"error": "INVALID_JSON", "message": str(exc)})
        if not body:
            return response(400, {"error": "EMPTY_BODY", "message": "Waitlist details are required."})
    # Implement against the actual WaitlistTable key/index schema.
    return not_implemented("Waitlist")
