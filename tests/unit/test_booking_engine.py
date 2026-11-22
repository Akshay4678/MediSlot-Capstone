"""Milestone 5: booking validation, capacity, concurrency (Mandatory Test 1), idempotency (Mandatory Test 2)."""

import threading
from datetime import timedelta

import pytest
from booking_support import NOW, SCHEMA, add_slot, make_deps, run_threads, seed
from sqlite_mysql import Conn, FileDb

from application.booking import service
from application.common.api import ApiError

START = NOW + timedelta(days=3, hours=3)  # 2026-11-13 09:00 UTC


@pytest.fixture
def conn():
    c = Conn(SCHEMA)
    seed(c.db, patients=3)
    add_slot(c.db, "d1#A#1", START, capacity=1)
    return c


@pytest.fixture
def deps():
    return make_deps()


def book(conn, deps, user="u-1", slot="d1#A#1", key=None, **extra):
    return service.book(conn, deps, user, dict({"slot_id": slot}, **extra), key)


def sql(conn, statement):
    """Change test data and COMMIT it (the service ends its read transactions with a rollback)."""
    conn.db.execute(statement)
    conn.commit()


def code(call):
    with pytest.raises(ApiError) as exc:
        call()
    return exc.value.status, exc.value.code


def appointments(conn):
    return conn.q("SELECT patient_id, slot_id, status FROM appointments ORDER BY created_at, patient_id")


# ------------------------------------------------------------------------------ happy path
def test_booking_creates_appointment_updates_slot_publishes_event_and_metric(conn, deps):
    status, body = book(conn, deps)
    appt = body["appointment"]
    assert status == 201 and appt["status"] == "CONFIRMED" and appt["fee"] == 500.0
    assert appt["doctor"]["name"] == "Dr. Asha Rao" and appt["consultation_type"]["type_name"] == "Standard Consult"
    assert appt["start_utc"] == "2026-11-13T09:00:00Z" and appt["can_cancel"] is True
    assert appt["refund_preview"]["tier"] == "FULL"
    assert conn.q("SELECT patient_id, fee_amount, starts_at_utc FROM appointments") == [("p-1", 500.0, "2026-11-13 09:00:00")]
    assert conn.q("SELECT booked_count, status FROM materialised_slots") == [(1, "FULL")]  # capacity 1 -> FULL
    assert deps.slot_table.items["d1#A#1"]["booked_count"] == 1
    booked = deps.events.of("AppointmentBooked")
    assert len(booked) == 1 and booked[0]["appointment_id"] == appt["appointment_id"]
    assert deps.metrics.total("BookingsConfirmed") == 1


def test_the_fee_is_frozen_at_booking_time(conn, deps):
    _, body = book(conn, deps)
    sql(conn, "UPDATE doctor_consultation_types SET fee = 999 WHERE doctor_id = 'd1'")
    assert service.get_for_patient(conn, deps, "u-1", body["appointment"]["appointment_id"])[1]["appointment"]["fee"] == 500.0


# ------------------------------------------------------------------------------ validation
def test_input_validation(conn, deps):
    assert code(lambda: service.book(conn, deps, "u-1", {}, None)) == (400, "INVALID_SLOT_ID")
    assert code(lambda: service.book(conn, deps, "u-1", {"slot_id": 5}, None)) == (400, "INVALID_SLOT_ID")
    assert code(lambda: service.book(conn, deps, "u-1", {"slot_id": "x", "patient_id": "p-2"}, None)) == (400, "UNKNOWN_FIELD")
    assert code(lambda: book(conn, deps, key="bad key!")) == (400, "INVALID_IDEMPOTENCY_KEY")
    assert code(lambda: book(conn, deps, key="k" * 129)) == (400, "INVALID_IDEMPOTENCY_KEY")
    assert code(lambda: book(conn, deps, notes="n" * 501)) == (400, "INVALID_FIELD")


def test_slot_must_exist(conn, deps):
    assert code(lambda: book(conn, deps, slot="nope")) == (404, "SLOT_NOT_FOUND")


