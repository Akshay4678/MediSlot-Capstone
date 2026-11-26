"""Milestone 6: the reschedule saga, including Mandatory Test 3 (forced failure during release)."""

import json
from datetime import timedelta

import pytest
from booking_support import NOW, SCHEMA, add_slot, make_deps, run_threads, seed
from sqlite_mysql import Conn, FileDb

from application.booking import service
from application.common import rules
from application.common.api import ApiError
from application.reschedule import saga
from application.reschedule import service as reschedule
from application.waitlist import service as waitlist

START = NOW + timedelta(days=3, hours=3)
LATER = START + timedelta(days=1)


def code(call):
    with pytest.raises(ApiError) as exc:
        call()
    return exc.value.status, exc.value.code


@pytest.fixture
def world():
    conn = Conn(SCHEMA)
    seed(conn.db, patients=4)
    add_slot(conn.db, "a", START, capacity=1)
    add_slot(conn.db, "b", LATER, capacity=1)
    add_slot(conn.db, "c", LATER + timedelta(hours=1), capacity=1)
    return conn, make_deps()


def book(conn, deps, user, slot):
    return service.book(conn, deps, user, {"slot_id": slot}, None)[1]["appointment"]["appointment_id"]


def move(conn, deps, user, appt, target):
    return reschedule.reschedule(conn, deps, user, appt, {"slot_id": target})


def appts(conn, patient="p-1"):
    return conn.q("SELECT slot_id, status, reschedule_count FROM appointments WHERE patient_id = ? ORDER BY created_at", patient)


def seats(conn, deps, slot):
    row = conn.q("SELECT booked_count, status FROM materialised_slots WHERE slot_id = ?", slot)[0]
    return row[0], deps.slot_table.items.get(slot, {}).get("booked_count", 0), row[1]


