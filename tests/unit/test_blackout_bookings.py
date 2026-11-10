"""Blackout date with patients already booked: keep or cancel, and no new bookings either way.

Runs the real SQL of application.doctor_api.blackouts / slot_materialiser.core against an in-memory
SQLite database (the statements used are portable apart from a few MySQL spellings translated below).
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from application.common.api import ApiError
from application.doctor_api import blackouts
from application.slot_materialiser import core

DOCTOR = "d1"
DAY = date.today() + timedelta(days=3)

SCHEMA = """
CREATE TABLE blackout_dates (blackout_id TEXT PRIMARY KEY, doctor_id TEXT, blackout_date TEXT, reason TEXT,
                             UNIQUE (doctor_id, blackout_date));
CREATE TABLE materialised_slots (slot_id TEXT PRIMARY KEY, doctor_id TEXT, consultation_type_id TEXT, "date" TEXT,
                                 start_time TEXT, start_utc TEXT, capacity INT, booked_count INT, status TEXT);
CREATE TABLE appointments (appointment_id TEXT PRIMARY KEY, patient_id TEXT, doctor_id TEXT, slot_id TEXT,
                           consultation_type_id TEXT, status TEXT, notes TEXT);
CREATE TABLE doctor_consultation_types (doctor_id TEXT, consultation_type_id TEXT, fee REAL);
CREATE TABLE refund_ledger (refund_id TEXT, appointment_id TEXT PRIMARY KEY, refund_amount REAL,
                            refund_percentage REAL, refund_status TEXT);
