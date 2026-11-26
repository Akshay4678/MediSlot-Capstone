import json
from urllib.parse import unquote
from application.common.api import ApiError
from application.common.http import response
from application.waitlist.store import default_store
from application.booking.deps import default_deps
from application.waitlist import service

_store = None

def _get_store():
    global _store
    if _store is None: _store = default_store()
    return _store

def lambda_handler(event, context):
    method = (event.get("httpMethod") or "").upper()
    path = event.get("path", "")
    body = json.loads(event.get("body") or "{}") if isinstance(event.get("body"), str) else (event.get("body") or {})
    claims = ((event.get("requestContext") or {}).get("authorizer") or {})
    patient_id = claims.get("userId") or claims.get("principalId")
    try:
        from application.database.connection import get_connection
        conn = get_connection()
        try:
            if method == "POST" and path.endswith("/waitlist"):
                return response(*service.join(conn, _get_store(), patient_id, body.get("slot_id"), default_deps().events))
            if method == "DELETE" and "/waitlist/" in path:
                slot_id = unquote(path.rstrip("/").split("/")[-1]).strip()
                return response(*service.leave(_get_store(), patient_id, slot_id, default_deps().events))
            if method == "GET" and path.endswith("/waitlist"):
                return response(*service.list_for_patient(conn, _get_store(), patient_id))
            return response(404, {"error": "NOT_FOUND"})
        finally:
            conn.close()
    except ApiError as exc:
        return response(exc.status, {"error": exc.code, "message": exc.message})
    except Exception:
        import logging; logging.getLogger().exception("waitlist request failed")
        return response(500, {"error": "INTERNAL_ERROR"})
