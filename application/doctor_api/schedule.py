"""Doctor schedule: GET /doctors/me/schedule?date=YYYY-MM-DD  or  ?from=YYYY-MM-DD&to=YYYY-MM-DD (max 14 days).

Each slot lists the patients booked into it, so the doctor sees the day (or week) at a glance.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from application.common.api import ApiError, as_date, fmt_time, fmt_utc, money, parse_date

MAX_RANGE_DAYS = 14
ACTIVE_STATUSES = ("CONFIRMED", "COMPLETED", "NO_SHOW")


def _range(ctx, doctor):
    q = ctx.query
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(doctor["timezone"])).date()
    if q.get("from") or q.get("to"):
        start = parse_date(q.get("from"), "from")
        end = parse_date(q.get("to") or q.get("from"), "to")
    else:
        start = end = parse_date(q["date"], "date") if q.get("date") else today
    if end < start:
        raise ApiError(400, "INVALID_RANGE", "to must not be before from.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ApiError(400, "RANGE_TOO_LARGE", f"The range may cover at most {MAX_RANGE_DAYS} days.")
    return start, end


def get(ctx):
    doctor = ctx.doctor()
    start, end = _range(ctx, doctor)

    with ctx.conn.cursor() as cur:
        cur.execute(
            "SELECT s.slot_id, s.`date`, s.start_time, s.end_time, s.start_utc, s.end_utc, s.capacity, "
            "       s.booked_count, s.status, ct.type_name, ct.duration_minutes, ct.fee "
            "FROM materialised_slots s JOIN consultation_types ct ON ct.consultation_type_id = s.consultation_type_id "
            "WHERE s.doctor_id = %s AND s.`date` BETWEEN %s AND %s ORDER BY s.start_utc",
            (doctor["doctor_id"], start, end))
        slot_rows = cur.fetchall()

        bookings = defaultdict(list)
        if slot_rows:
            ids = [r["slot_id"] for r in slot_rows]
            cur.execute(
                "SELECT a.appointment_id, a.slot_id, a.status, a.notes, p.first_name, p.last_name, p.phone "
                "FROM appointments a JOIN patients p ON p.patient_id = a.patient_id "
                f"WHERE a.doctor_id = %s AND a.slot_id IN ({', '.join(['%s'] * len(ids))}) "
                "ORDER BY a.created_at",
                [doctor["doctor_id"]] + ids)
            for a in cur.fetchall():
                bookings[a["slot_id"]].append({
                    "appointment_id": a["appointment_id"], "status": a["status"], "notes": a["notes"] or "",
                    "patient_name": f"{a['first_name']} {a['last_name']}".strip(), "patient_phone": a["phone"] or ""})

        cur.execute("SELECT blackout_date, reason FROM blackout_dates WHERE doctor_id = %s AND blackout_date BETWEEN %s AND %s",
                    (doctor["doctor_id"], start, end))
        blackout_by_date = {as_date(r["blackout_date"]).isoformat(): r["reason"] or "" for r in cur.fetchall()}

    days = {}
    day = start
    while day <= end:
        days[day.isoformat()] = {"date": day.isoformat(), "weekday": day.strftime("%A"),
                                 "blackout": day.isoformat() in blackout_by_date,
                                 "blackout_reason": blackout_by_date.get(day.isoformat(), ""), "slots": []}
        day += timedelta(days=1)

    for r in slot_rows:
        patients = [b for b in bookings[r["slot_id"]] if b["status"] in ACTIVE_STATUSES]
        days[as_date(r["date"]).isoformat()]["slots"].append({
            "slot_id": r["slot_id"], "start_time": fmt_time(r["start_time"]), "end_time": fmt_time(r["end_time"]),
            "start_utc": fmt_utc(r["start_utc"]), "end_utc": fmt_utc(r["end_utc"]), "type_name": r["type_name"],
            "duration_minutes": int(r["duration_minutes"]), "fee": money(r["fee"]), "capacity": int(r["capacity"]),
            "booked_count": int(r["booked_count"]), "available_seats": max(0, int(r["capacity"]) - int(r["booked_count"])),
            "status": r["status"], "appointments": patients})

    ordered = list(days.values())
    body = {"timezone": doctor["timezone"], "from": start.isoformat(), "to": end.isoformat(), "days": ordered}
    if start == end:
        body["date"] = ordered[0]["date"]
        body["slots"] = ordered[0]["slots"]
        body["blackout"] = ordered[0]["blackout"]
    return 200, body
