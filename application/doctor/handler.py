"""Read-only doctor, hospital and specialisation catalogue endpoints.
Uses the schema.sql columns in this MediSlot project.
Supports GET /doctors, /doctors/{doctor_id}, /hospitals, /specialisations.
"""
from application.common.http import response, request_method
from application.database.connection import get_connection

def _query(sql, params=(), one=False):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()

def _int(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default

def lambda_handler(event, context):
    method = (event.get("requestContext",{}).get("http",{}).get("method") or event.get("httpMethod","GET")).upper()
    if method != "GET":
        return response(405,{"error":"METHOD_NOT_ALLOWED","message":"Only GET is supported."})
    path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
    params = event.get("queryStringParameters") or {}
    try:
        if path.endswith("/hospitals"):
            limit = _int(params.get("limit"),50,1,100)
            city = (params.get("city") or "").strip()
            search = (params.get("search") or "").strip()
            where, values = ["is_active = TRUE"], []
            if city:
                where.append("city LIKE %s"); values.append(f"%{city}%")
            if search:
                where.append("(name LIKE %s OR city LIKE %s)")
                values.extend([f"%{search}%",f"%{search}%"])
            values.append(limit)
            rows = _query(
                "SELECT hospital_id,name,address_line1,address_line2,city,state,postal_code,country,phone,email "
                "FROM hospitals WHERE " + " AND ".join(where) + " ORDER BY name LIMIT %s",tuple(values))
            return response(200,{"items":rows,"count":len(rows)})
        if path.endswith("/specialisations") or path.endswith("/specializations"):
            limit = _int(params.get("limit"),100,1,200)
            rows = _query("SELECT specialisation_id,name,description FROM specialisations "
                          "WHERE is_active=TRUE ORDER BY name LIMIT %s",(limit,))
            return response(200,{"items":rows,"count":len(rows)})
        # /doctors and /doctors/{doctor_id}
        marker = "/doctors"
        idx = path.rfind(marker)
        if idx < 0:
            return response(404,{"error":"ROUTE_NOT_FOUND"})
        suffix = path[idx+len(marker):].strip("/")
        base_sql = (
            "SELECT d.doctor_id,d.user_id,d.first_name,d.last_name,d.display_name,"
            "d.qualifications,d.years_experience,d.bio,d.phone,d.consultation_fee,"
            "h.hospital_id,h.name AS hospital_name,h.city AS hospital_city,"
            "s.specialisation_id,s.name AS specialisation "
            "FROM doctors d JOIN hospitals h ON h.hospital_id=d.hospital_id "
            "JOIN specialisations s ON s.specialisation_id=d.specialisation_id "
            "JOIN users u ON u.user_id=d.user_id "
            "WHERE d.is_active=TRUE AND h.is_active=TRUE AND s.is_active=TRUE AND u.is_active=TRUE"
        )
        if suffix:
            doctor_id = suffix.split("/")[0]
            row = _query(base_sql + " AND d.doctor_id=%s LIMIT 1",(doctor_id,),one=True)
            if not row:
                return response(404,{"error":"DOCTOR_NOT_FOUND"})
            return response(200,{"doctor":row})
        where, values = [], []
        search = (params.get("search") or "").strip()
        city = (params.get("city") or "").strip()
        hospital_id = (params.get("hospital_id") or "").strip()
        specialisation_id = (params.get("specialisation_id") or "").strip()
        if search:
            where.append("(d.first_name LIKE %s OR d.last_name LIKE %s OR d.display_name LIKE %s)")
            values.extend([f"%{search}%"]*3)
        if city:
            where.append("h.city LIKE %s"); values.append(f"%{city}%")
        if hospital_id:
            where.append("d.hospital_id=%s"); values.append(hospital_id)
        if specialisation_id:
            where.append("d.specialisation_id=%s"); values.append(specialisation_id)
        sql = base_sql + ((" AND " + " AND ".join(where)) if where else "")
        limit = _int(params.get("limit"),50,1,100)
        offset = _int(params.get("offset"),0,0,100000)
        sql += " ORDER BY d.last_name,d.first_name LIMIT %s OFFSET %s"
        values.extend([limit,offset])
        rows = _query(sql,tuple(values))
        return response(200,{"items":rows,"count":len(rows),"limit":limit,"offset":offset})
    except Exception:
        print("Doctor/catalogue query failed; inspect CloudWatch logs for the exception.")
        return response(500,{"error":"INTERNAL_SERVER_ERROR","message":"Could not retrieve catalogue data."})
