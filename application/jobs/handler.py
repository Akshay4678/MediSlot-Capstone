"""One Lambda for the background work of Milestone 6 (it runs inside the VPC because it reads RDS).

Triggers
  EventBridge Scheduler  {"job": "expire_offers"}   every minute   -> waitlist.sweep_expired
                         {"job": "reminders"}       every 5 min    -> reminders.run
                         {"job": "attendance"}       every 5 min    -> attendance.run
  SQS (waitlist queue)   SlotSeatFreed events       -> waitlist.offer_next  (own queue and dead-letter queue)

Every job is safe to run twice: the work is guarded by conditional writes (see each module).
"""

import json
import logging
import os

from application.booking.deps import default_deps
from application.database.connection import get_connection
from application.jobs import attendance, reminders
from application.notification.dispatch_log import DispatchLog
from application.notification import subscriptions
from application.waitlist import service as waitlist

log = logging.getLogger()
log.setLevel(logging.INFO)

SEAT_FREED = "SlotSeatFreed"


def _dispatch_log(deps):
    import boto3
    return DispatchLog(boto3.resource("dynamodb").Table(os.environ["DISPATCH_LOG_TABLE_NAME"]), deps.now)


def lambda_handler(event, context, deps=None, conn_factory=get_connection, dispatch_log=None):
    deps = deps or default_deps()
    conn = conn_factory()
    try:
        if "Records" in event:
            return _seat_freed(conn, deps, event["Records"])
        job = event.get("job")
        if job == "expire_offers":
            return waitlist.sweep_expired(conn, deps)
        if job == "reminders":
            return reminders.run(conn, deps, dispatch_log or _dispatch_log(deps))
        if job in ("attendance", "no_shows"):  # "no_shows" is the old name of the same schedule
            return attendance.run(conn, deps)
        if job == "subscribe_users":  # one-off / repair: e-mail subscriptions for every existing patient and doctor
            return subscriptions.subscribe_all(conn)
        raise ValueError(f"unknown job {job!r}")
    finally:
        conn.close()


def _seat_freed(conn, deps, records):
    failures = []
    for record in records:
        try:
            envelope = json.loads(record["body"])
            if envelope.get("detail-type") == SEAT_FREED:
                waitlist.offer_next(conn, deps, (envelope.get("detail") or {})["slot_id"])
        except Exception:  # noqa: BLE001 - back to the queue, then to the dead-letter queue
            log.exception("seat-freed message %s failed", record.get("messageId"))
            conn.rollback()
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
