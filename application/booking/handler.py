import uuid
from datetime import datetime
from application.common.http import response, request_method, request_body
from application.common.auth_context import claims
from application.database.connection import get_connection

def lambda_handler(event, context):
    if request_method(event) != "POST":
        return response(405,{"error":"METHOD_NOT_ALLOWED"})
    identity = claims(event)
    if not identity["user_id"] or identity["role"] != "patient":
        return response(401,{"error":"UNAUTHORIZED","message":"A patient access token is required."})
    try: body = request_body(event)
    except ValueError as e: return response(400,{"error":"INVALID_JSON","message":str(e)})
    required=("doctor_id","consultation_type_id","slot_id","starts_at_utc","ends_at_utc","timezone_name","idempotency_key")
    missing=[k for k in required if not body.get(k)]
    if missing: return response(400,{"error":"MISSING_FIELDS","fields":missing})
    try:
        start=datetime.fromisoformat(str(body["starts_at_utc"]).replace("Z","+00:00"))
        end=datetime.fromisoformat(str(body["ends_at_utc"]).replace("Z","+00:00"))
        if end <= start: raise ValueError()
    except ValueError:
        return response(400,{"error":"INVALID_TIME","message":"starts_at_utc and ends_at_utc must be valid ISO timestamps and end must be later."})
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT patient_id FROM patients WHERE user_id=%s",(identity["user_id"],))
            patient=cur.fetchone()
            if not patient: return response(403,{"error":"PATIENT_PROFILE_REQUIRED"})
            # Idempotency at the RDS record level prevents duplicate appointments for a repeated key.
            cur.execute("SELECT appointment_id,status FROM appointments WHERE idempotency_key=%s",(str(body["idempotency_key"]),))
            prior=cur.fetchone()
            if prior:
                conn.rollback()
                return response(200,{"message":"Idempotent booking result","appointment":prior})
            cur.execute("""SELECT ct.doctor_id,ct.duration_minutes,ct.capacity,ct.is_active,
                                  d.is_active AS doctor_active
                           FROM consultation_types ct JOIN doctors d ON d.doctor_id=ct.doctor_id
                           WHERE ct.consultation_type_id=%s""",(str(body["consultation_type_id"]),))
            ct=cur.fetchone()
            if not ct or not ct["is_active"] or not ct["doctor_active"] or str(ct["doctor_id"])!=str(body["doctor_id"]):
                conn.rollback()
                return response(400,{"error":"INVALID_CONSULTATION_TYPE"})
            # Critical: slot capacity must be reserved with a DynamoDB conditional write before this insert.
            # Do not deploy this booking path for production until that reservation call is wired in.
            appointment_id=str(uuid.uuid4())
            cur.execute("""INSERT INTO appointments
                (appointment_id,patient_id,doctor_id,consultation_type_id,slot_id,starts_at_utc,ends_at_utc,timezone_name,status,idempotency_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)""",
                (appointment_id,patient["patient_id"],str(body["doctor_id"]),str(body["consultation_type_id"]),
                 str(body["slot_id"]),start.astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None),
                 end.astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None),
                 str(body["timezone_name"]),str(body["idempotency_key"])))
        conn.commit()
        return response(201,{"appointment_id":appointment_id,"status":"PENDING","message":"Appointment created pending capacity/workflow confirmation."})
    except Exception:
        conn.rollback()
        print("Booking failed; inspect CloudWatch logs.")
        return response(500,{"error":"INTERNAL_SERVER_ERROR"})
    finally: conn.close()
