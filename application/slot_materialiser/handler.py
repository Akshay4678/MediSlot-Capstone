from application.common.http import response, request_method, not_implemented

def lambda_handler(event, context):
    # Intended for scheduled/internal invocation, not a public endpoint.
    if request_method(event) in ("GET", "POST"):
        return not_implemented("Slot materialisation")
    return response(405, {"error": "METHOD_NOT_ALLOWED"})
