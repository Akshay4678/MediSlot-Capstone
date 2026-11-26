"""Booking, cancellation and refunds (Milestone 5).

Where the truth lives
    RDS   appointments + refund_ledger   system of record (survives everything)
          materialised_slots.booked_count  display copy, updated in the same transaction as the appointment
    DynamoDB SlotCapacity               the concurrency gate: a version-checked seat counter (slot_store.py)
    DynamoDB Idempotency                replay protection for retried booking requests (idempotency.py)

Booking, in order
    1 validate input, find the patient profile
    2 idempotency: replay the stored answer, or claim the key
    3 lock the PATIENT's row (serialises only that patient's own bookings, never other patients)
    4 validate the slot: exists, doctor active, not in the past, inside the booking window, open,
      not already booked by this patient, no overlapping appointment, max upcoming per doctor
    5 reserve a seat with a conditional DynamoDB write (retry with backoff on version conflict)
    6 insert the appointment, bump the RDS slot counter, commit
    7 publish AppointmentBooked, store the idempotent response, publish metrics
    If anything fails after step 5 the seat is released again, so a failed booking never keeps a seat.
"""

import logging
from datetime import timedelta

from application.booking import events, idempotency, refunds
from application.booking.errors import ContentionExceeded, SlotFull
from application.common import rules
from application.common.api import ApiError, as_date, clean_text, fmt_time, fmt_utc, money, new_id, parse_date

log = logging.getLogger()

MAX_LIMIT = 50
_KEY_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")


# ----------------------------------------------------------------------------- small helpers
def _dt(value):
    """DB DATETIME (datetime or ISO text) -> naive UTC datetime."""
    from datetime import datetime
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "").replace(" ", "T"))
    return value.replace(tzinfo=None)


def _count(row):
    return int(row["n"] if isinstance(row, dict) else row[0])


def _paging(q):
    try:
        limit = max(1, min(MAX_LIMIT, int(q.get("limit", 20))))
        offset = max(0, int(q.get("offset", 0)))
    except (TypeError, ValueError):
        raise ApiError(400, "INVALID_PAGINATION", "limit and offset must be integers.") from None
    return limit, offset


def validate_idempotency_key(key):
    if key is None or key == "":
        return None
    key = str(key).strip()
    if not 1 <= len(key) <= 128 or any(c not in _KEY_CHARS for c in key):
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY",
                       "Idempotency-Key must be 1-128 characters: letters, digits and . _ : -")
    return key


