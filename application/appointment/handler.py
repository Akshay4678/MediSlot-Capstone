from application.common.http import response, request_method, request_body
from application.common.auth_context import claims
from application.database.connection import get_connection

def lambda_handler(event, context):
    method=request_method(event); identity=claims(event)
    if not identity["user_id"]:
        return response(401,{"error":"UNAUTHORIZED"})
    path=(event.get("rawPath") or event.get("path") or "").rstrip("/")
    appointment_id=(event.get("pathParameters") or {}).get("appointment_id")
    if not appointment_id and "/appointments/" in path:
        appointment_id=path.split("/appointments/",1)[1].split("/",1)[0]
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            if method=="GET":
                if identity["role"]=="patient":
                    cur.execute("""SELECT a.* FROM appointments a JOIN patients p ON p.patient_id=a.patient_id
                                   WHERE p.user_id=%s AND (%s IS NULL OR a.appointment_id=%s)
                                   ORDER BY a.starts_at_utc DESC LIMIT 200""",
                                (identity["user_id"],appointment_id,appointment_id))
                elif identity["role"]=="doctor":
                    cur.execute("""SELECT a.* FROM appointments a JOIN doctors d ON d.doctor_id=a.doctor_id
                                   WHERE d.user_id=%s AND (%s IS NULL OR a.appointment_id=%s)
                                   ORDER BY a.starts_at_utc DESC LIMIT 200""",
                                (identity["user_id"],appointment_id,appointment_id))
                elif identity["role"]=="admin":
                    if appointment_id:
                        cur.execute("SELECT * FROM appointments WHERE appointment_id=%s",(appointment_id,))
                    else:
                        cur.execute("SELECT * FROM appointments ORDER BY starts_at_utc DESC LIMIT 200")
                else: return response(403,{"error":"FORBIDDEN"})
                rows=cur.fetchall()
                if appointment_id:
                    return response(200,{"appointment":rows[0]}) if rows else response(404,{"error":"APPOINTMENT_NOT_FOUND"})
                return response(200,{"items":rows,"count":len(rows)})
            if method=="PATCH":
                try: body=request_body(event)
                except ValueError as e: return response(400,{"error":"INVALID_JSON","message":str(e)})
                # Reschedule time/slot changes must go through the reschedule workflow.
                if any(k in body for k in ("slot_id","starts_at_utc","ends_at_utc","doctor_id")):
                    return response(400,{"error":"USE_RESCHEDULE_ENDPOINT","message":"Use the reschedule workflow to change slot/time/doctor."})
                return response(400,{"error":"NO_SUPPORTED_FIELDS","message":"No general appointment fields are patchable here."})
            if method=="DELETE":
                if not appointment_id: return response(400,{"error":"APPOINTMENT_ID_REQUIRED"})
                try: body=request_body(event)
                except ValueError: body={}
                if identity["role"] not in ("patient","admin"):
                    return response(403,{"error":"FORBIDDEN"})
                if identity["role"]=="patient":
                    cur.execute("""UPDATE appointments a JOIN patients p ON p.patient_id=a.patient_id
                                   SET a.status='CANCELLED',a.cancellation_reason=%s
                                   WHERE a.appointment_id=%s AND p.user_id=%s AND a.status IN ('PENDING','CONFIRMED')""",
                                (str(body.get("reason","Patient cancellation"))[:500],appointment_id,identity["user_id"]))
                else:
                    cur.execute("""UPDATE appointments SET status='CANCELLED',cancellation_reason=%s
                                   WHERE appointment_id=%s AND status IN ('PENDING','CONFIRMED')""",
                                (str(body.get("reason","Admin cancellation"))[:500],appointment_id))
                changed=cur.rowcount
                conn.commit()
                return response(200,{"message":"Appointment cancelled."}) if changed else response(409,{"error":"NOT_CANCELLABLE_OR_NOT_FOUND"})
            return response(405,{"error":"METHOD_NOT_ALLOWED"})
    except Exception:
        conn.rollback()
        print("Appointment handler failed; inspect CloudWatch logs.")
        return response(500,{"error":"INTERNAL_SERVER_ERROR"})
    finally: conn.close()
