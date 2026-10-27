from application.common.http import response, request_method
from application.common.auth_context import claims
from application.database.connection import get_connection

def lambda_handler(event, context):
    if request_method(event)!="GET": return response(405,{"error":"METHOD_NOT_ALLOWED"})
    identity=claims(event)
    if identity["role"]!="admin": return response(403,{"error":"ADMIN_REQUIRED"})
    q=event.get("queryStringParameters") or {}
    start=q.get("from"); end=q.get("to")
    if bool(start)!=bool(end): return response(400,{"error":"DATE_RANGE_REQUIRED","message":"Supply both from and to as YYYY-MM-DD."})
    where=""; vals=[]
    if start:
        where=" WHERE starts_at_utc >= %s AND starts_at_utc < DATE_ADD(%s, INTERVAL 1 DAY)"
        vals=[start,end]
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status,COUNT(*) AS appointment_count FROM appointments"+where+" GROUP BY status",tuple(vals))
            by_status=cur.fetchall()
            cur.execute("SELECT COUNT(*) AS total_appointments FROM appointments"+where,tuple(vals))
            total=cur.fetchone()
            cur.execute("""SELECT d.doctor_id,d.display_name,COUNT(*) AS appointment_count
                            FROM appointments a JOIN doctors d ON d.doctor_id=a.doctor_id"""+where+
                         " GROUP BY d.doctor_id,d.display_name ORDER BY appointment_count DESC LIMIT 10",tuple(vals))
            top_doctors=cur.fetchall()
        return response(200,{"total_appointments":total["total_appointments"],"by_status":by_status,"top_doctors":top_doctors,"from":start,"to":end})
    except Exception:
        print("Reporting query failed."); return response(500,{"error":"INTERNAL_SERVER_ERROR"})
    finally: conn.close()
