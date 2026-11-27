"""Reschedule saga (Milestone 6): move an appointment from its slot to another slot of the same doctor.

Why a saga
  Two seats live in two systems (the DynamoDB counters of the two slots) and the appointment lives in RDS.
  There is no transaction across them, so the move is split into small steps, each with an explicit undo.
  AWS Step Functions (infrastructure/cloudformation/workflows.yaml) runs the steps, retries the ones that can
  be retried and runs the compensation when a later step fails. This module holds the steps themselves.

The steps, in order
  1 validate           read-only. Owner, CONFIRMED, fewer than 2 reschedules, at least 6 hours to the start,
                       same doctor, target in the future, bookable, no overlap. Nothing has changed yet.
  2 claim_target       take ONE seat of the TARGET slot (the normal booking gate, SlotStore.reserve).
                       Full -> the saga ends here, the original appointment was never touched.
  3 move_appointment   ONE RDS transaction: the appointment row now points at the target slot, the two slot
                       counters follow, reschedule_count + 1. From this commit on the patient holds the
                       target. Before it, the original. A single row can only point to one slot, so the
                       patient can never hold two or none.
  4 release_original   give back the ORIGINAL slot's DynamoDB seat.
  5 publish            AppointmentRescheduled (patient and doctor are notified), ReschedulesSucceeded,
                       and the freed seat may go to the waitlist.

Compensation (what "release failed after a successful claim" means)
  If step 4 fails, or step 3 fails after step 2 claimed a seat, `compensate` runs: the appointment row is put
  back on the original slot (only if it was moved), the counters follow, and the TARGET seat is released.
  The original seat was never released, so nobody could take it in the meantime: the undo cannot fail
  because of other patients. The patient ends with exactly the appointment they started with.
  Every compensation is counted in ReschedulesCompensated (alarm: any compensation entry).

The saga takes no refund or fee decision: the fee on the appointment is kept as it was.
"""

import json
import logging
from datetime import timedelta

from application.booking import events
from application.booking.errors import ContentionExceeded, SlotFull
from application.booking.service import (_count, _dt, _fetch_view, _free_slot_row, _load_slot,
                                         _patient_id, _safe)
from application.common import rules, tracing
from application.common.api import ApiError, fmt_utc

log = logging.getLogger()


class SagaRejected(Exception):
    """A business rule stopped the saga. The message is JSON {status, code, message} for the HTTP answer."""

    def __init__(self, status, code, message):
        super().__init__(json.dumps({"status": status, "code": code, "message": message}))
        self.status, self.code, self.detail = status, code, message


def _reject(exc):
    return SagaRejected(exc.status, exc.code, exc.message)


# ----------------------------------------------------------------------------- 1 validate
def validate(conn, deps, state):
    now = deps.now()
    appointment_id, target_id = state["appointment_id"], state["target_slot_id"]
    try:
        with conn.cursor() as cur:
            patient_id = _patient_id(cur, state["user_id"])
            cur.execute(
                "SELECT appointment_id, doctor_id, slot_id, consultation_type_id, status, reschedule_count, starts_at_utc, "
                "ends_at_utc, local_date, local_start, local_end, timezone FROM appointments "
                "WHERE appointment_id = %s AND patient_id = %s", (appointment_id, patient_id))
            appt = cur.fetchone()
            if not appt:
                raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Appointment was not found.")
            if appt["status"] != "CONFIRMED":
                raise ApiError(409, "NOT_RESCHEDULABLE", "Only a confirmed appointment can be rescheduled.")
            start = _dt(appt["starts_at_utc"])
            if int(appt["reschedule_count"]) >= rules.max_reschedules():
                raise ApiError(409, "RESCHEDULE_LIMIT_REACHED",
                               f"An appointment can be rescheduled at most {rules.max_reschedules()} times.")
            if start - now < timedelta(hours=rules.min_reschedule_hours()):
                raise ApiError(409, "RESCHEDULE_TOO_LATE",
                               f"An appointment cannot be rescheduled within {rules.min_reschedule_hours()} hours of its start.")
            if target_id == appt["slot_id"]:
                raise ApiError(400, "SAME_SLOT", "Choose a different slot.")
            target = _load_slot(cur, target_id)
            if not target:
                raise ApiError(404, "SLOT_NOT_FOUND", "The target slot was not found.")
            if target["doctor_id"] != appt["doctor_id"]:
                raise ApiError(409, "DIFFERENT_DOCTOR", "An appointment can only be moved to another slot of the same doctor.")
            _check_target(cur, patient_id, target, appt, now)
    except ApiError as exc:
        conn.rollback()
        raise _reject(exc) from None
    conn.rollback()
    return dict(state, patient_id=patient_id, doctor_id=appt["doctor_id"], original_slot_id=appt["slot_id"],
                target_capacity=int(target["capacity"]),
                original={"consultation_type_id": appt["consultation_type_id"], "starts_at_utc": fmt_utc(start),
                          "ends_at_utc": fmt_utc(_dt(appt["ends_at_utc"])), "local_date": str(appt["local_date"])[:10],
                          "local_start": str(appt["local_start"]), "local_end": str(appt["local_end"]),
                          "timezone": appt["timezone"]},
                reschedule_count=int(appt["reschedule_count"]))


