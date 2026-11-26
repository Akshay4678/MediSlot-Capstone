"""Scheduled jobs through the jobs Lambda: no-show sweeper, reminders, offer expiry (all safe to run twice)."""

from datetime import timedelta

from fake_dynamo import FakeTable
from test_milestone6_api import ENC, SLOT, START, book, call, patient_handler, world  # noqa: F401 (fixture)

from application.jobs import handler as jobs
from application.notification.dispatch_log import DispatchLog


def run_job(conn, deps, job, log=None):
    return jobs.lambda_handler({"job": job}, None, deps=deps, conn_factory=lambda: conn,
                               dispatch_log=log or DispatchLog(FakeTable("dispatch_id"), deps.now))


def test_no_show_sweeper_marks_overdue_appointments_once_and_closes_the_slot(world):  # noqa: F811
    conn, deps = world
    book("u-1")
    deps.clock.now = START + timedelta(minutes=10)  # slot ends at +30, grace is 30 more minutes
    assert run_job(conn, deps, "no_shows")["marked_no_show"] == 0
    deps.clock.now = START + timedelta(minutes=70)
    assert run_job(conn, deps, "no_shows")["marked_no_show"] == 1
    assert run_job(conn, deps, "no_shows")["marked_no_show"] == 0  # idempotent
    assert conn.q("SELECT status FROM appointments") == [("NO_SHOW",)]
    assert conn.q("SELECT status FROM materialised_slots WHERE slot_id = ?", SLOT) == [("CLOSED",)]
    assert conn.q("SELECT refund_tier FROM refund_ledger") == [("NONE",)]


def test_reminders_are_published_once_per_window(world):  # noqa: F811
    conn, deps = world
    book("u-1")
    log = DispatchLog(FakeTable("dispatch_id"), deps.now)
    deps.clock.now = START - timedelta(hours=23, minutes=30)
    assert run_job(conn, deps, "reminders", log)["reminders_emitted"] == 1
    assert run_job(conn, deps, "reminders", log)["reminders_emitted"] == 0
    deps.clock.now = START - timedelta(hours=1, minutes=30)
    assert run_job(conn, deps, "reminders", log)["reminders_emitted"] == 1
    kinds = [d["reminder_type"] for d in deps.events.of("ReminderDue")]
    assert kinds == ["24H", "2H"]


def test_an_unknown_job_is_rejected(world):  # noqa: F811
    conn, deps = world
    try:
        run_job(conn, deps, "nope")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
