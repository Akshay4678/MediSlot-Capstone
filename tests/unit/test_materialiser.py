"""Slot materialisation: alignment, daylight-saving crossings, idempotency, blackouts."""

from datetime import date, datetime, time, timezone

from application.slot_materialiser import core

DOC = "11111111-1111-4111-8111-111111111111"
AV = "22222222-2222-4222-8222-222222222222"
CT = "33333333-3333-4333-8333-333333333333"
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


def pattern(start="09:00", end="12:00", minutes=30, capacity=1, day=1):
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    return {"availability_id": AV, "consultation_type_id": CT, "day_of_week": day, "start_time": time(h1, m1),
            "end_time": time(h2, m2), "capacity": capacity, "duration_minutes": minutes}


def starts(slots):
    return [s["start_utc"].strftime("%Y-%m-%d %H:%M") for s in slots]


# ---------------------------------------------------------------- alignment
def test_slots_align_to_duration_from_pattern_start():
    slots = core.build_slots(DOC, pattern("09:00", "12:00", 30), date(2026, 10, 26), "Asia/Kolkata", LONG_AGO)
    assert [s["start_time"].strftime("%H:%M") for s in slots] == ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]
    assert slots[-1]["end_time"] == time(12, 0)


def test_last_slot_must_fit_inside_the_window():
    slots = core.build_slots(DOC, pattern("09:00", "10:20", 30), date(2026, 10, 26), "Asia/Kolkata", LONG_AGO)
    assert [s["start_time"].strftime("%H:%M") for s in slots] == ["09:00", "09:30"]


def test_odd_start_time_keeps_alignment_to_the_pattern_start():
    slots = core.build_slots(DOC, pattern("09:10", "10:40", 15), date(2026, 10, 26), "Asia/Kolkata", LONG_AGO)
    assert [s["start_time"].strftime("%H:%M") for s in slots] == ["09:10", "09:25", "09:40", "09:55", "10:10", "10:25"]


def test_capacity_is_copied_from_the_pattern_and_ids_are_deterministic():
    first = core.build_slots(DOC, pattern("09:00", "10:00", 60, capacity=5), date(2026, 10, 26), "Asia/Kolkata", LONG_AGO)
    again = core.build_slots(DOC, pattern("09:00", "10:00", 60, capacity=5), date(2026, 10, 26), "Asia/Kolkata", LONG_AGO)
    assert first[0]["capacity"] == 5 and first[0]["slot_id"] == again[0]["slot_id"]
    assert first[0]["slot_id"] == f"{DOC}#{AV}#20261026T0330Z"
    assert first[0]["timezone"] == "Asia/Kolkata"


# ------------------------------------------------------- daylight-saving time
def test_new_york_dst_end_2026_keeps_09_00_local_while_utc_shifts():
    """US clocks go back on Sunday 2026-11-01: 09:00 local is 13:00Z before and 14:00Z after."""
    friday = core.build_slots(DOC, pattern(day=5), date(2026, 10, 30), "America/New_York", LONG_AGO)
    monday = core.build_slots(DOC, pattern(day=1), date(2026, 11, 2), "America/New_York", LONG_AGO)
    assert starts(friday)[0] == "2026-10-30 13:00"   # EDT, UTC-4
    assert starts(monday)[0] == "2026-11-02 14:00"   # EST, UTC-5
    for slots in (friday, monday):
        assert slots[0]["start_time"] == time(9, 0) and slots[-1]["end_time"] == time(12, 0) and len(slots) == 6


def test_new_york_9_to_5_is_9_to_5_local_on_both_sides_of_the_change():
    before = core.build_slots(DOC, pattern("09:00", "17:00", 60), date(2026, 10, 31), "America/New_York", LONG_AGO)
    after = core.build_slots(DOC, pattern("09:00", "17:00", 60), date(2026, 11, 2), "America/New_York", LONG_AGO)
    assert starts(before)[0] == "2026-10-31 13:00" and starts(before)[-1] == "2026-10-31 20:00"
    assert starts(after)[0] == "2026-11-02 14:00" and starts(after)[-1] == "2026-11-02 21:00"
    assert len(before) == len(after) == 8


def test_london_dst_end_2026():
    bst = core.build_slots(DOC, pattern(day=6), date(2026, 10, 24), "Europe/London", LONG_AGO)
    gmt = core.build_slots(DOC, pattern(day=1), date(2026, 10, 26), "Europe/London", LONG_AGO)
    assert starts(bst)[0] == "2026-10-24 08:00" and starts(gmt)[0] == "2026-10-26 09:00"


