from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from application.slot_materialiser.handler import _valid_local, _slot_id


def test_dst_spring_forward_nonexistent_local_time_is_skipped():
    zone = ZoneInfo("America/New_York")
    # 02:30 does not exist on 2026-03-08 in New York.
    assert _valid_local(datetime(2026, 3, 8, 2, 30), zone) is None


def test_dst_fall_back_ambiguous_time_uses_first_occurrence():
    zone = ZoneInfo("America/New_York")
    value = _valid_local(datetime(2026, 11, 1, 1, 30), zone, fold=0)
    assert value is not None
    assert value.astimezone(timezone.utc) == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)


def test_normal_local_time_converts_to_utc():
    zone = ZoneInfo("Asia/Kolkata")
    value = _valid_local(datetime(2026, 11, 1, 9, 0), zone)
    assert value is not None
    assert value.astimezone(timezone.utc) == datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)


def test_slot_id_is_deterministic_and_uses_utc_instant():
    instant = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert _slot_id(7, instant) == "7#20261101T053000Z"
    assert _slot_id(7, instant) == _slot_id(7, instant)
