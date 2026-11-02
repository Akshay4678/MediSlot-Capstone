"""Idempotently materialise the rolling availability window into RDS MySQL."""
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from application.database.connection import get_connection

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def _as_time(value):
    return value if isinstance(value, time) else time.fromisoformat(str(value))


def _valid_local(naive, zone, fold=0):
    """Convert local wall time to aware time; reject nonexistent DST wall times."""
    aware = naive.replace(tzinfo=zone, fold=fold)
    roundtrip = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    return aware if roundtrip == naive else None


def _slot_id(availability_id, start_utc):
    return f"{availability_id}#{start_utc.strftime('%Y%m%dT%H%M%SZ')}"


def _blackout_dates(cursor, doctor_id, start_date, end_date):
    cursor.execute(
        "SELECT blackout_date FROM blackout_dates "
        "WHERE doctor_id=%s AND blackout_date BETWEEN %s AND %s",
        (doctor_id, start_date, end_date),
    )
    return {
        row["blackout_date"].isoformat()
        if hasattr(row["blackout_date"], "isoformat")
        else str(row["blackout_date"])
        for row in cursor.fetchall()
    }


def materialise(start_date=None, days=30):
    """Create/update slots in RDS without resetting existing booked_count values."""
    days = int(days)
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    start_date = start_date or datetime.now(timezone.utc).date()
    end_date = start_date + timedelta(days=days - 1)
    created = existing = skipped_dst = blackout_skips = 0
    valid_slot_ids = set()
    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT ap.availability_id, ap.doctor_id, ap.consultation_type_id,
                          ap.day_of_week, ap.start_time, ap.end_time, ap.capacity,
                          ct.duration_minutes, d.timezone
                   FROM availability_patterns ap
                   JOIN consultation_types ct
                     ON ct.consultation_type_id = ap.consultation_type_id
                   JOIN doctors d ON d.doctor_id = ap.doctor_id
                   WHERE ap.is_active = TRUE AND d.is_active = TRUE
                   ORDER BY ap.availability_id"""
            )
            patterns = cursor.fetchall()
            blackout_cache = {}

            for pattern in patterns:
                doctor_id = int(pattern["doctor_id"])
                if doctor_id not in blackout_cache:
                    blackout_cache[doctor_id] = _blackout_dates(
                        cursor, doctor_id, start_date, end_date
                    )
                try:
                    zone = ZoneInfo(pattern.get("timezone") or "Asia/Kolkata")
                except ZoneInfoNotFoundError:
                    log.exception("Invalid timezone for doctor_id=%s", doctor_id)
                    continue

                local_start = _as_time(pattern["start_time"])
                local_end = _as_time(pattern["end_time"])
                duration = int(pattern["duration_minutes"])
                availability_id = int(pattern["availability_id"])
                capacity = int(pattern["capacity"])
                day = start_date

                while day <= end_date:
                    if day.isoweekday() == int(pattern["day_of_week"]):
                        if day.isoformat() in blackout_cache[doctor_id]:
                            blackout_skips += 1
                        else:
                            local_cursor = datetime.combine(day, local_start)
                            finish = datetime.combine(day, local_end)
                            while local_cursor + timedelta(minutes=duration) <= finish:
                                local_finish = local_cursor + timedelta(minutes=duration)
                                local_a = _valid_local(local_cursor, zone, 0)
                                local_b = _valid_local(local_finish, zone, 0)
                                if local_a is None or local_b is None:
                                    skipped_dst += 1
                                else:
                                    utc_a = local_a.astimezone(timezone.utc)
                                    utc_b = local_b.astimezone(timezone.utc)
                                    if (utc_b - utc_a).total_seconds() == duration * 60:
                                        slot_id = _slot_id(availability_id, utc_a)
                                        valid_slot_ids.add(slot_id)
                                        cursor.execute(
                                            """INSERT INTO materialised_slots
                                               (slot_id, availability_id, doctor_id,
                                                consultation_type_id, `date`, start_time,
                                                end_time, capacity, booked_count, status)
                                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,'AVAILABLE')
                                               ON DUPLICATE KEY UPDATE
                                                 capacity = GREATEST(VALUES(capacity), booked_count),
                                                 status = CASE
                                                   WHEN booked_count >= GREATEST(VALUES(capacity), booked_count)
                                                     THEN 'FULL'
                                                   WHEN status = 'UNAVAILABLE' AND booked_count = 0
                                                     THEN 'AVAILABLE'
                                                   ELSE status
                                                 END""",
                                            (slot_id, availability_id, doctor_id,
                                             int(pattern["consultation_type_id"]), day,
                                             local_cursor.time(), local_finish.time(), capacity),
                                        )
                                        if cursor.rowcount == 1:
                                            created += 1
                                        else:
                                            existing += 1
                                    else:
                                        skipped_dst += 1
                                local_cursor += timedelta(minutes=duration)
                    day += timedelta(days=1)

            # Disable stale future slots only when they have no bookings.
            cursor.execute(
                "SELECT slot_id FROM materialised_slots WHERE `date` BETWEEN %s AND %s "
                "AND booked_count = 0",
                (start_date, end_date),
            )
            for row in cursor.fetchall():
                if row["slot_id"] not in valid_slot_ids:
                    cursor.execute(
                        "UPDATE materialised_slots SET status='UNAVAILABLE' "
                        "WHERE slot_id=%s AND booked_count=0",
                        (row["slot_id"],),
                    )
        connection.commit()
    except Exception:
        connection.rollback()
        log.exception("RDS slot materialisation failed")
        raise
    finally:
        connection.close()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "created": created,
        "already_existed": existing,
        "skipped_dst_nonexistent_or_invalid": skipped_dst,
        "blackout_dates_skipped": blackout_skips,
    }


def lambda_handler(event, context):
    try:
        requested = (event or {}).get("start_date")
        start = date.fromisoformat(requested) if requested else datetime.now(timezone.utc).date()
        days = int((event or {}).get("days", os.environ.get("MATERIALISATION_DAYS", "30")))
        result = materialise(start, days)
        log.info("Slot materialisation complete: %s", result)
        return {"statusCode": 200, "body": result}
    except Exception:
        log.exception("Slot materialisation failed")
        raise
