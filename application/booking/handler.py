from application.common.http import response, request_method, request_body, not_implemented

def lambda_handler(event, context):
    if request_method(event) != "POST":
        return response(405, {"error": "METHOD_NOT_ALLOWED"})
    try:
        body = request_body(event)
    except ValueError as exc:
        return response(400, {"error": "INVALID_JSON", "message": str(exc)})
    if not body:
        return response(400, {"error": "EMPTY_BODY", "message": "Booking details are required."})
    # Do not book with a simple read-then-write. Implement transaction/conditional
    # capacity enforcement and idempotency using the actual table key schema.
    return not_implemented("Appointment booking")