def test_patient_profile_is_required(conn, deps):
    sql(conn, "INSERT INTO users VALUES ('u-new', 'n@x', 1)")
    assert code(lambda: book(conn, deps, user="u-new")) == (409, "PROFILE_REQUIRED")
    sql(conn, "UPDATE patients SET is_active = 0 WHERE user_id = 'u-1'")  # closed (soft-deleted) account
    assert code(lambda: book(conn, deps, user="u-1")) == (409, "PROFILE_REQUIRED")


def test_a_slot_in_the_past_or_already_started_cannot_be_booked(conn, deps):
    add_slot(conn.db, "past", NOW - timedelta(hours=1))
    add_slot(conn.db, "starts-now", NOW)
    assert code(lambda: book(conn, deps, slot="past")) == (409, "SLOT_IN_PAST")
    assert code(lambda: book(conn, deps, slot="starts-now")) == (409, "SLOT_IN_PAST")
    add_slot(conn.db, "in-a-minute", NOW + timedelta(minutes=1))
    assert book(conn, deps, slot="in-a-minute")[0] == 201


def test_booking_window_is_enforced(conn, deps):
    add_slot(conn.db, "edge", NOW + timedelta(days=30))
    add_slot(conn.db, "beyond", NOW + timedelta(days=30, minutes=1))
    assert book(conn, deps, slot="edge")[0] == 201
    assert code(lambda: book(conn, deps, user="u-2", slot="beyond")) == (409, "OUTSIDE_BOOKING_WINDOW")


def test_blocked_and_closed_slots_cannot_be_booked(conn, deps):
    add_slot(conn.db, "blocked", START, status="BLOCKED")
    add_slot(conn.db, "closed", START, status="CLOSED")
    assert code(lambda: book(conn, deps, slot="blocked")) == (409, "SLOT_UNAVAILABLE")
    assert code(lambda: book(conn, deps, slot="closed")) == (409, "SLOT_UNAVAILABLE")
    assert deps.slot_table.items == {}  # nothing was reserved


def test_inactive_doctor_slots_cannot_be_booked(conn, deps):
    sql(conn, "UPDATE doctors SET is_active = 0 WHERE doctor_id = 'd1'")
    assert code(lambda: book(conn, deps)) == (404, "SLOT_NOT_FOUND")


# ------------------------------------------------------------------------------ patient limits
def test_cannot_book_the_same_slot_twice(conn, deps):
    add_slot(conn.db, "camp", START, capacity=5, ctype="hc")
    book(conn, deps, slot="camp")
    assert code(lambda: book(conn, deps, slot="camp")) == (409, "ALREADY_BOOKED")
    assert deps.slot_table.items["camp"]["booked_count"] == 1  # the rejected attempt took no seat


def test_overlapping_appointments_are_rejected_but_back_to_back_is_fine(conn, deps):
    add_slot(conn.db, "d1-a", START, minutes=30)
    add_slot(conn.db, "d2-overlap", START + timedelta(minutes=15), doctor="d2")
    add_slot(conn.db, "d2-next", START + timedelta(minutes=30), doctor="d2")
    book(conn, deps, slot="d1-a")
    assert code(lambda: book(conn, deps, slot="d2-overlap")) == (409, "TIME_CONFLICT")
    assert book(conn, deps, slot="d2-next")[0] == 201  # starts exactly when the first one ends


def test_at_most_three_upcoming_appointments_with_the_same_doctor(conn, deps):
    for i in range(1, 6):
        add_slot(conn.db, f"s{i}", START + timedelta(hours=i))
    for i in range(1, 4):
        book(conn, deps, slot=f"s{i}")
    assert code(lambda: book(conn, deps, slot="s4")) == (409, "BOOKING_LIMIT_REACHED")
    # a different doctor is unaffected
    add_slot(conn.db, "other", START + timedelta(hours=9), doctor="d2")
    assert book(conn, deps, slot="other")[0] == 201
    # cancelling one frees a place with the first doctor again
    appt_id = conn.q("SELECT appointment_id FROM appointments WHERE slot_id = 's1'")[0][0]
    service.cancel_by_patient(conn, deps, "u-1", appt_id)
    assert book(conn, deps, slot="s4")[0] == 201


