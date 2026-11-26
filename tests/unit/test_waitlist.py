"""Milestone 6: waitlist, hold offers, exclusivity (Mandatory Test 4), acceptance and the expiry sweeper."""

from datetime import timedelta

import pytest
from booking_support import NOW, SCHEMA, add_slot, make_deps, run_threads, seed
from sqlite_mysql import Conn, FileDb

from application.booking import service
from application.common import rules
from application.common.api import ApiError
from application.waitlist import service as waitlist
from application.waitlist import store as wl

START = NOW + timedelta(days=3, hours=3)


def code(call):
    with pytest.raises(ApiError) as exc:
        call()
    return exc.value.status, exc.value.code


@pytest.fixture
def world():
    conn = Conn(SCHEMA)
    seed(conn.db, patients=5)
    add_slot(conn.db, "slot", START, capacity=1)
    return conn, make_deps()


def full_slot(conn, deps, holder="u-1", slot="slot"):
    """u-1 holds the only seat; returns that appointment id."""
    return service.book(conn, deps, holder, {"slot_id": slot}, None)[1]["appointment"]["appointment_id"]


def queue_up(conn, deps, users, slot="slot"):
    for u in users:
        deps.clock.now += timedelta(seconds=1)  # distinct join times: the queue is ordered by them
        assert waitlist.join(conn, deps, u, slot)[0] == 201


def live(deps, slot="slot"):
    return deps.waitlist.live_offers_for_slot(slot)


def slot_row(conn, slot="slot"):
    return conn.q("SELECT status, booked_count FROM materialised_slots WHERE slot_id = ?", slot)[0]


# ============================================================================ joining and leaving
def test_only_a_full_slot_has_a_waitlist(world):
    conn, deps = world
    assert code(lambda: waitlist.join(conn, deps, "u-2", "slot")) == (409, "SLOT_NOT_FULL")


def test_join_leave_and_listing_show_the_position(world):
    conn, deps = world
    full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3", "u-4"])
    status, body = waitlist.list_mine(conn, deps, "u-3")
    assert status == 200 and [i["position"] for i in body["items"]] == [2] and body["items"][0]["status"] == "WAITING"
    assert waitlist.leave(conn, deps, "u-2", "slot")[1]["left"] is True
    assert waitlist.list_mine(conn, deps, "u-3")[1]["items"][0]["position"] == 1
    assert code(lambda: waitlist.leave(conn, deps, "u-2", "slot")) == (404, "NOT_ON_WAITLIST")


def test_the_same_patient_cannot_join_twice_nor_join_a_slot_they_hold(world):
    conn, deps = world
    full_slot(conn, deps)
    waitlist.join(conn, deps, "u-2", "slot")
    assert code(lambda: waitlist.join(conn, deps, "u-2", "slot")) == (409, "ALREADY_WAITLISTED")
    assert code(lambda: waitlist.join(conn, deps, "u-1", "slot")) == (409, "ALREADY_BOOKED")


def test_a_patient_can_be_on_at_most_five_waitlists(world):
    conn, deps = world
    holders = ["u-3", "u-4", "u-5"]
    for i in range(6):   # six full slots, two per holder (the limit is 3 upcoming appointments per doctor)
        add_slot(conn.db, f"s{i}", START + timedelta(days=1 + i), capacity=1)
        service.book(conn, deps, holders[i % 3], {"slot_id": f"s{i}"}, None)
    results = [waitlist.join(conn, deps, "u-2", f"s{i}")[0] for i in range(5)]
    assert results == [201] * 5
    assert code(lambda: waitlist.join(conn, deps, "u-2", "s5")) == (409, "WAITLIST_LIMIT_REACHED")
    waitlist.leave(conn, deps, "u-2", "s0")                       # leaving one makes room again
    assert waitlist.join(conn, deps, "u-2", "s5")[0] == 201


def test_the_waitlist_limit_is_read_from_ssm(world, monkeypatch):
    conn, deps = world
    monkeypatch.setattr(rules, "waitlist_max_size", lambda: 1)
    add_slot(conn.db, "s2", START + timedelta(days=1), capacity=1)
    full_slot(conn, deps)
    full_slot(conn, deps, slot="s2")
    waitlist.join(conn, deps, "u-2", "slot")
    assert code(lambda: waitlist.join(conn, deps, "u-2", "s2")) == (409, "WAITLIST_LIMIT_REACHED")


