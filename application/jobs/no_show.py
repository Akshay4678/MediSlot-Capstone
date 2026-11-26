"""No-show sweeper (every 15 minutes).

An appointment still CONFIRMED 30 minutes after its slot ended (SSM rules/no-show-minutes) is marked NO_SHOW,
gets a 0 % refund row (the fee is kept) and, when nobody else in the slot is still CONFIRMED, the slot is
closed. The UPDATE only touches rows that are still CONFIRMED, so running the sweeper twice (or while the
doctor marks the same appointment by hand) changes each appointment once.

It also publishes two gauges for the dashboard: NoShowRate (last 30 days, percent) and SlotUtilisationPercent
(seats taken in the next 7 days, percent).
"""

import logging
from datetime import timedelta

from application.booking import refunds
from application.booking.service import _insert_refund, close_slot_if_resolved
from application.common import rules

log = logging.getLogger()
BATCH = 500


def run(conn, deps):
    now = deps.now()
    cutoff = now - timedelta(minutes=rules.no_show_minutes())
    with conn.cursor() as cur:
        cur.execute("SELECT appointment_id, slot_id, fee_amount FROM appointments WHERE status = 'CONFIRMED' "
                    "AND ends_at_utc <= %s ORDER BY ends_at_utc LIMIT %s", (cutoff, BATCH))
        due = cur.fetchall()
    conn.rollback()
    marked = 0
    for appt in due:
        with conn.cursor() as cur:
            cur.execute("UPDATE appointments SET status = 'NO_SHOW' WHERE appointment_id = %s AND status = 'CONFIRMED'",
                        (appt["appointment_id"],))
            if cur.rowcount != 1:
                conn.rollback()
                continue
            _insert_refund(cur, appt["appointment_id"], appt["fee_amount"], refunds.no_show_refund(appt["fee_amount"]))
            close_slot_if_resolved(cur, appt["slot_id"])
        conn.commit()
        marked += 1
    if marked:
        deps.metrics.put("NoShows", marked)
    _gauges(conn, deps, now)
    return {"marked_no_show": marked}


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
