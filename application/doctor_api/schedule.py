"""Doctor schedule: GET /doctors/me/schedule?date=YYYY-MM-DD  or  ?from=YYYY-MM-DD&to=YYYY-MM-DD (max 31 days).

Each slot lists the patients booked into it, so the doctor sees the day (or week) at a glance.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from application.common import slot_query
from application.common.api import ApiError, as_date, parse_date

MAX_RANGE_DAYS = slot_query.MAX_RANGE_DAYS
ACTIVE_STATUSES = ("CONFIRMED", "COMPLETED", "NO_SHOW")


def _naive(value):
    """fetch_slots returns start_utc as ISO text with a trailing Z."""
    return datetime.fromisoformat(str(value).replace("Z", "").replace(" ", "T"))


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

    slot_rows = slot_query.fetch_slots(ctx.conn, doctor["doctor_id"], start, end)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    started = {r["slot_id"] for r in slot_rows if _naive(r["start_utc"]) <= now}
    with ctx.conn.cursor() as cur:
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

    for slot in slot_rows:
        slot["appointments"] = [b for b in bookings[slot["slot_id"]] if b["status"] in ACTIVE_STATUSES]
        slot["started"] = slot["slot_id"] in started
        for b in slot["appointments"]:
            # the doctor records what happened once the time has come (no-show keeps the fee)
            b["can_mark_attendance"] = b["status"] == "CONFIRMED" and slot["started"]
        days[slot["date"]]["slots"].append(slot)

    # A blackout day overrides any weekly availability. Hide the BLOCKED slots the materialiser
    # created, and keep only slots that already hold an active appointment so the doctor stil
    # sees patients who were booked before the blackout was added.
    for d in days.values():
        if d["blackout"]:
            d["slots"] = [s for s in d["slots"] if s["appointments"]]

    ordered = list(days.values())
    body = {"timezone": doctor["timezone"], "from": start.isoformat(), "to": end.isoformat(), "days": ordered}
    if start == end:
        body["date"] = ordered[0]["date"]
        body["slots"] = ordered[0]["slots"]
        body["blackout"] = ordered[0]["blackout"]
    return 200, body