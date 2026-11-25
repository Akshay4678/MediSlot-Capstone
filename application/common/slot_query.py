"""ONE place that reads slots for a doctor.

Both dashboards use this: the patient's "available slots" page (patient_api.discovery.slots) and the
doctor's schedule page (doctor_api.schedule.get). They differ only in what they do with the rows:
the patient view keeps the bookable ones, the doctor view keeps everything and adds the patients.
"""

from datetime import date, timedelta

from application.common.api import as_date, fmt_time, fmt_utc, money

# A patient can book up to rules/booking-window-days (30) ahead, so one request may cover that whole window.
MAX_RANGE_DAYS = 31

SLOT_COLUMNS = (
    "s.slot_id, s.`date`, s.start_time, s.end_time, s.start_utc, s.end_utc, s.capacity, s.booked_count, "
    "s.status, ct.consultation_type_id, ct.type_name, ct.duration_minutes, dct.fee"
)


def fetch_slots(conn, doctor_id, start, end, after_utc=None):
    """Every materialised slot of one doctor with local date between start and end (inclusive).

    after_utc (naive UTC datetime) hides slots that have already started; leave it None to see all.
    Returns JSON-ready dicts ordered by start time.
    """
    sql = (
        f"SELECT {SLOT_COLUMNS} FROM materialised_slots s "
        "JOIN consultation_types ct ON ct.consultation_type_id = s.consultation_type_id "
        "JOIN doctor_consultation_types dct ON dct.doctor_id = s.doctor_id "
        "     AND dct.consultation_type_id = s.consultation_type_id "
        "WHERE s.doctor_id = %s AND s.`date` BETWEEN %s AND %s"
    )
    params = [doctor_id, start, end]
    if after_utc is not None:
        sql += " AND s.start_utc > %s"
        params.append(after_utc)
    sql += " ORDER BY s.start_utc, s.slot_id"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [_shape(r) for r in rows]


def _shape(r):
    capacity, booked = int(r["capacity"]), int(r["booked_count"])
    return {
        "slot_id": r["slot_id"],
        "date": as_date(r["date"]).isoformat(),
        "start_time": fmt_time(r["start_time"]),
        "end_time": fmt_time(r["end_time"]),
        "start_utc": fmt_utc(r["start_utc"]),
        "end_utc": fmt_utc(r["end_utc"]),
        "capacity": capacity,
        "booked_count": booked,
        "available_seats": max(0, capacity - booked),
        "status": r["status"],
        "consultation_type_id": r["consultation_type_id"],
        "type_name": r["type_name"],
        "duration_minutes": int(r["duration_minutes"]),
        "fee": money(r["fee"]),
    }


def is_bookable(slot):
    """A patient can book a slot only while it is OPEN and still has a free seat."""
    return slot["status"] == "OPEN" and slot["available_seats"] > 0


def is_patient_visible(slot):
    """Show full slots too: they can accept a waitlist entry. Blocked/closed slots stay hidden."""
    return slot["status"] not in ("BLOCKED", "CLOSED")


def group_by_day(slots, start, end):
    """[{date, slots}] for every day in the range, including empty days."""
    days, day = {}, start
    while day <= end:
        days[day.isoformat()] = []
        day += timedelta(days=1)
    for slot in slots:
        days.setdefault(slot["date"], []).append(slot)
    return [{"date": d, "slots": s} for d, s in days.items()]


def range_days(start: date, end: date):
    return (end - start).days + 1