"""


class Cursor:
    def __init__(self, conn):
        self._c = conn.cursor()
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @staticmethod
    def _sql(sql):
        sql = sql.replace("%s", "?").replace("GREATEST(", "MAX(").replace("`", '"')
        if "ON DUPLICATE KEY UPDATE" in sql:
            sql = sql.split("ON DUPLICATE KEY UPDATE")[0] + (
                "ON CONFLICT(appointment_id) DO UPDATE SET refund_amount = excluded.refund_amount, refund_percentage = 100")
        return sql

    @staticmethod
    def _arg(v):
        if isinstance(v, datetime):
            return v.isoformat(sep=" ")
        if isinstance(v, date):
            return v.isoformat()
        return v

    def execute(self, sql, params=()):
        self._c.execute(self._sql(sql), [self._arg(v) for v in params])
        self.rowcount = self._c.rowcount

    def fetchone(self):
        row = self._c.fetchone()
        return dict(zip([d[0] for d in self._c.description], row)) if row else None

    def fetchall(self):
        cols = [d[0] for d in self._c.description]
        return [dict(zip(cols, r)) for r in self._c.fetchall()]


class Conn:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)

    def cursor(self):
        return Cursor(self.db)

    def q(self, sql, *params):
        return self.db.execute(sql, params).fetchall()


def make_slot(conn, slot_id, capacity, booked, start_hour=10, day=DAY, status=None):
    start_utc = datetime(day.year, day.month, day.day, start_hour) if day > date.today() else \
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=start_hour)
    status = status or ("FULL" if booked >= capacity else "OPEN")
    conn.db.execute("INSERT INTO materialised_slots VALUES (?,?,?,?,?,?,?,?,?)",
                    (slot_id, DOCTOR, "t1", day.isoformat(), "10:00", start_utc.isoformat(sep=" "), capacity, booked, status))
    for n in range(booked):
        conn.db.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,NULL)",
                        (f"{slot_id}-a{n}", f"p{n}", DOCTOR, slot_id, "t1", "CONFIRMED"))


@pytest.fixture
def conn():
    c = Conn()
    c.db.execute("INSERT INTO doctor_consultation_types VALUES (?,?,?)", (DOCTOR, "t1", 300.0))
    make_slot(c, "camp", capacity=5, booked=2, start_hour=9)      # partly booked Health Camp slot
    make_slot(c, "single", capacity=1, booked=1, start_hour=11)   # fully booked single-seat slot
    make_slot(c, "empty", capacity=1, booked=0, start_hour=12)    # nobody booked
    return c


def ctx_for(conn, **body):
    doctor = {"doctor_id": DOCTOR, "timezone": "UTC"}
    return SimpleNamespace(conn=conn, body=dict({"blackout_date": DAY.isoformat(), "reason": "Leave"}, **body),
                           doctor=lambda: doctor)


def statuses(conn):
    return dict(conn.q("SELECT slot_id, status FROM materialised_slots"))


def test_bookings_present_and_no_choice_is_refused_and_changes_nothing(conn):
    with pytest.raises(ApiError) as exc:
        blackouts.create(ctx_for(conn))

    assert exc.value.status == 409 and exc.value.code == "BLACKOUT_HAS_BOOKINGS"
    assert conn.q("SELECT COUNT(*) FROM blackout_dates")[0][0] == 0
    assert statuses(conn) == {"camp": "OPEN", "single": "FULL", "empty": "OPEN"}


def test_keep_closes_every_slot_but_leaves_the_appointments_confirmed(conn):
    status, body = blackouts.create(ctx_for(conn, existing_bookings="keep"))

    assert status == 201
    assert body["appointments_kept"] == 3 and body["appointments_cancelled"] == 0
    # The partly booked camp slot is closed too, so no new patient can book it.
    assert statuses(conn) == {"camp": "BLOCKED", "single": "BLOCKED", "empty": "BLOCKED"}
    assert conn.q("SELECT COUNT(*) FROM appointments WHERE status = 'CONFIRMED'")[0][0] == 3
    assert conn.q("SELECT booked_count FROM materialised_slots WHERE slot_id = 'camp'")[0][0] == 2


def test_cancel_marks_doctor_cancellation_with_full_refund_and_frees_the_seats(conn):
    status, body = blackouts.create(ctx_for(conn, existing_bookings="cancel"))

    assert status == 201 and body["appointments_cancelled"] == 3
    assert conn.q("SELECT DISTINCT status FROM appointments") == [("CANCELLED_BY_DOCTOR",)]
    refunds = conn.q("SELECT refund_amount, refund_percentage, refund_status FROM refund_ledger")
    assert len(refunds) == 3 and set(refunds) == {(300.0, 100.0, "CALCULATED")}
    assert conn.q("SELECT SUM(booked_count) FROM materialised_slots")[0][0] == 0
    assert set(statuses(conn).values()) == {"BLOCKED"}


def test_day_without_bookings_needs_no_choice(conn):
    conn.db.execute("DELETE FROM appointments")
    conn.db.execute("UPDATE materialised_slots SET booked_count = 0, status = 'OPEN'")

    status, body = blackouts.create(ctx_for(conn))

    assert status == 201 and body["slots_blocked"] == 3 and body["slots_with_bookings"] == 0


def test_invalid_choice_is_rejected(conn):
    with pytest.raises(ApiError) as exc:
        blackouts.create(ctx_for(conn, existing_bookings="delete"))

    assert exc.value.status == 400


def test_other_days_and_other_doctors_are_untouched(conn):
    make_slot(conn, "next-day", capacity=1, booked=1, day=DAY + timedelta(days=1))
    conn.db.execute("UPDATE materialised_slots SET doctor_id = 'other' WHERE slot_id = 'single'")
    conn.db.execute("UPDATE appointments SET doctor_id = 'other' WHERE slot_id = 'single'")

    blackouts.create(ctx_for(conn, existing_bookings="cancel"))

    assert statuses(conn)["next-day"] == "FULL" and statuses(conn)["single"] == "FULL"
    assert conn.q("SELECT status FROM appointments WHERE slot_id = 'next-day'") == [("CONFIRMED",)]


def test_block_date_returns_slots_closed_and_how_many_had_bookings(conn):
    assert core.block_date(conn, DOCTOR, DAY) == (3, 2)
