#!/usr/bin/env python3
"""Live concurrency check of a deployed MediSlot API (standard library only).

    API_BASE_URL=https://<id>.execute-api.<region>.amazonaws.com/dev python scripts/concurrency_check.py
    PATIENTS=30 CAPACITY=3 KEEP=1 API_BASE_URL=... python scripts/concurrency_check.py

What it does
  1. creates a throw-away doctor with ONE Health Camp slot of CAPACITY seats (default 3)
  2. creates PATIENTS throw-away patients (default 24) and logs each one in
  3. releases all patients at the same instant: every one POSTs /appointments for that slot
  4. checks that EXACTLY CAPACITY bookings succeeded, the rest got 409 SLOT_FULL, nobody got a 5xx,
     and the doctor's schedule shows booked_count == CAPACITY
  5. replays one winner's Idempotency-Key and checks the ORIGINAL appointment comes back
  6. cleans up (the doctor cancels the slot, which refunds the winners in full) unless KEEP=1

Exits 0 only when every check holds. See docs/concurrency-design-note.md for how to read the
BookingConflictRetries metric afterwards (the script prints the exact AWS CLI command).
"""

import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

BASE = os.environ.get("API_BASE_URL", "").rstrip("/")
PATIENTS = int(os.environ.get("PATIENTS", "24"))
CAPACITY = int(os.environ.get("CAPACITY", "3"))
KEEP = os.environ.get("KEEP", "") == "1"
CODE = os.environ.get("DOCTOR_REGISTRATION_CODE", "")


