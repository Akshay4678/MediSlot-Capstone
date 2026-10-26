from application.common.http import response, request_method, request_body, not_implemented

def lambda_handler(event, context):
    method = request_method(event)
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return response(405, {"error": "METHOD_NOT_ALLOWED"})
    if method in ("POST", "PUT", "PATCH"):
        try:
            request_body(event)
        except ValueError as exc:
            return response(400, {"error": "INVALID_JSON", "message": str(exc)})
    # Enforce patient/doctor/admin ownership and valid status transitions.
    return not_implemented("Appointment management")
