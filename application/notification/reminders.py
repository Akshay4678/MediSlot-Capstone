"""Emit 24-hour and 2-hour appointment reminder events.

Actual email/SMS delivery is deliberately outside Milestone 4. This worker only creates
idempotent ReminderDue events so the notification layer can deliver them later.
"""

import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from application.database.connection import get_connection

cfg = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})
ddb = boto3.resource("dynamodb", config=cfg)
events = boto3.client("events", config=cfg)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _claim(table, dispatch_id, appointment_id, reminder_type):
    try:
        table.put_item(
            Item={
                "dispatch_id": dispatch_id,
                "appointment_id": appointment_id,
                "reminder_type": reminder_type,
                "expires_at": int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp()),
            },
            ConditionExpression="attribute_not_exists(dispatch_id)",
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def lambda_handler(event, context):
    dispatch_name = os.environ.get("NOTIFICATION_DISPATCH_TABLE_NAME")
    if not dispatch_name:
        raise RuntimeError("NOTIFICATION_DISPATCH_TABLE_NAME is not configured")

    now = datetime.now(timezone.utc)
    windows = {
        "24H": (now + timedelta(hours=23, minutes=45), now + timedelta(hours=24, minutes=15)),
        "2H": (now + timedelta(hours=1, minutes=45), now + timedelta(hours=2, minutes=15)),
    }
    table = ddb.Table(dispatch_name)
    sent = 0

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for reminder_type, (start, end) in windows.items():
                cur.execute(
                    "SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id, "
                    "s.start_utc, s.end_utc, d.first_name AS doctor_first_name, d.last_name AS doctor_last_name "
                    "FROM appointments a "
                    "JOIN materialised_slots s ON s.slot_id = a.slot_id "
                    "JOIN doctors d ON d.doctor_id = a.doctor_id "
                    "WHERE a.status = 'CONFIRMED' AND s.start_utc >= %s AND s.start_utc < %s",
                    (start.replace(tzinfo=None), end.replace(tzinfo=None)),
                )
                for row in cur.fetchall():
                    dispatch_id = f"{row['appointment_id']}#{reminder_type}"
                    if not _claim(table, dispatch_id, row["appointment_id"], reminder_type):
                        continue
                    events.put_events(
                        Entries=[{
                            "Source": "medislot.notifications",
                            "DetailType": "ReminderDue",
                            "Detail": __import__("json").dumps({
                                "appointment_id": row["appointment_id"],
                                "patient_id": row["patient_id"],
                                "doctor_id": row["doctor_id"],
                                "slot_id": row["slot_id"],
                                "reminder_type": reminder_type,
                                "starts_at_utc": _iso(row["start_utc"].replace(tzinfo=timezone.utc)),
                            }, default=str),
                            "EventBusName": os.environ.get("EVENT_BUS_NAME", "default"),
                        }]
                    )
                    sent += 1
    finally:
        conn.close()

    return {"statusCode": 200, "reminders_emitted": sent}
