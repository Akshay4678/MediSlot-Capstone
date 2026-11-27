"""A doctor changes the weekly availability while patients hold bookings.

A change of availability never cancels an appointment. The booked slot stays exactly as it was; a new slot is
created only where it does not overlap a booked one, and the skipped times are reported back.
"""

from datetime import date, timezone

import pytest
from booking_support import NOW, SCHEMA, make_deps, seed
from sqlite_mysql import Conn

from application.booking import service
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


def test_a_booked_quick_slot_survives_a_switch_to_detailed_consults_and_the_overlap_is_reported(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    appt = service.book(conn, deps, "u-1", {"slot_id": booked}, None)[1]["appointment"]["appointment_id"]

    change_pattern(conn, "td", "12:00", "15:00")          # Detailed Consult 12-3 instead of Quick Consult 1-3
    summary = rebuild(conn, deps)

    assert conn.q("SELECT status FROM appointments WHERE appointment_id = ?", appt) == [("CONFIRMED",)]
    assert conn.q("SELECT status, booked_count FROM materialised_slots WHERE slot_id = ?", booked) == [("FULL", 1)]
    assert conn.q("SELECT COUNT(*) FROM refund_ledger") == [(0,)]
    assert deps.events.of("AppointmentCancelled") == [] and deps.slot_table.items[booked]["booked_count"] == 1
    skipped = [s for s in summary["skipped_overlapping"] if s["date"] == DAY]
    assert [(s["start_time"], s["end_time"], s["overlaps_start"], s["overlaps_end"]) for s in skipped] == [
        ("13:00", "14:00", "13:30", "13:45")]
    assert summary["kept_booked"] == 1


def test_only_the_non_overlapping_new_slots_are_created(world):
    conn, deps = world
    service.book(conn, deps, "u-1", {"slot_id": slot_at(conn, DAY, "13:45")}, None)
    change_pattern(conn, "td", "13:00", "15:00")
    rebuild(conn, deps)
    assert open_slots(conn, DAY) == [("13:45", "14:00", "FULL"), ("14:00", "15:00", "OPEN")] or \
        open_slots(conn, DAY) == [("14:00", "15:00", "OPEN"), ("13:45", "14:00", "FULL")]
    assert conn.q("SELECT COUNT(*) FROM materialised_slots WHERE \"date\" = ? AND start_time = '13:00' "
                  "AND status <> 'CLOSED'", DAY) == [(0,)]


def test_the_kept_slot_stays_after_the_nightly_job_and_another_edit(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    service.book(conn, deps, "u-1", {"slot_id": booked}, None)
    change_pattern(conn, "td", "12:00", "15:00")
    rebuild(conn, deps)
    core.materialise_doctor(conn, "d1", "Asia/Kolkata", NOW_AWARE)
    rebuild(conn, deps)
    assert conn.q("SELECT status, booked_count FROM materialised_slots WHERE slot_id = ?", booked) == [("FULL", 1)]
    assert conn.q("SELECT COUNT(*) FROM materialised_slots WHERE \"date\" = ? AND start_time = '13:00' "
                  "AND status <> 'CLOSED'", DAY) == [(0,)]


def test_switching_the_pattern_off_keeps_the_bookings_and_removes_only_unbooked_slots(world):
    conn, deps = world
    other = "2026-11-24"
    a1 = service.book(conn, deps, "u-1", {"slot_id": slot_at(conn, DAY, "13:30")}, None)[1]["appointment"]["appointment_id"]
    a2 = service.book(conn, deps, "u-2", {"slot_id": slot_at(conn, other, "14:00")}, None)[1]["appointment"]["appointment_id"]
    change_pattern(conn, "tq", "13:00", "15:00", active=0)
    rebuild(conn, deps)
    assert conn.q("SELECT COUNT(*) FROM appointments WHERE status = 'CONFIRMED'") == [(2,)] and a1 and a2
    assert [r[0] for r in open_slots(conn, DAY)] == ["13:30"] and [r[0] for r in open_slots(conn, other)] == ["14:00"]


def test_a_smaller_capacity_never_drops_below_the_seats_taken(world):
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
    rebuild(conn, deps)
    assert conn.q("SELECT capacity, booked_count, status FROM materialised_slots WHERE slot_id = ?", booked) == [(2, 2, "FULL")]
    assert conn.q("SELECT COUNT(*) FROM appointments WHERE status = 'CONFIRMED'") == [(2,)]


def test_a_booked_slot_that_still_matches_the_new_schedule_is_kept(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    appt = service.book(conn, deps, "u-1", {"slot_id": booked}, None)[1]["appointment"]["appointment_id"]
    change_pattern(conn, "tq", "13:00", "16:00")
    summary = rebuild(conn, deps)
    assert summary["skipped_overlapping"] == []
    assert conn.q("SELECT status FROM appointments WHERE appointment_id = ?", appt) == [("CONFIRMED",)]
    assert conn.q("SELECT status, booked_count FROM materialised_slots WHERE slot_id = ?", booked) == [("FULL", 1)]


def test_a_higher_capacity_reopens_a_full_slot_that_is_kept(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    service.book(conn, deps, "u-1", {"slot_id": booked}, None)
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
    rebuild(conn, deps)
    assert conn.q("SELECT status FROM appointments WHERE appointment_id = ?", appt) == [("CONFIRMED",)]
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = ?", booked) == [("BLOCKED",)]


def test_an_unbooked_schedule_is_simply_regenerated(world):
    conn, deps = world
    change_pattern(conn, "td", "12:00", "15:00")
    summary = rebuild(conn, deps)
    assert summary["skipped_overlapping"] == [] and summary["removed_unbooked"] > 0
    assert open_slots(conn, DAY) == [("12:00", "13:00", "OPEN"), ("13:00", "14:00", "OPEN"), ("14:00", "15:00", "OPEN")]


def test_a_doctor_cancelled_slot_still_never_comes_back_as_available(world):
    conn, deps = world
    booked = slot_at(conn, DAY, "13:30")
    service.book(conn, deps, "u-1", {"slot_id": booked}, None)
    service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": booked})
    rebuild(conn, deps)
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = ?", booked) == [("CLOSED",)]
