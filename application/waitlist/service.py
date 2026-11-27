"""Waitlist, hold offers and their expiry (Milestone 6).

The rules (frozen rule set)
  * A patient may join the waitlist of a FULL slot; the queue is ordered by join time.
  * A patient may be on at most 5 waitlists at once (SSM rules/waitlist-max-size).
  * When a seat frees, the first waitlisted patient receives a HOLD OFFER. The hold lasts 15 minutes
    (SSM rules/hold-offer-minutes) and during it nobody else can book that seat.
  * Accepting converts the hold into a CONFIRMED appointment and the patient leaves the waitlist.
  * An offer that is not accepted expires and the SAME seat passes to the next patient in line.
  * The same freed seat is never offered to two patients at once.

How exclusivity is guaranteed (read this before the mentor review)
  A hold is not a flag, it is a real seat. Issuing an offer first takes a seat with the normal booking gate,
  SlotStore.reserve() (DynamoDB conditional write on version and capacity). Capacity bookings can never
  exceed the slot capacity, so with ONE freed seat only ONE reserve() can succeed, however many workers
  run at once; every other worker gets SlotFull and does nothing. The seat stays counted (DynamoDB counter
  and RDS booked_count) for the whole hold, which is also why a held seat is invisible to normal booking:
  the slot simply looks full. On top of that the queue entry is claimed with a conditional write
  (WAITING -> OFFERED), so one patient can never hold two offers for the same slot.

  accept()  : LIVE -> ACCEPTED (conditional: still LIVE and not expired), then the appointment is written.
  sweeper   : LIVE -> EXPIRED (conditional: still LIVE and due), then the seat moves to the next patient.
  Both are conditional writes on the same item, so an accept and an expiry that race produce exactly one winner.
"""

import logging
from datetime import timedelta

from application.booking import events
from application.booking.errors import ContentionExceeded, SlotFull
from application.booking.service import (_APPT_SELECT, _check_slot_and_patient, _count, _dt, _fetch_view,
                                         _free_slot_row, _load_slot, _patient_id, _safe)
from application.common import rules
from application.common.api import ApiError, as_date, fmt_time, fmt_utc, new_id
from application.waitlist import store as wl

log = logging.getLogger()


def _require_store(deps):
    if getattr(deps, "waitlist", None) is None:
        raise ApiError(503, "WAITLIST_UNAVAILABLE", "The waitlist is temporarily unavailable.")
    return deps.waitlist


def _slot_usable(slot, now):
    return (slot is not None and slot["status"] in ("OPEN", "FULL") and _dt(slot["start_utc"]) > now
            and _dt(slot["start_utc"]) <= now + timedelta(days=rules.booking_window_days()))


# ----------------------------------------------------------------------------- join / leave / list
def join(conn, deps, user_id, slot_id):
    """POST /slots/{slotId}/waitlist"""
    store, now = _require_store(deps), deps.now()
    try:
        with conn.cursor() as cur:
            patient_id = _patient_id(cur, user_id)
            cur.execute("SELECT patient_id FROM patients WHERE patient_id = %s FOR UPDATE", (patient_id,))
            slot = _load_slot(cur, slot_id)
            if not slot:
                raise ApiError(404, "SLOT_NOT_FOUND", "Slot was not found.")
            if _dt(slot["start_utc"]) <= now:
                raise ApiError(409, "SLOT_IN_PAST", "This slot has already started or finished.")
            if slot["status"] in ("BLOCKED", "CLOSED"):
                raise ApiError(409, "SLOT_UNAVAILABLE", "This slot is no longer available.")
            if slot["status"] != "FULL" and int(slot["booked_count"]) < int(slot["capacity"]):
                raise ApiError(409, "SLOT_NOT_FULL", "This slot still has free seats: book it directly.")
            cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND slot_id = %s "
                        "AND status = 'CONFIRMED'", (patient_id, slot_id))
            if _count(cur.fetchone()):
                raise ApiError(409, "ALREADY_BOOKED", "You already have an appointment in this slot.")
        conn.rollback()  # the patient lock is only needed while we check and add
        limit = rules.waitlist_max_size()
        mine = store.entries_for_patient(patient_id, now)
        if any(e["slot_id"] == slot_id for e in mine):
            raise ApiError(409, "ALREADY_WAITLISTED", "You are already on the waitlist of this slot.")
        if len(mine) >= limit:
            raise ApiError(409, "WAITLIST_LIMIT_REACHED", f"You can be on at most {limit} waitlists at once.")
        if not store.join(slot_id, patient_id, new_id(), now, _dt(slot["end_utc"])):
            raise ApiError(409, "ALREADY_WAITLISTED", "You are already on the waitlist of this slot.")
    except Exception:
        _safe(conn.rollback)
        raise
    # A seat may have been freed between our check and our join: no event will come for it, so look now.
    offer_next(conn, deps, slot_id)
    return 201, {"waitlist": _entry_view(conn, deps, patient_id, store.get_entry(slot_id, patient_id), now)}


