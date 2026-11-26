import os, uuid, json
from urllib.parse import unquote
from datetime import datetime, timedelta
import boto3
from botocore.config import Config
from application.common.api import ApiError
from application.common import rules

_sfn = None

def _client():
    global _sfn
    if _sfn is None: _sfn = boto3.client("stepfunctions", config=Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"}))
    return _sfn


def start(conn, patient_id, appointment_id, target_slot_id):
    target_slot_id = unquote(str(target_slot_id or "").strip())
    if not target_slot_id:
        raise ApiError(400, "INVALID_TARGET_SLOT", "target_slot_id is required.")
    with conn.cursor() as cur:
        # The JWT identifies the application user, while appointments store the
        # patient-domain identifier. Resolve that relationship before querying
        # the appointment; comparing appointments.patient_id directly with
        # the JWT user_id makes valid appointments look missing.
        cur.execute(
            "SELECT p.patient_id FROM patients p JOIN users u ON u.user_id = p.user_id "
            "WHERE p.user_id = %s AND p.is_active = TRUE AND u.is_active = TRUE",
            (patient_id,),
        )
        patient = cur.fetchone()
        if not patient:
            raise ApiError(409, "PROFILE_REQUIRED", "Create your patient profile first.")
        db_patient_id = patient["patient_id"]

        cur.execute("SELECT appointment_id, slot_id, status, starts_at_utc, reschedule_count FROM appointments WHERE appointment_id=%s AND patient_id=%s FOR UPDATE", (appointment_id, db_patient_id))
        appt = cur.fetchone()
        if not appt:
            raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Appointment was not found.")
        if appt["status"] != "CONFIRMED":
            raise ApiError(409, "NOT_RESCHEDULABLE", "Only confirmed appointments can be rescheduled.")
        if int(appt["reschedule_count"]) >= rules.max_reschedules():
            raise ApiError(409, "RESCHEDULE_LIMIT_REACHED", "The reschedule limit has been reached.")
        cutoff = appt["starts_at_utc"] - timedelta(hours=rules.min_reschedule_hours())
        if datetime.utcnow() >= cutoff:
            raise ApiError(409, "RESCHEDULE_CUTOFF", f"Rescheduling is allowed only until {rules.min_reschedule_hours()} hours before the appointment.")
        cur.execute("SELECT a.doctor_id FROM appointments a WHERE a.appointment_id=%s AND a.patient_id=%s", (appointment_id, db_patient_id))
        doctor_id = cur.fetchone()["doctor_id"]
        cur.execute("SELECT slot_id, doctor_id, start_utc, end_utc, status, booked_count, capacity FROM materialised_slots WHERE slot_id=%s FOR UPDATE", (target_slot_id,))
        target = cur.fetchone()
        if not target: raise ApiError(404, "TARGET_SLOT_NOT_FOUND", "Target slot was not found.")
        if target["doctor_id"] != doctor_id:
            raise ApiError(409, "INVALID_TARGET_SLOT", "Choose a slot offered by the same doctor.")
        if target["status"] in ("BLOCKED", "CLOSED") or int(target["booked_count"]) >= int(target["capacity"]):
            raise ApiError(409, "TARGET_SLOT_UNAVAILABLE", "Target slot is not available.")
        if target["start_utc"] <= datetime.utcnow():
            raise ApiError(409, "TARGET_SLOT_IN_PAST", "Choose a future slot.")
        if target_slot_id == appt["slot_id"]:
            raise ApiError(409, "SAME_SLOT", "Choose a different slot.")
    arn = os.environ.get("RESCHEDULE_STATE_MACHINE_ARN")
    if not arn: raise ApiError(503, "RESCHEDULE_UNAVAILABLE", "Reschedule workflow is not configured.")
    execution_name = f"reschedule-{appointment_id}-{uuid.uuid4().hex[:12]}"
    payload = {"patient_id": db_patient_id, "appointment_id": appointment_id, "target_slot_id": target_slot_id, "attempt": int(appt["reschedule_count"]) + 1}
    result = _client().start_execution(stateMachineArn=arn, name=execution_name, input=json.dumps(payload))
    with conn.cursor() as cur:
        cur.execute("UPDATE appointments SET reschedule_count = reschedule_count + 1 WHERE appointment_id=%s AND patient_id=%s AND status='CONFIRMED'", (appointment_id, db_patient_id))
    conn.commit()
    return 202, {"status": "RESCHEDULE_REQUESTED", "execution_arn": result["executionArn"], "attempt": int(appt["reschedule_count"]) + 1}
