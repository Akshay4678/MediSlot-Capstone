"""Weekly availability patterns: GET/PUT /doctors/me/availability.

PUT replaces the whole weekly schedule. Items that carry an existing availability_id are
updated in place (their slots keep the same ids); others are new; patterns that are not
mentioned are removed. Slots in the rolling window are then rebuilt for unbooked times.
"""

from application.common import rules
from application.common.api import ApiError, fmt_time, new_id, parse_hhmm, require_uuid
from application.slot_materialiser import core as slots

MAX_PATTERNS = 50
DAY_NAMES = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}


def _minutes(t):
    return t.hour * 60 + t.minute


def _fetch(cur, doctor_id):
    cur.execute(
        "SELECT ap.availability_id, ap.consultation_type_id, ct.type_name, ct.duration_minutes, "
        "       ap.day_of_week, ap.start_time, ap.end_time, ap.capacity, ap.is_active "
        "FROM availability_patterns ap JOIN consultation_types ct ON ct.consultation_type_id = ap.consultation_type_id "
        "WHERE ap.doctor_id = %s ORDER BY ap.day_of_week, ap.start_time", (doctor_id,))
    return [{
        "availability_id": r["availability_id"], "consultation_type_id": r["consultation_type_id"],
        "type_name": r["type_name"], "duration_minutes": int(r["duration_minutes"]),
        "day_of_week": int(r["day_of_week"]), "day_name": DAY_NAMES[int(r["day_of_week"])],
        "start_time": fmt_time(r["start_time"]), "end_time": fmt_time(r["end_time"]),
        "capacity": int(r["capacity"]), "is_active": bool(r["is_active"]),
    } for r in cur.fetchall()]


def get(ctx):
    doctor = ctx.doctor()
    with ctx.conn.cursor() as cur:
        patterns = _fetch(cur, doctor["doctor_id"])
    return 200, {"timezone": doctor["timezone"], "patterns": patterns}


def validate(items, types_by_id, existing_ids):
    """Validate and normalise the submitted patterns (pure function, unit-tested)."""
    if not isinstance(items, list):
        raise ApiError(400, "INVALID_FIELD", "patterns must be a list.")
    if len(items) > MAX_PATTERNS:
        raise ApiError(400, "TOO_MANY_PATTERNS", f"At most {MAX_PATTERNS} patterns are allowed.")

    clean, seen_ids = [], set()
    camp_min, camp_max = rules.health_camp_capacity_bounds()
    for index, item in enumerate(items, start=1):
        label = f"patterns[{index}]"
        if not isinstance(item, dict):
            raise ApiError(400, "INVALID_FIELD", f"{label} must be an object.")

        type_id = require_uuid(item.get("consultation_type_id"), f"{label}.consultation_type_id")
        ctype = types_by_id.get(type_id)
        if not ctype:
            raise ApiError(400, "UNKNOWN_CONSULTATION_TYPE", f"{label}: consultation type not found among your types.")

        try:
            day = int(item.get("day_of_week"))
        except (TypeError, ValueError):
            day = 0
        if not 1 <= day <= 7:
            raise ApiError(400, "INVALID_DAY", f"{label}.day_of_week must be 1 (Monday) to 7 (Sunday).")

        start, end = parse_hhmm(item.get("start_time"), f"{label}.start_time"), parse_hhmm(item.get("end_time"), f"{label}.end_time")
        if _minutes(end) <= _minutes(start):
            raise ApiError(400, "INVALID_TIME_RANGE", f"{label}: end_time must be after start_time.")
        if _minutes(end) - _minutes(start) < int(ctype["duration_minutes"]):
            raise ApiError(400, "WINDOW_TOO_SHORT",
                           f"{label}: the window is shorter than one {ctype['type_name']} ({ctype['duration_minutes']} min).")

        camp = rules.is_health_camp(ctype["type_name"])
        raw_capacity = item.get("capacity", camp_min if camp else 1)
        try:
            capacity = int(raw_capacity)
        except (TypeError, ValueError):
            capacity = 0
        if camp and not camp_min <= capacity <= camp_max:
            raise ApiError(400, "INVALID_CAPACITY", f"{label}: Health Camp capacity must be {camp_min} to {camp_max}.")
        if not camp and capacity != 1:
            raise ApiError(400, "INVALID_CAPACITY", f"{label}: only Health Camp sessions can have capacity above 1.")

        availability_id = item.get("availability_id")
        if availability_id is not None:
            availability_id = require_uuid(availability_id, f"{label}.availability_id")
            if availability_id not in existing_ids:
                raise ApiError(400, "UNKNOWN_AVAILABILITY", f"{label}: availability_id does not belong to you.")
            if availability_id in seen_ids:
                raise ApiError(400, "DUPLICATE_AVAILABILITY", f"{label}: availability_id used twice.")
            seen_ids.add(availability_id)

        clean.append({"availability_id": availability_id, "consultation_type_id": type_id, "day_of_week": day,
                      "start_time": start, "end_time": end, "capacity": capacity,
                      "is_active": bool(item.get("is_active", True))})

    # A doctor cannot be in two places: patterns on the same weekday must not overlap.
    for day in range(1, 8):
        day_items = sorted((p for p in clean if p["day_of_week"] == day and p["is_active"]), key=lambda p: p["start_time"])
        for first, second in zip(day_items, day_items[1:]):
            if _minutes(second["start_time"]) < _minutes(first["end_time"]):
                raise ApiError(400, "OVERLAPPING_PATTERNS",
                               f"{DAY_NAMES[day]}: {first['start_time']:%H:%M}-{first['end_time']:%H:%M} overlaps "
                               f"{second['start_time']:%H:%M}-{second['end_time']:%H:%M}.")
    return clean


