"""Reminder dispatcher (every 5 minutes): ReminderDue events for appointments 24 hours and 2 hours away.

An appointment is due for the 24H reminder while its start is between 23 and 24 hours away, and for the 2H
reminder while it is between 1 and 2 hours away (the one-hour window means a missed run is caught by the next
ones). The dispatch log item `<appointmentId>#24H` (or #2H) is claimed with a conditional write BEFORE the event
is published, so running the job twice, or two copies at once, emits each reminder once. If publishing fails the
claim is released and the next run tries again.

An appointment booked less than 24 hours ahead simply never enters the 24H window, so it gets no stale reminder.
"""

import logging
from datetime import timedelta

from application.booking import events
from application.common.api import fmt_utc
from application.notification import dispatch_log as dl

log = logging.getLogger()

WINDOWS = (("24H", timedelta(hours=24)), ("2H", timedelta(hours=2)))
WINDOW_WIDTH = timedelta(hours=1)


def run(conn, deps, dispatch_log):
    now = deps.now()
    emitted = 0
    for label, lead in WINDOWS:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT appointment_id, patient_id, doctor_id, slot_id, starts_at_utc FROM appointments "
                "WHERE status = 'CONFIRMED' AND starts_at_utc > %s AND starts_at_utc <= %s ORDER BY starts_at_utc",
                (now + lead - WINDOW_WIDTH, now + lead))
            rows = cur.fetchall()
        conn.rollback()
        for row in rows:
            key = f"{row['appointment_id']}#{label}"
            if dispatch_log.claim(key, reminder=label) != dl.CLAIMED:
                continue
            starts = row["starts_at_utc"]
            if isinstance(starts, str):
                from datetime import datetime
                starts = datetime.fromisoformat(starts.replace(" ", "T"))
            ok = deps.events.publish(events.REMINDER_DUE, {
                "appointment_id": row["appointment_id"], "patient_id": row["patient_id"], "doctor_id": row["doctor_id"],
                "slot_id": row["slot_id"], "reminder_type": label, "starts_at_utc": fmt_utc(starts)})
            if ok:
                dispatch_log.mark_sent(key)
                emitted += 1
            else:
                dispatch_log.release(key)
    return {"reminders_emitted": emitted}
