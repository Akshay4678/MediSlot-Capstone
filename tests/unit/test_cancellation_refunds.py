"""Milestone 5: refund tiers (every boundary, to the microsecond), cancellation, refund ledger, doctor slot cancellation."""

from datetime import timedelta, timezone
from decimal import Decimal

import pytest
from booking_support import NOW, SCHEMA, add_slot, make_deps, run_threads, seed
from sqlite_mysql import Conn, FileDb

from application.booking import refunds, service
from application.common import rules
from application.common.api import ApiError

BANDS = (48, 24, 6)
START = NOW + timedelta(days=3, hours=3)  # the appointment starts 2026-11-13 09:00 UTC
H = timedelta(hours=1)
US = timedelta(microseconds=1)


def sql(conn, statement):
    conn.db.execute(statement)
    conn.commit()


def code(call):
    with pytest.raises(ApiError) as exc:
        call()
    return exc.value.status, exc.value.code


# ============================================================================ the pure tier calculation
@pytest.mark.parametrize("left, tier, pct, amount", [
    (timedelta(days=30), "FULL", 100, "500.00"),
    (48 * H + US, "FULL", 100, "500.00"),
    (48 * H, "FULL", 100, "500.00"),          # EXACTLY 48 h: the better tier
    (48 * H - US, "TIER_75", 75, "375.00"),   # one microsecond short of 48 h
    (36 * H, "TIER_75", 75, "375.00"),
    (24 * H + US, "TIER_75", 75, "375.00"),
    (24 * H, "TIER_75", 75, "375.00"),        # EXACTLY 24 h
    (24 * H - US, "TIER_50", 50, "250.00"),
    (12 * H, "TIER_50", 50, "250.00"),
    (6 * H + US, "TIER_50", 50, "250.00"),
    (6 * H, "TIER_50", 50, "250.00"),         # EXACTLY 6 h
    (6 * H - US, "NONE", 0, "0.00"),
    (timedelta(minutes=1), "NONE", 0, "0.00"),
    (US, "NONE", 0, "0.00"),
    (timedelta(0), "NONE", 0, "0.00"),
    (-H, "NONE", 0, "0.00"),
])
def test_every_tier_boundary_to_the_microsecond(left, tier, pct, amount):
    result = refunds.calculate_refund(500, NOW + left, NOW, BANDS)
    assert (result["tier"], result["percentage"], result["amount"]) == (tier, pct, Decimal(amount))


def test_the_cutoff_instants_themselves():
    start = NOW + timedelta(days=5)
    for hours, better, worse in ((48, "FULL", "TIER_75"), (24, "TIER_75", "TIER_50"), (6, "TIER_50", "NONE")):
        cutoff = start - hours * H
        assert refunds.calculate_refund(500, start, cutoff, BANDS)["tier"] == better       # at the instant
        assert refunds.calculate_refund(500, start, cutoff - US, BANDS)["tier"] == better  # just before: even earlier
        assert refunds.calculate_refund(500, start, cutoff + US, BANDS)["tier"] == worse   # one microsecond after


def test_tier_windows_are_read_from_ssm_not_hard_coded(monkeypatch):
    values = {"rules/refund-full-hours": 72, "rules/refund-75-hours": 36, "rules/refund-50-hours": 12}
    monkeypatch.setattr(rules.config, "get_int", lambda name, default: values.get(name, default))
    assert rules.refund_bands() == (72, 36, 12)
    start = NOW + timedelta(days=10)
    assert refunds.calculate_refund(100, start, start - 72 * H, rules.refund_bands())["tier"] == "FULL"
    assert refunds.calculate_refund(100, start, start - 72 * H + US, rules.refund_bands())["tier"] == "TIER_75"
    assert refunds.calculate_refund(100, start, start - 12 * H, rules.refund_bands())["tier"] == "TIER_50"
    assert refunds.calculate_refund(100, start, start - 12 * H + US, rules.refund_bands())["tier"] == "NONE"


def test_the_default_windows_are_48_24_6(monkeypatch):
    monkeypatch.setattr(rules.config, "get_int", lambda name, default: default)
    assert rules.refund_bands() == (48, 24, 6)


