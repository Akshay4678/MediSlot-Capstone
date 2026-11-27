"""A doctor changes the weekly availability while patients hold bookings (the "13:30 Quick Consult" bug).

Before: the booked 13:30-13:45 Quick Consult stayed on the schedule next to the new 13:00-14:00 Detailed
Consult slots, overlapping them, and the patient was never told. Now a booked slot that the new schedule no
longer produces is cancelled by the doctor (100 % refund, event, seat returned) and CLOSED for good.
"""

from datetime import date, datetime, time, timedelta, timezone

import pytest
from booking_support import NOW, SCHEMA, make_deps, seed
from sqlite_mysql import Conn

from application.booking import service
from application.common.api import ApiError
from application.doctor_api import reconcile
from application.slot_materialiser import core

NOW_AWARE = NOW.replace(tzinfo=timezone.utc)
TUESDAY = 2          # 2026-11-10 is a Tuesday; the next one, 2026-11-17, is inside the booking window


@pytest.fixture
def world():
    conn = Conn(SCHEMA)
    seed(conn.db, patients=3)
    conn.db.executemany("INSERT INTO consultation_types VALUES (?,?,?)",
                        [("tq", "Quick Consult", 15), ("td", "Detailed Consult", 60)])
    conn.db.executemany("INSERT INTO doctor_consultation_types VALUES (?,?,?)",
                        [("d1", "tq", 450.0), ("d1", "td", 850.0)])
    conn.db.execute("INSERT INTO availability_patterns VALUES ('av1','d1','tq',?, '13:00', '15:00', 1, 1)", (TUESDAY,))
    conn.db.commit()
    deps = make_deps()
    core.materialise_doctor(conn, "d1", "Asia/Kolkata", NOW_AWARE)
    conn.commit()
    return conn, deps


def slot_at(conn, day, hhmm):
    rows = conn.q("SELECT slot_id FROM materialised_slots WHERE \"date\" = ? AND start_time = ? AND status <> 'CLOSED'",
                  day, hhmm)
    return rows[0][0] if rows else None


def change_pattern(conn, type_id, start, end, capacity=1, active=1):
    conn.db.execute("UPDATE availability_patterns SET consultation_type_id = ?, start_time = ?, end_time = ?, "
                    "capacity = ?, is_active = ? WHERE availability_id = 'av1'", (type_id, start, end, capacity, active))
    conn.db.commit()


def rebuild(conn, deps):
    return reconcile.rebuild(conn, "d1", "Asia/Kolkata", deps=deps, now_utc=NOW_AWARE)


def open_slots(conn, day):
    return conn.q("SELECT start_time, end_time, status FROM materialised_slots WHERE \"date\" = ? "
                  "AND status IN ('OPEN','FULL') ORDER BY start_time", day)


DAY = "2026-11-17"


