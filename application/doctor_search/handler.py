from application.common.http import response, request_method, not_implemented

def lambda_handler(event, context):
    if request_method(event) == "GET":
        # Implement parameterized SQL using your actual doctors table/column names.
        return not_implemented("Doctor search")
    return response(405, {"error": "METHOD_NOT_ALLOWED"})