def call(method, path, token=None, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            return error.code, json.loads(raw) if raw else {}
        except ValueError:
            return error.code, {"raw": raw[:200]}


def must(label, got, wanted):
    status, body = got
    if status != wanted:
        sys.exit(f"FAIL {label}: expected HTTP {wanted}, got {status}: {json.dumps(body)[:300]}")
    print(f"ok   {label}")
    return body


def check(label, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + label + ("" if condition else f"  {detail}"))
    return bool(condition)


def main():
    if not BASE:
        sys.exit("Set API_BASE_URL")
    if not 2 <= CAPACITY <= 10:
        sys.exit("CAPACITY must be between 2 and 10 (Health Camp Session seats)")
    if PATIENTS <= CAPACITY:
        sys.exit("PATIENTS must be larger than CAPACITY, otherwise nothing is contended")
    run = secrets.token_hex(4)
    password = "Conc" + secrets.token_hex(8) + "9"
    started = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ one doctor, one slot
    d_email = f"conc-doctor-{run}@smoke.medislot.test"
    reg = {"email": d_email, "password": password}
    if CODE:
        reg["registration_code"] = CODE
    must("doctor register", call("POST", "/auth/doctors/register", body=reg), 201)
    token = must("doctor login", call("POST", "/auth/doctors/login", body={"email": d_email, "password": password}), 200)["access_token"]
    hospital = must("hospitals", call("GET", "/hospitals", token), 200)["items"][0]["hospital_id"]
    spec = must("specialisations", call("GET", "/specialisations", token), 200)["items"][0]["specialisation_id"]
    must("doctor profile", call("PUT", "/doctors/me", token, {
        "first_name": "Concurrency", "last_name": f"D{run}", "licence_number": f"C-{secrets.randbelow(10**6):06d}",
        "hospital_id": hospital, "specialisation_id": spec, "timezone": "Asia/Kolkata", "experience_years": 5,
        "qualifications": "Load test"}), 201)
    ctype = must("health camp type", call("POST", "/doctors/me/consultation-types", token,
                 {"type_name": "Health Camp Session", "fee": 200}), 201)["consultation_type"]
    target = date.today() + timedelta(days=4)
    while target.isoweekday() > 5:
        target += timedelta(days=1)
    must(f"{CAPACITY}-seat slot on {target}", call("PUT", "/doctors/me/availability", token, {"patterns": [{
        "consultation_type_id": ctype["consultation_type_id"], "day_of_week": target.isoweekday(),
        "start_time": "09:00", "end_time": "10:00", "capacity": CAPACITY}]}), 200)
    doctor_id = must("doctor id", call("GET", "/doctors/me", token), 200)["profile"]["doctor_id"]

    # ------------------------------------------------------------------ the patients
    def make_patient(i):
        email = f"conc-patient-{run}-{i}@smoke.medislot.test"
        status, _ = call("POST", "/auth/patients/register",
                         body={"email": email, "password": password, "first_name": f"P{i}", "last_name": "Load"})
        if status != 201:
            sys.exit(f"FAIL patient {i} register: HTTP {status}")
        status, body = call("POST", "/auth/patients/login", body={"email": email, "password": password})
        if status != 200:
            sys.exit(f"FAIL patient {i} login: HTTP {status}")
        return body["access_token"]

    print(f"creating {PATIENTS} patients ...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(make_patient, range(PATIENTS)))
    slots = must("slots", call("GET", f"/doctors/{doctor_id}/slots?date={target.isoformat()}", tokens[0]), 200)["slots"]
    slot = next(s for s in slots if s["type_name"] == "Health Camp Session")
    check("slot shows the full capacity", slot["available_seats"] == CAPACITY, slot)

    # ------------------------------------------------------------------ the race
    barrier = threading.Barrier(PATIENTS)
    results = [None] * PATIENTS
    keys = [f"conc-{run}-{i}" for i in range(PATIENTS)]

    def attempt(i):
        barrier.wait()  # every patient is released at the same instant
        began = datetime.now(timezone.utc)
        status, body = call("POST", "/appointments", tokens[i], {"slot_id": slot["slot_id"]},
                            headers={"Idempotency-Key": keys[i]})
        results[i] = (status, body, (datetime.now(timezone.utc) - began).total_seconds())

    print(f"\nreleasing {PATIENTS} simultaneous bookings for a {CAPACITY}-seat slot ...")
    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(PATIENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tally = Counter((r[0], (r[1].get("error") or "created")) for r in results)
    print("\noutcomes:")
    for (status, what), count in sorted(tally.items()):
        print(f"  HTTP {status}  {what:<18} x {count}")
    winners = [i for i, r in enumerate(results) if r[0] == 201]
    slowest = max(r[2] for r in results)
    print(f"  slowest request: {slowest:.2f}s\n")

    ok = True
    ok &= check(f"exactly {CAPACITY} bookings succeeded", len(winners) == CAPACITY, f"got {len(winners)}")
    others = [r for r in results if r[0] != 201]
    ok &= check("every other request was a clean 409 SLOT_FULL",
                all(r[0] == 409 and r[1].get("error") == "SLOT_FULL" for r in others),
                Counter((r[0], r[1].get("error")) for r in others))
    ok &= check("nobody got a 5xx", not any(r[0] >= 500 for r in results))
    ok &= check("winners hold distinct appointments",
                len({results[i][1]["appointment"]["appointment_id"] for i in winners}) == len(winners))

    day = must("doctor schedule", call("GET", f"/doctors/me/schedule?date={target.isoformat()}", token), 200)
    shown = next(s for s in day["slots"] if s["slot_id"] == slot["slot_id"])
    ok &= check(f"doctor schedule shows {CAPACITY}/{CAPACITY} booked and FULL",
                shown["booked_count"] == CAPACITY and shown["status"] == "FULL", shown)
    ok &= check(f"doctor sees exactly {CAPACITY} appointments",
                call("GET", "/doctors/me/appointments", token)[1].get("total") == CAPACITY)
    gone = {s["slot_id"] for s in call("GET", f"/doctors/{doctor_id}/slots?date={target.isoformat()}", tokens[0])[1]["slots"]}
    ok &= check("the full slot is no longer offered to patients", slot["slot_id"] not in gone)

    if winners:
        w = winners[0]
        status, again = call("POST", "/appointments", tokens[w], {"slot_id": slot["slot_id"]}, headers={"Idempotency-Key": keys[w]})
        ok &= check("replaying a winner's Idempotency-Key returns the ORIGINAL appointment",
                    status == 201 and again.get("idempotent_replay") is True and
                    again["appointment"]["appointment_id"] == results[w][1]["appointment"]["appointment_id"], (status, again))
        ok &= check("... and still exactly one appointment for that patient",
                    call("GET", "/appointments", tokens[w])[1].get("total") == 1)

    # ------------------------------------------------------------------ clean up
    if KEEP:
        print("\nKEEP=1: leaving the doctor, patients and bookings in place.")
    else:
        must("doctor cancels the slot (refunds the winners in full)", call(
            "POST", "/doctors/me/slots/cancel", token, {"slot_id": slot["slot_id"], "reason": "load test finished"}), 200)
        if winners:
            ledger = call("GET", "/refunds", tokens[winners[0]])[1]
            ok &= check("a winner sees a 100% DOCTOR_CANCELLED refund",
                        ledger.get("total") == 1 and ledger["items"][0]["tier"] == "DOCTOR_CANCELLED", ledger)
        call("PUT", "/doctors/me/availability", token, {"patterns": []})
        for t in tokens:
            call("DELETE", "/patient/me", t)

    print("\nTo see the contention that was handled (BookingConflictRetries), run:\n")
    print("  aws cloudwatch get-metric-statistics --namespace MediSlot --metric-name BookingConflictRetries \\")
    print("    --dimensions Name=Environment,Value=dev --statistics Sum Maximum --period 60 \\")
    print(f"    --start-time {started.strftime('%Y-%m-%dT%H:%M:%SZ')} --end-time "
          f"{(datetime.now(timezone.utc) + timedelta(minutes=3)).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("\nRESULT:", "PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
