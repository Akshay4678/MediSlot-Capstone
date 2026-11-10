"""Doctor discovery for patients: search, public doctor profile and slot availability.

Search index choices (see docs/milestone-4.md for the full justification):
* name           -> prefix match on doctors.last_name (ix_doctors_name) and doctors.first_name (ix_doctors_first_name)
* hospital       -> prefix match on hospitals.hospital_name (ix_hospitals_name)
* city           -> prefix match on hospitals.city (ix_hospitals_city_active)
* specialisation -> substring match on specialisations.specialisation_name (tiny reference table, unique index)
A prefix pattern ('abc%') can use a B-tree index; a leading wildcard ('%abc%') cannot.
"""

from datetime import datetime, timedelta, timezone

from application.common.api import ApiError, as_date, fmt_time, money, parse_date

MAX_RANGE_DAYS = 14
MAX_LIMIT = 50


def _like_escape(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _prefix(term):
    return _like_escape(term) + "%"


def _paging(q):
    try:
        limit = max(1, min(MAX_LIMIT, int(q.get("limit", 20))))
        offset = max(0, int(q.get("offset", 0)))
    except (TypeError, ValueError):
        raise ApiError(400, "INVALID_PAGINATION", "limit and offset must be integers.") from None
    return limit, offset


def search(conn, q):
    q = {k: (v or "").strip() if isinstance(v, str) else v for k, v in (q or {}).items()}
    limit, offset = _paging(q)
    conditions = ["d.is_active = TRUE", "h.is_active = TRUE", "u.is_active = TRUE"]
    values = []

    name = " ".join(str(q.get("name") or "").split())
    if name:
        parts = name.split(" ")
        if len(parts) == 1:
            conditions.append("(d.last_name LIKE %s OR d.first_name LIKE %s)")
            values += [_prefix(name), _prefix(name)]
        else:  # "Asha Rao" -> first name Asha AND last name Rao (either order)
            first, last = parts[0], " ".join(parts[1:])
            conditions.append("((d.first_name LIKE %s AND d.last_name LIKE %s) OR (d.last_name LIKE %s AND d.first_name LIKE %s))")
            values += [_prefix(first), _prefix(last), _prefix(first), _prefix(last)]
    hospital = q.get("hospital")
    if hospital:
        conditions.append("h.hospital_name LIKE %s")
        values.append(_prefix(hospital))
    city = q.get("city")
    if city:
        conditions.append("h.city LIKE %s")
        values.append(_prefix(city))
    specialisation = q.get("specialisation")
    if specialisation:
        conditions.append("s.specialisation_name LIKE %s")
        values.append("%" + _like_escape(specialisation) + "%")
    legacy = q.get("search")  # kept for the older single-box search: doctor name OR hospital
    if legacy:
        conditions.append("(d.last_name LIKE %s OR d.first_name LIKE %s OR h.hospital_name LIKE %s)")
        values += [_prefix(legacy)] * 3

    joins = ("FROM doctors d JOIN hospitals h ON h.hospital_id = d.hospital_id "
             "JOIN specialisations s ON s.specialisation_id = d.specialisation_id "
             "JOIN users u ON u.user_id = d.user_id WHERE " + " AND ".join(conditions))
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n " + joins, tuple(values))
        total_row = cur.fetchone()
        total = int(total_row["n"] if isinstance(total_row, dict) else total_row[0])
        cur.execute(
            "SELECT d.doctor_id, d.first_name, d.last_name, d.qualifications, d.experience_years, d.timezone, "
            "h.hospital_id, h.hospital_name, h.city, s.specialisation_id, s.specialisation_name AS specialisation "
            + joins + " ORDER BY d.last_name, d.first_name, d.doctor_id LIMIT %s OFFSET %s",
            tuple(values + [limit, offset]))
        rows = cur.fetchall()
    return 200, {"items": rows, "count": len(rows), "total": total, "limit": limit, "offset": offset,
                 "has_more": offset + len(rows) < total}


def doctor_profile(conn, doctor_id):
    if not doctor_id:
        raise ApiError(400, "MISSING_PARAMETERS", "doctor_id is required.")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.doctor_id, d.first_name, d.last_name, d.qualifications, d.experience_years, d.timezone, "
            "h.hospital_id, h.hospital_name, h.city, s.specialisation_id, s.specialisation_name AS specialisation "
            "FROM doctors d JOIN hospitals h ON h.hospital_id = d.hospital_id "
            "JOIN specialisations s ON s.specialisation_id = d.specialisation_id "
            "JOIN users u ON u.user_id = d.user_id "
            "WHERE d.doctor_id = %s AND d.is_active = TRUE AND u.is_active = TRUE", (doctor_id,))
        doctor = cur.fetchone()
        if not doctor:
            raise ApiError(404, "DOCTOR_NOT_FOUND", "Doctor was not found.")
        cur.execute(
            "SELECT ct.consultation_type_id, ct.type_name, ct.duration_minutes, dct.fee "
            "FROM doctor_consultation_types dct JOIN consultation_types ct "
            "ON ct.consultation_type_id = dct.consultation_type_id "
            "WHERE dct.doctor_id = %s ORDER BY ct.duration_minutes, ct.type_name", (doctor_id,))
        types = [{"consultation_type_id": r["consultation_type_id"], "type_name": r["type_name"],
                  "duration_minutes": int(r["duration_minutes"]), "fee": money(r["fee"])} for r in cur.fetchall()]
    return 200, {"doctor": doctor, "consultation_types": types}


