"""Milestone 6 at the API boundary: the new patient routes (reschedule, waitlist, accept offer) and the doctor's
no-show / completed marking, all through the Lambda handlers."""

import json
from datetime import timedelta

import pytest
from booking_support import NOW, SCHEMA, add_slot, make_deps, seed
from sqlite_mysql import Conn

from application.doctor_api import appointments as doctor_appointments, handler as doctor_handler
from application.patient_api import handler as patient_handler

START = NOW + timedelta(days=3, hours=3)


@pytest.fixture
def world(monkeypatch):
    conn = Conn(SCHEMA)
    seed(conn.db, patients=4)
    add_slot(conn.db, "doc#av#20261113T0900Z", START, capacity=1)
    add_slot(conn.db, "other", START + timedelta(days=1), capacity=1)
    deps = make_deps()
    conn.close = lambda: None
    monkeypatch.setattr(patient_handler, "get_connection", lambda: conn)
    monkeypatch.setattr(patient_handler, "default_deps", lambda: deps)
    monkeypatch.setattr(doctor_handler, "get_connection", lambda: conn)
    monkeypatch.setattr(doctor_appointments, "default_deps", lambda: deps)
    monkeypatch.delenv("RESCHEDULE_STATE_MACHINE_ARN", raising=False)
    return conn, deps


