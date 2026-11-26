"""What happens to the slots when a doctor changes the weekly availability or the timezone.

The weekly patterns describe which slots should exist. When they change:

  1 FUTURE slots that nobody booked are simply deleted and generated again from the new patterns.
  2 FUTURE slots that someone booked and that still match the new patterns are kept untouched.
  3 FUTURE slots that someone booked but that the new patterns no longer produce (other start time,
    other consultation type or length, pattern removed or switched off, capacity now too small) are
    CANCELLED by the doctor: every patient in them is refunded 100 % (tier DOCTOR_CANCELLED), the slot is
    CLOSED (so it can never come back as an open slot), seats are returned and AppointmentCancelled
    events tell the patients.

Without step 3 the old booked slot (for example a 13:30-13:45 Quick Consult) stays next to the new slots
(13:00-14:00 Detailed Consult) and overlaps them. This is the one place that does steps 1-3.
"""

import logging
from datetime import datetime, timezone

from application.booking import service
from application.booking.deps import default_deps
from application.common.api import money
from application.slot_materialiser import core as slots

log = logging.getLogger()

REASON = "Cancelled because the doctor changed their availability"


def rebuild(conn, doctor_id, tz_name, deps=None, now_utc=None):
    """Re-materialise the future slots and cancel the booked slots that no longer fit. Commits.

    Returns the materialiser summary plus slots_cancelled, appointments_cancelled and refunds_total.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    stale = slots.stale_booked_slots(conn, doctor_id, tz_name, now_utc)
    closed = []  # (slot_id, [(appointment, refund)])
    if stale:
        deps = deps or default_deps()
        now = now_utc.replace(tzinfo=None)
        with conn.cursor() as cur:
            for slot_id in stale:
                cur.execute("SELECT slot_id FROM materialised_slots WHERE slot_id = %s FOR UPDATE", (slot_id,))
                closed.append((slot_id, service.close_slot_rows(cur, slot_id, REASON, now)))
    slots.sync_kept_slots(conn, doctor_id, tz_name, now_utc)
    summary = slots.rebuild_future_slots(conn, doctor_id, tz_name, now_utc)
    conn.commit()  # the rows are final: only now are seats returned and patients told (those cannot be rolled back)
    for slot_id, cancelled in closed:
        service.slot_closed(deps, doctor_id, slot_id, cancelled)
    appointments = [pair for _, cancelled in closed for pair in cancelled]
    summary.update({
        "slots_cancelled": len(closed),
        "appointments_cancelled": len(appointments),
        "refunds_total": money(sum(refund["amount"] for _, refund in appointments)) or 0.0,
    })
    return summary