def _booking_body(body):
    allowed = {"slot_id", "notes"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ApiError(400, "UNKNOWN_FIELD", f"Unknown field(s): {', '.join(unknown)}.")
    slot_id = body.get("slot_id")
    if not isinstance(slot_id, str) or not slot_id.strip() or len(slot_id) > 160:
        raise ApiError(400, "INVALID_SLOT_ID", "slot_id is required.")
    return slot_id.strip(), clean_text(body.get("notes"), "notes", 500)


def _patient_id(cur, user_id):
    cur.execute(
        "SELECT p.patient_id FROM patients p JOIN users u ON u.user_id = p.user_id "
        "WHERE p.user_id = %s AND p.is_active = TRUE AND u.is_active = TRUE", (user_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(409, "PROFILE_REQUIRED", "Create your patient profile first.")
    return row["patient_id"]


def _doctor_id(cur, user_id):
    cur.execute("SELECT doctor_id FROM doctors WHERE user_id = %s AND is_active = TRUE", (user_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(409, "PROFILE_REQUIRED", "Create your doctor profile first (PUT /doctors/me).")
    return row["doctor_id"]


def _fail_metric(deps, reason):
    deps.metrics.put("BookingsRejected", 1, {"Reason": reason})


# ----------------------------------------------------------------------------- appointment views
_APPT_SELECT = (
    "SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id, a.consultation_type_id, a.status, a.notes, "
    "a.fee_amount, a.created_at, a.cancelled_at, a.cancellation_reason, a.starts_at_utc, a.ends_at_utc, "
    "a.local_date, a.local_start, a.local_end, a.timezone, a.reschedule_count, ct.type_name, d.first_name AS doctor_first, "
    "d.last_name AS doctor_last, h.hospital_name, h.city, sp.specialisation_name, "
    "p.first_name AS patient_first, p.last_name AS patient_last, p.phone AS patient_phone, "
    "r.refund_amount, r.refund_percentage, r.refund_tier, r.refund_status "
    "FROM appointments a "
    "JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id "
    "JOIN doctors d ON d.doctor_id = a.doctor_id "
    "JOIN hospitals h ON h.hospital_id = d.hospital_id "
    "JOIN specialisations sp ON sp.specialisation_id = d.specialisation_id "
    "JOIN patients p ON p.patient_id = a.patient_id "
    "LEFT JOIN refund_ledger r ON r.appointment_id = a.appointment_id "
)


def _view(r, now, for_doctor=False):
    view = {
        "appointment_id": r["appointment_id"],
        "status": r["status"],
        "slot_id": r["slot_id"],
        "consultation_type": {"consultation_type_id": r["consultation_type_id"], "type_name": r["type_name"]},
        "date": as_date(r["local_date"]).isoformat() if r["local_date"] else None,
        "start_time": fmt_time(r["local_start"]) if r["local_start"] else None,
        "end_time": fmt_time(r["local_end"]) if r["local_end"] else None,
        "start_utc": fmt_utc(_dt(r["starts_at_utc"])) if r["starts_at_utc"] else None,
        "end_utc": fmt_utc(_dt(r["ends_at_utc"])) if r["ends_at_utc"] else None,
        "timezone": r["timezone"],
        "fee": money(r["fee_amount"]),
        "notes": r["notes"] or "",
        "created_at": fmt_utc(r["created_at"]),
        "cancelled_at": fmt_utc(r["cancelled_at"]) if r["cancelled_at"] else None,
        "cancellation_reason": r["cancellation_reason"],
        "doctor": {"doctor_id": r["doctor_id"], "name": f"Dr. {r['doctor_first']} {r['doctor_last']}",
                   "hospital_name": r["hospital_name"], "city": r["city"], "specialisation": r["specialisation_name"]},
        "refund": None,
        "reschedule_count": int(r["reschedule_count"] or 0),
        "can_cancel": False,
        "can_reschedule": False,
    }
    if for_doctor:
        view["patient"] = {"patient_id": r["patient_id"], "name": f"{r['patient_first']} {r['patient_last']}".strip(),
                           "phone": r["patient_phone"] or ""}
    if r["refund_tier"] is not None:
        view["refund"] = {"tier": r["refund_tier"], "percentage": float(r["refund_percentage"]),
                          "amount": money(r["refund_amount"]), "status": r["refund_status"]}
    if r["status"] == "CONFIRMED" and r["starts_at_utc"] and _dt(r["starts_at_utc"]) > now:
        view["can_cancel"] = True
        preview = refunds.calculate_refund(r["fee_amount"], _dt(r["starts_at_utc"]), now, rules.refund_bands())
        view["refund_preview"] = {"tier": preview["tier"], "percentage": preview["percentage"],
                                  "amount": money(preview["amount"])}
        view["can_reschedule"] = (not for_doctor and view["reschedule_count"] < rules.max_reschedules()
                                  and _dt(r["starts_at_utc"]) - now >= timedelta(hours=rules.min_reschedule_hours()))
    if for_doctor:
        # The doctor records what happened once the appointment time has come.
        view["can_mark_attendance"] = bool(r["status"] == "CONFIRMED" and r["starts_at_utc"]
                                           and _dt(r["starts_at_utc"]) <= now)
    return view


# ----------------------------------------------------------------------------- booking
def book(conn, deps, user_id, body, idempotency_key=None):
    """POST /appointments. Returns (status, body)."""
    slot_id, notes = _booking_body(body or {})
    key = validate_idempotency_key(idempotency_key)
    now = deps.now()

    with conn.cursor() as cur:
        patient_id = _patient_id(cur, user_id)
    conn.rollback()  # end the read transaction; the booking transaction starts with the patient lock

    if key:
        state, stored = deps.idempotency.begin(user_id, key, idempotency.request_hash({"slot_id": slot_id, "notes": notes}))
        if state == idempotency.REPLAY:
            return stored["status"], dict(stored["body"], idempotent_replay=True)

    try:
        status, result, replay = _book_once(conn, deps, patient_id, slot_id, notes, key, now)
    except Exception:
        _safe(conn.rollback)
        if key:
            _safe(deps.idempotency.abandon, user_id, key)
        raise
    if key and not replay:
        _safe(deps.idempotency.complete, user_id, key, status, result)
    return status, (dict(result, idempotent_replay=True) if replay else result)


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - cleanup must never hide the real outcome
        log.exception("cleanup step failed")


def _load_slot(cur, slot_id):
    cur.execute(
        "SELECT s.slot_id, s.doctor_id, s.consultation_type_id, s.`date`, s.start_time, s.end_time, s.start_utc, "
        "s.end_utc, s.timezone, s.capacity, s.booked_count, s.status, dct.fee, ct.type_name "
        "FROM materialised_slots s JOIN doctors d ON d.doctor_id = s.doctor_id "
        "JOIN users u ON u.user_id = d.user_id "
        "JOIN consultation_types ct ON ct.consultation_type_id = s.consultation_type_id "
        "JOIN doctor_consultation_types dct ON dct.doctor_id = s.doctor_id "
        "     AND dct.consultation_type_id = s.consultation_type_id "
        "WHERE s.slot_id = %s AND d.is_active = TRUE AND u.is_active = TRUE", (slot_id,))
    return cur.fetchone()


def _check_slot_and_patient(cur, deps, patient_id, slot, now):
    start, end = _dt(slot["start_utc"]), _dt(slot["end_utc"])
    if start <= now:
        raise ApiError(409, "SLOT_IN_PAST", "This slot has already started or finished.")
    if start > now + timedelta(days=rules.booking_window_days()):
        raise ApiError(409, "OUTSIDE_BOOKING_WINDOW",
                       f"Slots can be booked at most {rules.booking_window_days()} days ahead.")
    if slot["status"] in ("BLOCKED", "CLOSED"):
        raise ApiError(409, "SLOT_UNAVAILABLE", "This slot is no longer available.")
    if slot["status"] == "FULL" or int(slot["booked_count"]) >= int(slot["capacity"]):
        raise ApiError(409, "SLOT_FULL", "This slot is full.")

    cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND slot_id = %s AND status = 'CONFIRMED'",
                (patient_id, slot["slot_id"]))
    if _count(cur.fetchone()):
        raise ApiError(409, "ALREADY_BOOKED", "You already have an appointment in this slot.")
    cur.execute(
        "SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND status = 'CONFIRMED' "
        "AND starts_at_utc < %s AND ends_at_utc > %s",
        (patient_id, end, start))
    if _count(cur.fetchone()):
        raise ApiError(409, "TIME_CONFLICT", "You already have an appointment that overlaps this time.")
    limit = rules.max_upcoming_per_doctor()
    cur.execute(
        "SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND doctor_id = %s "
        "AND status = 'CONFIRMED' AND starts_at_utc > %s", (patient_id, slot["doctor_id"], now))
    if _count(cur.fetchone()) >= limit:
        raise ApiError(409, "BOOKING_LIMIT_REACHED",
                       f"You can hold at most {limit} upcoming appointments with the same doctor.")


def _book_once(conn, deps, patient_id, slot_id, notes, key, now):
    with conn.cursor() as cur:
        # Serialise this patient's own requests only (never other patients): limits cannot be raced.
        cur.execute("SELECT patient_id FROM patients WHERE patient_id = %s FOR UPDATE", (patient_id,))
        if key:  # last line of defence for "exactly one appointment per key", even if the store lost the record
            cur.execute("SELECT appointment_id, slot_id, notes FROM appointments "
                        "WHERE patient_id = %s AND idempotency_key = %s", (patient_id, key))
            original = cur.fetchone()
            if original:
                if original["slot_id"] != slot_id or (original["notes"] or None) != notes:
                    raise ApiError(422, "IDEMPOTENCY_KEY_REUSED",
                                   "This Idempotency-Key was already used with a different request.")
                return 201, _fetch_view(cur, original["appointment_id"], now), True
        slot = _load_slot(cur, slot_id)
        if not slot:
            raise ApiError(404, "SLOT_NOT_FOUND", "Slot was not found.")
        try:
            _check_slot_and_patient(cur, deps, patient_id, slot, now)
        except ApiError as exc:
            _fail_metric(deps, exc.code)
            raise

    # ---- the concurrency gate: version-checked conditional write in DynamoDB ----
    try:
        reservation = deps.slots.reserve(slot_id, int(slot["capacity"]))
    except SlotFull as exc:
        _conflicts(deps, getattr(exc, "conflicts", 0))
        _fail_metric(deps, "SLOT_FULL")
        raise ApiError(409, "SLOT_FULL", "This slot is full.") from None
    except ContentionExceeded as exc:
        _conflicts(deps, getattr(exc, "conflicts", exc.attempts))
        _fail_metric(deps, "CONTENTION")
        raise ApiError(409, "BOOKING_CONFLICT",
                       "Too many people are booking this slot right now. Please try again.") from None
    _conflicts(deps, reservation.conflicts)

    appointment_id = new_id()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO appointments (appointment_id, patient_id, doctor_id, slot_id, consultation_type_id, status, "
                "notes, fee_amount, idempotency_key, starts_at_utc, ends_at_utc, local_date, local_start, local_end, "
                "timezone) VALUES (%s, %s, %s, %s, %s, 'CONFIRMED', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (appointment_id, patient_id, slot["doctor_id"], slot_id, slot["consultation_type_id"], notes,
                 slot["fee"], key, _dt(slot["start_utc"]), _dt(slot["end_utc"]), slot["date"],
                 slot["start_time"], slot["end_time"], slot["timezone"]))
            cur.execute(
                "UPDATE materialised_slots SET booked_count = booked_count + 1 "
                "WHERE slot_id = %s AND status = 'OPEN' AND booked_count < capacity", (slot_id,))
            if cur.rowcount != 1:
                raise ApiError(409, "SLOT_UNAVAILABLE", "This slot is no longer available.")
            cur.execute(
                "UPDATE materialised_slots SET status = 'FULL' "
                "WHERE slot_id = %s AND status = 'OPEN' AND booked_count >= capacity", (slot_id,))
        conn.commit()
    except Exception as exc:
        _safe(conn.rollback)
        _safe(deps.slots.release, slot_id)  # a failed booking must not keep its seat
        if type(exc).__name__ == "IntegrityError":
            raise ApiError(409, "ALREADY_BOOKED", "You already have an appointment in this slot.") from None
        raise

    with conn.cursor() as cur:
        result = _fetch_view(cur, appointment_id, now)
    conn.rollback()
    view = result["appointment"]
    deps.metrics.put("BookingsConfirmed", 1)
    deps.events.publish(events.APPOINTMENT_BOOKED, {
        "appointment_id": appointment_id, "patient_id": patient_id, "doctor_id": slot["doctor_id"],
        "slot_id": slot_id, "consultation_type_id": slot["consultation_type_id"],
        "starts_at_utc": view["start_utc"], "ends_at_utc": view["end_utc"], "fee": view["fee"],
        "seats_booked": reservation.booked_count})
    return 201, result, False


def _conflicts(deps, count):
    if count:
        deps.metrics.put("BookingConflictRetries", count)


def _fetch_view(cur, appointment_id, now, for_doctor=False):
    cur.execute(_APPT_SELECT + "WHERE a.appointment_id = %s", (appointment_id,))
    return {"appointment": _view(cur.fetchone(), now, for_doctor)}


# ----------------------------------------------------------------------------- listing and detail
def list_for_patient(conn, deps, user_id, q):
    q, now = q or {}, deps.now()
    limit, offset = _paging(q)
    scope = (q.get("scope") or "all").lower()
    status = (q.get("status") or "").upper()
    if scope not in ("all", "upcoming", "past"):
        raise ApiError(400, "INVALID_SCOPE", "scope must be all, upcoming or past.")
    with conn.cursor() as cur:
        patient_id = _patient_id(cur, user_id)
        where, params = ["a.patient_id = %s"], [patient_id]
        if status:
            where.append("a.status = %s")
            params.append(status)
        if scope == "upcoming":
            where.append("a.status = 'CONFIRMED' AND a.starts_at_utc > %s")
            params.append(now)
        elif scope == "past":
            where.append("(a.status <> 'CONFIRMED' OR a.starts_at_utc <= %s)")
            params.append(now)
        cond = " AND ".join(where)
        cur.execute("SELECT COUNT(*) AS n FROM appointments a WHERE " + cond, tuple(params))
        total = _count(cur.fetchone())
        cur.execute(_APPT_SELECT + "WHERE " + cond + " ORDER BY a.starts_at_utc DESC, a.appointment_id LIMIT %s OFFSET %s",
                    tuple(params + [limit, offset]))
        items = [_view(r, now) for r in cur.fetchall()]
    conn.rollback()
    return 200, {"items": items, "count": len(items), "total": total, "limit": limit, "offset": offset,
                 "has_more": offset + len(items) < total}


def get_for_patient(conn, deps, user_id, appointment_id):
    with conn.cursor() as cur:
        patient_id = _patient_id(cur, user_id)
        cur.execute(_APPT_SELECT + "WHERE a.appointment_id = %s AND a.patient_id = %s", (appointment_id, patient_id))
        row = cur.fetchone()
    conn.rollback()
    if not row:
        raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Appointment was not found.")
    return 200, {"appointment": _view(row, deps.now())}


def list_for_doctor(conn, deps, user_id, q):
    """GET /doctors/me/appointments?from=&to=&status=  (default: today and later, soonest first)."""
    q, now = q or {}, deps.now()
    limit, offset = _paging(q)
    status = (q.get("status") or "").upper()
    with conn.cursor() as cur:
        doctor_id = _doctor_id(cur, user_id)
        where, params = ["a.doctor_id = %s"], [doctor_id]
        if q.get("from"):
            where.append("a.local_date >= %s")
            params.append(parse_date(q["from"], "from"))
        if q.get("to"):
            where.append("a.local_date <= %s")
            params.append(parse_date(q["to"], "to"))
        if not q.get("from") and not q.get("to"):
            where.append("a.starts_at_utc > %s")
            params.append(now - timedelta(hours=12))
        if status:
            where.append("a.status = %s")
            params.append(status)
        cond = " AND ".join(where)
        cur.execute("SELECT COUNT(*) AS n FROM appointments a WHERE " + cond, tuple(params))
        total = _count(cur.fetchone())
        cur.execute(_APPT_SELECT + "WHERE " + cond + " ORDER BY a.starts_at_utc, a.appointment_id LIMIT %s OFFSET %s",
                    tuple(params + [limit, offset]))
        items = [_view(r, now, for_doctor=True) for r in cur.fetchall()]
    conn.rollback()
    return 200, {"items": items, "count": len(items), "total": total, "limit": limit, "offset": offset,
                 "has_more": offset + len(items) < total}


def get_for_doctor(conn, deps, user_id, appointment_id):
    with conn.cursor() as cur:
        doctor_id = _doctor_id(cur, user_id)
        cur.execute(_APPT_SELECT + "WHERE a.appointment_id = %s AND a.doctor_id = %s", (appointment_id, doctor_id))
        row = cur.fetchone()
    conn.rollback()
    if not row:
        raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Appointment was not found.")
    return 200, {"appointment": _view(row, deps.now(), for_doctor=True)}


# ----------------------------------------------------------------------------- cancellation
def _insert_refund(cur, appointment_id, fee, refund):
    status = "CALCULATED" if refund["amount"] > 0 else "NOT_APPLICABLE"
    cur.execute(
        "INSERT INTO refund_ledger (refund_id, appointment_id, refund_amount, refund_percentage, fee_amount, "
        "refund_tier, refund_status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (new_id(), appointment_id, refund["amount"], refund["percentage"], fee, refund["tier"], status))


def _free_slot_row(cur, slot_id, seats):
    cur.execute("UPDATE materialised_slots SET booked_count = booked_count - %s "
                "WHERE slot_id = %s AND booked_count >= %s", (seats, slot_id, seats))
    cur.execute("UPDATE materialised_slots SET status = 'OPEN' "
                "WHERE slot_id = %s AND status = 'FULL' AND booked_count < capacity", (slot_id,))


def cancel_by_patient(conn, deps, user_id, appointment_id, body=None):
    """POST /appointments/{id}/cancel : cancel one's own appointment; the refund follows the SSM tiers."""
    body = body or {}
    unknown = sorted(set(body) - {"reason"})
    if unknown:
        raise ApiError(400, "UNKNOWN_FIELD", f"Unknown field(s): {', '.join(unknown)}.")
    reason = clean_text(body.get("reason"), "reason", 255)
    now = deps.now()
    try:
        with conn.cursor() as cur:
            patient_id = _patient_id(cur, user_id)
            cur.execute(
                "SELECT appointment_id, doctor_id, slot_id, status, fee_amount, starts_at_utc FROM appointments "
                "WHERE appointment_id = %s AND patient_id = %s FOR UPDATE", (appointment_id, patient_id))
            appt = cur.fetchone()
            if not appt:
                raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Appointment was not found.")
            if appt["status"] != "CONFIRMED":
                raise ApiError(409, "NOT_CANCELLABLE", "This appointment is already cancelled or finished.")
            start = _dt(appt["starts_at_utc"])
            if now >= start:
                raise ApiError(409, "APPOINTMENT_STARTED", "This appointment has already started.")
            refund = refunds.calculate_refund(appt["fee_amount"], start, now, rules.refund_bands())
            cur.execute(
                "UPDATE appointments SET status = 'CANCELLED_BY_PATIENT', cancelled_at = %s, cancellation_reason = %s "
                "WHERE appointment_id = %s AND status = 'CONFIRMED'", (now, reason, appointment_id))
            if cur.rowcount != 1:
                raise ApiError(409, "NOT_CANCELLABLE", "This appointment is already cancelled or finished.")
            _insert_refund(cur, appointment_id, appt["fee_amount"], refund)
            _free_slot_row(cur, appt["slot_id"], 1)
        conn.commit()
    except Exception:
        _safe(conn.rollback)
        raise

    _safe(deps.slots.release, appt["slot_id"])  # after commit: the appointment is already cancelled
    deps.metrics.put("CancellationsByTier", 1, {"Tier": refund["tier"]})
    deps.events.publish(events.APPOINTMENT_CANCELLED, {
        "appointment_id": appointment_id, "patient_id": patient_id, "doctor_id": appt["doctor_id"],
        "slot_id": appt["slot_id"], "cancelled_by": "patient",
        "refund": {"tier": refund["tier"], "percentage": refund["percentage"], "amount": money(refund["amount"])}})
    deps.events.publish(events.SLOT_SEAT_FREED, {
        "slot_id": appt["slot_id"], "doctor_id": appt["doctor_id"], "seats_freed": 1,
        "starts_at_utc": fmt_utc(start)})
    offer_freed_seat(conn, deps, appt["slot_id"])
    with conn.cursor() as cur:
        view = _fetch_view(cur, appointment_id, now)
    conn.rollback()
    return 200, view


def offer_freed_seat(conn, deps, slot_id):
    """A seat of the slot was just freed: hold it for the first waiting patient (the waitlist "fast path").

    Best effort. The SlotSeatFreed event reaches the same function through SQS (application/jobs/handler.py),
    which is the safety net: offer_next() only ever acts on a REAL free seat that it can reserve, so calling it
    twice, from here and from the queue, can never create two offers for one seat.
    """
    if getattr(deps, "waitlist", None) is None:
        return None
    try:
        from application.waitlist import service as waitlist
        return waitlist.offer_next(conn, deps, slot_id)
    except Exception:  # noqa: BLE001 - the cancellation has already succeeded
        log.exception("could not offer the freed seat of %s", slot_id)
        _safe(conn.rollback)
        return None


def close_slot_if_resolved(cur, slot_id):
    """Close a slot once every appointment in it is settled (nobody is still CONFIRMED). Returns True if closed."""
    cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE slot_id = %s AND status = 'CONFIRMED'", (slot_id,))
    if _count(cur.fetchone()):
        return False
    cur.execute("UPDATE materialised_slots SET status = 'CLOSED' WHERE slot_id = %s AND status IN ('OPEN', 'FULL')",
                (slot_id,))
    return bool(cur.rowcount)


def close_slot_rows(cur, slot_id, reason, now):
    """Inside the caller's transaction: refund and cancel every CONFIRMED appointment of the slot, then CLOSE it.

    Used when a doctor cancels a slot and when a change of availability makes a booked slot disappear.
    Returns [(appointment row, refund)]; the caller commits and then calls slot_closed().
    """
    cancelled = []
    cur.execute("SELECT appointment_id, patient_id, fee_amount FROM appointments "
                "WHERE slot_id = %s AND status = 'CONFIRMED' ORDER BY created_at FOR UPDATE", (slot_id,))
    for appt in cur.fetchall():
        refund = refunds.doctor_cancellation_refund(appt["fee_amount"])
        cur.execute("UPDATE appointments SET status = 'CANCELLED_BY_DOCTOR', cancelled_at = %s, "
                    "cancellation_reason = %s WHERE appointment_id = %s AND status = 'CONFIRMED'",
                    (now, reason, appt["appointment_id"]))
        _insert_refund(cur, appt["appointment_id"], appt["fee_amount"], refund)
        cancelled.append((appt, refund))
    cur.execute("UPDATE materialised_slots SET status = 'CLOSED', booked_count = 0 WHERE slot_id = %s", (slot_id,))
    return cancelled


def slot_closed(deps, doctor_id, slot_id, cancelled):
    """After the commit of close_slot_rows(): give the seats back, clear the waitlist, count and announce."""
    held = 0
    if getattr(deps, "waitlist", None) is not None:
        try:
            held = deps.waitlist.close_slot(slot_id)  # live hold offers are cancelled and the queue is emptied
        except Exception:  # noqa: BLE001
            log.exception("could not clear the waitlist of %s", slot_id)
    seats = len(cancelled) + held
    if seats:
        _safe(deps.slots.release, slot_id, seats)
    if cancelled:
        deps.metrics.put("CancellationsByTier", len(cancelled), {"Tier": refunds.DOCTOR_CANCELLED})
    for appt, refund in cancelled:
        deps.events.publish(events.APPOINTMENT_CANCELLED, {
            "appointment_id": appt["appointment_id"], "patient_id": appt["patient_id"], "doctor_id": doctor_id,
            "slot_id": slot_id, "cancelled_by": "doctor",
            "refund": {"tier": refund["tier"], "percentage": refund["percentage"], "amount": money(refund["amount"])}})


def cancel_slot_by_doctor(conn, deps, user_id, body):
    """POST /doctors/me/slots/cancel : close one of the doctor's slots and refund every patient in full."""
    body = body or {}
    unknown = sorted(set(body) - {"slot_id", "reason"})
    if unknown:
        raise ApiError(400, "UNKNOWN_FIELD", f"Unknown field(s): {', '.join(unknown)}.")
    slot_id = body.get("slot_id")
    if not isinstance(slot_id, str) or not slot_id.strip() or len(slot_id) > 160:
        raise ApiError(400, "INVALID_SLOT_ID", "slot_id is required.")
    slot_id = slot_id.strip()
    reason = clean_text(body.get("reason"), "reason", 255) or "Cancelled by the doctor"
    now = deps.now()
    try:
        with conn.cursor() as cur:
            doctor_id = _doctor_id(cur, user_id)
            cur.execute("SELECT slot_id, start_utc, status FROM materialised_slots "
                        "WHERE slot_id = %s AND doctor_id = %s FOR UPDATE", (slot_id, doctor_id))
            slot = cur.fetchone()
            if not slot:
                raise ApiError(404, "SLOT_NOT_FOUND", "Slot was not found.")
            if _dt(slot["start_utc"]) <= now:
                raise ApiError(409, "SLOT_IN_PAST", "A slot that has already started cannot be cancelled.")
            if slot["status"] == "CLOSED":
                raise ApiError(409, "SLOT_ALREADY_CANCELLED", "This slot is already cancelled.")
            cancelled = close_slot_rows(cur, slot_id, reason, now)
        conn.commit()
    except Exception:
        _safe(conn.rollback)
        raise
    slot_closed(deps, doctor_id, slot_id, cancelled)
    total = sum(r["amount"] for _, r in cancelled)
    return 200, {"slot_id": slot_id, "status": "CLOSED", "cancelled_appointments": len(cancelled),
                 "refunds_total": money(total) or 0.0}


# ----------------------------------------------------------------------------- what happened at the appointment
ATTENDANCE_STATUS = {"no_show": "NO_SHOW", "completed": "COMPLETED"}


def record_attendance(conn, deps, user_id, appointment_id, outcome):
    """POST /doctors/me/appointments/{id}/no-show or /complete : the doctor records what happened.

    Allowed once the appointment time has come, and only for the doctor's own CONFIRMED appointment.
    NO_SHOW keeps the fee (a 0 % refund row is written so the ledger shows the decision); COMPLETED
    needs no refund. Either way, when nobody else in the slot is still CONFIRMED the slot is CLOSED.
    The no-show sweeper (application/jobs/no_show.py) marks the ones the doctor forgot.
    """
    status = ATTENDANCE_STATUS[outcome]
    now = deps.now()
    try:
        with conn.cursor() as cur:
            doctor_id = _doctor_id(cur, user_id)
            cur.execute("SELECT appointment_id, patient_id, slot_id, status, fee_amount, starts_at_utc FROM appointments "
                        "WHERE appointment_id = %s AND doctor_id = %s FOR UPDATE", (appointment_id, doctor_id))
            appt = cur.fetchone()
            if not appt:
                raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Appointment was not found.")
            if appt["status"] != "CONFIRMED":
                raise ApiError(409, "NOT_CONFIRMED", "Only a confirmed appointment can be marked.")
            if now < _dt(appt["starts_at_utc"]):
                raise ApiError(409, "APPOINTMENT_NOT_STARTED",
                               "The appointment time has not come yet. Use cancel if the slot cannot be kept.")
            cur.execute("UPDATE appointments SET status = %s WHERE appointment_id = %s AND status = 'CONFIRMED'",
                        (status, appointment_id))
            if cur.rowcount != 1:
                raise ApiError(409, "NOT_CONFIRMED", "Only a confirmed appointment can be marked.")
            if status == "NO_SHOW":
                _insert_refund(cur, appointment_id, appt["fee_amount"], refunds.no_show_refund(appt["fee_amount"]))
            slot_closed_now = close_slot_if_resolved(cur, appt["slot_id"])
        conn.commit()
    except Exception:
        _safe(conn.rollback)
        raise
    deps.metrics.put("NoShows" if status == "NO_SHOW" else "AppointmentsCompleted", 1)
    deps.events.publish(events.APPOINTMENT_NO_SHOW if status == "NO_SHOW" else "AppointmentCompleted", {
        "appointment_id": appointment_id, "patient_id": appt["patient_id"], "doctor_id": doctor_id,
        "slot_id": appt["slot_id"], "marked_by": "doctor"})
    with conn.cursor() as cur:
        result = _fetch_view(cur, appointment_id, now, for_doctor=True)
    conn.rollback()
    result["slot_closed"] = slot_closed_now
    return 200, result


# ----------------------------------------------------------------------------- refund ledger
def list_refunds(conn, deps, user_id, q):
    """GET /refunds : the caller's refund ledger, newest first."""
    q = q or {}
    limit, offset = _paging(q)
    with conn.cursor() as cur:
        patient_id = _patient_id(cur, user_id)
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(r.refund_amount), 0) AS total "
                    "FROM refund_ledger r JOIN appointments a ON a.appointment_id = r.appointment_id "
                    "WHERE a.patient_id = %s", (patient_id,))
        agg = cur.fetchone()
        cur.execute(
            "SELECT r.refund_id, r.appointment_id, r.refund_amount, r.refund_percentage, r.fee_amount, r.refund_tier, "
            "r.refund_status, r.created_at, a.status AS appointment_status, a.local_date, a.local_start, "
            "a.starts_at_utc, ct.type_name, d.first_name, d.last_name "
            "FROM refund_ledger r JOIN appointments a ON a.appointment_id = r.appointment_id "
            "JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id "
            "JOIN doctors d ON d.doctor_id = a.doctor_id "
            "WHERE a.patient_id = %s ORDER BY r.created_at DESC, r.refund_id LIMIT %s OFFSET %s",
            (patient_id, limit, offset))
        rows = cur.fetchall()
    conn.rollback()
    items = [{
        "refund_id": r["refund_id"], "appointment_id": r["appointment_id"], "tier": r["refund_tier"],
        "percentage": float(r["refund_percentage"]), "amount": money(r["refund_amount"]), "fee": money(r["fee_amount"]),
        "status": r["refund_status"], "appointment_status": r["appointment_status"],
        "doctor_name": f"Dr. {r['first_name']} {r['last_name']}", "type_name": r["type_name"],
        "appointment_date": as_date(r["local_date"]).isoformat() if r["local_date"] else None,
        "appointment_start": fmt_time(r["local_start"]) if r["local_start"] else None,
        "created_at": fmt_utc(r["created_at"])} for r in rows]
    total = _count(agg)
    return 200, {"items": items, "count": len(items), "total": total, "limit": limit, "offset": offset,
                 "has_more": offset + len(items) < total, "total_refunded": money(agg["total"]) or 0.0}