def leave(conn, deps, user_id, slot_id):
    """DELETE /slots/{slotId}/waitlist. Leaving while holding an offer gives the seat to the next patient."""
    store, now = _require_store(deps), deps.now()
    with conn.cursor() as cur:
        patient_id = _patient_id(cur, user_id)
    conn.rollback()
    entry = store.get_entry(slot_id, patient_id)
    if not entry:
        raise ApiError(404, "NOT_ON_WAITLIST", "You are not on the waitlist of this slot.")
    offer = store.live_offer_for(slot_id, patient_id) if entry.get("status") == wl.OFFERED else None
    store.remove(slot_id, patient_id)
    handed_over = False
    if offer and store.transition(offer["offer_id"], wl.LIVE, wl.CANCELLED):
        _hand_over_seat(conn, deps, slot_id, now)
        handed_over = True
    return 200, {"slot_id": slot_id, "left": True, "seat_passed_on": handed_over}


def _entry_view(conn, deps, patient_id, entry, now):
    store = deps.waitlist
    if entry is None:
        return None
    queue = store.entries_for_slot(entry["slot_id"])
    position = next((i for i, e in enumerate(queue, 1) if e["patient_id"] == patient_id), None)
    offer = store.live_offer_for(entry["slot_id"], patient_id)
    view = {"slot_id": entry["slot_id"], "status": entry.get("status", wl.WAITING), "position": position,
            "joined_at": entry.get("joined_at"), "offer": None}
    if offer:
        view["offer"] = {"offer_id": offer["offer_id"], "expires_at": offer["expires_at_utc"] + "Z",
                         "seconds_left": max(0, offer["expires_at"] - wl.epoch(now))}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.`date`, s.start_time, s.end_time, s.start_utc, ct.type_name, d.first_name, d.last_name, "
            "h.hospital_name FROM materialised_slots s JOIN consultation_types ct ON ct.consultation_type_id = s.consultation_type_id "
            "JOIN doctors d ON d.doctor_id = s.doctor_id JOIN hospitals h ON h.hospital_id = d.hospital_id "
            "WHERE s.slot_id = %s", (entry["slot_id"],))
        row = cur.fetchone()
    conn.rollback()
    if row:
        view.update({"date": as_date(row["date"]).isoformat(), "start_time": fmt_time(row["start_time"]),
                     "end_time": fmt_time(row["end_time"]), "type_name": row["type_name"],
                     "doctor_name": f"Dr. {row['first_name']} {row['last_name']}", "hospital_name": row["hospital_name"],
                     "start_utc": fmt_utc(_dt(row["start_utc"]))})
    return view


def list_mine(conn, deps, user_id):
    """GET /patients/me/waitlists : my queues with position and any live offer."""
    store, now = _require_store(deps), deps.now()
    with conn.cursor() as cur:
        patient_id = _patient_id(cur, user_id)
    conn.rollback()
    items = [_entry_view(conn, deps, patient_id, e, now) for e in store.entries_for_patient(patient_id, now)]
    items = [i for i in items if i and i.get("start_utc")]
    items.sort(key=lambda i: i["start_utc"])
    return 200, {"items": items, "count": len(items), "limit": rules.waitlist_max_size()}


# ----------------------------------------------------------------------------- issuing offers
def _eligible(cur, deps, patient_id, slot, now):
    """Would this patient be allowed to book the slot? (the same checks as a normal booking, minus seat/status)"""
    probe = dict(slot, status="OPEN", booked_count=0)
    try:
        _check_slot_and_patient(cur, deps, patient_id, probe, now)
    except ApiError:
        return False
    cur.execute("SELECT COUNT(*) AS n FROM patients p JOIN users u ON u.user_id = p.user_id "
                "WHERE p.patient_id = %s AND p.is_active = TRUE AND u.is_active = TRUE", (patient_id,))
    return bool(_count(cur.fetchone()))


