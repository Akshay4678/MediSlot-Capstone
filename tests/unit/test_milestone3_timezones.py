
"""Timezone and deterministic-ID tests for the current slot materialiser."""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from application.slot_materialiser import core


DOCTOR_ID = "doctor-123"
AVAILABILITY_ID = "availability-456"
CONSULTATION_TYPE_ID = "consultation-789"


def _pattern():
    return {
        "availability_id": AVAILABILITY_ID,
        "consultation_type_id": CONSULTATION_TYPE_ID,
        "duration_minutes": 30,
        "start_time": time(9, 0),
        "end_time": time(10, 0),
        "capacity": 2,
    }


def test_kolkata_local_time_converts_to_utc():
    local_value = datetime(2026, 11, 1, 9, 0)

    converted = core.local_to_utc(
        local_value,
        ZoneInfo("Asia/Kolkata")
    )

    assert converted == datetime(
        2026, 11, 1, 3, 30,
        tzinfo=timezone.utc
    )


def test_utc_timezone_keeps_the_same_clock_time():
    local_value = datetime(2026, 11, 1, 9, 0)

    converted = core.local_to_utc(
        local_value,
        ZoneInfo("UTC")
    )

    assert converted == datetime(
        2026, 11, 1, 9, 0,
        tzinfo=timezone.utc
    )


def test_slot_id_is_deterministic_and_contains_utc_start():
    now_utc = datetime(
        2026, 11, 1, 0, 0,
        tzinfo=timezone.utc
    )

    first = core.build_slots(
        DOCTOR_ID,
        _pattern(),
        date(2026, 11, 1),
        "Asia/Kolkata",
        now_utc
    )

    second = core.build_slots(
        DOCTOR_ID,
        _pattern(),
        date(2026, 11, 1),
        "Asia/Kolkata",
        now_utc
    )

    assert len(first) == 2

    assert first[0]["slot_id"] == (
        f"{DOCTOR_ID}#{AVAILABILITY_ID}#20261101T0330Z"
    )

    assert [slot["slot_id"] for slot in first] == [
        slot["slot_id"] for slot in second
    ]


def test_generated_slot_keeps_doctors_timezone_and_local_times():
    now_utc = datetime(
        2026, 11, 1, 0, 0,
        tzinfo=timezone.utc
    )

    slots = core.build_slots(
        DOCTOR_ID,
        _pattern(),
        date(2026, 11, 1),
        "Asia/Kolkata",
        now_utc
    )

    assert slots[0]["timezone"] == "Asia/Kolkata"
    assert slots[0]["start_time"] == time(9, 0)
    assert slots[0]["end_time"] == time(9, 30)

    assert slots[0]["start_utc"] == datetime(
        2026, 11, 1, 3, 30
    )

    assert slots[0]["end_utc"] == datetime(
        2026, 11, 1, 4, 0
    )
