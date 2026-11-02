"""Doctor-domain API: profile, photo upload, consultation catalogue, patterns and blackouts."""
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
try:
    from aws_xray_sdk.core import patch_all
    patch_all()
except ImportError:
    pass
from botocore.exceptions import ClientError
from application.common.http import response, request_method, request_body
from application.database.connection import get_connection

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

PROFILE_FIELDS = {
    "hospital_id": "hospital_id", "specialisation_id": "specialisation_id",
    "first_name": "first_name", "last_name": "last_name",
    "licence_number": "licence_number", "experience_years": "experience_years",
    "qualifications": "qualifications", "timezone": "timezone", "is_active": "is_active",
}


def _body(event):
    return request_body(event)


def _identity(event):
    auth = (event.get("requestContext") or {}).get("authorizer") or {}
    ctx = auth.get("lambda") or auth.get("claims") or auth
    uid = ctx.get("userId") or ctx.get("user_id") or ctx.get("sub")
    role = str(ctx.get("role", "")).lower()
    if not uid:
        raise PermissionError("Authenticated user context is missing")
    return int(uid), role


def _doctor_id(cursor, user_id):
    cursor.execute("SELECT doctor_id FROM doctors WHERE user_id=%s LIMIT 1", (user_id,))
    row = cursor.fetchone()
    return row["doctor_id"] if row else None


def _require_doctor(role):
    if role not in ("doctor", "admin"):
        raise PermissionError("DOCTOR_ROLE_REQUIRED")


def _json_int(value, field, minimum=0):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer")
    if result < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return result


def _validate_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError("timezone must be a valid IANA timezone, e.g. Asia/Kolkata")
    return value


def _profile(cursor, doctor_id):
    cursor.execute("""SELECT d.doctor_id,d.user_id,d.hospital_id,d.specialisation_id,
        d.first_name,d.last_name,d.licence_number,d.experience_years,d.qualifications,
        d.photo_url,d.timezone,d.is_active,d.created_at,d.updated_at,
        h.hospital_name,s.specialisation_name
        FROM doctors d JOIN hospitals h ON h.hospital_id=d.hospital_id
        JOIN specialisations s ON s.specialisation_id=d.specialisation_id
        WHERE d.doctor_id=%s""", (doctor_id,))
    return cursor.fetchone()


