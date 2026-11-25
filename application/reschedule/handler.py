from application.common.api import ApiError
from application.common.auth_context import claims
from application.common.http import request_body, response
from application.database.connection import get_connection
from application.reschedule.service import start

def lambda_handler(event, context):
    try:
        c = claims(event); patient_id = str(c["user_id"])
        appointment_id = ((event.get("pathParameters") or {}).get("appointment_id") or "").strip()
        body = request_body(event)
        conn = get_connection()
        try:
            status, result = start(conn, patient_id, appointment_id, body.get("target_slot_id"))
            return response(status, result)
        finally:
            conn.close()
    except ApiError as exc:
        return response(exc.status, {"error": exc.code, "message": exc.message})
    except Exception:
        import logging; logging.getLogger().exception("reschedule start failed")
        return response(500, {"error": "INTERNAL_SERVER_ERROR", "message": "The request could not be completed."})
