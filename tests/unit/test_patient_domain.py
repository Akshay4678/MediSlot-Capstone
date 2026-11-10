"""Milestone 4 patient domain: profile CRUD + isolation, doctor search, public profile, slot ranges, booking gap.

The real SQL runs against an in-memory SQLite database (see sqlite_mysql.py).
"""

from datetime import datetime, timedelta

import pytest
from sqlite_mysql import Conn

from application.common.api import ApiError
from application.patient_api import discovery, profile

SCHEMA = """
CREATE TABLE users (user_id TEXT PRIMARY KEY, email TEXT, is_active INT DEFAULT 1);
CREATE TABLE patients (patient_id TEXT PRIMARY KEY, user_id TEXT UNIQUE, first_name TEXT, last_name TEXT,
  date_of_birth TEXT, gender TEXT, phone TEXT, address TEXT, city TEXT, emergency_contact TEXT);
CREATE TABLE hospitals (hospital_id TEXT PRIMARY KEY, hospital_name TEXT, city TEXT, is_active INT DEFAULT 1);
CREATE TABLE specialisations (specialisation_id TEXT PRIMARY KEY, specialisation_name TEXT);
CREATE TABLE doctors (doctor_id TEXT PRIMARY KEY, user_id TEXT, hospital_id TEXT, specialisation_id TEXT,
  first_name TEXT, last_name TEXT, qualifications TEXT, experience_years INT, timezone TEXT, is_active INT DEFAULT 1);
CREATE TABLE consultation_types (consultation_type_id TEXT PRIMARY KEY, type_name TEXT, duration_minutes INT);
CREATE TABLE doctor_consultation_types (doctor_id TEXT, consultation_type_id TEXT, fee REAL);
CREATE TABLE materialised_slots (slot_id TEXT PRIMARY KEY, doctor_id TEXT, consultation_type_id TEXT, "date" TEXT,
  start_time TEXT, end_time TEXT, start_utc TEXT, end_utc TEXT, capacity INT, booked_count INT, status TEXT);
CREATE TABLE appointments (appointment_id TEXT PRIMARY KEY, patient_id TEXT, doctor_id TEXT, slot_id TEXT,
  consultation_type_id TEXT, status TEXT);
"""

NOW = datetime(2026, 11, 10, 6, 0, 0)


@pytest.fixture
def conn():
    c = Conn(SCHEMA)
    db = c.db
    db.executemany("INSERT INTO users VALUES (?,?,1)", [("u-a", "a@x"), ("u-b", "b@x"), ("u-d1", "d1@x"), ("u-d2", "d2@x"),
                                                         ("u-d3", "d3@x")])
    db.executemany("INSERT INTO patients (patient_id, user_id, first_name, last_name, phone) VALUES (?,?,?,?,?)",
                   [("p-a", "u-a", "Alice", "Aaa", "+91 98765 43210"), ("p-b", "u-b", "Bob", "Bbb", "+91 90000 00000")])
    db.executemany("INSERT INTO hospitals VALUES (?,?,?,1)", [("h1", "Apollo Hospital", "Hyderabad"), ("h2", "City Care", "Chennai")])
    db.executemany("INSERT INTO specialisations VALUES (?,?)", [("s1", "Cardiology"), ("s2", "Dermatology")])
    db.executemany("INSERT INTO doctors VALUES (?,?,?,?,?,?,?,?,?,1)", [
        ("d1", "u-d1", "h1", "s1", "Asha", "Rao", "MBBS", 5, "Asia/Kolkata"),
        ("d2", "u-d2", "h2", "s2", "Ravi", "Kumar", "MD", 9, "Asia/Kolkata"),
        ("d3", "u-d3", "h1", "s2", "Asha", "Menon", "MBBS", 2, "Asia/Kolkata")])
    db.executemany("INSERT INTO consultation_types VALUES (?,?,?)", [("t30", "Standard Consult", 30), ("t15", "Quick Consult", 15)])
    db.executemany("INSERT INTO doctor_consultation_types VALUES (?,?,?)",
                   [("d1", "t30", 500.0), ("d1", "t15", 250.0), ("d2", "t30", 400.0)])
    return c


def add_slot(conn, slot_id, day, hour, capacity=1, booked=0, status="OPEN", doctor="d1", ctype="t30"):
    start = datetime.fromisoformat(f"{day} {hour:02d}:00:00")
    conn.db.execute("INSERT INTO materialised_slots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (slot_id, doctor, ctype, day, f"{hour:02d}:00", f"{hour:02d}:30", start.isoformat(sep=" "),
                     (start + timedelta(minutes=30)).isoformat(sep=" "), capacity, booked, status))


