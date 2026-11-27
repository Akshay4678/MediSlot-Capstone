"""A patient who already holds a seat is not offered Book / waitlist for it; waitlist rows expire at the slot's end."""

from datetime import timedelta

from test_milestone6_api import ENC, SLOT, START, book, call, patient_handler, world  # noqa: F401 (fixture)

from application.waitlist.store import epoch


def test_slots_are_flagged_when_the_patient_already_booked_them(world):  # noqa: F811
    conn, deps = world
    book("u-1")
    from application.patient_api import handler as h
    payload = {"slots": [{"slot_id": SLOT}, {"slot_id": "other"}], "full_slots": [{"slot_id": SLOT}], "days": [], "full_days": []}
    h._mark_booked_by_me(conn, "u-1", payload)
    assert [s["booked_by_me"] for s in payload["slots"]] == [True, False]
    assert payload["full_slots"][0]["booked_by_me"] is True and payload["my_booked_slot_ids"] == [SLOT]
    other = {"slots": [{"slot_id": SLOT}], "full_slots": [], "days": [], "full_days": []}
    h._mark_booked_by_me(conn, "u-2", other)
    assert other["slots"][0]["booked_by_me"] is False


def test_a_waitlist_row_carries_the_slot_end_as_its_ttl_and_expired_rows_are_hidden(world):  # noqa: F811
    conn, deps = world
    book("u-1")
    status, _ = call(patient_handler, "POST", "/slots/{slot_id}/waitlist", user="u-2", path={"slot_id": ENC})
    assert status == 201
    row = deps.queue_table.get_item(Key={"slot_id": SLOT, "patient_id": "p-2"})["Item"]
    assert row["expire_time"] == epoch(START + timedelta(minutes=30))
    deps.clock.now = START + timedelta(minutes=31)  # the slot is over; DynamoDB may not have deleted the row yet
    assert deps.waitlist.entries_for_patient("p-2", deps.now()) == []
    assert len(deps.waitlist.entries_for_patient("p-2")) == 1
