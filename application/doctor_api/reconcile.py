"""What happens to the slots when a doctor changes the weekly availability or the timezone.

A change of availability NEVER cancels anybody's appointment. When the weekly patterns change:

  1 FUTURE slots that nobody booked are deleted and generated again from the new patterns.
  2 FUTURE slots that someone booked (or that a waitlisted patient holds) are kept exactly as they were: same
    time, same consultation type, same patients. Only a changed capacity is carried over (OPEN <-> FULL).
  3 A NEW slot is created only where it does not overlap a kept slot. A candidate that would overlap one is
    skipped and reported back (`skipped_overlapping`), so the doctor sees which times were left out and why.

Example: Tuesday 13:00-15:00 Quick Consult, a patient booked 13:45-14:00. The doctor switches the pattern to
13:00-15:00 Detailed Consult. 13:45-14:00 stays booked, 13:00-14:00 is skipped (it overlaps the booking) and
14:00-15:00 is created. To move that patient the doctor cancels the slot on purpose (100 % refund).
"""

from datetime import datetime, timezone

from application.slot_materialiser import core as slots


def rebuild(conn, doctor_id, tz_name, deps=None, now_utc=None):
    """Re-materialise the future slots around the kept booked slots. Commits.

    Returns the materialiser summary: inserted, removed_unbooked, skipped_overlapping (list), kept_booked (count).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    slots.sync_kept_slots(conn, doctor_id, tz_name, now_utc)
    summary = slots.rebuild_future_slots(conn, doctor_id, tz_name, now_utc)
    with conn.cursor() as cur:
        dates = slots.window_dates(now_utc, tz_name, summary["window_days"])
        first, last = dates[0], dates[-1]
        kept = len(slots._held_slots(cur, doctor_id, first, last, now_utc))
    summary["kept_booked"] = kept
    conn.commit()
    return summary