def put(ctx):
    doctor = ctx.doctor()
    if "patterns" not in ctx.body:
        raise ApiError(400, "MISSING_FIELD", "patterns is required (use an empty list to clear the schedule).")

    doctor_id = doctor["doctor_id"]
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT consultation_type_id, type_name, duration_minutes FROM consultation_types WHERE doctor_id = %s",
                    (doctor_id,))
        types_by_id = {r["consultation_type_id"]: r for r in cur.fetchall()}
        cur.execute("SELECT availability_id FROM availability_patterns WHERE doctor_id = %s", (doctor_id,))
        existing_ids = {r["availability_id"] for r in cur.fetchall()}

        patterns = validate(ctx.body["patterns"], types_by_id, existing_ids)

        keep = [p["availability_id"] for p in patterns if p["availability_id"]]
        if keep:
            cur.execute(
                f"DELETE FROM availability_patterns WHERE doctor_id = %s AND availability_id NOT IN ({', '.join(['%s'] * len(keep))})",
                [doctor_id] + keep)
        else:
            cur.execute("DELETE FROM availability_patterns WHERE doctor_id = %s", (doctor_id,))

        for p in patterns:
            if p["availability_id"]:
                cur.execute(
                    "UPDATE availability_patterns SET consultation_type_id = %s, day_of_week = %s, start_time = %s, "
                    "end_time = %s, capacity = %s, is_active = %s WHERE availability_id = %s AND doctor_id = %s",
                    (p["consultation_type_id"], p["day_of_week"], p["start_time"], p["end_time"], p["capacity"],
                     p["is_active"], p["availability_id"], doctor_id))
            else:
                cur.execute(
                    "INSERT INTO availability_patterns (availability_id, doctor_id, consultation_type_id, day_of_week, "
                    "start_time, end_time, capacity, is_active) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (new_id(), doctor_id, p["consultation_type_id"], p["day_of_week"], p["start_time"], p["end_time"],
                     p["capacity"], p["is_active"]))

    summary = slots.rebuild_future_slots(ctx.conn, doctor_id, doctor["timezone"])
    with ctx.conn.cursor() as cur:
        saved = _fetch(cur, doctor_id)
    return 200, {"timezone": doctor["timezone"], "patterns": saved, "slots": {
        "created": summary["inserted"], "removed_unbooked": summary["removed_unbooked"],
        "window_days": summary["window_days"]}}
