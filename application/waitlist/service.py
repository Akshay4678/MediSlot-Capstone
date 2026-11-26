from application.common.api import ApiError, as_date, fmt_time
from application.booking import events as booking_events


def join(conn, store, patient_id, slot_id, events=None):
    if not slot_id:
        raise ApiError(400, "INVALID_SLOT_ID", "slot_id is required.")
    with conn.cursor() as cur:
        cur.execute("SELECT slot_id, doctor_id, consultation_type_id, status, booked_count, capacity, start_utc FROM materialised_slots WHERE slot_id = %s", (slot_id,))
        slot = cur.fetchone()
    if not slot:
        raise ApiError(404, "SLOT_NOT_FOUND", "Slot was not found.")
    if slot["status"] in ("BLOCKED", "CLOSED"):
        raise ApiError(409, "SLOT_UNAVAILABLE", "This slot is not accepting waitlist entries.")
    if int(slot["booked_count"]) < int(slot["capacity"]):
        raise ApiError(409, "SLOT_AVAILABLE", "This slot is available for normal booking.")
    try:
        item, created = store.join(slot_id, patient_id, slot["doctor_id"], slot["consultation_type_id"])
    except RuntimeError as exc:
        if str(exc) == "WAITLIST_FULL":
            raise ApiError(409, "WAITLIST_FULL", "This slot already has the maximum number of waitlisted patients.") from None
        raise
    if not created:
        return 200, {"waitlist": item, "already_joined": True}
    if events:
        events.publish(booking_events.WAITLIST_JOINED, {"waitlist_id": item["waitlist_id"], "slot_id": slot_id, "patient_id": patient_id, "doctor_id": slot["doctor_id"]})
    return 201, {"waitlist": item, "already_joined": False}


def leave(store, patient_id, slot_id, events=None):
    existing = store.get(slot_id, patient_id)
    if existing and existing.get("status") == "HELD":
        raise ApiError(409, "OFFER_ACTIVE", "You already have a live offer for this slot; accept it or wait for it to expire.")
    if not store.leave(slot_id, patient_id):
        raise ApiError(404, "WAITLIST_ENTRY_NOT_FOUND", "Waitlist entry was not found.")
    if events:
        events.publish(booking_events.WAITLIST_LEFT, {"slot_id": slot_id, "patient_id": patient_id})
    return 200, {"slot_id": slot_id, "status": "REMOVED"}


def list_for_patient(conn, store, patient_id):
    rows = store.table.query(
        IndexName="patient_id-index",
        KeyConditionExpression="patient_id = :p",
        ExpressionAttributeValues={":p": str(patient_id)},
    )
    raw_items = rows.get("Items", []) if isinstance(rows, dict) else []
    items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        status = store._decode(raw_item.get("status"))
        if status in ("WAITING", "HELD"):
            items.append(store._decode_item(raw_item))
    if not items:
        return 200, {"items": [], "count": 0}

    slot_ids = [item["slot_id"] for item in items]
    placeholders = ", ".join(["%s"] * len(slot_ids))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.slot_id, s.doctor_id, s.consultation_type_id, s.`date`, s.start_time, s.end_time, "
            "s.timezone, s.status, s.capacity, s.booked_count, ct.type_name "
            "FROM materialised_slots s "
            "JOIN consultation_types ct ON ct.consultation_type_id = s.consultation_type_id "
            f"WHERE s.slot_id IN ({placeholders})",
            tuple(slot_ids),
        )
        slot_rows = {row["slot_id"]: row for row in cur.fetchall()}
    conn.rollback()

    for item in items:
        row = slot_rows.get(item["slot_id"])
        if row:
            item["slot"] = {
                "slot_id": row["slot_id"],
                "doctor_id": row["doctor_id"],
                "consultation_type_id": row["consultation_type_id"],
                "date": as_date(row["date"]).isoformat(),
                "start_time": fmt_time(row["start_time"]),
                "end_time": fmt_time(row["end_time"]),
                "timezone": row["timezone"],
                "status": row["status"],
                "capacity": int(row["capacity"]),
                "booked_count": int(row["booked_count"]),
                "type_name": row["type_name"],
            }
    return 200, {"items": items, "count": len(items)}


def promote_first(conn, deps, slot_id):
    """Claim one free seat and issue exactly one live hold offer."""
    existing = deps.waitlist.get_hold(slot_id)
    if existing and existing.get("status") == "LIVE" and int(existing.get("expires_at", 0)) > int(deps.now().timestamp()):
        return None
    entry = deps.waitlist.first_waiting(slot_id)
    if not entry:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT capacity, status FROM materialised_slots WHERE slot_id = %s", (slot_id,))
        slot = cur.fetchone()
    if not slot or slot["status"] in ("BLOCKED", "CLOSED"):
        return None
    reservation = deps.slots.reserve(slot_id, int(slot["capacity"]))
    rds_counted = False
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE materialised_slots SET booked_count = booked_count + 1, status = CASE WHEN booked_count + 1 >= capacity THEN 'FULL' ELSE 'OPEN' END WHERE slot_id = %s AND status = 'OPEN' AND booked_count < capacity", (slot_id,))
            if cur.rowcount != 1:
                raise RuntimeError("SLOT_NO_LONGER_FREE")
            conn.commit(); rds_counted = True
        offer = deps.waitlist.create_hold(slot_id, entry["patient_id"], entry["waitlist_id"])
        if not offer:
            if rds_counted:
                with conn.cursor() as cur:
                    cur.execute("UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0), status = CASE WHEN status='FULL' THEN 'OPEN' ELSE status END WHERE slot_id = %s", (slot_id,)); conn.commit()
            deps.slots.release(slot_id)
            return None
        deps.waitlist.mark_held(slot_id, entry["patient_id"], offer["offer_id"], offer["expires_at"])
    except Exception:
        try: conn.rollback()
        except Exception: pass
        if rds_counted:
            with conn.cursor() as cur:
                cur.execute("UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0), status = CASE WHEN status='FULL' THEN 'OPEN' ELSE status END WHERE slot_id = %s", (slot_id,)); conn.commit()
        deps.slots.release(slot_id)
        raise
    deps.events.publish(booking_events.WAITLIST_HOLD_OFFERED, {
        "event_id": offer["offer_id"], "slot_id": slot_id, "patient_id": entry["patient_id"],
        "offer_id": offer["offer_id"], "expires_at": offer["expires_at"]})
    deps.metrics.put("WaitlistPromotions", 1)
    return offer


