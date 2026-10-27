```python
"""
Scheduled internal job: materialises schedule windows for inspection.
This does not write to DynamoDB because the deployed SlotCapacityTable key schema
and slot ID convention must be confirmed before capacity items are created.
"""

from datetime import date, timedelta

from application.database.connection import get_connection


def lambda_handler(event, context):
    # Determine how many days of schedule to calculate
    days = int(event.get("days", 14)) if isinstance(event, dict) else 14
    days = max(1, min(days, 31))

    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            # Retrieve active availability patterns and consultation details
            cursor.execute(
                """
                SELECT
                    ap.doctor_id,
                    ap.consultation_type_id,
                    ap.weekday,
                    ap.start_local_time,
                    ap.end_local_time,
                    ap.timezone_name,
                    ap.effective_from,
                    ap.effective_until,
                    ct.duration_minutes,
                    ct.capacity
                FROM availability_patterns ap
                JOIN consultation_types ct
                    ON ct.consultation_type_id = ap.consultation_type_id
                WHERE ap.active = TRUE
                    AND ct.is_active = TRUE
                """
            )

            availability_patterns = cursor.fetchall()

            # Retrieve doctor blackout periods
            cursor.execute(
                """
                SELECT
                    doctor_id,
                    local_date,
                    start_local_time,
                    end_local_time,
                    reason
                FROM blackout_dates
                """
            )

            blackout_dates = cursor.fetchall()

        today = date.today()
        schedule_windows = []

        # Calculate schedule windows for each requested day
        for day_offset in range(days):
            target_date = today + timedelta(days=day_offset)

            for pattern in availability_patterns:
                # Check weekday and effective date range
                if (
                    target_date.isoweekday() != int(pattern["weekday"])
                    or target_date < pattern["effective_from"]
                    or (
                        pattern["effective_until"]
                        and target_date > pattern["effective_until"]
                    )
                ):
                    continue

                # Find blackout periods for the doctor on this date
                doctor_blackouts = [
                    blackout
                    for blackout in blackout_dates
                    if (
                        str(blackout["doctor_id"])
                        == str(pattern["doctor_id"])
                        and blackout["local_date"] == target_date
                    )
                ]

                # Skip the schedule window if a blackout overlaps it
                has_blackout = any(
                    blackout["start_local_time"] is None
                    or not (
                        pattern["end_local_time"]
                        <= blackout["start_local_time"]
                        or pattern["start_local_time"]
                        >= blackout["end_local_time"]
                    )
                    for blackout in doctor_blackouts
                )

                if has_blackout:
                    continue

                # Add the available schedule window
                schedule_windows.append(
                    {
                        "doctor_id": pattern["doctor_id"],
                        "consultation_type_id": pattern[
                            "consultation_type_id"
                        ],
                        "date": str(target_date),
                        "start_local_time": str(
                            pattern["start_local_time"]
                        ),
                        "end_local_time": str(
                            pattern["end_local_time"]
                        ),
                        "timezone_name": pattern["timezone_name"],
                        "duration_minutes": pattern["duration_minutes"],
                        "capacity": pattern["capacity"],
                    }
                )

        return {
            "statusCode": 200,
            "materialised_schedule_windows": len(schedule_windows),
            "windows": schedule_windows,
            "note": (
                "Schedule windows calculated only; DynamoDB slot-capacity "
                "writes require confirmed key schema."
            ),
        }

    finally:
        connection.close()
```