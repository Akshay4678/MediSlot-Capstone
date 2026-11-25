#!/usr/bin/env python3
"""End-to-end smoke test of a deployed MediSlot API (standard library only).

    API_BASE_URL=https://<id>.execute-api.<region>.amazonaws.com/dev python scripts/smoke_test.py

Optional: DOCTOR_REGISTRATION_CODE when the SSM registration-code gate is enabled.
Exits non-zero on the first failed check, so it can gate the pipeline.
It creates one throw-away patient and one throw-away doctor with random credentials.
"""

import json
import os
import secrets
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE = os.environ.get("API_BASE_URL", "").rstrip("/")
CODE = os.environ.get("DOCTOR_REGISTRATION_CODE", "")
steps = 0


def call(method, path, token=None, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            return error.code, json.loads(raw) if raw else {}
        except ValueError:
            return error.code, {"raw": raw[:200]}


def expect(label, got, wanted):
    global steps
    steps += 1
    status, body = got
    if status != wanted:
        print(f"FAIL {label}: expected HTTP {wanted}, got {status}: {json.dumps(body)[:300]}")
        sys.exit(1)
    print(f"ok   {label} ({status})")
    return body


def main():
    if not BASE:
        sys.exit("Set API_BASE_URL")
    run = secrets.token_hex(4)
    password = "Smoke" + secrets.token_hex(8) + "9"

    # ------------------------------------------------------------ patient + token rules
    p_email = f"smoke-patient-{run}@smoke.medislot.test"
    expect("patient register", call("POST", "/auth/patients/register",
           body={"email": p_email, "password": password, "first_name": "Smoke", "last_name": "Patient"}), 201)
    expect("duplicate email rejected", call("POST", "/auth/patients/register",
           body={"email": p_email, "password": password, "first_name": "S", "last_name": "P"}), 409)
    expect("weak password rejected", call("POST", "/auth/patients/register",
           body={"email": f"w-{run}@smoke.medislot.test", "password": "short1", "first_name": "S", "last_name": "P"}), 400)
    expect("wrong password", call("POST", "/auth/patients/login", body={"email": p_email, "password": "Wrong-password-1"}), 401)
    expect("patient cannot use the doctor login", call("POST", "/auth/doctors/login", body={"email": p_email, "password": password}), 401)

    first = expect("patient login", call("POST", "/auth/patients/login", body={"email": p_email, "password": password}), 200)
    assert "user" not in first and "email" not in json.dumps(first), "login must not return user details"
    expect("access token accepted", call("GET", "/auth/test-token", first["access_token"]), 200)
    expect("patient token rejected on doctor endpoint (403)", call("GET", "/doctors/me", first["access_token"]), 403)
    expect("tampered token (401)", call("GET", "/auth/test-token", first["access_token"] + "x"), 401)
    expect("no token (401)", call("GET", "/auth/test-token"), 401)

    second = expect("second login", call("POST", "/auth/patients/login", body={"email": p_email, "password": password}), 200)
    expect("old access token replaced (401)", call("GET", "/auth/test-token", first["access_token"]), 401)
    expect("new access token accepted", call("GET", "/auth/test-token", second["access_token"]), 200)

    rotated = expect("refresh", call("POST", "/auth/refresh", body={"refresh_token": second["refresh_token"]}), 200)
    expect("access token replaced by refresh (401)", call("GET", "/auth/test-token", second["access_token"]), 401)
    expect("rotated refresh token cannot be replayed", call("POST", "/auth/refresh", body={"refresh_token": second["refresh_token"]}), 401)
    expect("logout", call("POST", "/auth/logout", rotated["access_token"], {"refresh_token": rotated["refresh_token"]}), 200)
    expect("access token after logout (401)", call("GET", "/auth/test-token", rotated["access_token"]), 401)
    expect("refresh token after logout (401)", call("POST", "/auth/refresh", body={"refresh_token": rotated["refresh_token"]}), 401)

    # ------------------------------------------------------------------- doctor domain
    d_email = f"smoke-doctor-{run}@smoke.medislot.test"
    reg = {"email": d_email, "password": password}
    if CODE:
        reg["registration_code"] = CODE
    expect("doctor register", call("POST", "/auth/doctors/register", body=reg), 201)
    token = expect("doctor login", call("POST", "/auth/doctors/login", body={"email": d_email, "password": password}), 200)["access_token"]
    expect("doctor token rejected on patient endpoint (403)", call("GET", "/patient/me", token), 403)
    expect("profile not created yet", call("GET", "/doctors/me", token), 404)
    expect("availability needs a profile", call("GET", "/doctors/me/availability", token), 409)

    hospital = expect("hospitals", call("GET", "/hospitals", token), 200)["items"][0]["hospital_id"]
    spec = expect("specialisations", call("GET", "/specialisations", token), 200)["items"][0]["specialisation_id"]
    profile = {"first_name": "Smoke", "last_name": f"D{run}", "licence_number": f"S-{secrets.randbelow(10**6):06d}",
               "hospital_id": hospital, "specialisation_id": spec, "timezone": "Asia/Kolkata",
               "experience_years": 3, "qualifications": "Smoke test"}
    expect("profile create", call("PUT", "/doctors/me", token, profile), 201)
    expect("profile read", call("GET", "/doctors/me", token), 200)
    for bad in ("AB12", "AB-123456", "ABCD123", "SMOKE-1"):  # <5 chars, >8 chars, >3 letters, >3 letters
        expect(f"bad licence {bad} rejected", call("PUT", "/doctors/me", token, {"licence_number": bad}), 400)
    expect("bad timezone rejected", call("PUT", "/doctors/me", token, {"timezone": "Mars/Base"}), 400)
    expect("profile update", call("PUT", "/doctors/me", token, {"experience_years": 4}), 200)

    grant = expect("photo upload url", call("POST", "/doctors/me/photo", token, {"content_type": "image/png"}), 200)
    assert grant["upload"]["url"].startswith("https://") and "key" in grant["upload"]["fields"]
    expect("unsupported photo type", call("POST", "/doctors/me/photo", token, {"content_type": "text/html"}), 400)

    expect("invalid consultation type", call("POST", "/doctors/me/consultation-types", token, {"type_name": "Spa", "fee": 5}), 400)
    ctype = expect("consultation type create", call("POST", "/doctors/me/consultation-types", token,
                   {"type_name": "Standard Consult", "fee": 500}), 201)["consultation_type"]
    assert ctype["duration_minutes"] == 30
    expect("duplicate consultation type", call("POST", "/doctors/me/consultation-types", token,
           {"type_name": "Standard Consult", "fee": 500}), 409)
    expect("fee update", call("PUT", f"/doctors/me/consultation-types/{ctype['consultation_type_id']}", token, {"fee": 550}), 200)

    pattern = lambda start, end, day=1: {"consultation_type_id": ctype["consultation_type_id"], "day_of_week": day,  # noqa: E731
                                          "start_time": start, "end_time": end}
    expect("overlapping patterns rejected", call("PUT", "/doctors/me/availability", token,
           {"patterns": [pattern("09:00", "12:00"), pattern("11:00", "13:00")]}), 400)
    # Normal consultations are always one patient: a larger capacity is accepted but forced to 1.
    forced = expect("normal consult capacity forced to 1", call("PUT", "/doctors/me/availability", token,
                    {"patterns": [dict(pattern("09:00", "12:00"), capacity=3)]}), 200)
    assert all(p["capacity"] == 1 for p in forced["patterns"]), forced["patterns"]
    saved = expect("availability save", call("PUT", "/doctors/me/availability", token,
                   {"patterns": [pattern("09:00", "12:00", day) for day in (1, 2, 3, 4, 5)]}), 200)
    assert saved["slots"]["created"] > 0, "saving availability should create slots in the 30-day window"
    again = expect("availability save again (idempotent)", call("PUT", "/doctors/me/availability", token,
                   {"patterns": [dict(p, availability_id=p["availability_id"]) for p in saved["patterns"]]}), 200)
    assert again["slots"]["created"] == saved["slots"]["created"], "rebuild must reproduce the same slots"

    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
    day = expect("schedule day view", call("GET", f"/doctors/me/schedule?date={monday.isoformat()}", token), 200)
    assert len(day["slots"]) == 6 and day["slots"][0]["start_time"] == "09:00", day["slots"][:1]
    week = expect("schedule week view", call("GET", f"/doctors/me/schedule?from={monday.isoformat()}"
                  f"&to={(monday + timedelta(days=6)).isoformat()}", token), 200)
    assert len(week["days"]) == 7
    expect("schedule range limit", call("GET", f"/doctors/me/schedule?from={monday.isoformat()}"
           f"&to={(monday + timedelta(days=40)).isoformat()}", token), 400)

    black = expect("blackout add", call("POST", "/doctors/me/blackouts", token,
                   {"blackout_date": monday.isoformat(), "reason": "smoke"}), 201)
    assert black["slots_blocked"] == 6
    expect("duplicate blackout", call("POST", "/doctors/me/blackouts", token, {"blackout_date": monday.isoformat()}), 409)
    blocked = call("GET", f"/doctors/me/schedule?date={monday.isoformat()}", token)[1]
    assert blocked["blackout"] and all(s["status"] == "BLOCKED" for s in blocked["slots"])
    expect("past blackout rejected", call("POST", "/doctors/me/blackouts", token, {"blackout_date": "2020-01-01"}), 400)
    expect("blackout delete", call("DELETE", f"/doctors/me/blackouts/{black['blackout']['blackout_id']}", token), 200)
    reopened = call("GET", f"/doctors/me/schedule?date={monday.isoformat()}", token)[1]
    assert all(s["status"] == "OPEN" for s in reopened["slots"]) and len(reopened["slots"]) == 6

    # ------------------------------------------------------------ patient domain (Milestone 4)
    doctor_id = expect("doctor id", call("GET", "/doctors/me", token), 200)["profile"]["doctor_id"]
    a_token = expect("patient A login", call("POST", "/auth/patients/login",
                     body={"email": p_email, "password": password}), 200)["access_token"]
    b_email = f"smoke-patient-b-{run}@smoke.medislot.test"
    expect("patient B register", call("POST", "/auth/patients/register",
           body={"email": b_email, "password": password, "first_name": "Other", "last_name": "Person"}), 201)
    b_token = expect("patient B login", call("POST", "/auth/patients/login",
                     body={"email": b_email, "password": password}), 200)["access_token"]

    # Profile: read, update, validation, second create refused.
    mine = expect("patient profile read", call("GET", "/patient/me", a_token), 200)["patient"]
    assert mine["first_name"] == "Smoke", mine
    expect("patient profile update", call("PUT", "/patient/me", a_token, {"city": "Hyderabad"}), 200)
    expect("patient profile bad field", call("PUT", "/patient/me", a_token, {"user_id": "x"}), 400)
    expect("patient profile exists", call("POST", "/patient/me", a_token, {"first_name": "A", "last_name": "B"}), 409)

    # Data isolation: there is no id in the URL, so each patient only ever sees their own profile.
    theirs = expect("patient B profile read", call("GET", "/patient/me", b_token), 200)["patient"]
    assert theirs["first_name"] == "Other" and theirs["patient_id"] != mine["patient_id"], theirs
    leak = expect("patient id in query is ignored",
                  call("GET", f"/patient/me?patient_id={mine['patient_id']}", b_token), 200)["patient"]
    assert leak["patient_id"] == theirs["patient_id"], "patient B must never receive patient A's profile"
    expect("patient B cannot edit A via body", call("PUT", "/patient/me", b_token,
           {"patient_id": mine["patient_id"], "city": "Hacked"}), 400)
    assert call("GET", "/patient/me", a_token)[1]["patient"]["city"] == "Hyderabad"

    # Doctor search: filters and pagination.
    found = expect("search by name", call("GET", f"/doctors/search?name=Smoke%20D{run}", a_token), 200)
    assert any(d["doctor_id"] == doctor_id for d in found["items"]), found
    expect("search by hospital", call("GET", "/doctors/search?hospital=" + urllib.parse.quote(
        found["items"][0]["hospital_name"][:6]), a_token), 200)
    mine_row = next(d for d in found["items"] if d["doctor_id"] == doctor_id)
    both = expect("search by city and specialisation", call(
        "GET", f"/doctors/search?name=Smoke%20D{run}&city=" + urllib.parse.quote(mine_row["city"]) +
        "&specialisation=" + urllib.parse.quote(mine_row["specialisation"]), a_token), 200)
    assert any(d["doctor_id"] == doctor_id for d in both["items"])
    page = expect("search pagination", call("GET", "/doctors/search?limit=1&offset=0", a_token), 200)
    assert page["count"] == 1 and page["limit"] == 1 and "total" in page and "has_more" in page, page
    expect("search bad pagination", call("GET", "/doctors/search?limit=abc", a_token), 400)

    # Public doctor profile with consultation types and fees.
    public = expect("public doctor profile", call("GET", f"/doctors/{doctor_id}", a_token), 200)
    assert any(t["consultation_type_id"] == ctype["consultation_type_id"] and float(t["fee"]) == 550
               for t in public["consultation_types"]), public
    expect("unknown doctor 404", call("GET", f"/doctors/{uuid.uuid4()}", a_token), 404)

    # Free seats per slot for a date range.
    rng = expect("slots for a date range", call(
        "GET", f"/doctors/{doctor_id}/slots?from={monday.isoformat()}&to={(monday + timedelta(days=6)).isoformat()}",
        a_token), 200)
    assert len(rng["days"]) == 7 and all("available_seats" in x for x in rng["slots"]), rng
    day_slots = next(d for d in rng["days"] if d["date"] == monday.isoformat())["slots"]
    assert len(day_slots) == 6 and day_slots[0]["available_seats"] == 1, day_slots
    # Any date inside the 30-day booking window may be viewed; beyond it is a clear 400.
    edge = date.today() + timedelta(days=29)  # today + the next 29 days = the 30-day window
    expect("slots on the last day of the window", call(
        "GET", f"/doctors/{doctor_id}/slots?date={edge.isoformat()}", a_token), 200)
    beyond = expect("slots beyond the booking window", call(
        "GET", f"/doctors/{doctor_id}/slots?date={(edge + timedelta(days=1)).isoformat()}", a_token), 400)
    assert beyond["error"] == "OUTSIDE_BOOKING_WINDOW", beyond

    # ------------------------------------------------- booking, cancellation, refunds (Milestone 5)
    book_day = date.today() + timedelta(days=4)  # >= 3.4 days away, so a cancellation earns a FULL refund
    while book_day.isoweekday() > 5:
        book_day += timedelta(days=1)
    open_slots = expect("slots for the booking day", call(
        "GET", f"/doctors/{doctor_id}/slots?date={book_day.isoformat()}", a_token), 200)["slots"]
    assert len(open_slots) >= 2, open_slots
    slot_one, slot_two = open_slots[0]["slot_id"], open_slots[1]["slot_id"]
    key = f"smoke-{run}-1"

    first_booking = expect("book a slot", call("POST", "/appointments", a_token, {"slot_id": slot_one},
                           headers={"Idempotency-Key": key}), 201)["appointment"]
    assert first_booking["status"] == "CONFIRMED" and first_booking["fee"] == 550.0, first_booking
    replay = expect("same Idempotency-Key replayed", call("POST", "/appointments", a_token, {"slot_id": slot_one},
                    headers={"Idempotency-Key": key}), 201)
    assert replay["idempotent_replay"] is True and replay["appointment"]["appointment_id"] == first_booking["appointment_id"]
    expect("same key, different request rejected", call("POST", "/appointments", a_token, {"slot_id": slot_two},
           headers={"Idempotency-Key": key}), 422)
    expect("full slot returns 409", call("POST", "/appointments", b_token, {"slot_id": slot_one}), 409)
    expect("unknown slot returns 404", call("POST", "/appointments", b_token, {"slot_id": "no-such-slot"}), 404)
    expect("patient id in the body is refused", call("POST", "/appointments", b_token,
           {"slot_id": slot_two, "patient_id": mine["patient_id"]}), 400)
    visible = {s["slot_id"]: s for s in call("GET", f"/doctors/{doctor_id}/slots?date={book_day.isoformat()}", a_token)[1]["slots"]}
    assert slot_one in visible and visible[slot_one]["status"] == "FULL", (
        "a full slot remains visible to patients so it can be used for waitlist entry"
    )

    listing = expect("my appointments", call("GET", "/appointments", a_token), 200)
    assert listing["total"] == 1 and listing["items"][0]["appointment_id"] == first_booking["appointment_id"], listing
    expect("appointment detail", call("GET", f"/appointments/{first_booking['appointment_id']}", a_token), 200)
    expect("another patient cannot read it", call("GET", f"/appointments/{first_booking['appointment_id']}", b_token), 404)
    expect("another patient cannot cancel it", call("POST", f"/appointments/{first_booking['appointment_id']}/cancel", b_token), 404)
    assert call("GET", "/appointments", b_token)[1]["total"] == 0
    expect("doctor token rejected on patient bookings (403)", call("GET", "/appointments", token), 403)
    seen = expect("doctor sees the booking", call("GET", "/doctors/me/appointments", token), 200)
    assert seen["total"] == 1 and seen["items"][0]["patient"]["name"] == "Smoke Patient", seen
    expect("patient token rejected on doctor appointments (403)", call("GET", "/doctors/me/appointments", a_token), 403)

    cancelled = expect("patient cancels (48 h or more ahead)", call(
        "POST", f"/appointments/{first_booking['appointment_id']}/cancel", a_token, {"reason": "smoke"}), 200)["appointment"]
    assert cancelled["status"] == "CANCELLED_BY_PATIENT" and cancelled["refund"]["tier"] == "FULL"
    assert cancelled["refund"]["amount"] == 550.0 and cancelled["refund"]["percentage"] == 100.0, cancelled
    expect("cancel twice rejected", call("POST", f"/appointments/{first_booking['appointment_id']}/cancel", a_token), 409)
    reopened_ids = {s["slot_id"] for s in call("GET", f"/doctors/{doctor_id}/slots?date={book_day.isoformat()}", a_token)[1]["slots"]}
    assert slot_one in reopened_ids, "the cancelled seat must be bookable again"

    second_booking = expect("book the second slot", call("POST", "/appointments", a_token, {"slot_id": slot_two}), 201)["appointment"]
    expect("doctor cancels the slot", call("POST", "/doctors/me/slots/cancel", token,
           {"slot_id": slot_two, "reason": "smoke"}), 200)
    expect("cancelled slot cannot be booked", call("POST", "/appointments", b_token, {"slot_id": slot_two}), 409)
    after = call("GET", f"/appointments/{second_booking['appointment_id']}", a_token)[1]["appointment"]
    assert after["status"] == "CANCELLED_BY_DOCTOR" and after["refund"]["tier"] == "DOCTOR_CANCELLED", after
    ledger = expect("refund ledger", call("GET", "/refunds", a_token), 200)
    assert ledger["total"] == 2 and ledger["total_refunded"] == 1100.0, ledger
    expect("other patient has no refunds", call("GET", "/refunds", b_token), 200)
    expect("doctor token rejected on refunds (403)", call("GET", "/refunds", token), 403)

    # Clean up the throw-away patients.
    expect("patient profile delete", call("DELETE", "/patient/me", b_token), 200)
    expect("patient A profile delete", call("DELETE", "/patient/me", a_token), 200)

    expect("clear availability", call("PUT", "/doctors/me/availability", token, {"patterns": []}), 200)
    expect("logout doctor", call("POST", "/auth/logout", token), 200)
    expect("doctor token after logout (401)", call("GET", "/doctors/me", token), 401)
    print(f"\nSmoke test passed: {steps} checks.")


if __name__ == "__main__":
    main()