from application.common.http import response, request_method, request_body
from application.common.auth_context import claims
from application.database.connection import get_connection

def lambda_handler(event, context):
    identity=claims(event)
    if not identity["user_id"]: return response(401,{"error":"UNAUTHORIZED"})
    method=request_method(event)
    if method=="GET":
        conn=get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT p.patient_id,p.first_name,p.last_name,p.date_of_birth,p.phone,p.gender,u.email
                               FROM patients p JOIN users u ON u.user_id=p.user_id WHERE p.user_id=%s""",(identity["user_id"],))
                row=cur.fetchone()
            return response(200,{"patient":row}) if row else response(404,{"error":"PATIENT_NOT_FOUND"})
        except Exception:
            print("Patient profile query failed."); return response(500,{"error":"INTERNAL_SERVER_ERROR"})
        finally: conn.close()
    if method in ("PATCH","PUT"):
        try: body=request_body(event)
        except ValueError as e: return response(400,{"error":"INVALID_JSON","message":str(e)})
        allowed={"first_name","last_name","phone","gender","date_of_birth"}
        if not body or set(body)-allowed: return response(400,{"error":"INVALID_FIELDS","allowed_fields":sorted(allowed)})
        fields=[]; values=[]
        for key,value in body.items():
            fields.append(f"{key}=%s"); values.append(value)
        values.append(identity["user_id"])
        conn=get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE patients SET "+", ".join(fields)+" WHERE user_id=%s",tuple(values))
                if not cur.rowcount: conn.rollback(); return response(404,{"error":"PATIENT_NOT_FOUND"})
            conn.commit(); return response(200,{"message":"Patient profile updated."})
        except Exception:
            conn.rollback(); return response(500,{"error":"PROFILE_UPDATE_FAILED"})
        finally: conn.close()
    return response(405,{"error":"METHOD_NOT_ALLOWED"})