def err(call):
    with pytest.raises(ApiError) as exc:
        call()
    return exc.value


# ----------------------------------------------------------------------------- profile CRUD + isolation
def test_patient_reads_only_their_own_profile(conn):
    assert profile.get(conn, "u-a")[1]["patient"]["first_name"] == "Alice"
    assert profile.get(conn, "u-b")[1]["patient"]["first_name"] == "Bob"
    assert err(lambda: profile.get(conn, "u-someone-else")).status == 404


def test_update_changes_only_the_callers_row(conn):
    status, body = profile.update(conn, "u-a", {"phone": "+91 91111 22222", "city": "Pune", "gender": "female"})
    assert status == 200 and body["patient"]["gender"] == "Female" and body["patient"]["city"] == "Pune"
    assert profile.get(conn, "u-b")[1]["patient"]["phone"] == "+91 90000 00000"


def test_identity_fields_cannot_be_updated_so_nobody_can_take_over_another_profile(conn):
    for field in ("patient_id", "user_id"):
        assert err(lambda field=field: profile.update(conn, "u-a", {"first_name": "X", field: "p-b"})).code == "UNKNOWN_FIELD"
    assert conn.q("SELECT first_name FROM patients WHERE patient_id = 'p-a'") == [("Alice",)]


@pytest.mark.parametrize("body,code", [
    ({"phone": "abc"}, "INVALID_PHONE"),
    ({"phone": "12"}, "INVALID_PHONE"),
    ({"gender": "robot"}, "INVALID_FIELD"),
    ({"date_of_birth": "2999-01-01"}, "INVALID_FIELD"),
    ({"date_of_birth": "01-01-2000"}, "INVALID_DATE"),
    ({"first_name": "  "}, "MISSING_FIELD"),
    ({"city": "x" * 101}, "INVALID_FIELD"),
    ({}, "NO_CHANGES"),
])
def test_update_validation(conn, body, code):
    assert err(lambda: profile.update(conn, "u-a", body)).code == code


def test_create_requires_names_and_refuses_a_second_profile(conn):
    conn.db.execute("INSERT INTO users VALUES ('u-new', 'n@x', 1)")
    assert err(lambda: profile.create(conn, "u-new", {"first_name": "Only"})).code == "MISSING_FIELD"
    status, body = profile.create(conn, "u-new", {"first_name": "Nina", "last_name": "N", "date_of_birth": "1990-05-04"})
    assert status == 201 and body["patient"]["date_of_birth"] == "1990-05-04"
    assert err(lambda: profile.create(conn, "u-new", {"first_name": "Again", "last_name": "N"})).status == 409


def test_delete_is_refused_with_upcoming_appointments(conn):
    add_slot(conn, "s-future", "2026-11-12", 10)
    conn.db.execute("INSERT INTO appointments VALUES ('a1','p-a','d1','s-future','t30','CONFIRMED')")
    assert err(lambda: profile.delete(conn, "u-a", NOW)).code == "HAS_ACTIVE_APPOINTMENTS"
    assert conn.q("SELECT is_active FROM users WHERE user_id = 'u-a'") == [(1,)]


def test_delete_erases_personal_details_and_closes_the_account(conn):
    status, _ = profile.delete(conn, "u-a", NOW)
    assert status == 200
    assert conn.q("SELECT first_name, last_name, phone FROM patients WHERE patient_id = 'p-a'") == [("Deleted", "Patient", None)]
    assert conn.q("SELECT is_active FROM users WHERE user_id = 'u-a'") == [(0,)]
    assert conn.q("SELECT first_name FROM patients WHERE patient_id = 'p-b'") == [("Bob",)]


# ----------------------------------------------------------------------------- search
def names(body):
    return [(d["first_name"], d["last_name"]) for d in body["items"]]


def test_search_by_name_prefix_and_full_name(conn):
    assert names(discovery.search(conn, {"name": "Asha"})[1]) == [("Asha", "Menon"), ("Asha", "Rao")]
    assert names(discovery.search(conn, {"name": "rao"})[1]) == [("Asha", "Rao")]
    assert names(discovery.search(conn, {"name": "Asha Rao"})[1]) == [("Asha", "Rao")]
    assert names(discovery.search(conn, {"name": "Rao Asha"})[1]) == [("Asha", "Rao")]