def _profile_route(method, path, user_id, role, body):
    _require_doctor(role)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            doctor_id = _doctor_id(cur, user_id)
            if path == "/doctor/profile" and method == "POST":
                if doctor_id:
                    return response(409, {"error":"PROFILE_ALREADY_EXISTS"})
                missing = [f for f in ("hospital_id","specialisation_id","first_name","last_name","licence_number") if body.get(f) in (None, "") or (isinstance(body.get(f),str) and not body.get(f).strip())]
                if missing:
                    return response(400, {"error":"MISSING_FIELDS","fields":missing})
                timezone = _validate_timezone(body.get("timezone", "Asia/Kolkata"))
                experience = _json_int(body.get("experience_years", 0), "experience_years")
                cur.execute("SELECT hospital_id FROM hospitals WHERE hospital_id=%s AND is_active=TRUE", (body["hospital_id"],))
                if not cur.fetchone(): return response(400,{"error":"INVALID_HOSPITAL_ID"})
                cur.execute("SELECT specialisation_id FROM specialisations WHERE specialisation_id=%s", (body["specialisation_id"],))
                if not cur.fetchone(): return response(400,{"error":"INVALID_SPECIALISATION_ID"})
                cur.execute("SELECT doctor_id FROM doctors WHERE licence_number=%s", (str(body["licence_number"]).strip(),))
                if cur.fetchone(): return response(409,{"error":"LICENCE_NUMBER_EXISTS"})
                cur.execute("""INSERT INTO doctors (user_id,hospital_id,specialisation_id,first_name,last_name,
                    licence_number,experience_years,qualifications,timezone) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (user_id,body["hospital_id"],body["specialisation_id"],str(body["first_name"]).strip(),
                     str(body["last_name"]).strip(),str(body["licence_number"]).strip(),experience,
                     body.get("qualifications"),timezone))
                doctor_id = cur.lastrowid
                conn.commit()
                return response(201,{"doctor":_profile(cur,doctor_id)})
            if not doctor_id:
                return response(404,{"error":"DOCTOR_PROFILE_NOT_FOUND","message":"Create your profile first using POST /doctor/profile."})
            if path == "/doctor/profile" and method == "GET":
                return response(200,{"doctor":_profile(cur,doctor_id)})
            if path == "/doctor/profile" and method in ("PUT","PATCH"):
                allowed = {k:v for k,v in body.items() if k in PROFILE_FIELDS}
                if "experience_years" in allowed: allowed["experience_years"] = _json_int(allowed["experience_years"],"experience_years")
                if "timezone" in allowed: allowed["timezone"] = _validate_timezone(allowed["timezone"])
                if "is_active" in allowed:
                    if not isinstance(allowed["is_active"], bool): return response(400,{"error":"is_active must be a JSON boolean"})
                if not allowed: return response(400,{"error":"NO_UPDATABLE_FIELDS"})
                if "hospital_id" in allowed:
                    cur.execute("SELECT hospital_id FROM hospitals WHERE hospital_id=%s AND is_active=TRUE",(allowed["hospital_id"],))
                    if not cur.fetchone(): return response(400,{"error":"INVALID_HOSPITAL_ID"})
                if "specialisation_id" in allowed:
                    cur.execute("SELECT specialisation_id FROM specialisations WHERE specialisation_id=%s",(allowed["specialisation_id"],))
                    if not cur.fetchone(): return response(400,{"error":"INVALID_SPECIALISATION_ID"})
                if "licence_number" in allowed:
                    cur.execute("SELECT doctor_id FROM doctors WHERE licence_number=%s AND doctor_id<>%s",(allowed["licence_number"],doctor_id))
                    if cur.fetchone(): return response(409,{"error":"LICENCE_NUMBER_EXISTS"})
                assignments=", ".join(f"{PROFILE_FIELDS[k]}=%s" for k in allowed)
                cur.execute(f"UPDATE doctors SET {assignments} WHERE doctor_id=%s",tuple(allowed.values())+(doctor_id,))
                conn.commit()
                return response(200,{"doctor":_profile(cur,doctor_id)})
            if path == "/doctor/profile/photo-upload-url" and method == "POST":
                bucket=os.environ.get("PROFILE_PHOTO_BUCKET")
                if not bucket: raise RuntimeError("PROFILE_PHOTO_BUCKET is not configured")
                content_type=body.get("content_type")
                ext={"image/jpeg":"jpg","image/png":"png","image/webp":"webp"}.get(content_type)
                if not ext: return response(400,{"error":"UNSUPPORTED_IMAGE_TYPE","allowed":["image/jpeg","image/png","image/webp"]})
                try:
                    file_size = int(body.get("file_size", 0))
                except (TypeError, ValueError):
                    return response(400, {"error": "INVALID_FILE_SIZE"})
                if file_size < 1 or file_size > 5 * 1024 * 1024:
                    return response(400, {"error": "FILE_SIZE_LIMIT", "message": "Image must be between 1 byte and 5 MB."})
                key=f"doctor-profiles/{doctor_id}/{uuid.uuid4().hex}.{ext}"
                upload=boto3.client("s3").generate_presigned_url("put_object",Params={"Bucket":bucket,"Key":key,"ContentType":content_type,"ContentLength":file_size},ExpiresIn=300)
                photo_uri=f"s3://{bucket}/{key}"
                cur.execute("UPDATE doctors SET photo_url=%s WHERE doctor_id=%s",(photo_uri,doctor_id)); conn.commit()
                return response(200,{"upload_url":upload,"method":"PUT","headers":{"Content-Type":content_type},"photo_url":photo_uri,"expires_in":300})
    finally:
        conn.close()
    return response(405,{"error":"METHOD_NOT_ALLOWED"})


def _consultation_route(method,path,role,body):
    if role not in ("doctor", "admin"):
        return response(403,{"error":"DOCTOR_ROLE_REQUIRED"})
    # ERD has no doctor_id on CONSULTATION_TYPES: it is a shared catalogue.
    # Mutations are restricted to admins to avoid one doctor changing every doctor's catalogue.
    if method in ("POST","PUT","PATCH","DELETE") and role != "admin":
        return response(403,{"error":"ADMIN_ROLE_REQUIRED","message":"Consultation types are global catalogue entries in the ERD."})
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            parts=path.strip("/").split("/")
            item_id=parts[2] if len(parts)>2 else None
            if method=="GET":
                if item_id:
                    cur.execute("SELECT * FROM consultation_types WHERE consultation_type_id=%s",(item_id,)); item=cur.fetchone()
                    return response(200,{"consultation_type":item}) if item else response(404,{"error":"NOT_FOUND"})
                cur.execute("SELECT * FROM consultation_types WHERE is_active=TRUE ORDER BY type_name")
                return response(200,{"items":cur.fetchall()})
            if method=="POST":
                name=str(body.get("type_name","")).strip(); duration=_json_int(body.get("duration_minutes"),"duration_minutes",1)
                try: fee=Decimal(str(body.get("fee",0)))
                except InvalidOperation: return response(400,{"error":"fee must be a valid decimal"})
                if not name or len(name)>120 or not fee.is_finite() or fee<0: return response(400,{"error":"INVALID_CONSULTATION_TYPE"})
                cur.execute("INSERT INTO consultation_types(type_name,duration_minutes,fee) VALUES(%s,%s,%s)",(name,duration,fee)); conn.commit()
                return response(201,{"consultation_type_id":cur.lastrowid})
            if method in ("PUT","PATCH") and item_id:
                fields={k:body[k] for k in ("type_name","duration_minutes","fee","is_active") if k in body}
                if not fields: return response(400,{"error":"NO_UPDATABLE_FIELDS"})
                if "duration_minutes" in fields: fields["duration_minutes"]=_json_int(fields["duration_minutes"],"duration_minutes",1)
                if "fee" in fields:
                    try: fields["fee"]=Decimal(str(fields["fee"]))
                    except InvalidOperation: return response(400,{"error":"INVALID_FEE"})
                    if not fields["fee"].is_finite() or fields["fee"]<0: return response(400,{"error":"INVALID_FEE"})
                cur.execute("UPDATE consultation_types SET "+", ".join(f"{k}=%s" for k in fields)+" WHERE consultation_type_id=%s",tuple(fields.values())+(item_id,)); conn.commit()
                return response(200,{"message":"Consultation type updated"})
            if method=="DELETE" and item_id:
                cur.execute("UPDATE consultation_types SET is_active=FALSE WHERE consultation_type_id=%s",(item_id,)); conn.commit()
                return response(200,{"message":"Consultation type deactivated"})
    finally: conn.close()
    return response(405,{"error":"METHOD_NOT_ALLOWED"})


def _availability_route(method,path,user_id,role,body):
    _require_doctor(role)
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            doctor_id=_doctor_id(cur,user_id)
            if not doctor_id: return response(404,{"error":"DOCTOR_PROFILE_NOT_FOUND"})
            parts=path.strip("/").split("/"); item_id=parts[2] if len(parts)>2 else None
            if method=="GET":
                sql="SELECT ap.*,ct.type_name,ct.duration_minutes,ct.fee FROM availability_patterns ap JOIN consultation_types ct ON ct.consultation_type_id=ap.consultation_type_id WHERE ap.doctor_id=%s"
                params=[doctor_id]
                if item_id: sql+=" AND ap.availability_id=%s"; params.append(item_id)
                sql+=" ORDER BY ap.day_of_week,ap.start_time"
                cur.execute(sql,tuple(params)); rows=cur.fetchall()
                if item_id and not rows: return response(404,{"error":"AVAILABILITY_PATTERN_NOT_FOUND"})
                return response(200,{"items":rows} if not item_id else {"availability_pattern":rows[0]})
            if method=="POST":
                ctid=_json_int(body.get("consultation_type_id"),"consultation_type_id",1)
                day=_json_int(body.get("day_of_week"),"day_of_week",1)
                cap=_json_int(body.get("capacity",1),"capacity",1)
                if day>7: return response(400,{"error":"day_of_week must be 1..7"})
                start=body.get("start_time"); end=body.get("end_time")
                try:
                    start_value=time.fromisoformat(str(start)); end_value=time.fromisoformat(str(end))
                except (TypeError,ValueError):
                    return response(400,{"error":"start_time and end_time must be valid HH:MM[:SS] values"})
                if end_value<=start_value: return response(400,{"error":"INVALID_TIME_RANGE"})
                start=start_value.strftime("%H:%M:%S"); end=end_value.strftime("%H:%M:%S")
                cur.execute("SELECT consultation_type_id FROM consultation_types WHERE consultation_type_id=%s AND is_active=TRUE",(ctid,))
                if not cur.fetchone(): return response(400,{"error":"INVALID_CONSULTATION_TYPE"})
                cur.execute("INSERT INTO availability_patterns(doctor_id,consultation_type_id,day_of_week,start_time,end_time,capacity) VALUES(%s,%s,%s,%s,%s,%s)",(doctor_id,ctid,day,start,end,cap)); conn.commit()
                return response(201,{"availability_id":cur.lastrowid})
            if method in ("PUT","PATCH") and item_id:
                fields={k:body[k] for k in ("consultation_type_id","day_of_week","start_time","end_time","capacity","is_active") if k in body}
                if not fields: return response(400,{"error":"NO_UPDATABLE_FIELDS"})
                if "day_of_week" in fields and not 1<=int(fields["day_of_week"])<=7: return response(400,{"error":"INVALID_DAY_OF_WEEK"})
                if "capacity" in fields: fields["capacity"]=_json_int(fields["capacity"],"capacity",1)
                if "day_of_week" in fields: fields["day_of_week"]=_json_int(fields["day_of_week"],"day_of_week",1)
                if "day_of_week" in fields and fields["day_of_week"]>7: return response(400,{"error":"INVALID_DAY_OF_WEEK"})
                if "consultation_type_id" in fields:
                    cur.execute("SELECT consultation_type_id FROM consultation_types WHERE consultation_type_id=%s AND is_active=TRUE",(fields["consultation_type_id"],))
                    if not cur.fetchone(): return response(400,{"error":"INVALID_CONSULTATION_TYPE"})
                if "start_time" in fields or "end_time" in fields:
                    cur.execute("SELECT start_time,end_time FROM availability_patterns WHERE availability_id=%s AND doctor_id=%s",(item_id,doctor_id))
                    current=cur.fetchone()
                    if not current: return response(404,{"error":"AVAILABILITY_PATTERN_NOT_FOUND"})
                    try:
                        st=time.fromisoformat(str(fields.get("start_time",current["start_time"])))
                        en=time.fromisoformat(str(fields.get("end_time",current["end_time"])))
                    except (TypeError,ValueError):
                        return response(400,{"error":"INVALID_TIME_RANGE"})
                    if en<=st: return response(400,{"error":"INVALID_TIME_RANGE"})
                    if "start_time" in fields: fields["start_time"]=st.strftime("%H:%M:%S")
                    if "end_time" in fields: fields["end_time"]=en.strftime("%H:%M:%S")
                cur.execute("UPDATE availability_patterns SET "+", ".join(f"{k}=%s" for k in fields)+" WHERE availability_id=%s AND doctor_id=%s",tuple(fields.values())+(item_id,doctor_id)); conn.commit()
                return response(200,{"message":"Availability pattern updated"})
            if method=="DELETE" and item_id:
                cur.execute("UPDATE availability_patterns SET is_active=FALSE WHERE availability_id=%s AND doctor_id=%s",(item_id,doctor_id)); conn.commit()
                return response(200,{"message":"Availability pattern deactivated"})
    finally: conn.close()
    return response(405,{"error":"METHOD_NOT_ALLOWED"})


def _blackout_route(method,path,user_id,role,body):
    _require_doctor(role)
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            doctor_id=_doctor_id(cur,user_id)
            if not doctor_id: return response(404,{"error":"DOCTOR_PROFILE_NOT_FOUND"})
            parts=path.strip("/").split("/"); item_id=parts[2] if len(parts)>2 else None
            if method=="GET":
                cur.execute("SELECT * FROM blackout_dates WHERE doctor_id=%s ORDER BY blackout_date",(doctor_id,)); return response(200,{"items":cur.fetchall()})
            if method=="POST":
                try: target=date.fromisoformat(str(body.get("blackout_date")))
                except ValueError: return response(400,{"error":"blackout_date must be YYYY-MM-DD"})
                cur.execute("INSERT INTO blackout_dates(doctor_id,blackout_date,reason) VALUES(%s,%s,%s)",(doctor_id,target,body.get("reason"))); conn.commit()
                return response(201,{"blackout_id":cur.lastrowid})
            if method=="DELETE" and item_id:
                cur.execute("DELETE FROM blackout_dates WHERE blackout_id=%s AND doctor_id=%s",(item_id,doctor_id)); conn.commit()
                return response(200,{"message":"Blackout date deleted"})
    finally: conn.close()
    return response(405,{"error":"METHOD_NOT_ALLOWED"})


def _schedule(event,user_id,role):
    _require_doctor(role)
    try:
        target=date.fromisoformat((event.get("queryStringParameters") or {}).get("date",""))
    except ValueError:
        return response(400,{"error":"date query parameter must be YYYY-MM-DD"})
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            doctor_id=_doctor_id(cur,user_id)
            if not doctor_id:
                return response(404,{"error":"DOCTOR_PROFILE_NOT_FOUND"})
            cur.execute(
                """SELECT slot_id, availability_id, doctor_id, consultation_type_id,
                          `date` AS date, start_time, end_time, capacity, booked_count, status
                   FROM materialised_slots
                   WHERE doctor_id=%s AND `date`=%s
                   ORDER BY start_time""",
                (int(doctor_id),target),
            )
            items=cur.fetchall()
        return response(200,{"doctor_id":doctor_id,"date":target.isoformat(),"items":items,"count":len(items)})
    finally:
        conn.close()


def lambda_handler(event, context):
    try:
        method=request_method(event); path=(event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
        user_id,role=_identity(event)
        body=_body(event) if method in ("POST","PUT","PATCH") else {}
        if path.startswith("/doctor/profile"): return _profile_route(method,path,user_id,role,body)
        if path.startswith("/doctor/consultation-types"): return _consultation_route(method,path,role,body)
        if path.startswith("/doctor/availability-patterns"): return _availability_route(method,path,user_id,role,body)
        if path.startswith("/doctor/blackout-dates"): return _blackout_route(method,path,user_id,role,body)
        if path=="/doctor/schedule" and method=="GET": return _schedule(event,user_id,role)
        return response(404,{"error":"ROUTE_NOT_FOUND","path":path})
    except PermissionError as exc:
        return response(403,{"error":"FORBIDDEN","message":str(exc)})
    except (ValueError,TypeError) as exc:
        return response(400,{"error":"INVALID_REQUEST","message":str(exc)})
    except ClientError:
        log.exception("AWS service operation failed")
        return response(502,{"error":"DEPENDENCY_ERROR"})
    except Exception:
        log.exception("Doctor domain request failed")
        return response(500,{"error":"INTERNAL_SERVER_ERROR"})