@pytest.mark.parametrize("fee, tier, amount", [
    ("399.99", "TIER_75", "299.99"),   # 299.9925 -> 299.99
    ("0.01", "TIER_50", "0.01"),       # 0.005 -> rounds half UP, never down to zero
    ("0.03", "TIER_75", "0.02"),       # 0.0225 -> 0.02
    ("1234.50", "TIER_50", "617.25"),
    ("100", "FULL", "100.00"),
    ("0", "FULL", "0.00"),
    ("250.55", "NONE", "0.00"),
])
def test_refund_amounts_are_exact_money(fee, tier, amount):
    assert refunds.refund_for(Decimal(fee), tier)[0] == Decimal(amount)


def test_a_doctor_cancellation_is_always_a_full_refund():
    result = refunds.doctor_cancellation_refund(Decimal("350.00"))
    assert (result["tier"], result["percentage"], result["amount"]) == ("DOCTOR_CANCELLED", 100, Decimal("350.00"))


def test_hours_before_start_is_reported():
    assert refunds.calculate_refund(500, NOW + 36 * H, NOW, BANDS)["hours_before_start"] == 36.0


# ============================================================================ cancellation through the service
@pytest.fixture
def conn():
    c = Conn(SCHEMA)
    seed(c.db, patients=4)
    add_slot(c.db, "slot", START, capacity=1)
    return c


@pytest.fixture
def deps():
    return make_deps()


def booked(conn, deps, user="u-1", slot="slot"):
    return service.book(conn, deps, user, {"slot_id": slot}, None)[1]["appointment"]["appointment_id"]


@pytest.mark.parametrize("left, tier, pct, amount", [
    (48 * H, "FULL", 100, 500.0),
    (48 * H - US, "TIER_75", 75, 375.0),
    (24 * H, "TIER_75", 75, 375.0),
    (24 * H - US, "TIER_50", 50, 250.0),
    (6 * H, "TIER_50", 50, 250.0),
    (6 * H - US, "NONE", 0, 0.0),
    (timedelta(seconds=1), "NONE", 0, 0.0),
])
def test_patient_cancellation_at_each_boundary_writes_the_right_ledger_row(conn, deps, left, tier, pct, amount):
    appt_id = booked(conn, deps)
    deps.clock.now = START - left  # the cancellation instant
    status, body = service.cancel_by_patient(conn, deps, "u-1", appt_id, {"reason": "plans changed"})
    appt = body["appointment"]
    assert status == 200 and appt["status"] == "CANCELLED_BY_PATIENT" and appt["can_cancel"] is False
    assert appt["refund"] == {"tier": tier, "percentage": float(pct), "amount": amount,
                              "status": "CALCULATED" if amount else "NOT_APPLICABLE"}
    assert conn.q("SELECT refund_amount, refund_percentage, fee_amount, refund_tier FROM refund_ledger") == [
        (amount, float(pct), 500.0, tier)]
    assert conn.q("SELECT status, cancellation_reason FROM appointments") == [("CANCELLED_BY_PATIENT", "plans changed")]
    assert deps.metrics.total("CancellationsByTier", Tier=tier) == 1  # the CancellationsByTier metric


def test_cancelling_frees_the_seat_everywhere_and_publishes_both_events(conn, deps):
    appt_id = booked(conn, deps)
    assert conn.q("SELECT booked_count, status FROM materialised_slots") == [(1, "FULL")]
    service.cancel_by_patient(conn, deps, "u-1", appt_id)
    assert conn.q("SELECT booked_count, status FROM materialised_slots") == [(0, "OPEN")]   # RDS view reopened
    assert deps.slot_table.items["slot"]["booked_count"] == 0                                 # DynamoDB counter
    cancelled, freed = deps.events.of("AppointmentCancelled"), deps.events.of("SlotSeatFreed")
    assert len(cancelled) == 1 and cancelled[0]["appointment_id"] == appt_id and cancelled[0]["cancelled_by"] == "patient"
    assert cancelled[0]["refund"] == {"tier": "FULL", "percentage": 100, "amount": 500.0}
    assert freed == [{"slot_id": "slot", "doctor_id": "d1", "seats_freed": 1, "starts_at_utc": "2026-11-13T09:00:00Z"}]
    assert booked(conn, deps, user="u-2") and conn.q("SELECT booked_count FROM materialised_slots") == [(1,)]


