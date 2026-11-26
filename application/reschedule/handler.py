"""Lambda behind every Task state of the reschedule state machine: event = {"step": <name>, ...saga state}.

One function, one step per invocation. The state machine decides the order and the compensation; this
handler only runs the named step against RDS and DynamoDB. A business rejection is raised as SagaRejected
so Step Functions can catch it by name.
"""

import logging

from application.booking.deps import default_deps
from application.database.connection import get_connection
from application.reschedule import saga

log = logging.getLogger()
log.setLevel(logging.INFO)

STEPS = {
    "validate": saga.validate,
    "claim_target": saga.claim_target,
    "move_appointment": saga.move_appointment,
    "release_original": saga.release_original,
    "publish": saga.publish,
    "compensate": saga.compensate,
    "final": None,
}


def lambda_handler(event, context):
    step = event.get("step")
    state = {k: v for k, v in event.items() if k not in ("step", "error")}
    if step == "final":  # shape the successful output: the HTTP layer reads it back with a fresh query
        return {"result": saga_result(state)}
    fn = STEPS.get(step)
    if fn is None:
        raise ValueError(f"unknown saga step {step!r}")
    conn = get_connection()
    try:
        return fn(conn, default_deps(), state)
    finally:
        conn.close()


def saga_result(state):
    conn = get_connection()
    try:
        from application.booking.service import _fetch_view
        deps = default_deps()
        with conn.cursor() as cur:
            view = _fetch_view(cur, state["appointment_id"], deps.now())
        conn.rollback()
        return view
    finally:
        conn.close()