def _issue(conn, deps, slot, waiters, now, rds_counted):
    """Hand ONE already-reserved seat to the first eligible waiting patient. Returns the offer or None.

    The seat must already be taken in DynamoDB. rds_counted says whether materialised_slots.booked_count
    already includes it (true when a seat is passed from one expired offer to the next).
    """
    store, slot_id = deps.waitlist, slot["slot_id"]
    if not rds_counted:
        with conn.cursor() as cur:
            cur.execute("UPDATE materialised_slots SET booked_count = booked_count + 1 "
                        "WHERE slot_id = %s AND status IN ('OPEN', 'FULL') AND booked_count < capacity", (slot_id,))
            took = cur.rowcount == 1
            if took:
                cur.execute("UPDATE materialised_slots SET status = 'FULL' "
                            "WHERE slot_id = %s AND status = 'OPEN' AND booked_count >= capacity", (slot_id,))
        conn.commit()
        if not took:
            _safe(deps.slots.release, slot_id)
            return None

    hold = timedelta(minutes=rules.hold_offer_minutes())
    expires = min(now + hold, _dt(slot["start_utc"]))
    for entry in waiters:
        patient_id = entry["patient_id"]
        with conn.cursor() as cur:
            ok = _eligible(cur, deps, patient_id, slot, now)
        conn.rollback()
        if not ok or not store.claim(slot_id, patient_id):
            continue  # not allowed to book this slot right now, or another worker claimed them first
        offer_id = new_id()
        try:
            store.create_offer(offer_id, slot_id, patient_id, now, expires)
        except Exception:
            store.unclaim(slot_id, patient_id)
            raise
        deps.metrics.put("OffersIssued", 1)
        deps.events.publish(events.WAITLIST_OFFER_ISSUED, {
            "offer_id": offer_id, "slot_id": slot_id, "patient_id": patient_id, "doctor_id": slot["doctor_id"],
            "expires_at_utc": wl.iso(expires) + "Z", "starts_at_utc": fmt_utc(_dt(slot["start_utc"])),
            "hold_minutes": rules.hold_offer_minutes()})
        return {"offer_id": offer_id, "patient_id": patient_id, "slot_id": slot_id, "expires_at": expires}

    # Nobody could take the seat: give it back so it is bookable again.
    _release_seat(conn, deps, slot_id)
    return None


def _release_seat(conn, deps, slot_id):
    with conn.cursor() as cur:
        _free_slot_row(cur, slot_id, 1)
    conn.commit()
    _safe(deps.slots.release, slot_id)


def offer_next(conn, deps, slot_id):
    """A seat of the slot may be free: if people are waiting, hold it for the first one.

    Called by the SlotSeatFreed consumer, by the cancellation itself (fast path) and after a join.
    Safe to call any number of times: it only acts when a REAL free seat can be reserved.
    """
    store, now = _require_store(deps), deps.now()
    with conn.cursor() as cur:
        slot = _load_slot(cur, slot_id)
    conn.rollback()
    if not _slot_usable(slot, now):
        return None
    waiters = store.waiting(slot_id)
    if not waiters:
        return None
    try:
        deps.slots.reserve(slot_id, int(slot["capacity"]))  # THE gate: one free seat, one reserve() wins
    except (SlotFull, ContentionExceeded):
        return None
    return _issue(conn, deps, slot, waiters, now, rds_counted=False)


def _hand_over_seat(conn, deps, slot_id, now):
    """A held seat is no longer wanted by its holder: pass it to the next waiting patient, or free it."""
    store = deps.waitlist
    with conn.cursor() as cur:
        slot = _load_slot(cur, slot_id)
    conn.rollback()
    if _slot_usable(slot, now):
        waiters = store.waiting(slot_id)
        if waiters:
            return _issue(conn, deps, slot, waiters, now, rds_counted=True)
    if slot is not None and slot["status"] in ("OPEN", "FULL"):
        _release_seat(conn, deps, slot_id)
    else:  # slot closed or blocked meanwhile: only the DynamoDB seat is left to return
        _safe(deps.slots.release, slot_id)
    return None