def test_a_cancelled_appointment_cannot_be_cancelled_again(conn, deps):
    appt_id = booked(conn, deps)
    service.cancel_by_patient(conn, deps, "u-1", appt_id)
    assert code(lambda: service.cancel_by_patient(conn, deps, "u-1", appt_id)) == (409, "NOT_CANCELLABLE")
    assert conn.q("SELECT COUNT(*) FROM refund_ledger") == [(1,)]  # still exactly one refund
    assert deps.slot_table.items["slot"]["booked_count"] == 0       # and the seat was not freed twice


def test_an_appointment_that_has_started_cannot_be_cancelled(conn, deps):
    appt_id = booked(conn, deps)
    deps.clock.now = START
    assert code(lambda: service.cancel_by_patient(conn, deps, "u-1", appt_id)) == (409, "APPOINTMENT_STARTED")
    deps.clock.now = START + US
    assert code(lambda: service.cancel_by_patient(conn, deps, "u-1", appt_id)) == (409, "APPOINTMENT_STARTED")
    assert conn.q("SELECT COUNT(*) FROM refund_ledger") == [(0,)]


def test_one_patient_cannot_cancel_or_see_another_patients_appointment(conn, deps):
    appt_id = booked(conn, deps, user="u-1")
    assert code(lambda: service.cancel_by_patient(conn, deps, "u-2", appt_id)) == (404, "APPOINTMENT_NOT_FOUND")
    assert code(lambda: service.get_for_patient(conn, deps, "u-2", appt_id)) == (404, "APPOINTMENT_NOT_FOUND")
    assert conn.q("SELECT status FROM appointments") == [("CONFIRMED",)]
    assert code(lambda: service.cancel_by_patient(conn, deps, "u-1", "no-such-id")) == (404, "APPOINTMENT_NOT_FOUND")


def test_cancel_rejects_unknown_fields(conn, deps):
    appt_id = booked(conn, deps)
    assert code(lambda: service.cancel_by_patient(conn, deps, "u-1", appt_id, {"refund": 100})) == (400, "UNKNOWN_FIELD")


def test_two_simultaneous_cancellations_refund_once(tmp_path):
    db = FileDb(tmp_path / "rds.db", SCHEMA)
    boot = db.connect()
    seed(boot.db, patients=1)
    add_slot(boot.db, "slot", START, capacity=1)
    boot.close()
    deps = make_deps()
    c = db.connect()
    appt_id = service.book(c, deps, "u-1", {"slot_id": "slot"}, None)[1]["appointment"]["appointment_id"]
    c.close()

    def attempt(i):
        conn = db.connect()
        try:
            return service.cancel_by_patient(conn, deps, "u-1", appt_id)
        finally:
            conn.close()

    results = run_threads(2, attempt)
    check = db.connect()
    assert check.q("SELECT COUNT(*) FROM refund_ledger") == [(1,)]
    assert deps.slot_table.items["slot"]["booked_count"] == 0
    assert len([r for r in results if isinstance(r, tuple)]) == 1
    assert {r.code for r in results if isinstance(r, ApiError)} <= {"NOT_CANCELLABLE"}


def test_a_failed_seat_release_after_commit_does_not_undo_the_cancellation(conn, deps):
    appt_id = booked(conn, deps)

    def broken(slot_id, count=1):
        raise RuntimeError("dynamodb unavailable")

    deps.slots.release = broken
    status, _ = service.cancel_by_patient(conn, deps, "u-1", appt_id)
    assert status == 200 and conn.q("SELECT status FROM appointments") == [("CANCELLED_BY_PATIENT",)]


# ============================================================================ doctor cancels a slot
@pytest.fixture
def camp(conn, deps):
    add_slot(conn.db, "camp", START, capacity=5, ctype="hc", minutes=60)
    ids = [booked(conn, deps, user=f"u-{i}", slot="camp") for i in (1, 2, 3)]
    return ids


