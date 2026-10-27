from application.common.http import response, request_method
from application.database.connection import get_connection

def lambda_handler(event, context):
    if request_method(event)!="GET": return response(405,{"error":"METHOD_NOT_ALLOWED"})
    q=event.get("queryStringParameters") or {}
    search=(q.get("search") or "").strip(); city=(q.get("city") or "").strip()
    spec=(q.get("specialisation") or "").strip()
    try: limit=max(1,min(100,int(q.get("limit",50))))
    except ValueError: limit=50
    try: offset=max(0,min(100000,int(q.get("offset",0))))
    except ValueError: offset=0
    where=["d.is_active=TRUE","h.is_active=TRUE","s.is_active=TRUE","u.is_active=TRUE"]; vals=[]
    if search:
        where.append("(d.first_name LIKE %s OR d.last_name LIKE %s OR d.display_name LIKE %s OR h.name LIKE %s)")
        vals.extend([f"%{search}%"]*4)
    if city: where.append("h.city LIKE %s"); vals.append(f"%{city}%")
    if spec: where.append("s.name LIKE %s"); vals.append(f"%{spec}%")
    sql="""SELECT d.doctor_id,d.first_name,d.last_name,d.display_name,d.qualifications,d.years_experience,
                  d.bio,d.consultation_fee,h.hospital_id,h.name AS hospital_name,h.city,
                  s.specialisation_id,s.name AS specialisation
           FROM doctors d JOIN hospitals h ON h.hospital_id=d.hospital_id
           JOIN specialisations s ON s.specialisation_id=d.specialisation_id
           JOIN users u ON u.user_id=d.user_id WHERE """+" AND ".join(where)+" ORDER BY d.last_name,d.first_name LIMIT %s OFFSET %s"
    vals.extend([limit,offset]); conn=None
    try:
        conn=get_connection()
        with conn.cursor() as cur: cur.execute(sql,tuple(vals)); rows=cur.fetchall()
        return response(200,{"items":rows,"count":len(rows),"limit":limit,"offset":offset})
    except Exception:
        print("Doctor search failed; inspect CloudWatch logs.")
        return response(500,{"error":"INTERNAL_SERVER_ERROR"})
    finally:
        if conn: conn.close()