# ------------------------------------------------------------------------------ capacity
def test_a_full_slot_returns_409(conn, deps):
    book(conn, deps, user="u-1")
    assert code(lambda: book(conn, deps, user="u-2")) == (409, "SLOT_FULL")
    assert conn.q("SELECT COUNT(*) FROM appointments") == [(1,)]
    assert deps.metrics.total("BookingsRejected", Reason="SLOT_FULL") == 1


def test_health_camp_capacity_is_enforced_exactly(deps):
    c = Conn(SCHEMA)
    seed(c.db, patients=8)
    add_slot(c.db, "camp", START, capacity=5, ctype="hc", minutes=60)
    for i in range(1, 6):
        assert book(c, deps, user=f"u-{i}", slot="camp")[0] == 201
        seats = c.q("SELECT booked_count, status FROM materialised_slots")[0]
        assert seats == (i, "FULL" if i == 5 else "OPEN")
    assert code(lambda: book(c, deps, user="u-6", slot="camp")) == (409, "SLOT_FULL")
    assert deps.slot_table.items["camp"]["booked_count"] == 5


def test_capacity_always_comes_from_rds_so_a_changed_capacity_is_respected(conn, deps):
    add_slot(conn.db, "grow", START + timedelta(hours=2), capacity=1)
    book(conn, deps, slot="grow")
    sql(conn, "UPDATE materialised_slots SET capacity = 2, status = 'OPEN' WHERE slot_id = 'grow'")
    assert book(conn, deps, user="u-2", slot="grow")[0] == 201
    assert deps.slot_table.items["grow"]["booked_count"] == 2


# ------------------------------------------------------------------------------ failure keeps no seat
def test_a_booking_that_fails_after_reserving_gives_the_seat_back(conn, deps, monkeypatch):
    real = conn.cursor

    class Boom:
        def __init__(self):
            self.inner = real()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO appointments"):
                raise RuntimeError("database went away")
            return self.inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def cursor():
        return Boom()

    monkeypatch.setattr(conn, "cursor", cursor)
    with pytest.raises(RuntimeError):
        book(conn, deps)
    monkeypatch.setattr(conn, "cursor", real)
    assert deps.slot_table.items["d1#A#1"]["booked_count"] == 0  # seat released
    assert conn.q("SELECT COUNT(*) FROM appointments") == [(0,)]
    assert book(conn, deps)[0] == 201  # and the slot can still be booked


# ------------------------------------------------------------------------------ OCC unit tests
def test_version_increases_with_every_change():
    deps = make_deps()
    store = deps.slots
    store.reserve("s", 3)
    store.reserve("s", 3)
    store.release("s")
    assert deps.slot_table.items["s"] == {"slot_id": "s", "booked_count": 1, "version": 3}


def test_the_conditional_write_rejects_a_stale_version():
    deps = make_deps()
    store = deps.slots
    store.reserve("s", 3)
    booked, version, _ = store.read("s")
    store.reserve("s", 3)  # somebody else wins the race
    from fake_dynamo import FakeClientError
    with pytest.raises(FakeClientError) as exc:
        store.table.update_item(
            Key={"slot_id": "s"}, UpdateExpression="SET #bc = #bc + :one, #ver = #ver + :one",
            ConditionExpression="#ver = :v AND #bc < :cap",
            ExpressionAttributeNames={"#bc": "booked_count", "#ver": "version"},
            ExpressionAttributeValues={":one": 1, ":v": version, ":cap": 3})
    assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


def test_retries_are_bounded_and_counted():
    from application.booking.errors import ContentionExceeded
    deps = make_deps(max_attempts=4)
    store = deps.slots
    state = {"n": 0}
    real_update = store.table.update_item

    def always_lose(**kw):  # another booking wins every single time
        state["n"] += 1
        store.table.items["s"]["version"] += 1
        return real_update(**kw)

    store.reserve("s", 99)
    store.table.update_item = always_lose
    with pytest.raises(ContentionExceeded) as exc:
        store.reserve("s", 99)
    assert exc.value.attempts == 4 and exc.value.conflicts == 4 and state["n"] == 4


