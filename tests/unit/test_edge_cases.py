"""Loophole checks: states that must not be reachable by clicking in an unexpected order."""

from datetime import timedelta

from test_milestone6_api import (ENC, SLOT, START, add_slot, attendance, book, call, doctor_handler,  # noqa: F401
                                 patient_handler, world)


def cancel(appt, user="u-1"):
    return call(patient_handler, "POST", "/appointments/{appointment_id}/cancel", user=user, path={"appointment_id": appt})


def resched(appt, slot, user="u-1"):
    return call(patient_handler, "PATCH", "/appointments/{appointment_id}", user=user, path={"appointment_id": appt},
                body={"slot_id": slot})


def test_a_no_show_or_completed_appointment_cannot_be_cancelled_or_rescheduled(world):  # noqa: F811
    conn, deps = world
    appt = book("u-1")
    deps.clock.now = START + timedelta(minutes=5)
    assert attendance(deps, appt, "no-show")[0] == 200
    assert cancel(appt)[0] == 409
    assert resched(appt, "other")[0] == 409
    assert conn.q("SELECT COUNT(*) FROM refund_ledger") == [(1,)]      # still just the 0 % no-show row


def test_nothing_can_be_booked_or_waitlisted_after_the_slot_started(world):  # noqa: F811
    conn, deps = world
    deps.clock.now = START + timedelta(minutes=1)
    status, body = call(patient_handler, "POST", "/appointments", user="u-1", body={"slot_id": SLOT})
    assert status == 409
    status, body = call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-1", path={"slot_id": ENC})
    assert status == 409


def test_a_closed_slot_cannot_be_booked_or_waitlisted(world):  # noqa: F811
    conn, deps = world
    book("u-1")
    deps.clock.now = START + timedelta(minutes=5)
    appt = conn.q("SELECT appointment_id FROM appointments")[0][0]
    attendance(deps, appt, "no-show")
    deps.clock.now = START - timedelta(days=1)
    assert call(patient_handler, "POST", "/appointments", user="u-2", body={"slot_id": SLOT})[0] == 409
    assert call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})[0] == 409


def test_a_patient_cannot_see_or_touch_another_patients_appointment(world):  # noqa: F811
    appt = book("u-1")
    assert call(patient_handler, "GET", "/appointments/{appointment_id}", user="u-2", path={"appointment_id": appt})[0] == 404
    assert cancel(appt, user="u-2")[0] == 404
    assert resched(appt, "other", user="u-2")[0] == 404


def test_a_doctor_cannot_cancel_or_mark_another_doctors_slot(world):  # noqa: F811
    conn, deps = world
    appt = book("u-1")
    conn.db.execute("INSERT INTO users VALUES ('u-d3','d3@x',1)")
    conn.db.execute("INSERT INTO doctors VALUES ('d3','u-d3','h1','s1','Ivy','Nair','MD',3,'Asia/Kolkata',1)")
    conn.db.commit()
    status, _ = call(doctor_handler, "POST", "/doctors/me/slots/cancel", user="u-d3", role="doctor", body={"slot_id": SLOT})
    assert status == 404
    assert attendance(deps, appt, "no-show", user="u-d3")[0] in (404, 409)


def test_a_patient_token_cannot_use_doctor_routes(world):  # noqa: F811
    appt = book("u-1")
    status, _ = call(doctor_handler, "POST", "/doctors/me/appointments/{appointment_id}/no-show", user="u-1", role="patient",
                     path={"appointment_id": appt})
    assert status == 403


def test_an_offer_cannot_be_accepted_twice_or_after_it_expired(world):  # noqa: F811
    conn, deps = world
    appt = book("u-1")
    call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    cancel(appt)
    offer = call(patient_handler, "GET", "/patient/me/waitlists", user="u-2")[1]["items"][0]["offer"]["offer_id"]
    deps.clock.now = deps.clock.now + timedelta(minutes=16)
    status, body = call(patient_handler, "POST", "/waitlist-offers/{offer_id}/accept", user="u-2", path={"offer_id": offer})
    assert status == 409


def test_a_cancelled_appointment_can_be_cancelled_only_once(world):  # noqa: F811
    appt = book("u-1")
    assert cancel(appt)[0] == 200
    assert cancel(appt)[0] == 409


def test_booking_the_same_slot_twice_with_different_keys_is_refused(world):  # noqa: F811
    book("u-1")
    status, body = call(patient_handler, "POST", "/appointments", user="u-1", body={"slot_id": SLOT})
    assert status == 409


def test_a_doctor_cancelling_a_slot_clears_its_waitlist_and_nobody_is_offered_a_closed_slot(world):  # noqa: F811
    conn, deps = world
    book("u-1")
    call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    status, body = call(doctor_handler, "POST", "/doctors/me/slots/cancel", user="u-d1", role="doctor", body={"slot_id": SLOT})
    assert status == 200
    assert deps.waitlist.entries_for_slot(SLOT) == []
    assert deps.waitlist.live_offers_for_slot(SLOT) == []
    assert call(patient_handler, "GET", "/patient/me/waitlists", user="u-2")[1]["count"] == 0


def test_no_show_marking_does_not_free_a_seat_for_the_waitlist(world):  # noqa: F811
    conn, deps = world
    appt = book("u-1")
    call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    deps.clock.now = START + timedelta(minutes=5)
    assert attendance(deps, appt, "no-show")[0] == 200
    assert deps.waitlist.live_offers_for_slot(SLOT) == []
