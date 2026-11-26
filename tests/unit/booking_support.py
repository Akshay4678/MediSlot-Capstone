"""Shared fixtures for the Milestone 5 tests: a MySQL-shaped SQLite schema, seed data and fake AWS services."""

import calendar
import threading
import time
from datetime import datetime, timedelta

from application.booking.deps import Deps
from application.booking.idempotency import IdempotencyStore
from application.booking.slot_store import SlotStore
from fake_dynamo import FakeTable

NOW = datetime(2026, 11, 10, 6, 0, 0)

SCHEMA = """
CREATE TABLE users (user_id TEXT PRIMARY KEY, email TEXT, is_active INT DEFAULT 1);
CREATE TABLE patients (patient_id TEXT PRIMARY KEY, user_id TEXT UNIQUE, first_name TEXT, last_name TEXT,
  date_of_birth TEXT, gender TEXT, phone TEXT, address TEXT, city TEXT, emergency_contact TEXT, is_active INT DEFAULT 1);
CREATE TABLE hospitals (hospital_id TEXT PRIMARY KEY, hospital_name TEXT, city TEXT, is_active INT DEFAULT 1);
CREATE TABLE specialisations (specialisation_id TEXT PRIMARY KEY, specialisation_name TEXT);
CREATE TABLE doctors (doctor_id TEXT PRIMARY KEY, user_id TEXT, hospital_id TEXT, specialisation_id TEXT,
  first_name TEXT, last_name TEXT, qualifications TEXT, experience_years INT, timezone TEXT, is_active INT DEFAULT 1);
CREATE TABLE consultation_types (consultation_type_id TEXT PRIMARY KEY, type_name TEXT, duration_minutes INT);
CREATE TABLE doctor_consultation_types (doctor_id TEXT, consultation_type_id TEXT, fee REAL);
CREATE TABLE availability_patterns (availability_id TEXT PRIMARY KEY, doctor_id TEXT, consultation_type_id TEXT,
  day_of_week INT, start_time TEXT, end_time TEXT, capacity INT, is_active INT DEFAULT 1);
CREATE TABLE blackout_dates (blackout_id TEXT PRIMARY KEY, doctor_id TEXT, blackout_date TEXT, reason TEXT);
CREATE TABLE materialised_slots (slot_id TEXT PRIMARY KEY, availability_id TEXT, doctor_id TEXT,
  consultation_type_id TEXT, "date" TEXT, start_time TEXT, end_time TEXT, start_utc TEXT, end_utc TEXT,
  timezone TEXT DEFAULT 'Asia/Kolkata', capacity INT, booked_count INT DEFAULT 0, status TEXT DEFAULT 'OPEN',
  CHECK (booked_count <= capacity));
CREATE TABLE appointments (appointment_id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, doctor_id TEXT NOT NULL,
  slot_id TEXT NOT NULL, consultation_type_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'CONFIRMED', notes TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, reschedule_count INT DEFAULT 0,
  fee_amount REAL NOT NULL DEFAULT 0, idempotency_key TEXT, starts_at_utc TEXT, ends_at_utc TEXT, local_date TEXT,
  local_start TEXT, local_end TEXT, timezone TEXT, cancelled_at TEXT, cancellation_reason TEXT,
  active_flag INTEGER GENERATED ALWAYS AS (CASE WHEN status = 'CONFIRMED' THEN 1 END) VIRTUAL);
CREATE UNIQUE INDEX uq_appt_patient_slot_active ON appointments (patient_id, slot_id, active_flag);
CREATE UNIQUE INDEX uq_appt_patient_idem ON appointments (patient_id, idempotency_key);
CREATE TABLE refund_ledger (refund_id TEXT PRIMARY KEY, appointment_id TEXT NOT NULL UNIQUE, refund_amount REAL NOT NULL,
  refund_percentage REAL NOT NULL, fee_amount REAL NOT NULL DEFAULT 0, refund_tier TEXT NOT NULL DEFAULT 'NONE',
  refund_status TEXT NOT NULL DEFAULT 'CALCULATED', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def seed(db, patients=3):
    """users/patients u-1..u-N, doctors d1 and d2, types t30 (Standard) and hc (Health Camp)."""
    db.executemany("INSERT INTO users VALUES (?,?,1)",
                   [(f"u-{i}", f"p{i}@x") for i in range(1, patients + 1)] + [("u-d1", "d1@x"), ("u-d2", "d2@x")])
    db.executemany("INSERT INTO patients (patient_id, user_id, first_name, last_name, phone) VALUES (?,?,?,?,?)",
                   [(f"p-{i}", f"u-{i}", f"Pat{i}", "Test", "+91 90000 00000") for i in range(1, patients + 1)])
    db.executemany("INSERT INTO hospitals VALUES (?,?,?,1)", [("h1", "Apollo Hospital", "Hyderabad")])
    db.executemany("INSERT INTO specialisations VALUES (?,?)", [("s1", "Cardiology")])
    db.executemany("INSERT INTO doctors VALUES (?,?,?,?,?,?,?,?,?,1)", [
        ("d1", "u-d1", "h1", "s1", "Asha", "Rao", "MBBS", 5, "Asia/Kolkata"),
        ("d2", "u-d2", "h1", "s1", "Ravi", "Kumar", "MD", 9, "Asia/Kolkata")])
    db.executemany("INSERT INTO consultation_types VALUES (?,?,?)",
                   [("t30", "Standard Consult", 30), ("hc", "Health Camp Session", 60)])
    db.executemany("INSERT INTO doctor_consultation_types VALUES (?,?,?)",
                   [("d1", "t30", 500.0), ("d1", "hc", 200.0), ("d2", "t30", 400.0)])
    db.commit()


def add_slot(db, slot_id, start, capacity=1, booked=0, status="OPEN", doctor="d1", ctype="t30", minutes=30):
    end = start + timedelta(minutes=minutes)
    db.execute(
        "INSERT INTO materialised_slots (slot_id, availability_id, doctor_id, consultation_type_id, \"date\", start_time, "
        "end_time, start_utc, end_utc, capacity, booked_count, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (slot_id, "av1", doctor, ctype, start.date().isoformat(), start.strftime("%H:%M"), end.strftime("%H:%M"),
         start.isoformat(sep=" "), end.isoformat(sep=" "), capacity, booked, status))
    db.commit()


class FakeEvents:
    def __init__(self):
        self.published = []

    def publish(self, detail_type, detail):
        self.published.append((detail_type, detail))
        return True

    def of(self, detail_type):
        return [d for t, d in self.published if t == detail_type]


class FakeMetrics:
    def __init__(self):
        self.records = []

    def put(self, name, value=1, dimensions=None, unit="Count"):
        self.records.append((name, value, dict(dimensions or {})))
        return True

    def total(self, name, **dims):
        return sum(v for n, v, d in self.records if n == name and all(d.get(k) == x for k, x in dims.items()))


class Clock:
    """A settable 'now' (naive UTC) shared by the service and the idempotency store."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def epoch(self):
        return calendar.timegm(self.now.timetuple())