# ============================================================================ the happy path
def test_a_reschedule_moves_the_appointment_and_both_seat_counters(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    status, body = move(conn, deps, "u-1", appt, "b")
    assert status == 200 and body["appointment"]["slot_id"] == "b"
    assert appts(conn) == [("b", "CONFIRMED", 1)]
    assert seats(conn, deps, "a") == (0, 0, "OPEN") and seats(conn, deps, "b") == (1, 1, "FULL")
    assert deps.metrics.total("ReschedulesSucceeded") == 1 and deps.metrics.total("ReschedulesCompensated") == 0


def test_a_reschedule_publishes_appointment_rescheduled_and_frees_the_old_seat(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    move(conn, deps, "u-1", appt, "b")
    event = deps.events.of("AppointmentRescheduled")[0]
    assert event["appointment_id"] == appt and event["from_slot_id"] == "a" and event["to_slot_id"] == "b"
    assert event["reschedule_count"] == 1
    assert deps.events.of("SlotSeatFreed")[-1]["slot_id"] == "a"


def test_a_reschedule_carries_no_fee_or_refund_implication(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    move(conn, deps, "u-1", appt, "b")
    assert conn.q("SELECT fee_amount FROM appointments") == [(500.0,)]
    assert conn.q("SELECT COUNT(*) FROM refund_ledger") == [(0,)]


def test_the_freed_original_seat_goes_to_the_waitlist(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    waitlist.join(conn, deps, "u-2", "a")
    move(conn, deps, "u-1", appt, "b")
    assert [o["patient_id"] for o in deps.waitlist.live_offers_for_slot("a")] == ["p-2"]


# ============================================================================ the rules
def test_a_full_target_fails_and_the_original_stays_confirmed(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    book(conn, deps, "u-2", "b")
    assert code(lambda: move(conn, deps, "u-1", appt, "b")) == (409, "SLOT_FULL")
    assert appts(conn) == [("a", "CONFIRMED", 0)] and seats(conn, deps, "a")[:2] == (1, 1)


def test_the_target_seat_is_claimed_in_dynamodb_too_not_just_checked_in_rds(world):
    """RDS says free but DynamoDB says full (a booking in flight): the claim step refuses."""
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    deps.slot_table.items["b"] = {"slot_id": "b", "booked_count": 1, "version": 3}
    assert code(lambda: move(conn, deps, "u-1", appt, "b")) == (409, "SLOT_FULL")
    assert appts(conn) == [("a", "CONFIRMED", 0)]


def test_an_appointment_can_be_rescheduled_at_most_twice(world):
    conn, deps = world
    add_slot(conn.db, "d", LATER + timedelta(hours=2), capacity=1)
    appt = book(conn, deps, "u-1", "a")
    move(conn, deps, "u-1", appt, "b")
    move(conn, deps, "u-1", appt, "c")
    assert code(lambda: move(conn, deps, "u-1", appt, "d")) == (409, "RESCHEDULE_LIMIT_REACHED")
    assert appts(conn) == [("c", "CONFIRMED", 2)]


def test_the_reschedule_limit_comes_from_ssm(world, monkeypatch):
    conn, deps = world
    monkeypatch.setattr(rules, "max_reschedules", lambda: 1)
    appt = book(conn, deps, "u-1", "a")
    move(conn, deps, "u-1", appt, "b")
    assert code(lambda: move(conn, deps, "u-1", appt, "c")) == (409, "RESCHEDULE_LIMIT_REACHED")


@pytest.mark.parametrize("hours_left, allowed", [(6, True), (6.01, True), (5.99, False), (1, False)])
def test_rescheduling_within_six_hours_of_the_start_is_not_permitted(world, hours_left, allowed):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    deps.clock.now = START - timedelta(hours=hours_left)
    if allowed:
        assert move(conn, deps, "u-1", appt, "b")[0] == 200
    else:
        assert code(lambda: move(conn, deps, "u-1", appt, "b")) == (409, "RESCHEDULE_TOO_LATE")
        assert appts(conn) == [("a", "CONFIRMED", 0)]


def test_a_cancelled_appointment_cannot_be_rescheduled(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    service.cancel_by_patient(conn, deps, "u-1", appt)
    assert code(lambda: move(conn, deps, "u-1", appt, "b")) == (409, "NOT_RESCHEDULABLE")


def test_validation_errors(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    assert code(lambda: move(conn, deps, "u-1", appt, "a")) == (400, "SAME_SLOT")
    assert code(lambda: move(conn, deps, "u-1", appt, "nope")) == (404, "SLOT_NOT_FOUND")
    assert code(lambda: move(conn, deps, "u-2", appt, "b")) == (404, "APPOINTMENT_NOT_FOUND")   # not theirs
    assert code(lambda: reschedule.reschedule(conn, deps, "u-1", appt, {})) == (400, "INVALID_SLOT_ID")
    assert code(lambda: reschedule.reschedule(conn, deps, "u-1", appt, {"slot_id": "b", "x": 1})) == (400, "UNKNOWN_FIELD")
    add_slot(conn.db, "other", LATER, capacity=1, doctor="d2")
    assert code(lambda: move(conn, deps, "u-1", appt, "other")) == (409, "DIFFERENT_DOCTOR")
    add_slot(conn.db, "gone", LATER, capacity=1, status="BLOCKED")
    assert code(lambda: move(conn, deps, "u-1", appt, "gone")) == (409, "SLOT_UNAVAILABLE")
    deps.clock.now = START - timedelta(hours=10)
    add_slot(conn.db, "past", deps.clock.now - timedelta(hours=1), capacity=1)
    assert code(lambda: move(conn, deps, "u-1", appt, "past")) == (409, "SLOT_IN_PAST")


def test_moving_to_a_slot_that_overlaps_the_patients_other_appointment_is_refused(world):
    conn, deps = world
    book(conn, deps, "u-1", "c")
    appt = book(conn, deps, "u-1", "a")
    add_slot(conn.db, "clash", LATER + timedelta(hours=1, minutes=10), capacity=1)
    assert code(lambda: move(conn, deps, "u-1", appt, "clash")) == (409, "TIME_CONFLICT")


def test_a_slot_overlapping_only_the_appointment_being_moved_is_fine(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    add_slot(conn.db, "near", START + timedelta(minutes=10), capacity=1)
    assert move(conn, deps, "u-1", appt, "near")[0] == 200


# ============================================================================ MANDATORY TEST 3
class ReleaseFails:
    """Wraps the slot store so that releasing ONE particular slot raises (the forced failure)."""

    def __init__(self, real, slot_id):
        self.real, self.slot_id = real, slot_id

    def release(self, slot_id, count=1):
        if slot_id == self.slot_id:
            raise RuntimeError("DynamoDB is unavailable (forced failure)")
        return self.real.release(slot_id, count)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_mandatory_3_reschedule_saga_forced_failure_during_release_patient_holds_exactly_one_appointment(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    deps.slots = ReleaseFails(deps.slots, "a")          # step 4 (release original) fails AFTER the target was claimed

    assert code(lambda: move(conn, deps, "u-1", appt, "b")) == (409, "RESCHEDULE_FAILED")

    held = conn.q("SELECT slot_id FROM appointments WHERE patient_id = 'p-1' AND status = 'CONFIRMED'")
    assert held == [("a",)], "exactly one live appointment, and it is the ORIGINAL one"
    assert conn.q("SELECT COUNT(*) FROM appointments") == [(1,)]
    assert appts(conn) == [("a", "CONFIRMED", 0)]       # reschedule_count rolled back too
    # the compensation returned the claimed target seat; the original seat was never released
    assert deps.slot_table.items["a"]["booked_count"] == 1 and deps.slot_table.items["b"]["booked_count"] == 0
    assert conn.q("SELECT booked_count, status FROM materialised_slots WHERE slot_id = 'a'") == [(1, "FULL")]
    assert conn.q("SELECT booked_count, status FROM materialised_slots WHERE slot_id = 'b'") == [(0, "OPEN")]
    assert deps.metrics.total("ReschedulesCompensated") == 1 and deps.metrics.total("ReschedulesSucceeded") == 0
    assert deps.events.of("AppointmentRescheduled") == []
    # and the target is bookable by someone else again
    assert service.book(conn, deps, "u-2", {"slot_id": "b"}, None)[0] == 201


def test_mandatory_3_the_same_holds_when_the_database_move_fails_after_the_claim(world, monkeypatch):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")

    def broken(conn_, deps_, state):
        raise RuntimeError("RDS connection lost")
    monkeypatch.setattr(saga, "move_appointment", broken)
    assert code(lambda: move(conn, deps, "u-1", appt, "b")) == (409, "RESCHEDULE_FAILED")
    assert appts(conn) == [("a", "CONFIRMED", 0)]
    assert deps.slot_table.items["b"]["booked_count"] == 0 and deps.slot_table.items["a"]["booked_count"] == 1


def test_compensation_is_safe_when_nothing_had_moved_yet(world):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    state = saga.validate(conn, deps, {"user_id": "u-1", "appointment_id": appt, "target_slot_id": "b"})
    state = saga.claim_target(conn, deps, state)
    saga.compensate(conn, deps, state)      # the appointment never moved: only the claimed seat goes back
    saga.compensate(conn, deps, dict(state, target_claimed=False))   # running it again changes nothing
    assert appts(conn) == [("a", "CONFIRMED", 0)] and seats(conn, deps, "a")[:2] == (1, 1)
    assert deps.slot_table.items["b"]["booked_count"] == 0


def test_two_simultaneous_reschedules_into_the_last_seat_have_one_winner(tmp_path):
    db = FileDb(tmp_path / "rds.db", SCHEMA)
    boot = db.connect()
    seed(boot.db, patients=3)
    add_slot(boot.db, "a", START, capacity=1)
    add_slot(boot.db, "a2", START + timedelta(hours=1), capacity=1)
    add_slot(boot.db, "b", LATER, capacity=1)
    boot.close()
    deps = make_deps()
    c = db.connect()
    first, second = book(c, deps, "u-1", "a"), book(c, deps, "u-2", "a2")
    c.close()

    def attempt(i):
        conn = db.connect()
        try:
            return move(conn, deps, "u-1" if i == 0 else "u-2", first if i == 0 else second, "b")
        finally:
            conn.close()

    results = run_threads(2, attempt)
    assert len([r for r in results if isinstance(r, tuple)]) == 1
    assert len([r for r in results if isinstance(r, ApiError)]) == 1
    check = db.connect()
    assert check.q("SELECT COUNT(*) FROM appointments WHERE slot_id = 'b' AND status = 'CONFIRMED'") == [(1,)]
    assert deps.slot_table.items["b"]["booked_count"] == 1
    assert check.q("SELECT COUNT(*) FROM appointments WHERE status = 'CONFIRMED'") == [(2,)]   # nobody lost an appointment


# ============================================================================ the Step Functions boundary
def execution(status, output=None, error=None, cause=None):
    return {"status": status, "output": json.dumps(output) if output else None, "error": error, "cause": cause}


def test_a_succeeded_execution_returns_the_appointment():
    assert saga.result_from_execution(execution("SUCCEEDED", {"result": {"appointment": {"x": 1}}})) == (200, {"appointment": {"x": 1}})


def test_a_rejection_inside_step_functions_keeps_its_status_and_code():
    rejected = saga.SagaRejected(409, "SLOT_FULL", "full")
    cause = json.dumps({"errorMessage": str(rejected), "errorType": "SagaRejected"})
    with pytest.raises(ApiError) as exc:
        saga.result_from_execution(execution("FAILED", error="SagaRejected", cause=cause))
    assert (exc.value.status, exc.value.code) == (409, "SLOT_FULL")


def test_a_compensated_execution_is_reported_as_a_clean_failure():
    with pytest.raises(ApiError) as exc:
        saga.result_from_execution(execution("FAILED", error="RescheduleCompensated", cause="rolled back"))
    assert (exc.value.status, exc.value.code) == (409, "RESCHEDULE_FAILED")


def test_with_a_state_machine_arn_the_request_goes_through_step_functions(world, monkeypatch):
    conn, deps = world
    appt = book(conn, deps, "u-1", "a")
    seen = {}

    class Sfn:
        def start_sync_execution(self, **kw):
            seen.update(kw)
            return execution("SUCCEEDED", {"result": {"appointment": {"ok": True}}})
    deps.sfn = Sfn()
    monkeypatch.setenv("RESCHEDULE_STATE_MACHINE_ARN", "arn:aws:states:ap-south-1:1:stateMachine:x")
    assert move(conn, deps, "u-1", appt, "b") == (200, {"appointment": {"ok": True}})
    assert json.loads(seen["input"]) == {"user_id": "u-1", "appointment_id": appt, "target_slot_id": "b"}
