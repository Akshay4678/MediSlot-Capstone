from application.common.http import response, request_method, request_body, not_implemented

def lambda_handler(event, context):
    if request_method(event) != "POST":
        return response(405, {"error": "METHOD_NOT_ALLOWED"})
    try:
        body = request_body(event)
    except ValueError as exc:
        return response(400, {"error": "INVALID_JSON", "message": str(exc)})
    if not body:
        return response(400, {"error": "EMPTY_BODY", "message": "New slot details are required."})
    # Reschedule must atomically reserve the new slot and release the old slot.
    return not_implemented("Appointment rescheduling")
