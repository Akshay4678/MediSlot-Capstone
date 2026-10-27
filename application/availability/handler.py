from datetime import datetime, timezone
from application.common.http import response, request_method
from application.database.connection import get_connection

def lambda_handler(event, context):
    if request_method(event) != "GET":
        return response(405, {"error":"METHOD_NOT_ALLOWED"})
    q = event.get("queryStringParameters") or {}
    doctor_id = (q.get("doctor_id") or "").strip()
    date = (q.get("date") or "").strip()  # YYYY-MM-DD in doctor's local timezone
    if not doctor_id or not date:
        return response(400, {"error":"MISSING_PARAMETERS","message":"doctor_id and date (YYYY-MM-DD) are required."})
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return response(400, {"error":"INVALID_DATE","message":"date must use YYYY-MM-DD."})
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT ap.availability_pattern_id, ap.consultation_type_id,
                                  ap.weekday, ap.start_local_time, ap.end_local_time,
                                  ap.timezone_name, ap.effective_from, ap.effective_until,
                                  ct.name AS consultation_type, ct.duration_minutes,
                                  ct.fee, ct.capacity
                           FROM availability_patterns ap
                           JOIN consultation_types ct ON ct.consultation_type_id=ap.consultation_type_id
                           WHERE ap.doctor_id=%s AND ap.active=TRUE AND ct.is_active=TRUE
                             AND ap.weekday=%s AND ap.effective_from<=%s
                             AND (ap.effective_until IS NULL OR ap.effective_until>=%s)
                           ORDER BY ap.start_local_time""",
                        (doctor_id, target_date.isoweekday(), target_date, target_date))
            patterns = cur.fetchall()
            cur.execute("""SELECT start_local_time,end_local_time,reason FROM blackout_dates
                           WHERE doctor_id=%s AND local_date=%s""",(doctor_id,target_date))
            blackouts = cur.fetchall()
            cur.execute("""SELECT slot_id, starts_at_utc, ends_at_utc, status
                           FROM appointments WHERE doctor_id=%s AND DATE(starts_at_utc)=%s
                           AND status IN ('PENDING','CONFIRMED')""",(doctor_id,target_date))
            booked = cur.fetchall()
        # Patterns are schedules, not guaranteed live slots. Slot capacity is stored in DynamoDB,
        # so this response explicitly labels the output as schedule windows.
        return response(200, {"doctor_id":doctor_id,"date":date,"timezone_note":"Times in patterns are local to each pattern's timezone.",
                              "schedule_windows":patterns,"blackout_windows":blackouts,
                              "appointments":booked,
                              "availability_source":"schedule plus RDS appointments; verify DynamoDB slot capacity before booking"})
    except Exception:
        print("Availability query failed; inspect CloudWatch logs.")
        return response(500,{"error":"INTERNAL_SERVER_ERROR"})
    finally:
        conn.close()