def test_backoff_grows_exponentially_with_jitter_and_is_capped():
    from application.booking.slot_store import SlotStore
    store = SlotStore(None, backoff_ms=25, rng=lambda: 1.0)
    assert [round(store._delay(n), 3) for n in (1, 2, 3, 4, 5, 6, 7)] == [0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0]
    assert SlotStore(None, backoff_ms=25, rng=lambda: 0.0)._delay(5) == 0.0


def test_contention_beyond_the_retry_budget_is_a_409_conflict_not_a_500(conn, deps):
    from application.booking.errors import ContentionExceeded

    def boom(slot_id, capacity):
        err = ContentionExceeded(6)
        err.conflicts = 6
        raise err

    deps.slots.reserve = boom
    assert code(lambda: book(conn, deps)) == (409, "BOOKING_CONFLICT")
    assert deps.metrics.total("BookingConflictRetries") == 6


# ============================================================================== MANDATORY TEST 1
def test_mandatory_1_concurrency_24_simultaneous_bookings_on_a_capacity_3_slot_exactly_3_succeed(tmp_path):
    """24 patients book the same capacity-3 slot at the same instant (real threads, real conditional writes)."""
    players = 24
    db = FileDb(tmp_path / "rds.db", SCHEMA)
    boot = db.connect()
    seed(boot.db, patients=players)
    boot.db.execute("PRAGMA journal_mode=WAL")
    add_slot(boot.db, "hot", START, capacity=3, ctype="hc", minutes=60)
    boot.close()

    gate = threading.Barrier(players)
    reads = []

    def line_everyone_up():
        # Every booking reads the counter before ANY of them writes: contention is real, not avoided.
        reads.append(1)
        if len(reads) <= players:
            gate.wait(timeout=30)

    deps = make_deps(on_get=line_everyone_up, max_attempts=6)

    def attempt(i):
        conn = db.connect()
        try:
            return service.book(conn, deps, f"u-{i + 1}", {"slot_id": "hot"}, f"key-{i}")
        finally:
            conn.close()

    results = run_threads(players, attempt)

    ok = [r for r in results if isinstance(r, tuple) and r[0] == 201]
    refused = [r for r in results if isinstance(r, ApiError)]
    assert len(ok) == 3, results
    assert len(refused) == players - 3 and {e.status for e in refused} == {409}
    assert {e.code for e in refused} == {"SLOT_FULL"}  # nobody got a 500 or an unexplained failure

    check = db.connect()
    assert check.q("SELECT COUNT(*) FROM appointments WHERE slot_id = 'hot' AND status = 'CONFIRMED'") == [(3,)]
    assert check.q("SELECT booked_count, status FROM materialised_slots WHERE slot_id = 'hot'") == [(3, "FULL")]
    assert len({r[1]["appointment"]["appointment_id"] for r in ok}) == 3
    check.close()

    item = deps.slot_table.items["hot"]
    assert item["booked_count"] == 3  # the DynamoDB counter agrees with RDS: never oversold
    assert item["version"] == 3       # one version bump per successful seat, nothing lost or doubled
    # Contention genuinely happened and was handled (this is what BookingConflictRetries shows in CloudWatch).
    assert deps.metrics.total("BookingConflictRetries") >= players - 1
    assert len(deps.events.of("AppointmentBooked")) == 3