def accept_offer(conn, deps, patient_id, offer_id):
    hold = deps.waitlist.accept_hold(offer_id, patient_id)
    if not hold:
        raise ApiError(409, "OFFER_EXPIRED_OR_INVALID", "This waitlist offer is no longer valid.")
    slot_id = hold["slot_id"]
    now = deps.now()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT slot_id, doctor_id, consultation_type_id, status, booked_count, capacity, fee, start_utc, end_utc, date, start_time, end_time, timezone FROM materialised_slots WHERE slot_id = %s FOR UPDATE", (slot_id,))
            slot = cur.fetchone()
            if not slot or slot["status"] in ("BLOCKED", "CLOSED"):
                raise ApiError(409, "SLOT_UNAVAILABLE", "The held slot is no longer available.")
            cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND slot_id = %s AND status = 'CONFIRMED'", (patient_id, slot_id))
            if int(cur.fetchone()["n"]) > 0:
                raise ApiError(409, "ALREADY_BOOKED", "You already have an appointment in this slot.")
            cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND status = 'CONFIRMED' AND starts_at_utc < %s AND ends_at_utc > %s", (patient_id, slot["end_utc"], slot["start_utc"]))
            if int(cur.fetchone()["n"]) > 0:
                raise ApiError(409, "TIME_CONFLICT", "You already have an appointment that overlaps this time.")
            from application.booking.service import new_id, _fetch_view
            appointment_id = new_id()
            cur.execute("INSERT INTO appointments (appointment_id, patient_id, doctor_id, slot_id, consultation_type_id, status, notes, fee_amount, idempotency_key, starts_at_utc, ends_at_utc, local_date, local_start, local_end, timezone) VALUES (%s, %s, %s, %s, %s, 'CONFIRMED', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (appointment_id, patient_id, slot["doctor_id"], slot_id, slot["consultation_type_id"], None, slot["fee"], f"WAITLIST:{offer_id}", slot["start_utc"], slot["end_utc"], slot["date"], slot["start_time"], slot["end_time"], slot["timezone"]))
            # The held seat was already counted in materialised_slots when the offer was issued.
            # Accepting the offer therefore creates the appointment without incrementing booked_count again.
            conn.commit()
            deps.waitlist.mark_promoted(slot_id, patient_id)
            deps.waitlist.decrement_active_count(slot_id)
    except Exception:
        try: conn.rollback()
        except Exception: pass
        # The hold was consumed only after the transaction was validated; release the reserved seat on failure.
        deps.waitlist.hold_table.update_item(Key={"slot_id": slot_id}, UpdateExpression="SET #s = :s", ConditionExpression="#s = :accepted", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "LIVE", ":accepted": "ACCEPTED"})
        with conn.cursor() as cur:
            cur.execute("UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0), status = CASE WHEN status='FULL' THEN 'OPEN' ELSE status END WHERE slot_id = %s", (slot_id,)); conn.commit()
        deps.slots.release(slot_id)
        raise
    deps.events.publish(booking_events.APPOINTMENT_BOOKED, {"event_id": appointment_id, "appointment_id": appointment_id, "patient_id": patient_id, "doctor_id": slot["doctor_id"], "slot_id": slot_id, "source": "WAITLIST_OFFER_ACCEPTED"})
    deps.events.publish(booking_events.WAITLIST_OFFER_ACCEPTED, {"event_id": offer_id, "offer_id": offer_id, "slot_id": slot_id, "patient_id": patient_id, "appointment_id": appointment_id})
    deps.metrics.put("BookingsConfirmed", 1)
    return 201, {"appointment_id": appointment_id, "slot_id": slot_id, "status": "CONFIRMED"}


def expire_offers(conn, deps):
    now = int(deps.now().timestamp())
    rows = deps.waitlist.hold_table.scan(FilterExpression="#s = :live AND expires_at <= :now", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":live": "LIVE", ":now": now}).get("Items", [])
    expired = 0
    for hold in rows:
        if not deps.waitlist.expire_hold(hold["slot_id"], hold["offer_id"]):
            continue
        deps.waitlist.mark_waiting(hold["slot_id"], hold["patient_id"])
        with conn.cursor() as cur:
            cur.execute("UPDATE materialised_slots SET booked_count = GREATEST(booked_count - 1, 0), status = CASE WHEN status='FULL' THEN 'OPEN' ELSE status END WHERE slot_id = %s", (hold["slot_id"],))
            conn.commit()
        deps.slots.release(hold["slot_id"])
        deps.events.publish(booking_events.OFFERS_EXPIRED, {"event_id": hold["offer_id"], "offer_id": hold["offer_id"], "slot_id": hold["slot_id"], "patient_id": hold["patient_id"]})
        deps.events.publish(booking_events.SLOT_SEAT_FREED, {"event_id": f"expired:{hold['offer_id']}", "slot_id": hold["slot_id"], "seats_freed": 1})
        expired += 1
    if expired: deps.metrics.put("OffersExpired", expired)
    return expired