def _check_target(cur, patient_id, target, appt, now):
    start, end = _dt(target["start_utc"]), _dt(target["end_utc"])
    if start <= now:
        raise ApiError(409, "SLOT_IN_PAST", "The target slot has already started or finished.")
    if start > now + timedelta(days=rules.booking_window_days()):
        raise ApiError(409, "OUTSIDE_BOOKING_WINDOW",
                       f"Slots can be booked at most {rules.booking_window_days()} days ahead.")
    if target["status"] in ("BLOCKED", "CLOSED"):
        raise ApiError(409, "SLOT_UNAVAILABLE", "The target slot is no longer available.")
    if target["status"] == "FULL" or int(target["booked_count"]) >= int(target["capacity"]):
        raise ApiError(409, "SLOT_FULL", "The target slot is full. Your current appointment is unchanged.")
    cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND slot_id = %s AND status = 'CONFIRMED'",
                (patient_id, target["slot_id"]))
    if _count(cur.fetchone()):
        raise ApiError(409, "ALREADY_BOOKED", "You already have an appointment in that slot.")
    cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND status = 'CONFIRMED' "
                "AND appointment_id <> %s AND starts_at_utc < %s AND ends_at_utc > %s",
                (patient_id, appt["appointment_id"], end, start))
    if _count(cur.fetchone()):
        raise ApiError(409, "TIME_CONFLICT", "You already have another appointment that overlaps that time.")


# ----------------------------------------------------------------------------- 2 claim the target seat
def claim_target(conn, deps, state):
    try:
        reservation = deps.slots.reserve(state["target_slot_id"], state["target_capacity"])
    except SlotFull:
        raise SagaRejected(409, "SLOT_FULL", "The target slot is full. Your current appointment is unchanged.") from None
    except ContentionExceeded:
        raise SagaRejected(409, "BOOKING_CONFLICT", "Too many people are booking that slot right now. Please try again.") from None
    if reservation.conflicts:
        deps.metrics.put("BookingConflictRetries", reservation.conflicts)
    return dict(state, target_claimed=True)


# ----------------------------------------------------------------------------- 3 move the appointment (RDS)
def move_appointment(conn, deps, state):
    appointment_id, original, target_id = state["appointment_id"], state["original_slot_id"], state["target_slot_id"]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, slot_id FROM appointments WHERE appointment_id = %s FOR UPDATE", (appointment_id,))
            row = cur.fetchone()
            if not row or row["status"] != "CONFIRMED" or row["slot_id"] != original:
                raise SagaRejected(409, "NOT_RESCHEDULABLE", "The appointment changed while it was being moved.")
            target = _load_slot(cur, target_id)
            cur.execute("UPDATE materialised_slots SET booked_count = booked_count + 1 "
                        "WHERE slot_id = %s AND status = 'OPEN' AND booked_count < capacity", (target_id,))
            if cur.rowcount != 1:
                raise SagaRejected(409, "SLOT_FULL", "The target slot is no longer available.")
            cur.execute("UPDATE materialised_slots SET status = 'FULL' "
                        "WHERE slot_id = %s AND status = 'OPEN' AND booked_count >= capacity", (target_id,))
            cur.execute(
                "UPDATE appointments SET slot_id = %s, consultation_type_id = %s, starts_at_utc = %s, ends_at_utc = %s, "
                "local_date = %s, local_start = %s, local_end = %s, timezone = %s, reschedule_count = reschedule_count + 1 "
                "WHERE appointment_id = %s AND status = 'CONFIRMED' AND slot_id = %s",
                (target_id, target["consultation_type_id"], _dt(target["start_utc"]), _dt(target["end_utc"]),
                 target["date"], target["start_time"], target["end_time"], target["timezone"], appointment_id, original))
            if cur.rowcount != 1:
                raise SagaRejected(409, "NOT_RESCHEDULABLE", "The appointment changed while it was being moved.")
            _free_slot_row(cur, original, 1)
        conn.commit()
    except Exception as exc:
        _safe(conn.rollback)
        if type(exc).__name__ == "IntegrityError":
            raise SagaRejected(409, "ALREADY_BOOKED", "You already have an appointment in that slot.") from None
        raise
    return dict(state, moved=True)


# ----------------------------------------------------------------------------- 4 release the original seat
def release_original(conn, deps, state):
    deps.slots.release(state["original_slot_id"])
    return dict(state, original_released=True)


