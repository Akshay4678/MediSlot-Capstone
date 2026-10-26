from application.common.http import response, request_method, not_implemented

def lambda_handler(event, context):
    if request_method(event) in ("GET", "POST", "PATCH"):
        # Add your notification provider and actual dispatch-table schema here.
        return not_implemented("Notification management")
    return response(405, {"error": "METHOD_NOT_ALLOWED"})
