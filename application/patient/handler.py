from application.common.http import response, request_method, not_implemented

def lambda_handler(event, context):
    if request_method(event) in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        # Enforce patient identity/ownership before reading or modifying records.
        return not_implemented("Patient management")
    return response(405, {"error": "METHOD_NOT_ALLOWED"})