# ----------------------------------------------------------------------------- 5 publish
def publish(conn, deps, state):
    """Best effort: the reschedule has already succeeded, so nothing here may fail it."""
    now = deps.now()
    appointment_id = state["appointment_id"]
    try:
        with conn.cursor() as cur:
            view = _fetch_view(cur, appointment_id, now)["appointment"]
        conn.rollback()
        deps.metrics.put("ReschedulesSucceeded", 1)
        deps.events.publish(events.APPOINTMENT_RESCHEDULED, {
            "appointment_id": appointment_id, "patient_id": state["patient_id"], "doctor_id": state["doctor_id"],
            "from_slot_id": state["original_slot_id"], "to_slot_id": state["target_slot_id"],
            "from_starts_at_utc": state["original"]["starts_at_utc"], "starts_at_utc": view["start_utc"],
            "ends_at_utc": view["end_utc"], "reschedule_count": state["reschedule_count"] + 1})
        deps.events.publish(events.SLOT_SEAT_FREED, {
            "slot_id": state["original_slot_id"], "doctor_id": state["doctor_id"], "seats_freed": 1,
            "starts_at_utc": state["original"]["starts_at_utc"]})
        from application.booking.service import offer_freed_seat
        offer_freed_seat(conn, deps, state["original_slot_id"])
    except Exception:  # noqa: BLE001
        log.exception("reschedule post-processing failed for %s", appointment_id)
        _safe(conn.rollback)
    return dict(state, published=True)


# ----------------------------------------------------------------------------- compensation
def compensate(conn, deps, state):
    """Undo whatever the saga managed to do. Safe to call when only some of the steps happened."""
    appointment_id, original, target_id = state["appointment_id"], state["original_slot_id"], state["target_slot_id"]
    o = state["original"]
    moved_back = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE appointments SET slot_id = %s, consultation_type_id = %s, starts_at_utc = %s, ends_at_utc = %s, "
                "local_date = %s, local_start = %s, local_end = %s, timezone = %s, reschedule_count = reschedule_count - 1 "
                "WHERE appointment_id = %s AND slot_id = %s AND status = 'CONFIRMED'",
                (original, o["consultation_type_id"], _dt(o["starts_at_utc"]), _dt(o["ends_at_utc"]), o["local_date"],
                 o["local_start"], o["local_end"], o["timezone"], appointment_id, target_id))
            moved_back = cur.rowcount == 1
            if moved_back:  # counters follow the appointment: original +1, target -1
                cur.execute("UPDATE materialised_slots SET booked_count = booked_count + 1 "
                            "WHERE slot_id = %s AND booked_count < capacity", (original,))
                cur.execute("UPDATE materialised_slots SET status = 'FULL' "
                            "WHERE slot_id = %s AND status = 'OPEN' AND booked_count >= capacity", (original,))
                _free_slot_row(cur, target_id, 1)
        conn.commit()
    except Exception:
        _safe(conn.rollback)
        raise
    if state.get("target_claimed"):
        deps.slots.release(target_id)
    if state.get("original_released"):  # cannot happen in the current order; kept so a reordering stays safe
        deps.slots.reserve(original, int(state.get("original_capacity", 1)))
    tracing.annotate("reschedule_compensated", True)
    deps.metrics.put("ReschedulesCompensated", 1)
    log.warning("reschedule of %s compensated (moved_back=%s)", appointment_id, moved_back)
    return dict(state, compensated=True, moved_back=moved_back)


# ----------------------------------------------------------------------------- the same flow without Step Functions
def run_local(conn, deps, user_id, appointment_id, target_slot_id):
    """Runs the exact state machine in-process (unit tests, local development).

    Mirrors workflows.yaml: Validate -> ClaimTarget -> MoveAppointment -> ReleaseOriginal -> Publish, with the
    Catch of MoveAppointment and ReleaseOriginal leading to Compensate. Returns (status, body).
    """
    state = {"user_id": user_id, "appointment_id": appointment_id, "target_slot_id": target_slot_id}
    try:
        state = validate(conn, deps, state)
        state = claim_target(conn, deps, state)
    except SagaRejected as exc:
        raise ApiError(exc.status, exc.code, exc.detail) from None
    try:
        state = move_appointment(conn, deps, state)
        state = release_original(conn, deps, state)
    except Exception as exc:  # noqa: BLE001 - ANY failure after the claim triggers the compensation path
        log.exception("reschedule step failed: %s", exc)
        compensate(conn, deps, state)
        raise ApiError(409, "RESCHEDULE_FAILED",
                       "The reschedule could not be completed and was rolled back. Your original appointment is unchanged.") from None
    publish(conn, deps, state)
    with conn.cursor() as cur:
        view = _fetch_view(cur, appointment_id, deps.now())
    conn.rollback()
    return 200, view


def result_from_execution(execution):
    """Turn the StartSyncExecution response into (status, body) for the HTTP caller."""
    if execution.get("status") == "SUCCEEDED":
        return 200, json.loads(execution["output"])["result"]
    error, cause = execution.get("error"), execution.get("cause") or ""
    if error == "SagaRejected":
        try:
            inner = json.loads(json.loads(cause)["errorMessage"])
            raise ApiError(inner["status"], inner["code"], inner["message"])
        except (ValueError, KeyError, TypeError):
            pass
    if error == "RescheduleCompensated":
        raise ApiError(409, "RESCHEDULE_FAILED", "The reschedule could not be completed and was rolled back. "
                       "Your original appointment is unchanged.")
    raise ApiError(502, "RESCHEDULE_ERROR", "The reschedule could not be completed. Your original appointment is unchanged.")
