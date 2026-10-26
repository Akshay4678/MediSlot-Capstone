from application.common.http import response, request_method, not_implemented

def lambda_handler(event, context):
    if request_method(event) == "GET":
        # Availability must read the canonical slot source and handle time zones.
        return not_implemented("Availability lookup")
    return response(405, {"error": "METHOD_NOT_ALLOWED"})