def evt(method, resource, user="u-1", role="patient", body=None, path=None):
    return {"httpMethod": method, "resource": resource, "path": resource, "headers": {}, "pathParameters": path,
            "queryStringParameters": None, "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"userId": user, "role": role}}}


def call(handler, *args, **kw):
    result = handler.lambda_handler(evt(*args, **kw), None)
    return result["statusCode"], json.loads(result["body"])


def book(user, slot="doc#av#20261113T0900Z"):
    status, body = call(patient_handler, "POST", "/appointments", user=user, body={"slot_id": slot})
    assert status == 201, body
    return body["appointment"]["appointment_id"]


SLOT = "doc#av#20261113T0900Z"
ENC = "doc%23av%2320261113T0900Z"   # how the slot id arrives in the URL: "#" is percent-encoded


# --------------------------------------------------------------------------------- waitlist + offers
def test_waitlist_join_list_leave_through_the_lambda(world):
    book("u-1")
    status, body = call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    assert status == 201 and body["waitlist"]["position"] == 1 and body["waitlist"]["slot_id"] == SLOT
    status, body = call(patient_handler, "GET", "/patient/me/waitlists", user="u-2")
    assert status == 200 and body["count"] == 1 and body["limit"] == 5
    status, body = call(patient_handler, "DELETE", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    assert status == 200 and body["left"] is True
    status, body = call(patient_handler, "DELETE", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    assert (status, body["error"]) == (404, "NOT_ON_WAITLIST")


def test_offer_accept_journey_through_the_lambda(world):
    conn, deps = world
    appt = book("u-1")
    call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    status, _ = call(patient_handler, "POST", "/appointments/{appointment_id}/cancel", user="u-1", path={"appointment_id": appt})
    assert status == 200
    status, mine = call(patient_handler, "GET", "/patient/me/waitlists", user="u-2")
    offer = mine["items"][0]["offer"]
    assert mine["items"][0]["status"] == "OFFERED" and offer["seconds_left"] == 15 * 60
    # somebody else cannot accept it, the patient can
    assert call(patient_handler, "POST", "/waitlist-offers/{offer_id}/accept", user="u-3",
                path={"offer_id": offer["offer_id"]})[0] == 404
    status, body = call(patient_handler, "POST", "/waitlist-offers/{offer_id}/accept", user="u-2",
                        path={"offer_id": offer["offer_id"]})
    assert status == 201 and body["appointment"]["status"] == "CONFIRMED"


def test_a_doctor_token_is_refused_on_the_patient_routes(world):
    status, body = call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-d1", role="doctor", path={"slot_id": ENC})
    assert status == 403


def test_waitlist_unavailable_answers_503_not_500(world):
    conn, deps = world
    book("u-1")
    deps.waitlist = None
    status, body = call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    assert (status, body["error"]) == (503, "WAITLIST_UNAVAILABLE")


# --------------------------------------------------------------------------------- reschedule
def test_reschedule_through_the_lambda(world):
    appt = book("u-1")
    status, body = call(patient_handler, "PATCH", "/appointments/{appointment_id}", user="u-1",
                        path={"appointment_id": appt}, body={"slot_id": "other"})
    assert status == 200 and body["appointment"]["slot_id"] == "other" and body["appointment"]["reschedule_count"] == 1
    status, body = call(patient_handler, "PATCH", "/appointments/{appointment_id}", user="u-1",
                        path={"appointment_id": appt}, body={})
    assert (status, body["error"]) == (400, "INVALID_SLOT_ID")


def test_the_appointment_view_says_whether_it_can_be_rescheduled(world):
    conn, deps = world
    appt = book("u-1")
    status, body = call(patient_handler, "GET", "/appointments/{appointment_id}", path={"appointment_id": appt})
    assert body["appointment"]["can_reschedule"] is True
    deps.clock.now = START - timedelta(hours=5)
    status, body = call(patient_handler, "GET", "/appointments/{appointment_id}", path={"appointment_id": appt})
    assert body["appointment"]["can_reschedule"] is False and body["appointment"]["can_cancel"] is True


# --------------------------------------------------------------------------------- the doctor records what happened
def attendance(deps, appt, outcome, user="u-d1"):
    return call(doctor_handler, "POST", f"/doctors/me/appointments/{{appointment_id}}/{outcome}", user=user, role="doctor",
                path={"appointment_id": appt})


def test_a_doctor_marks_a_no_show_and_the_slot_closes(world):
    conn, deps = world
    appt = book("u-1")
    deps.clock.now = START + timedelta(minutes=5)
    status, body = attendance(deps, appt, "no-show")
    assert status == 200 and body["appointment"]["status"] == "NO_SHOW" and body["slot_closed"] is True
    assert body["appointment"]["refund"]["percentage"] == 0.0
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = ?", SLOT) == [("CLOSED",)]
    assert conn.q("SELECT refund_tier, refund_amount FROM refund_ledger") == [("NONE", 0.0)]
    assert deps.metrics.total("NoShows") == 1 and len(deps.events.of("AppointmentNoShow")) == 1


def test_a_doctor_marks_completed(world):
    conn, deps = world
    appt = book("u-1")
    deps.clock.now = START + timedelta(minutes=5)
    status, body = attendance(deps, appt, "complete")
    assert status == 200 and body["appointment"]["status"] == "COMPLETED"
    assert conn.q("SELECT COUNT(*) FROM refund_ledger") == [(0,)]


def test_the_attendance_can_only_be_recorded_once_the_time_has_come(world):
    conn, deps = world
    appt = book("u-1")
    status, body = attendance(deps, appt, "no-show")
    assert (status, body["error"]) == (409, "APPOINTMENT_NOT_STARTED")
    deps.clock.now = START
    assert attendance(deps, appt, "no-show")[0] == 200            # exactly at the start is allowed


def test_attendance_is_recorded_once_and_only_by_the_owning_doctor(world):
    conn, deps = world
    appt = book("u-1")
    deps.clock.now = START + timedelta(minutes=5)
    assert attendance(deps, appt, "no-show")[0] == 200
    status, body = attendance(deps, appt, "no-show")
    assert (status, body["error"]) == (409, "NOT_CONFIRMED")
    assert attendance(deps, appt, "complete")[0] == 409
    conn.db.execute("INSERT INTO users VALUES ('u-d3','d3@x',1)")
    conn.db.execute("INSERT INTO doctors VALUES ('d3','u-d3','h1','s1','Ivy','Nair','MD',3,'Asia/Kolkata',1)")
    conn.db.commit()
    assert attendance(deps, appt, "no-show", user="u-d3")[0] == 404


def test_a_multi_seat_slot_closes_only_when_everybody_is_settled(world):
    conn, deps = world
    add_slot(conn.db, "camp", START, capacity=2, ctype="hc", minutes=60)
    first, second = book("u-1", "camp"), book("u-2", "camp")
    deps.clock.now = START + timedelta(minutes=5)
    assert attendance(deps, first, "no-show")[1]["slot_closed"] is False
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = 'camp'") == [("FULL",)]
    assert attendance(deps, second, "complete")[1]["slot_closed"] is True


def test_the_doctor_view_offers_the_buttons_only_for_started_confirmed_appointments(world):
    conn, deps = world
    appt = book("u-1")
    status, body = call(doctor_handler, "GET", "/doctors/me/appointments/{appointment_id}", user="u-d1", role="doctor",
                        path={"appointment_id": appt})
    assert body["appointment"]["can_mark_attendance"] is False
    deps.clock.now = START + timedelta(minutes=1)
    status, body = call(doctor_handler, "GET", "/doctors/me/appointments/{appointment_id}", user="u-d1", role="doctor",
                        path={"appointment_id": appt})
    assert body["appointment"]["can_mark_attendance"] is True


def test_the_attendance_buttons_exist_only_while_the_slot_is_running(world):
    conn, deps = world
    add_slot(conn.db, "long", START + timedelta(days=2), capacity=1, ctype="hc", minutes=60)
    appt = book("u-1", "long")
    begin = START + timedelta(days=2)

    def can():
        return call(doctor_handler, "GET", "/doctors/me/appointments/{appointment_id}", user="u-d1", role="doctor",
                    path={"appointment_id": appt})[1]["appointment"]

    deps.clock.now = begin - timedelta(minutes=1)
    assert can()["can_mark_attendance"] is False
    deps.clock.now = begin
    assert can()["can_mark_attendance"] is True
    deps.clock.now = begin + timedelta(minutes=59, seconds=59)       # a 60 minute slot stays open for all 60 minutes
    assert can()["can_mark_attendance"] is True
    deps.clock.now = begin + timedelta(minutes=60)
    view = can()
    assert view["can_mark_attendance"] is False and view["status"] == "COMPLETED"   # shown as attended at once
    status, body = attendance(deps, appt, "no-show")
    assert (status, body["error"]) == (409, "ATTENDANCE_WINDOW_CLOSED")
    assert conn.q("SELECT status FROM appointments") == [("CONFIRMED",)]            # the sweeper stores it next
