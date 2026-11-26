"""PATCH /appointments/{id}: start the reschedule saga and turn its outcome into an HTTP answer.

With RESCHEDULE_STATE_MACHINE_ARN set (deployed) the saga runs in AWS Step Functions (an EXPRESS state machine
called synchronously). Without it (local development, unit tests) the same steps run in-process.
"""

import json
import os

from application.common.api import ApiError
from application.reschedule import saga


def reschedule(conn, deps, user_id, appointment_id, body):
    body = body or {}
    unknown = sorted(set(body) - {"slot_id"})
    if unknown:
        raise ApiError(400, "UNKNOWN_FIELD", f"Unknown field(s): {', '.join(unknown)}.")
    slot_id = body.get("slot_id")
    if not isinstance(slot_id, str) or not slot_id.strip() or len(slot_id) > 160:
        raise ApiError(400, "INVALID_SLOT_ID", "slot_id (the target slot) is required.")
    arn = os.environ.get("RESCHEDULE_STATE_MACHINE_ARN")
    if not arn:
        return saga.run_local(conn, deps, user_id, appointment_id, slot_id.strip())
    import boto3
    from botocore.config import Config
    client = getattr(deps, "sfn", None) or boto3.client(
        "stepfunctions", config=Config(connect_timeout=2, read_timeout=20, retries={"total_max_attempts": 2}))
    execution = client.start_sync_execution(
        stateMachineArn=arn,
        input=json.dumps({"user_id": user_id, "appointment_id": appointment_id, "target_slot_id": slot_id.strip()}))
    return saga.result_from_execution(execution)