def make_deps(clock=None, max_attempts=6, on_get=None, slot_table=None, idem_table=None, events=None, metrics=None):
    clock = clock or Clock()
    slot_table = slot_table or FakeTable("slot_id", on_get=on_get)
    idem_table = idem_table or FakeTable("idempotency_key")
    from application.waitlist.store import WaitlistStore
    queue = FakeTable("slot_id", range_key="patient_id", indexes={
        "order-index": ("slot_id", "joined_at"), "patient-index": ("patient_id", "slot_id")}, page_size=2)
    offers = FakeTable("offer_id", indexes={
        "slot-index": ("slot_id", "created_at"), "patient-index": ("patient_id", "created_at"),
        "status-index": ("status", "expires_at")}, page_size=2)
    deps = Deps(
        slots=SlotStore(slot_table, max_attempts=max_attempts, backoff_ms=1, sleep=lambda s: time.sleep(min(s, 0.002))),
        idempotency=IdempotencyStore(idem_table, ttl_hours=24, lock_seconds=30, clock=clock.epoch),
        events=events or FakeEvents(), metrics=metrics or FakeMetrics(), now=clock, waitlist=WaitlistStore(queue, offers))
    deps.clock, deps.slot_table, deps.idem_table = clock, slot_table, idem_table
    deps.queue_table, deps.offers_table = queue, offers
    return deps


def run_threads(count, target):
    """Start `count` threads that all wait on a barrier, then run target(i); returns the list of results."""
    barrier, results, lock = threading.Barrier(count), [None] * count, threading.Lock()

    def worker(i):
        barrier.wait()
        try:
            outcome = target(i)
        except Exception as exc:  # noqa: BLE001 - the test inspects exceptions as outcomes
            outcome = exc
        with lock:
            results[i] = outcome

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results