def test_concurrency_never_oversells_even_when_retries_run_out(tmp_path):
    """40 bookings on a capacity-10 slot with a SMALL retry budget: some give up (409 BOOKING_CONFLICT), none oversell."""
    players, capacity = 40, 10
    db = FileDb(tmp_path / "rds.db", SCHEMA)
    boot = db.connect()
    seed(boot.db, patients=players)
    boot.db.execute("PRAGMA journal_mode=WAL")
    add_slot(boot.db, "camp", START, capacity=capacity, ctype="hc", minutes=60)
    boot.close()
    deps = make_deps(max_attempts=3)

    def attempt(i):
        conn = db.connect()
        try:
            return service.book(conn, deps, f"u-{i + 1}", {"slot_id": "camp"}, None)
        finally:
            conn.close()

    results = run_threads(players, attempt)
    ok = [r for r in results if isinstance(r, tuple)]
    errors = [r for r in results if isinstance(r, ApiError)]
    assert len(ok) + len(errors) == players, results
    assert {e.code for e in errors} <= {"SLOT_FULL", "BOOKING_CONFLICT"}
    assert len(ok) <= capacity
    check = db.connect()
    confirmed = check.q("SELECT COUNT(*) FROM appointments WHERE slot_id = 'camp' AND status = 'CONFIRMED'")[0][0]
    booked = check.q("SELECT booked_count FROM materialised_slots WHERE slot_id = 'camp'")[0][0]
    check.close()
    assert confirmed == len(ok) == booked == deps.slot_table.items["camp"]["booked_count"]


def test_the_same_patient_cannot_double_book_by_racing_themselves(tmp_path):
    db = FileDb(tmp_path / "rds.db", SCHEMA)
    boot = db.connect()
    seed(boot.db, patients=1)
    add_slot(boot.db, "camp", START, capacity=5, ctype="hc", minutes=60)
    boot.close()
    deps = make_deps()

    def attempt(i):
        conn = db.connect()
        try:
            return service.book(conn, deps, "u-1", {"slot_id": "camp"}, None)
        finally:
            conn.close()

    results = run_threads(6, attempt)
    confirmed = db.connect().q("SELECT COUNT(*) FROM appointments WHERE patient_id = 'p-1' AND status = 'CONFIRMED'")[0][0]
    assert confirmed == 1  # the unique (patient, slot, active) rule is the backstop in the database
    assert len([r for r in results if isinstance(r, tuple)]) == 1
    # losers are stopped either by the checks (they saw the winner's row) or by the unique rule at INSERT time
    assert {r.code for r in results if isinstance(r, ApiError)} <= {"ALREADY_BOOKED", "TIME_CONFLICT", "SLOT_FULL"}
    assert deps.slot_table.items["camp"]["booked_count"] == 1  # every losing attempt gave its seat back


# ============================================================================== MANDATORY TEST 2
def test_mandatory_2_idempotency_the_same_key_replayed_creates_exactly_one_appointment(conn, deps):
    add_slot(conn.db, "camp", START + timedelta(hours=5), capacity=5, ctype="hc", minutes=60)
    first_status, first = book(conn, deps, slot="camp", key="order-123")
    replays = [book(conn, deps, slot="camp", key="order-123") for _ in range(4)]
    assert first_status == 201 and "idempotent_replay" not in first
    for status, body in replays:
        assert status == 201 and body["idempotent_replay"] is True
        assert body["appointment"]["appointment_id"] == first["appointment"]["appointment_id"]  # the ORIGINAL appointment
    assert conn.q("SELECT COUNT(*) FROM appointments") == [(1,)]
    assert conn.q("SELECT booked_count FROM materialised_slots WHERE slot_id = 'camp'") == [(1,)]
    assert deps.slot_table.items["camp"]["booked_count"] == 1  # only one seat was ever taken
    assert len(deps.events.of("AppointmentBooked")) == 1       # and only one event was published


def test_mandatory_2_idempotency_holds_under_concurrent_replays(tmp_path):
    db = FileDb(tmp_path / "rds.db", SCHEMA)
    boot = db.connect()
    seed(boot.db, patients=1)
    add_slot(boot.db, "camp", START, capacity=5, ctype="hc", minutes=60)
    boot.close()
    deps = make_deps()

    def attempt(i):
        conn = db.connect()
        try:
            return service.book(conn, deps, "u-1", {"slot_id": "camp"}, "same-key")
        finally:
            conn.close()

    results = run_threads(8, attempt)
    check = db.connect()
    assert check.q("SELECT COUNT(*) FROM appointments") == [(1,)]
    ids = {r[1]["appointment"]["appointment_id"] for r in results if isinstance(r, tuple)}
    assert len(ids) == 1                                   # every success returned the same appointment
    for r in results:                                      # the rest were told the first is still running
        assert isinstance(r, tuple) or (isinstance(r, ApiError) and r.code == "REQUEST_IN_PROGRESS")
    assert deps.slot_table.items["camp"]["booked_count"] == 1