# ----------------------------------------------------------------------------- accepting an offer
def accept(conn, deps, user_id, offer_id):
    """POST /waitlist-offers/{offerId}/accept : turn the hold into a confirmed appointment."""
    store, now = _require_store(deps), deps.now()
    with conn.cursor() as cur:
        patient_id = _patient_id(cur, user_id)
    conn.rollback()
    offer = store.get_offer(str(offer_id))
    if not offer or offer["patient_id"] != patient_id:
        raise ApiError(404, "OFFER_NOT_FOUND", "Offer was not found.")
    key = f"offer-{offer['offer_id']}"
    if offer["status"] == wl.ACCEPTED:  # a retried request: return the appointment that was created
        with conn.cursor() as cur:
            cur.execute(_APPT_SELECT + "WHERE a.patient_id = %s AND a.idempotency_key = %s", (patient_id, key))
            row = cur.fetchone()
        conn.rollback()
        if row:
            from application.booking.service import _view
            return 200, {"appointment": _view(row, now), "idempotent_replay": True}
    if offer["status"] != wl.LIVE or offer["expires_at"] <= wl.epoch(now):
        raise ApiError(409, "OFFER_EXPIRED", "This offer has expired or is no longer available.")
    if not store.transition(offer["offer_id"], wl.LIVE, wl.ACCEPTED, not_after=wl.epoch(now)):
        raise ApiError(409, "OFFER_EXPIRED", "This offer has expired or is no longer available.")

    slot_id, appointment_id = offer["slot_id"], new_id()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT patient_id FROM patients WHERE patient_id = %s FOR UPDATE", (patient_id,))
            slot = _load_slot(cur, slot_id)
            if slot is None or slot["status"] in ("BLOCKED", "CLOSED"):
                raise ApiError(409, "SLOT_UNAVAILABLE", "This slot is no longer available.")
            probe = dict(slot, status="OPEN", booked_count=0)  # the seat is already ours: check only the patient rules
            _check_slot_and_patient(cur, deps, patient_id, probe, now)
            cur.execute(
                "INSERT INTO appointments (appointment_id, patient_id, doctor_id, slot_id, consultation_type_id, status, "
                "notes, fee_amount, idempotency_key, starts_at_utc, ends_at_utc, local_date, local_start, local_end, "
                "timezone) VALUES (%s, %s, %s, %s, %s, 'CONFIRMED', NULL, %s, %s, %s, %s, %s, %s, %s, %s)",
                (appointment_id, patient_id, slot["doctor_id"], slot_id, slot["consultation_type_id"], slot["fee"], key,
                 _dt(slot["start_utc"]), _dt(slot["end_utc"]), slot["date"], slot["start_time"], slot["end_time"],
                 slot["timezone"]))
        conn.commit()
    except Exception as exc:
        _safe(conn.rollback)
        # The patient cannot take the seat after all: it must not stay blocked, so it moves on.
        if store.transition(offer["offer_id"], wl.ACCEPTED, wl.EXPIRED):
            store.remove(slot_id, patient_id)
            _hand_over_seat(conn, deps, slot_id, now)
        if isinstance(exc, ApiError):
            raise
        if type(exc).__name__ == "IntegrityError":
            raise ApiError(409, "ALREADY_BOOKED", "You already have an appointment in this slot.") from None
        raise

    store.remove(slot_id, patient_id)  # leaves the waitlist
    deps.metrics.put("WaitlistPromotions", 1)
    deps.metrics.put("BookingsConfirmed", 1)
    with conn.cursor() as cur:
        result = _fetch_view(cur, appointment_id, now)
    conn.rollback()
    view = result["appointment"]
    deps.events.publish(events.APPOINTMENT_BOOKED, {
        "appointment_id": appointment_id, "patient_id": patient_id, "doctor_id": slot["doctor_id"], "slot_id": slot_id,
        "consultation_type_id": slot["consultation_type_id"], "starts_at_utc": view["start_utc"],
        "ends_at_utc": view["end_utc"], "fee": view["fee"], "via": "waitlist"})
    return 201, result


# ----------------------------------------------------------------------------- the expiry sweeper
def sweep_expired(conn, deps):
    """Every minute: LIVE offers whose hold ran out become EXPIRED and their seat moves to the next patient.

    Safe to run twice (or twice at once): the LIVE -> EXPIRED write is conditional, so each offer is
    expired by exactly one run, and an offer accepted a moment earlier is left alone.
    """
    store, now = _require_store(deps), deps.now()
    expired = passed_on = 0
    for offer in store.due_offers(now):
        if not store.transition(offer["offer_id"], wl.LIVE, wl.EXPIRED, not_before=wl.epoch(now)):
            continue
        expired += 1
        store.remove(offer["slot_id"], offer["patient_id"])
        deps.metrics.put("OffersExpired", 1)
        deps.events.publish(events.WAITLIST_OFFER_EXPIRED, {
            "offer_id": offer["offer_id"], "slot_id": offer["slot_id"], "patient_id": offer["patient_id"]})
        try:
            if _hand_over_seat(conn, deps, offer["slot_id"], now):
                passed_on += 1
        except Exception:  # noqa: BLE001 - one broken slot must not stop the sweep; the next run retries nothing
            log.exception("could not pass on the seat of %s", offer["slot_id"])
            _safe(conn.rollback)
    return {"expired": expired, "passed_on": passed_on}