def test_search_by_hospital_city_and_specialisation(conn):
    assert len(discovery.search(conn, {"hospital": "Apollo"})[1]["items"]) == 2
    assert names(discovery.search(conn, {"city": "chen"})[1]) == [("Ravi", "Kumar")]
    assert names(discovery.search(conn, {"specialisation": "ermat"})[1]) == [("Ravi", "Kumar"), ("Asha", "Menon")]
    both = discovery.search(conn, {"hospital": "Apollo", "specialisation": "Cardio", "name": "Asha"})[1]
    assert names(both) == [("Asha", "Rao")]


def test_search_wildcards_in_user_input_are_not_patterns(conn):
    assert discovery.search(conn, {"name": "%"})[1]["total"] == 0
    assert discovery.search(conn, {"city": "_"})[1]["total"] == 0


def test_search_pagination(conn):
    first = discovery.search(conn, {"limit": "2", "offset": "0"})[1]
    second = discovery.search(conn, {"limit": "2", "offset": "2"})[1]
    assert first["total"] == 3 and first["count"] == 2 and first["has_more"] is True
    assert second["count"] == 1 and second["has_more"] is False
    assert not set(names(first)) & set(names(second))
    assert err(lambda: discovery.search(conn, {"limit": "x"})).code == "INVALID_PAGINATION"
    assert discovery.search(conn, {"limit": "9999"})[1]["limit"] == discovery.MAX_LIMIT


def test_inactive_doctors_and_hospitals_are_hidden(conn):
    conn.db.execute("UPDATE doctors SET is_active = 0 WHERE doctor_id = 'd1'")
    conn.db.execute("UPDATE hospitals SET is_active = 0 WHERE hospital_id = 'h2'")
    assert names(discovery.search(conn, {})[1]) == [("Asha", "Menon")]


# ----------------------------------------------------------------------------- public profile
def test_public_profile_lists_consultation_types_with_fees(conn):
    status, body = discovery.doctor_profile(conn, "d1")
    assert status == 200 and body["doctor"]["hospital_name"] == "Apollo Hospital"
    assert [(t["type_name"], t["fee"]) for t in body["consultation_types"]] == [("Quick Consult", 250.0), ("Standard Consult", 500.0)]
    assert "user_id" not in body["doctor"] and "licence_number" not in body["doctor"]


def test_public_profile_unknown_or_inactive_doctor_is_404(conn):
    assert err(lambda: discovery.doctor_profile(conn, "nope")).status == 404
    conn.db.execute("UPDATE doctors SET is_active = 0 WHERE doctor_id = 'd1'")
    assert err(lambda: discovery.doctor_profile(conn, "d1")).status == 404


# ----------------------------------------------------------------------------- slots for a date range
def test_slots_for_a_single_date_show_free_seats(conn):
    add_slot(conn, "camp", "2026-11-12", 9, capacity=5, booked=2)
    add_slot(conn, "full", "2026-11-12", 10, capacity=1, booked=1, status="FULL")
    add_slot(conn, "blocked", "2026-11-12", 11, status="BLOCKED")
    add_slot(conn, "open", "2026-11-12", 12)
    status, body = discovery.slots(conn, "d1", {"date": "2026-11-12"}, NOW)
    assert status == 200 and body["date"] == "2026-11-12"
    assert [(s["slot_id"], s["available_seats"]) for s in body["slots"]] == [("camp", 3), ("open", 1)]


def test_slots_for_a_date_range_are_grouped_by_day(conn):
    add_slot(conn, "mon", "2026-11-16", 9)
    add_slot(conn, "tue", "2026-11-17", 9)
    add_slot(conn, "later", "2026-11-30", 9)
    status, body = discovery.slots(conn, "d1", {"from": "2026-11-16", "to": "2026-11-18"}, NOW)
    assert [d["date"] for d in body["days"]] == ["2026-11-16", "2026-11-17", "2026-11-18"]
    assert [len(d["slots"]) for d in body["days"]] == [1, 1, 0]
    assert [s["slot_id"] for s in body["slots"]] == ["mon", "tue"]


def test_slot_range_limits_and_past_slots(conn):
    add_slot(conn, "past", "2026-11-10", 5)
    assert discovery.slots(conn, "d1", {"date": "2026-11-10"}, NOW)[1]["slots"] == []
    assert err(lambda: discovery.slots(conn, "d1", {"from": "2026-11-01", "to": "2026-11-30"}, NOW)).code == "RANGE_TOO_LARGE"
    assert err(lambda: discovery.slots(conn, "d1", {"from": "2026-11-05", "to": "2026-11-01"}, NOW)).code == "INVALID_RANGE"
    assert err(lambda: discovery.slots(conn, "d1", {}, NOW)).code == "INVALID_DATE"
    assert err(lambda: discovery.slots(conn, "zzz", {"date": "2026-11-12"}, NOW)).status == 404
