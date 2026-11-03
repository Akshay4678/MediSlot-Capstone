"""Slot materialisation: weekly availability patterns -> dated rows in materialised_slots.

How it works
  * A pattern is a weekly template in the doctor's LOCAL time (e.g. Monday 09:00-12:00,
    Standard Consult). The consultation type supplies the slot length.
  * For every date in the rolling booking window (default 30 days, counted in the doctor's
    local calendar) that matches the pattern's weekday and is not a blackout date, slot
    start times are generated from the pattern start in steps of the consultation duration;
    the last slot must end at or before the pattern end.
  * Local times are converted to UTC with the doctor's IANA timezone (stored on the doctor);
    both the UTC instants and the originating timezone are stored on every slot.
    Daylight-saving transitions are deliberately out of scope: use a timezone without
    daylight saving (the default is Asia/Kolkata).
  * slot_id = doctor_id#availability_id#startUTC is deterministic and INSERT IGNORE is used,
    so running the job again for dates that already exist creates no duplicates.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from application.common import rules
from application.common.api import as_date, as_time

log = logging.getLogger()
UTC = timezone.utc

INSERT_SQL = (
    "INSERT IGNORE INTO materialised_slots "
    "(slot_id, availability_id, doctor_id, consultation_type_id, `date`, start_time, end_time, "
    " start_utc, end_utc, timezone, capacity, booked_count, status) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 'OPEN')"
)


def local_to_utc(naive_local, tz):
    """Convert a naive local datetime in the doctor's timezone to an aware UTC datetime."""
    return naive_local.replace(tzinfo=tz).astimezone(UTC)


def build_slots(doctor_id, pattern, local_date, tz_name, now_utc=None):
    """Slots (dicts) for ONE pattern on ONE local date. Pure function - no database access."""
    tz = ZoneInfo(tz_name)
    duration = timedelta(minutes=int(pattern["duration_minutes"]))
    start_t, end_t = as_time(pattern["start_time"]), as_time(pattern["end_time"])
    cursor = datetime.combine(local_date, start_t)  # naive wall-clock
    limit = datetime.combine(local_date, end_t)

    slots = []
    while cursor + duration <= limit:
        following = cursor + duration
        start_utc = local_to_utc(cursor, tz)
        if now_utc is None or start_utc > now_utc:
            end_utc = start_utc + duration
            slots.append({
                "slot_id": f"{doctor_id}#{pattern['availability_id']}#{start_utc:%Y%m%dT%H%M}Z",
                "availability_id": pattern["availability_id"],
                "doctor_id": doctor_id,
                "consultation_type_id": pattern["consultation_type_id"],
                "date": local_date,
                "start_time": cursor.time(),
                "end_time": following.time(),
                "start_utc": start_utc.replace(tzinfo=None),
                "end_utc": end_utc.replace(tzinfo=None),
                "timezone": tz_name,
                "capacity": int(pattern["capacity"]),
            })
        cursor = following
    return slots


def window_dates(now_utc, tz_name, days):
    today = now_utc.astimezone(ZoneInfo(tz_name)).date()
    return [today + timedelta(days=i) for i in range(days)]


def _fetch_patterns(cur, doctor_id):
    cur.execute(
        "SELECT ap.availability_id, ap.consultation_type_id, ap.day_of_week, ap.start_time, ap.end_time, "
        "       ap.capacity, ct.duration_minutes "
        "FROM availability_patterns ap "
        "JOIN consultation_types ct ON ct.consultation_type_id = ap.consultation_type_id "
        "WHERE ap.doctor_id = %s AND ap.is_active = TRUE",
        (doctor_id,),
    )
    return cur.fetchall()


def materialise_doctor(conn, doctor_id, tz_name, now_utc=None, window_days=None):
    """Create any missing slots for one doctor across the rolling window. Idempotent."""
    now_utc = now_utc or datetime.now(UTC)
    days = window_days or rules.booking_window_days()
    dates = window_dates(now_utc, tz_name, days)

    with conn.cursor() as cur:
        patterns = _fetch_patterns(cur, doctor_id)
        cur.execute(
            "SELECT blackout_date FROM blackout_dates WHERE doctor_id = %s AND blackout_date BETWEEN %s AND %s",
            (doctor_id, dates[0], dates[-1]),
        )
        blackouts = {as_date(row["blackout_date"]) for row in cur.fetchall()}

        slots = []
        for local_date in dates:
            if local_date in blackouts:
                continue
            for pattern in patterns:
                if int(pattern["day_of_week"]) == local_date.isoweekday():
                    slots.extend(build_slots(doctor_id, pattern, local_date, tz_name, now_utc))

        inserted = 0
        if slots:
            cur.executemany(INSERT_SQL, [
                (s["slot_id"], s["availability_id"], s["doctor_id"], s["consultation_type_id"], s["date"],
                 s["start_time"], s["end_time"], s["start_utc"], s["end_utc"], s["timezone"], s["capacity"])
                for s in slots
            ])
            inserted = max(0, cur.rowcount or 0)
    return {"doctor_id": doctor_id, "window_days": days, "candidate_slots": len(slots),
            "inserted": inserted, "already_existing": len(slots) - inserted}


def rebuild_future_slots(conn, doctor_id, tz_name, now_utc=None):
    """After a pattern/timezone change: drop future slots nobody has booked, then re-materialise.

    Slots that already have bookings are never deleted (deterministic ids mean an unchanged
    booked slot is simply left as it is).
    """
    now_utc = now_utc or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM materialised_slots WHERE doctor_id = %s AND start_utc > %s AND booked_count = 0",
            (doctor_id, now_utc.replace(tzinfo=None)),
        )
        removed = cur.rowcount or 0
    summary = materialise_doctor(conn, doctor_id, tz_name, now_utc)
    summary["removed_unbooked"] = removed
    return summary


def block_date(conn, doctor_id, local_date):
    """Blackout added: unbooked slots on that date become BLOCKED. Returns (blocked, with_bookings)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE materialised_slots SET status = 'BLOCKED' "
            "WHERE doctor_id = %s AND `date` = %s AND booked_count = 0 AND status IN ('OPEN', 'FULL')",
            (doctor_id, local_date),
        )
        blocked = cur.rowcount or 0
        cur.execute(
            "SELECT COUNT(*) AS n FROM materialised_slots WHERE doctor_id = %s AND `date` = %s AND booked_count > 0",
            (doctor_id, local_date),
        )
        row = cur.fetchone()
    return blocked, int(row["n"] if isinstance(row, dict) else row[0])


def unblock_date(conn, doctor_id, tz_name, local_date, now_utc=None):
    """Blackout removed: BLOCKED slots reopen and any slots skipped while blacked out are created."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE materialised_slots SET status = IF(booked_count >= capacity, 'FULL', 'OPEN') "
            "WHERE doctor_id = %s AND `date` = %s AND status = 'BLOCKED'",
            (doctor_id, local_date),
        )
        reopened = cur.rowcount or 0
    summary = materialise_doctor(conn, doctor_id, tz_name, now_utc)
    summary["reopened"] = reopened
    return summary


def purge_old_slots(conn, now_utc=None, keep_days=7):
    """Remove long-finished slots that were never booked (keeps the table small)."""
    now_utc = now_utc or datetime.now(UTC)
    cutoff = (now_utc - timedelta(days=keep_days)).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM materialised_slots WHERE end_utc < %s AND booked_count = 0", (cutoff,))
        return cur.rowcount or 0