# ============================================================================ hold offers
def test_a_freed_seat_is_held_for_the_first_in_line_and_is_invisible_to_everyone_else(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    offers = live(deps)
    assert len(offers) == 1 and offers[0]["patient_id"] == "p-2"
    assert slot_row(conn) == ("FULL", 1)                       # the held seat still counts: nobody else can book it
    assert deps.slot_table.items["slot"]["booked_count"] == 1
    assert code(lambda: service.book(conn, deps, "u-4", {"slot_id": "slot"}, None)) == (409, "SLOT_FULL")
    assert deps.waitlist.get_entry("slot", "p-2")["status"] == "OFFERED"
    assert deps.waitlist.get_entry("slot", "p-3")["status"] == "WAITING"
    assert [d["patient_id"] for d in deps.events.of("WaitlistOfferIssued")] == ["p-2"]


def test_a_hold_lasts_the_ssm_duration(world, monkeypatch):
    conn, deps = world
    monkeypatch.setattr(rules, "hold_offer_minutes", lambda: 7)
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2"])
    issued_at = deps.clock.now
    service.cancel_by_patient(conn, deps, "u-1", appt)
    offer = live(deps)[0]
    assert offer["expires_at"] - wl.epoch(issued_at) == 7 * 60


def test_the_default_hold_is_fifteen_minutes(world):
    assert rules.hold_offer_minutes() == 15


def test_the_freed_seat_is_not_taken_by_an_outsider_before_the_waitlist_is_looked_at(world):
    """A seat freed in a slot WITH a queue never stays free: the cancellation itself holds it."""
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    assert slot_row(conn)[0] == "FULL"


def test_a_freed_seat_without_a_queue_simply_reopens(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    service.cancel_by_patient(conn, deps, "u-1", appt)
    assert slot_row(conn) == ("OPEN", 0) and live(deps) == []


def test_patients_who_cannot_book_the_slot_are_skipped(world):
    conn, deps = world
    add_slot(conn.db, "clash", START, capacity=1, doctor="d2")   # same time, other doctor
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.book(conn, deps, "u-2", {"slot_id": "clash"}, None)    # u-2 now has an overlapping appointment
    service.cancel_by_patient(conn, deps, "u-1", appt)
    assert [o["patient_id"] for o in live(deps)] == ["p-3"]


# ============================================================================ MANDATORY TEST 4
def test_mandatory_4_waitlist_exclusivity_one_freed_seat_three_waiting_one_live_offer(world):
    """One freed seat, three waitlisted patients: at every moment there is exactly one live offer."""
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3", "u-4"])
    service.cancel_by_patient(conn, deps, "u-1", appt)

    seen = []
    for expected in ("p-2", "p-3", "p-4"):
        offers = live(deps)
        assert len(offers) == 1, "exactly one live offer at any moment"
        assert offers[0]["patient_id"] == expected
        assert slot_row(conn) == ("FULL", 1) and deps.slot_table.items["slot"]["booked_count"] == 1
        seen.append(expected)
        # hammering the seat-freed handler (event redelivery, a second consumer) must not create a second offer
        for _ in range(5):
            assert waitlist.offer_next(conn, deps, "slot") is None
        assert len(live(deps)) == 1
        deps.clock.now += timedelta(minutes=15)
        waitlist.sweep_expired(conn, deps)
    assert seen == ["p-2", "p-3", "p-4"]
    assert live(deps) == []
    assert slot_row(conn) == ("OPEN", 0) and deps.slot_table.items["slot"]["booked_count"] == 0  # nobody left: seat is free


def test_mandatory_4_concurrent_seat_freed_handlers_create_only_one_offer(tmp_path):
    db = FileDb(tmp_path / "rds.db", SCHEMA)
    boot = db.connect()
    seed(boot.db, patients=5)
    add_slot(boot.db, "slot", START, capacity=1)
    boot.close()
    deps = make_deps()
    c = db.connect()
    appt = full_slot(c, deps)
    queue_up(c, deps, ["u-2", "u-3", "u-4"])
    # free the seat WITHOUT the inline fast path, so that only the racing consumers decide who gets it
    deps.waitlist, kept = None, deps.waitlist
    service.cancel_by_patient(c, deps, "u-1", appt)
    deps.waitlist = kept
    c.close()

    def consumer(i):
        conn = db.connect()
        try:
            return waitlist.offer_next(conn, deps, "slot")
        finally:
            conn.close()

    results = run_threads(8, consumer)
    assert not [r for r in results if isinstance(r, Exception)], results
    assert len([r for r in results if r]) == 1
    offers = live(deps)
    assert len(offers) == 1 and offers[0]["patient_id"] == "p-2"
    assert deps.slot_table.items["slot"]["booked_count"] == 1
    assert [e["status"] for e in deps.waitlist.entries_for_slot("slot")] == ["OFFERED", "WAITING", "WAITING"]


def test_a_capacity_two_slot_with_one_freed_seat_makes_one_offer(world):
    conn, deps = world
    add_slot(conn.db, "camp", START + timedelta(hours=2), capacity=2, ctype="hc", minutes=60)
    a1 = full_slot(conn, deps, "u-1", "camp")
    full_slot(conn, deps, "u-2", "camp")
    queue_up(conn, deps, ["u-3", "u-4"], "camp")
    service.cancel_by_patient(conn, deps, "u-1", a1)
    assert len(live(deps, "camp")) == 1 and slot_row(conn, "camp") == ("FULL", 2)


# ============================================================================ accepting
def test_accepting_turns_the_hold_into_a_confirmed_appointment(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    offer = live(deps)[0]
    status, body = waitlist.accept(conn, deps, "u-2", offer["offer_id"])
    assert status == 201 and body["appointment"]["status"] == "CONFIRMED"
    assert slot_row(conn) == ("FULL", 1) and deps.slot_table.items["slot"]["booked_count"] == 1   # no double count
    assert deps.waitlist.get_entry("slot", "p-2") is None                                           # left the waitlist
    assert deps.waitlist.get_entry("slot", "p-3")["status"] == "WAITING"
    assert live(deps) == [] and deps.metrics.total("WaitlistPromotions") == 1
    assert deps.events.of("AppointmentBooked")[-1]["via"] == "waitlist"


def test_accepting_twice_returns_the_same_appointment(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    offer_id = live(deps)[0]["offer_id"]
    first = waitlist.accept(conn, deps, "u-2", offer_id)[1]["appointment"]["appointment_id"]
    status, again = waitlist.accept(conn, deps, "u-2", offer_id)
    assert status == 200 and again["appointment"]["appointment_id"] == first and again["idempotent_replay"] is True
    assert conn.q("SELECT COUNT(*) FROM appointments WHERE patient_id = 'p-2'") == [(1,)]


def test_only_the_offered_patient_can_accept(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    offer_id = live(deps)[0]["offer_id"]
    assert code(lambda: waitlist.accept(conn, deps, "u-3", offer_id)) == (404, "OFFER_NOT_FOUND")
    assert code(lambda: waitlist.accept(conn, deps, "u-2", "no-such-offer")) == (404, "OFFER_NOT_FOUND")


def test_an_expired_offer_cannot_be_accepted(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    offer_id = live(deps)[0]["offer_id"]
    deps.clock.now += timedelta(minutes=15, seconds=1)
    assert code(lambda: waitlist.accept(conn, deps, "u-2", offer_id)) == (409, "OFFER_EXPIRED")


def test_accept_and_expiry_racing_have_exactly_one_winner(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    offer = live(deps)[0]
    deps.clock.now = deps.clock.now.replace(microsecond=0)
    deps.clock.now += timedelta(minutes=14)
    # the sweeper claims the expiry first (as if it ran a split second before the accept)
    assert deps.waitlist.transition(offer["offer_id"], wl.LIVE, wl.EXPIRED)
    assert code(lambda: waitlist.accept(conn, deps, "u-2", offer["offer_id"])) == (409, "OFFER_EXPIRED")
    assert conn.q("SELECT COUNT(*) FROM appointments WHERE patient_id = 'p-2'") == [(0,)]


def test_a_patient_who_cannot_take_the_seat_after_all_passes_it_on(world):
    conn, deps = world
    add_slot(conn.db, "clash", START, capacity=1, doctor="d2")
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    offer_id = live(deps)[0]["offer_id"]
    service.book(conn, deps, "u-2", {"slot_id": "clash"}, None)    # u-2 books something overlapping in the meantime
    assert code(lambda: waitlist.accept(conn, deps, "u-2", offer_id)) == (409, "TIME_CONFLICT")
    assert [o["patient_id"] for o in live(deps)] == ["p-3"]
    assert deps.slot_table.items["slot"]["booked_count"] == 1


# ============================================================================ the sweeper
def test_the_sweeper_passes_an_expired_offer_to_the_next_patient_and_tells_the_first(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    deps.clock.now += timedelta(minutes=16)
    assert waitlist.sweep_expired(conn, deps) == {"expired": 1, "passed_on": 1}
    assert [o["patient_id"] for o in live(deps)] == ["p-3"]
    assert deps.waitlist.get_entry("slot", "p-2") is None
    assert [d["patient_id"] for d in deps.events.of("WaitlistOfferExpired")] == ["p-2"]
    assert deps.metrics.total("OffersExpired") == 1


def test_the_sweeper_is_safe_to_run_twice(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    deps.clock.now += timedelta(minutes=16)
    waitlist.sweep_expired(conn, deps)
    assert waitlist.sweep_expired(conn, deps) == {"expired": 0, "passed_on": 0}
    assert len(deps.events.of("WaitlistOfferExpired")) == 1 and len(live(deps)) == 1


def test_a_live_offer_is_not_expired_early(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    deps.clock.now += timedelta(minutes=14, seconds=59)
    assert waitlist.sweep_expired(conn, deps)["expired"] == 0 and len(live(deps)) == 1


def test_leaving_while_holding_an_offer_passes_the_seat_on(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)
    assert waitlist.leave(conn, deps, "u-2", "slot")[1]["seat_passed_on"] is True
    assert [o["patient_id"] for o in live(deps)] == ["p-3"]


def test_a_doctor_cancelling_the_slot_cancels_offers_and_clears_the_queue(world):
    conn, deps = world
    appt = full_slot(conn, deps)
    queue_up(conn, deps, ["u-2", "u-3"])
    service.cancel_by_patient(conn, deps, "u-1", appt)             # p-2 now holds the seat
    service.cancel_slot_by_doctor(conn, deps, "u-d1", {"slot_id": "slot"})
    assert live(deps) == [] and deps.waitlist.entries_for_slot("slot") == []
    assert deps.slot_table.items["slot"]["booked_count"] == 0
    assert slot_row(conn)[0] == "CLOSED"
