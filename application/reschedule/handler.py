import uuid
from datetime import datetime, timezone
from application.common.http import response, request_method, request_body
from application.common.auth_context import claims
from application.database.connection import get_connection

def lambda_handler(event, context):
    if request_method(event)!="POST": return response(405,{"error":"METHOD_NOT_ALLOWED"})
    identity=claims(event)
    if not identity["user_id"] or identity["role"] not in ("patient","admin"):
        return response(401,{"error":"UNAUTHORIZED"})
    try: body=request_body(event)
    except ValueError as e: return response(400,{"error":"INVALID_JSON","message":str(e)})
    appointment_id=(event.get("pathParameters") or {}).get("appointment_id") or body.get("appointment_id")
    required=("new_slot_id","starts_at_utc","ends_at_utc","timezone_name")
    if not appointment_id or any(not body.get(k) for k in required):
        return response(400,{"error":"MISSING_FIELDS","fields":["appointment_id"]+[k for k in required if not body.get(k)]})
    try:
        start=datetime.fromisoformat(str(body["starts_at_utc"]).replace("Z","+00:00"))
        end=datetime.fromisoformat(str(body["ends_at_utc"]).replace("Z","+00:00"))
        if end<=start: raise ValueError()
    except ValueError: return response(400,{"error":"INVALID_TIME"})
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            if identity["role"]=="patient":
                cur.execute("""SELECT a.*,p.user_id FROM appointments a JOIN patients p ON p.patient_id=a.patient_id
                               WHERE a.appointment_id=%s FOR UPDATE""",(appointment_id,))
            else:
                cur.execute("SELECT * FROM appointments WHERE appointment_id=%s FOR UPDATE",(appointment_id,))
            old=cur.fetchone()
            if not old or (identity["role"]=="patient" and str(old["user_id"])!=identity["user_id"]):
                conn.rollback(); return response(404,{"error":"APPOINTMENT_NOT_FOUND"})
            if old["status"] not in ("PENDING","CONFIRMED") or int(old["reschedule_count"])>=2:
                conn.rollback(); return response(409,{"error":"NOT_RESCHEDULABLE"})
            # Must reserve new slot capacity conditionally in DynamoDB and release old capacity
            # with compensation if the RDS transaction fails. This package intentionally fails closed
            # until the actual SlotCapacityTable key schema and reservation protocol are wired in.
            conn.rollback()
            return response(501,{"error":"CAPACITY_WORKFLOW_NOT_CONFIGURED",
                "message":"Connect the DynamoDB conditional capacity reservation and compensation workflow before enabling rescheduling."})
    except Exception:
        conn.rollback(); print("Reschedule lookup failed; inspect CloudWatch logs.")
        return response(500,{"error":"INTERNAL_SERVER_ERROR"})
    finally: conn.close()