def test_the_same_key_with_a_different_request_is_rejected(conn, deps):
    add_slot(conn.db, "other", START + timedelta(hours=4))
    book(conn, deps, key="k1")
    assert code(lambda: book(conn, deps, slot="other", key="k1")) == (422, "IDEMPOTENCY_KEY_REUSED")
    assert conn.q("SELECT COUNT(*) FROM appointments") == [(1,)]


def test_keys_are_private_to_each_user(conn, deps):
    add_slot(conn.db, "camp", START + timedelta(hours=5), capacity=5)
    book(conn, deps, user="u-1", slot="camp", key="shared")
    status, body = book(conn, deps, user="u-2", slot="camp", key="shared")
    assert status == 201 and "idempotent_replay" not in body  # a different patient's own booking
    assert conn.q("SELECT COUNT(*) FROM appointments") == [(2,)]


def test_a_failed_request_releases_its_key_so_the_client_can_retry(conn, deps):
    book(conn, deps, user="u-1")
    assert code(lambda: book(conn, deps, user="u-2", key="retry-me")) == (409, "SLOT_FULL")
    assert deps.idem_table.items == {}  # the failed attempt left no record behind
    service.cancel_by_patient(conn, deps, "u-1", conn.q("SELECT appointment_id FROM appointments")[0][0])
    assert book(conn, deps, user="u-2", key="retry-me")[0] == 201


def test_a_stuck_in_progress_key_can_be_taken_over_after_the_lock_expires(conn, deps):
    from application.booking.idempotency import request_hash
    pk = deps.idempotency.storage_key("u-1", "stuck")
    now = deps.clock.epoch()
    deps.idem_table.items[pk] = {"idempotency_key": pk, "request_hash": request_hash({"slot_id": "d1#A#1", "notes": None}),
                                 "state": "IN_PROGRESS", "lock_until": now + 30, "expires_at": now + 86400}
    assert code(lambda: book(conn, deps, key="stuck")) == (409, "REQUEST_IN_PROGRESS")
    deps.clock.now = NOW + timedelta(seconds=31)  # the Lambda that held it died long ago
    assert book(conn, deps, key="stuck")[0] == 201


def test_the_database_still_returns_the_original_if_the_key_record_was_lost(conn, deps):
    _, first = book(conn, deps, key="lost")
    deps.idem_table.items.clear()  # TTL or a table problem erased the record
    status, again = book(conn, deps, key="lost")
    assert status == 201 and again["idempotent_replay"] is True
    assert again["appointment"]["appointment_id"] == first["appointment"]["appointment_id"]
    assert conn.q("SELECT COUNT(*) FROM appointments") == [(1,)]


def test_after_the_store_record_expires_the_database_still_binds_the_key_to_its_appointment(conn, deps):
    add_slot(conn.db, "camp", START + timedelta(hours=5), capacity=5)
    add_slot(conn.db, "other", START + timedelta(hours=8), capacity=5)
    first = book(conn, deps, slot="camp", key="old")[1]
    deps.clock.now = NOW + timedelta(hours=25)  # past the 24 h TTL, though DynamoDB has not deleted the record yet
    status, again = book(conn, deps, slot="camp", key="old")  # the same request still returns the original
    assert status == 201 and again["idempotent_replay"] is True
    assert again["appointment"]["appointment_id"] == first["appointment"]["appointment_id"]
    # a DIFFERENT request must not silently get the old appointment back, and must not book anything
    assert code(lambda: book(conn, deps, slot="other", key="old")) == (422, "IDEMPOTENCY_KEY_REUSED")
    assert conn.q("SELECT COUNT(*) FROM appointments") == [(1,)]
    assert "other" not in deps.slot_table.items or deps.slot_table.items["other"]["booked_count"] == 0
