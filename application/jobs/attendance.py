"""Attendance sweeper (every minute).

The doctor can mark an appointment Attended or No-show only while its slot is running (start to end). An appointment
that is still CONFIRMED when its slot has ended was not marked: it is recorded as COMPLETED (attended), and the slot
is closed when nobody else in it is still CONFIRMED. The service already SHOWS such an appointment as attended the
moment the slot ends (service._view), so this job only has to persist the answer; it is not time critical.
The UPDATE only touches rows that are still CONFIRMED, so running the sweeper twice (or while the doctor marks the same
appointment) changes each appointment once.

It also publishes two gauges for the dashboard: NoShowRate (last 30 days, percent) and SlotUtilisationPercent
(seats taken in the next 7 days, percent).
"""

import logging
from datetime import timedelta

from application.booking import events
from application.booking.service import close_slot_if_resolved

log = logging.getLogger()
BATCH = 500


def run(conn, deps):
    now = deps.now()
    with conn.cursor() as cur:
        cur.execute("SELECT appointment_id, patient_id, doctor_id, slot_id FROM appointments WHERE status = 'CONFIRMED' "
                    "AND ends_at_utc <= %s ORDER BY ends_at_utc LIMIT %s", (now, BATCH))
        due = cur.fetchall()
    conn.rollback()
    completed = 0
    for appt in due:
        with conn.cursor() as cur:
            cur.execute("UPDATE appointments SET status = 'COMPLETED' WHERE appointment_id = %s AND status = 'CONFIRMED'",
                        (appt["appointment_id"],))
            if cur.rowcount != 1:
                conn.rollback()
                continue
            close_slot_if_resolved(cur, appt["slot_id"])
        conn.commit()
        completed += 1
        deps.events.publish(events.APPOINTMENT_COMPLETED, {
            "appointment_id": appt["appointment_id"], "patient_id": appt["patient_id"], "doctor_id": appt["doctor_id"],
            "slot_id": appt["slot_id"], "marked_by": "system"})
    if completed:
        deps.metrics.put("AppointmentsCompleted", completed)
    _gauges(conn, deps, now)
    return {"auto_completed": completed}


def _gauges(conn, deps, now):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(CASE WHEN status = 'NO_SHOW' THEN 1 ELSE 0 END) AS missed, "
                        "SUM(CASE WHEN status IN ('NO_SHOW', 'COMPLETED') THEN 1 ELSE 0 END) AS seen "
                        "FROM appointments WHERE ends_at_utc > %s AND ends_at_utc <= %s",
                        (now - timedelta(days=30), now))
            row = cur.fetchone() or {}
            seen = int(row.get("seen") or 0)
            if seen:
                deps.metrics.put("NoShowRate", 100.0 * int(row.get("missed") or 0) / seen, unit="Percent")
            cur.execute("SELECT SUM(booked_count) AS booked, SUM(capacity) AS cap FROM materialised_slots "
                        "WHERE start_utc > %s AND start_utc <= %s AND status IN ('OPEN', 'FULL')",
                        (now, now + timedelta(days=7)))
            row = cur.fetchone() or {}
            cap = int(row.get("cap") or 0)
            if cap:
                deps.metrics.put("SlotUtilisationPercent", 100.0 * int(row.get("booked") or 0) / cap, unit="Percent")
        conn.rollback()
    except Exception:  # noqa: BLE001 - gauges are nice to have
        log.exception("could not publish the gauges")
        conn.rollback()
