from application.common.http import response, request_method, not_implemented

def lambda_handler(event, context):
    if request_method(event) in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        # Implement after confirming doctors table schema and admin/doctor permissions.
        return not_implemented("Doctor management")
    return response(405, {"error": "METHOD_NOT_ALLOWED"})