def test_doctor_slot_cancellation_refunds_every_patient_in_full_whatever_the_time_left(conn, deps, camp):
    deps.clock.now = START - timedelta(minutes=30)  # a patient cancelling now would get nothing
    status, body = service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "camp", "reason": "Doctor unwell"})
    assert status == 200 and body == {"slot_id": "camp", "status": "CLOSED", "cancelled_appointments": 3,
                                      "refunds_total": 600.0}
    assert conn.q("SELECT status, cancellation_reason FROM appointments") == [("CANCELLED_BY_DOCTOR", "Doctor unwell")] * 3
    assert conn.q("SELECT refund_amount, refund_percentage, refund_tier, refund_status FROM refund_ledger") == [
        (200.0, 100.0, "DOCTOR_CANCELLED", "CALCULATED")] * 3
    assert conn.q("SELECT status, booked_count FROM materialised_slots WHERE slot_id = 'camp'") == [("CLOSED", 0)]
    assert deps.slot_table.items["camp"]["booked_count"] == 0
    events = deps.events.of("AppointmentCancelled")
    assert len(events) == 3 and {e["cancelled_by"] for e in events} == {"doctor"}
    assert {e["appointment_id"] for e in events} == set(camp)
    assert deps.metrics.total("CancellationsByTier", Tier="DOCTOR_CANCELLED") == 3


def test_each_affected_patient_sees_the_refund_in_their_ledger(conn, deps, camp):
    service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "camp"})
    status, body = service.list_refunds(conn, deps, "u-2", {})
    assert status == 200 and body["total"] == 1 and body["total_refunded"] == 200.0
    item = body["items"][0]
    assert (item["tier"], item["percentage"], item["amount"], item["appointment_status"]) == (
        "DOCTOR_CANCELLED", 100.0, 200.0, "CANCELLED_BY_DOCTOR")
    assert service.get_for_patient(conn, deps, "u-2", camp[1])[1]["appointment"]["cancellation_reason"] == "Cancelled by the doctor"


def test_a_cancelled_slot_cannot_be_booked_and_is_not_offered(conn, deps, camp):
    service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "camp"})
    assert code(lambda: service.book(conn, deps, "u-4", {"slot_id": "camp"}, None)) == (409, "SLOT_UNAVAILABLE")
    from application.patient_api import discovery
    offered = discovery.slots(conn, "d1", {"date": "2026-11-13"}, now_utc=NOW)[1]["slots"]
    assert "camp" not in [s["slot_id"] for s in offered]


def test_a_slot_cancelled_by_the_doctor_is_not_recreated_by_a_rebuild():
    """rebuild_future_slots deletes unbooked future slots; a CLOSED (cancelled) slot must survive it."""
    c = Conn(SCHEMA)
    seed(c.db, patients=1)
    add_slot(c.db, "open-empty", START + timedelta(days=1))
    add_slot(c.db, "cancelled", START, status="CLOSED")
    from application.slot_materialiser import core
    core.rebuild_future_slots(c, "d1", "Asia/Kolkata", NOW.replace(tzinfo=timezone.utc))
    left = {r[0] for r in c.q("SELECT slot_id FROM materialised_slots")}
    assert "cancelled" in left and "open-empty" not in left


def test_a_doctor_cannot_cancel_another_doctors_slot(conn, deps):
    add_slot(conn.db, "d2-slot", START + timedelta(hours=2), doctor="d2")
    assert code(lambda: service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "d2-slot"})) == (404, "SLOT_NOT_FOUND")
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = 'd2-slot'") == [("OPEN",)]


def test_doctor_slot_cancellation_validation(conn, deps, camp):
    assert code(lambda: service.cancel_slot_by_doctor(conn, deps, "u-d1", {})) == (400, "INVALID_SLOT_ID")
    assert code(lambda: service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "camp", "x": 1})) == (400, "UNKNOWN_FIELD")
    assert code(lambda: service.cancel_slot_by_doctor(conn, deps, "u-1", {"slot_id": "camp"})) == (409, "PROFILE_REQUIRED")
    service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "camp"})
    assert code(lambda: service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "camp"})) == (409, "SLOT_ALREADY_CANCELLED")
    deps.clock.now = START + timedelta(minutes=1)
    add_slot(conn.db, "gone", START)
    assert code(lambda: service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "gone"})) == (409, "SLOT_IN_PAST")


def test_cancelling_an_empty_slot_just_closes_it(conn, deps):
    status, body = service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "slot"})
    assert status == 200 and body["cancelled_appointments"] == 0 and body["refunds_total"] == 0.0
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = 'slot'") == [("CLOSED",)]
    assert deps.events.of("AppointmentCancelled") == []


