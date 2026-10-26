from application.common.http import response, request_method, not_implemented

def lambda_handler(event, context):
    if request_method(event) == "GET":
        # Add parameterized reporting queries after confirming schema and access rules.
        return not_implemented("Reporting")
    return response(405, {"error": "METHOD_NOT_ALLOWED"})
