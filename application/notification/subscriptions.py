"""E-mail delivery of notifications through SNS: every patient and doctor is subscribed, with the e-mail address they
registered with, to the patient or doctor topic.

One topic per role, one subscription per person. The subscription carries a FILTER POLICY on the message attribute
`recipient_id` (the patient_id or doctor_id), so a notice published for patient X is delivered only to X's address.
SNS sends the person a "Confirm subscription" e-mail once; until they click it nothing is delivered (AWS rule). Every
later e-mail carries an unsubscribe link.

Subscribing never blocks the caller: a failure is logged and the profile is saved anyway (the `subscribe_users` job
can repair or backfill every account later; it is safe to run repeatedly).
"""

import json
import logging
import os

log = logging.getLogger()

PATIENT, DOCTOR = "PATIENT", "DOCTOR"
TOPIC_ENV = {PATIENT: "PATIENT_TOPIC_ARN", DOCTOR: "DOCTOR_TOPIC_ARN"}


def _client():
    import boto3
    return boto3.client("sns")


def ensure_subscribed(role, entity_id, email, client=None):
    """Subscribe `email` to the topic of `role` for notices addressed to `entity_id`. Returns True when requested."""
    topic = os.environ.get(TOPIC_ENV[role], "")
    if not topic or not email or not entity_id:
        return False
    try:
        (client or _client()).subscribe(
            TopicArn=topic, Protocol="email", Endpoint=email, ReturnSubscriptionArn=True,
            Attributes={"FilterPolicy": json.dumps({"recipient_id": [entity_id]}), "FilterPolicyScope": "MessageAttributes"})
        return True
    except Exception:  # noqa: BLE001 - never fail a profile save because of e-mail delivery
        log.exception("could not subscribe %s %s to notifications", role, entity_id)
        return False


def user_email(cur, user_id):
    cur.execute("SELECT email FROM users WHERE user_id = %s AND is_active = TRUE", (user_id,))
    row = cur.fetchone()
    return row["email"] if row else None


def subscribe_all(conn, client=None):
    """Backfill: subscribe every active patient and doctor. Returns the counts."""
    done = {"patients": 0, "doctors": 0}
    with conn.cursor() as cur:
        cur.execute("SELECT p.patient_id AS entity_id, u.email FROM patients p JOIN users u ON u.user_id = p.user_id "
                    "WHERE p.is_active = TRUE AND u.is_active = TRUE")
        patients = cur.fetchall()
        cur.execute("SELECT d.doctor_id AS entity_id, u.email FROM doctors d JOIN users u ON u.user_id = d.user_id "
                    "WHERE d.is_active = TRUE AND u.is_active = TRUE")
        doctors = cur.fetchall()
    conn.rollback()
    for row in patients:
        done["patients"] += bool(ensure_subscribed(PATIENT, row["entity_id"], row["email"], client))
    for row in doctors:
        done["doctors"] += bool(ensure_subscribed(DOCTOR, row["entity_id"], row["email"], client))
    return done