# ============================================================================ listing, detail, ledger
def test_patient_listing_is_scoped_filtered_and_paged(conn, deps):
    for i in range(1, 6):
        add_slot(conn.db, f"s{i}", START + timedelta(days=i - 3 if i > 3 else 0, hours=i), doctor="d2")
    ids = [booked(conn, deps, user="u-1", slot=f"s{i}") for i in range(1, 4)]
    booked(conn, deps, user="u-2", slot="s4")
    service.cancel_by_patient(conn, deps, "u-1", ids[0])
    _, everything = service.list_for_patient(conn, deps, "u-1", {})
    assert everything["total"] == 3 and {i["appointment_id"] for i in everything["items"]} == set(ids)  # never u-2's
    assert service.list_for_patient(conn, deps, "u-1", {"scope": "upcoming"})[1]["total"] == 2
    assert service.list_for_patient(conn, deps, "u-1", {"scope": "past"})[1]["total"] == 1
    assert service.list_for_patient(conn, deps, "u-1", {"status": "cancelled_by_patient"})[1]["total"] == 1
    page = service.list_for_patient(conn, deps, "u-1", {"limit": "2", "offset": "0"})[1]
    assert page["count"] == 2 and page["has_more"] is True
    assert service.list_for_patient(conn, deps, "u-1", {"limit": "2", "offset": "2"})[1]["has_more"] is False
    assert code(lambda: service.list_for_patient(conn, deps, "u-1", {"limit": "x"})) == (400, "INVALID_PAGINATION")
    assert code(lambda: service.list_for_patient(conn, deps, "u-1", {"scope": "later"})) == (400, "INVALID_SCOPE")


def test_listing_marks_what_can_still_be_cancelled_with_a_refund_preview(conn, deps):
    booked(conn, deps)
    deps.clock.now = START - 30 * H
    item = service.list_for_patient(conn, deps, "u-1", {})[1]["items"][0]
    assert item["can_cancel"] is True and item["refund_preview"] == {"tier": "TIER_75", "percentage": 75, "amount": 375.0}


def test_doctor_sees_only_their_own_appointments_with_patient_details(conn, deps):
    add_slot(conn.db, "d2s", START + timedelta(hours=2), doctor="d2")
    mine = booked(conn, deps, user="u-1", slot="slot")
    theirs = booked(conn, deps, user="u-2", slot="d2s")
    _, body = service.list_for_doctor(conn, deps, "u-d1", {})
    assert [i["appointment_id"] for i in body["items"]] == [mine]
    assert body["items"][0]["patient"] == {"patient_id": "p-1", "name": "Pat1 Test", "phone": "+91 90000 00000"}
    assert service.get_for_doctor(conn, deps, "u-d1", mine)[1]["appointment"]["patient"]["name"] == "Pat1 Test"
    assert code(lambda: service.get_for_doctor(conn, deps, "u-d1", theirs)) == (404, "APPOINTMENT_NOT_FOUND")
    assert service.list_for_doctor(conn, deps, "u-d1", {"from": "2026-11-14", "to": "2026-11-15"})[1]["total"] == 0
    assert service.list_for_doctor(conn, deps, "u-d1", {"from": "2026-11-13", "to": "2026-11-13"})[1]["total"] == 1
    assert code(lambda: service.list_for_doctor(conn, deps, "u-1", {})) == (409, "PROFILE_REQUIRED")


def test_refund_ledger_lists_only_the_callers_refunds_with_a_total(conn, deps):
    add_slot(conn.db, "slot2", START + timedelta(hours=3))
    a = booked(conn, deps, user="u-1", slot="slot")
    b = booked(conn, deps, user="u-1", slot="slot2")
    service.cancel_by_patient(conn, deps, "u-1", a)                       # >48h  -> 100 % of 500
    deps.clock.now = START + timedelta(hours=3) - 30 * H
    service.cancel_by_patient(conn, deps, "u-1", b)                       # 30h   -> 75 % of 500
    _, body = service.list_refunds(conn, deps, "u-1", {})
    assert body["total"] == 2 and body["total_refunded"] == 875.0
    assert sorted((i["tier"], i["amount"]) for i in body["items"]) == [("FULL", 500.0), ("TIER_75", 375.0)]
    assert service.list_refunds(conn, deps, "u-2", {})[1] == {
        "items": [], "count": 0, "total": 0, "limit": 20, "offset": 0, "has_more": False, "total_refunded": 0.0}