def _range(q):
    q = q or {}
    single = (q.get("date") or "").strip()
    if single:
        start = end = parse_date(single, "date")
    else:
        start = parse_date((q.get("from") or "").strip(), "from")
        end = parse_date((q.get("to") or q.get("from") or "").strip(), "to")
    if end < start:
        raise ApiError(400, "INVALID_RANGE", "to must not be before from.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ApiError(400, "RANGE_TOO_LARGE", f"The range may cover at most {MAX_RANGE_DAYS} days.")
    return start, end, bool(single)


def slots(conn, doctor_id, q, now_utc=None):
    """Open slots with free seats for one date (?date=) or a date range (?from=&to=, max 14 days)."""
    if not doctor_id:
        raise ApiError(400, "MISSING_PARAMETERS", "doctor_id is required.")
    start, end, single = _range(q)
    now_utc = (now_utc or datetime.now(timezone.utc)).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("SELECT timezone FROM doctors WHERE doctor_id = %s AND is_active = TRUE", (doctor_id,))
        doctor = cur.fetchone()
        if not doctor:
            raise ApiError(404, "DOCTOR_NOT_FOUND", "Doctor was not found.")
        cur.execute(
            "SELECT s.slot_id, s.`date`, s.start_time, s.end_time, s.start_utc, s.end_utc, s.capacity, s.booked_count, "
            "s.status, ct.consultation_type_id, ct.type_name, ct.duration_minutes, dct.fee "
            "FROM materialised_slots s "
            "JOIN consultation_types ct ON ct.consultation_type_id = s.consultation_type_id "
            "JOIN doctor_consultation_types dct ON dct.doctor_id = s.doctor_id "
            "     AND dct.consultation_type_id = s.consultation_type_id "
            "WHERE s.doctor_id = %s AND s.`date` BETWEEN %s AND %s AND s.start_utc > %s "
            "ORDER BY s.start_utc", (doctor_id, start, end, now_utc))
        rows = cur.fetchall()
    items, by_day = [], {}
    day = start
    while day <= end:
        by_day[day.isoformat()] = []
        day += timedelta(days=1)
    for r in rows:
        free = max(0, int(r["capacity"]) - int(r["booked_count"]))
        if r["status"] != "OPEN" or free == 0:
            continue
        item = {"slot_id": r["slot_id"], "date": as_date(r["date"]).isoformat(),
                "start_time": fmt_time(r["start_time"]), "end_time": fmt_time(r["end_time"]),
                "start_utc": r["start_utc"], "end_utc": r["end_utc"], "capacity": int(r["capacity"]),
                "booked_count": int(r["booked_count"]), "available_seats": free, "status": r["status"],
                "consultation_type_id": r["consultation_type_id"], "type_name": r["type_name"],
                "duration_minutes": int(r["duration_minutes"]), "fee": money(r["fee"])}
        items.append(item)
        by_day[item["date"]].append(item)
    body = {"doctor_id": doctor_id, "timezone": doctor["timezone"], "from": start.isoformat(), "to": end.isoformat(),
            "slots": items, "days": [{"date": d, "slots": s} for d, s in by_day.items()]}
    if single:
        body["date"] = start.isoformat()
    return 200, body