def test_spring_forward_gap_times_are_skipped_not_shifted():
    """2026-03-08 02:00-03:00 does not exist in New York."""
    slots = core.build_slots(DOC, pattern("01:30", "04:00", 30, day=7), date(2026, 3, 8), "America/New_York", LONG_AGO)
    assert [s["start_time"].strftime("%H:%M") for s in slots] == ["01:30", "03:00", "03:30"]
    assert starts(slots) == ["2026-03-08 06:30", "2026-03-08 07:00", "2026-03-08 07:30"]


def test_kolkata_has_no_dst_and_is_utc_plus_5_30():
    slots = core.build_slots(DOC, pattern(), date(2026, 10, 26), "Asia/Kolkata", LONG_AGO)
    assert starts(slots)[0] == "2026-10-26 03:30"


def test_slots_in_the_past_are_not_created():
    now = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)  # 10:00 in Kolkata
    slots = core.build_slots(DOC, pattern(), date(2026, 10, 26), "Asia/Kolkata", now)
    assert [s["start_time"].strftime("%H:%M") for s in slots] == ["10:30", "11:00", "11:30"]


# --------------------------------------------------------- window and database
def test_window_is_30_local_dates_starting_today_in_the_doctors_timezone():
    now = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)  # already 31 Oct 01:30 in Kolkata
    dates = core.window_dates(now, "Asia/Kolkata", 30)
    assert len(dates) == 30 and dates[0] == date(2026, 10, 31) and dates[-1] == date(2026, 11, 29)
    assert core.window_dates(now, "America/New_York", 30)[0] == date(2026, 10, 30)


class FakeDb:
    """In-memory stand-in for the three tables the materialiser touches."""

    def __init__(self, patterns, blackouts=()):
        self.patterns, self.blackouts, self.slots = patterns, list(blackouts), {}

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, db):
        self.db, self.rowcount, self.rows = db, 0, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        if "FROM availability_patterns" in sql:
            self.rows = self.db.patterns
        elif "FROM blackout_dates" in sql:
            self.rows = [{"blackout_date": d} for d in self.db.blackouts]
        elif sql.startswith("DELETE FROM materialised_slots") and "start_utc >" in sql:
            doomed = [k for k, v in self.db.slots.items() if v["start_utc"] > params[1] and v["booked_count"] == 0]
            for key in doomed:
                del self.db.slots[key]
            self.rowcount = len(doomed)

    def executemany(self, sql, rows):
        assert sql.startswith("INSERT IGNORE")
        self.rowcount = 0
        for r in rows:
            if r[0] not in self.db.slots:
                self.db.slots[r[0]] = {"start_utc": r[7], "booked_count": 0, "capacity": r[10]}
                self.rowcount += 1

    def fetchall(self):
        return self.rows


NOW = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)  # Monday


def test_materialising_twice_creates_no_duplicates():
    db = FakeDb([pattern(day=1)])
    first = core.materialise_doctor(db, DOC, "Asia/Kolkata", NOW, window_days=30)
    count = len(db.slots)
    second = core.materialise_doctor(db, DOC, "Asia/Kolkata", NOW, window_days=30)
    assert first["inserted"] == count > 0
    assert second["inserted"] == 0 and second["already_existing"] == count and len(db.slots) == count


def test_a_rolled_forward_window_only_adds_the_new_day():
    db = FakeDb([pattern(day=1)])
    core.materialise_doctor(db, DOC, "Asia/Kolkata", NOW, window_days=30)
    before = len(db.slots)
    later = core.materialise_doctor(db, DOC, "Asia/Kolkata", datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc), window_days=30)
    assert later["inserted"] == 6 and len(db.slots) == before + 6  # one more Monday = 6 slots


def test_blackout_dates_are_skipped():
    db = FakeDb([pattern(day=1)], blackouts=[date(2026, 11, 2)])
    core.materialise_doctor(db, DOC, "Asia/Kolkata", NOW, window_days=30)
    assert not any("20261102" in key for key in db.slots) and any("20261026" in key for key in db.slots)


def test_rebuild_keeps_booked_slots_and_is_idempotent():
    db = FakeDb([pattern(day=1)])
    core.materialise_doctor(db, DOC, "Asia/Kolkata", NOW, window_days=30)
    booked = sorted(db.slots)[3]
    db.slots[booked]["booked_count"] = 1
    total = len(db.slots)
    summary = core.rebuild_future_slots(db, DOC, "Asia/Kolkata", NOW)
    assert summary["removed_unbooked"] == total - 1 and booked in db.slots and len(db.slots) == total
    assert db.slots[booked]["booked_count"] == 1