def test_the_booked_quick_slot_is_cancelled_when_the_availability_becomes_detailed_consults(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    appt = service.book(conn, deps, "u-1", {"slot_id": booked}, None)[1]["appointment"]["appointment_id"]
    assert deps.slot_table.items[booked]["booked_count"] == 1

    change_pattern(conn, "td", "12:00", "15:00")          # Detailed Consult 12-3 instead of Quick Consult 1-3
    summary = rebuild(conn, deps)

    assert summary["slots_cancelled"] == 1 and summary["appointments_cancelled"] == 1
    assert conn.q("SELECT status, cancellation_reason FROM appointments WHERE appointment_id = ?", appt) == [
        ("CANCELLED_BY_DOCTOR", reconcile.REASON)]
    assert conn.q("SELECT refund_tier, refund_percentage, refund_amount FROM refund_ledger") == [("DOCTOR_CANCELLED", 100.0, 450.0)]
    assert conn.q("SELECT status, booked_count FROM materialised_slots WHERE slot_id = ?", booked) == [("CLOSED", 0)]
    assert deps.slot_table.items[booked]["booked_count"] == 0                      # the seat went back
    cancelled = deps.events.of("AppointmentCancelled")
    assert [(e["appointment_id"], e["cancelled_by"]) for e in cancelled] == [(appt, "doctor")]
    assert deps.metrics.total("CancellationsByTier", Tier="DOCTOR_CANCELLED") == 1


def test_after_the_change_only_the_new_slots_are_open_and_none_overlap(world):
    conn, deps = world
    service.book(conn, deps, "u-1", {"slot_id": slot_at(conn, DAY, "13:30")}, None)
    change_pattern(conn, "td", "12:00", "15:00")
    rebuild(conn, deps)
    assert open_slots(conn, DAY) == [("12:00", "13:00", "OPEN"), ("13:00", "14:00", "OPEN"), ("14:00", "15:00", "OPEN")]


def test_the_cancelled_slot_never_comes_back_as_available(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    service.book(conn, deps, "u-1", {"slot_id": booked}, None)
    change_pattern(conn, "td", "12:00", "15:00")
    rebuild(conn, deps)
    core.materialise_doctor(conn, "d1", "Asia/Kolkata", NOW_AWARE)       # the nightly job
    rebuild(conn, deps)                                                  # and another edit of the same schedule
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = ?", booked) == [("CLOSED",)]
    with pytest.raises(ApiError) as exc:
        service.book(conn, deps, "u-2", {"slot_id": booked}, None)
    assert (exc.value.status, exc.value.code) == (409, "SLOT_UNAVAILABLE")


def test_a_booked_slot_that_still_matches_the_new_schedule_is_kept(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    appt = service.book(conn, deps, "u-1", {"slot_id": booked}, None)[1]["appointment"]["appointment_id"]
    change_pattern(conn, "tq", "13:00", "16:00")          # longer day, same Quick Consult grid: 13:30 is still a slot
    summary = rebuild(conn, deps)
    assert summary["slots_cancelled"] == 0
    assert conn.q("SELECT status FROM appointments WHERE appointment_id = ?", appt) == [("CONFIRMED",)]
    assert conn.q("SELECT status, booked_count FROM materialised_slots WHERE slot_id = ?", booked) == [("FULL", 1)]
    assert conn.q("SELECT COUNT(*) FROM refund_ledger") == [(0,)]


def test_switching_the_pattern_off_cancels_its_bookings_but_not_other_days(world):
    conn, deps = world
    other = "2026-11-24"
    service.book(conn, deps, "u-1", {"slot_id": slot_at(conn, DAY, "13:30")}, None)
    service.book(conn, deps, "u-2", {"slot_id": slot_at(conn, other, "14:00")}, None)
    change_pattern(conn, "tq", "13:00", "15:00", active=0)
    summary = rebuild(conn, deps)
    assert summary["slots_cancelled"] == 2 and summary["appointments_cancelled"] == 2
    assert open_slots(conn, DAY) == [] and open_slots(conn, other) == []


def test_a_smaller_capacity_than_the_seats_taken_cancels_the_slot(world):
    conn, deps = world
    conn.db.execute("UPDATE availability_patterns SET capacity = 2")
    conn.db.execute("DELETE FROM materialised_slots")
    conn.db.commit()
    core.materialise_doctor(conn, "d1", "Asia/Kolkata", NOW_AWARE)
    conn.commit()
    booked = slot_at(conn, DAY, "13:30")
    service.book(conn, deps, "u-1", {"slot_id": booked}, None)
    service.book(conn, deps, "u-2", {"slot_id": booked}, None)
    change_pattern(conn, "tq", "13:00", "15:00", capacity=1)
    summary = rebuild(conn, deps)
    assert summary["appointments_cancelled"] == 2 and summary["refunds_total"] == 900.0


def test_a_higher_capacity_reopens_a_full_slot_that_is_kept(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    service.book(conn, deps, "u-1", {"slot_id": booked}, None)
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = ?", booked) == [("FULL",)]
    change_pattern(conn, "tq", "13:00", "15:00", capacity=2)
    rebuild(conn, deps)
    assert conn.q("SELECT status, capacity, booked_count FROM materialised_slots WHERE slot_id = ?", booked) == [("OPEN", 2, 1)]


def test_a_blacked_out_day_with_kept_bookings_is_not_cancelled_by_an_unrelated_edit(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    appt = service.book(conn, deps, "u-1", {"slot_id": booked}, None)[1]["appointment"]["appointment_id"]
    conn.db.execute("INSERT INTO blackout_dates VALUES ('b1','d1',?, 'leave')", (DAY,))
    conn.db.commit()
    core.block_date(conn, "d1", date.fromisoformat(DAY))
    conn.commit()
    summary = rebuild(conn, deps)                      # the weekly pattern did not change
    assert summary["slots_cancelled"] == 0
    assert conn.q("SELECT status FROM appointments WHERE appointment_id = ?", appt) == [("CONFIRMED",)]
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = ?", booked) == [("BLOCKED",)]


def test_an_unbooked_schedule_is_simply_regenerated(world):
    conn, deps = world
    change_pattern(conn, "td", "12:00", "15:00")
    summary = rebuild(conn, deps)
    assert summary["slots_cancelled"] == 0 and summary["removed_unbooked"] > 0
    assert open_slots(conn, DAY) == [("12:00", "13:00", "OPEN"), ("13:00", "14:00", "OPEN"), ("14:00", "15:00", "OPEN")]


def test_a_waitlist_hold_on_a_vanished_slot_is_cancelled_too(world):
    from application.waitlist import service as waitlist
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    appt = service.book(conn, deps, "u-1", {"slot_id": booked}, None)[1]["appointment"]["appointment_id"]
    waitlist.join(conn, deps, "u-2", booked)
    service.cancel_by_patient(conn, deps, "u-1", appt)          # p-2 now holds the seat (booked_count 1, no appointment)
    assert deps.slot_table.items[booked]["booked_count"] == 1
    change_pattern(conn, "td", "12:00", "15:00")
    rebuild(conn, deps)
    assert deps.waitlist.live_offers_for_slot(booked) == [] and deps.waitlist.entries_for_slot(booked) == []
    assert deps.slot_table.items[booked]["booked_count"] == 0
