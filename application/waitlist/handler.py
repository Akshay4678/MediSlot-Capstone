```python
import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

from application.common.http import (
    response,
    request_method,
    request_body,
)
from application.common.auth_context import claims


dynamodb = boto3.resource("dynamodb")

WAITLIST_TABLE_NAME = os.environ["WAITLIST_TABLE_NAME"]
WAITLIST_GSI_NAME = os.environ.get(
    "WAITLIST_GSI_NAME",
    "user_id-index",
)

waitlist_table = dynamodb.Table(WAITLIST_TABLE_NAME)


def get_identity(event):
    """
    Extract the authenticated identity from the API Gateway event.

    Adapt this function if your existing claims() helper expects
    different arguments or returns a different structure.
    """
    identity = claims(event)

    if not isinstance(identity, dict):
        return None

    user_id = identity.get("user_id") or identity.get("sub")
    role = identity.get("role")

    if not user_id or not role:
        return None

    return {
        "user_id": user_id,
        "role": str(role).lower(),
    }


def get_all_user_entries(user_id):
    """Retrieve all waitlist entries belonging to one user."""
    items = []
    last_key = None

    while True:
        params = {
            "IndexName": WAITLIST_GSI_NAME,
            "KeyConditionExpression": Key("user_id").eq(user_id),
        }

        if last_key:
            params["ExclusiveStartKey"] = last_key

        result = waitlist_table.query(**params)
        items.extend(result.get("Items", []))

        last_key = result.get("LastEvaluatedKey")

        if not last_key:
            break

    return items


def lambda_handler(event, context):
    identity = get_identity(event)

    if not identity:
        return response(
            401,
            {"message": "Unauthorized"},
        )

    if identity["role"] != "patient":
        return response(
            403,
            {"message": "Only patients can manage waitlist entries"},
        )

    method = request_method(event)

    try:
        if method == "POST":
            return join_waitlist(identity, event)

        if method == "GET":
            return list_waitlist(identity)

        if method == "DELETE":
            return cancel_waitlist(identity, event)

        return response(
            405,
            {"message": "Method not allowed"},
        )

    except Exception:
        # Log the detailed exception to CloudWatch in production.
        print("Waitlist request failed")
        return response(
            500,
            {"message": "Internal server error"},
        )


def join_waitlist(identity, event):
    body = request_body(event) or {}

    slot_id = body.get("slot_id")
    doctor_id = body.get("doctor_id")

    if not slot_id or not doctor_id:
        return response(
            400,
            {"message": "slot_id and doctor_id are required"},
        )

    now = int(time.time())

    # The table's sort key is created_at, so use a consistent
    # string representation for the DynamoDB key.
    created_at = datetime.fromtimestamp(
        now,
        tz=timezone.utc,
    ).isoformat()

    item = {
        "slot_id": str(slot_id),
        "created_at": created_at,
        "waitlist_id": str(uuid.uuid4()),
        "user_id": identity["user_id"],
        "doctor_id": str(doctor_id),
        "status": "WAITING",
        "created_at_epoch": now,
        "ttl": now + (90 * 24 * 60 * 60),
    }

    waitlist_table.put_item(
        Item=item,
        ConditionExpression=(
            "attribute_not_exists(slot_id) "
            "AND attribute_not_exists(created_at)"
        ),
    )

    return response(
        201,
        {
            "message": "Successfully joined waitlist",
            "waitlist": item,
        },
    )


def list_waitlist(identity):
    items = get_all_user_entries(identity["user_id"])

    # Do not return internal TTL metadata.
    for item in items:
        item.pop("ttl", None)

    return response(
        200,
        {
            "count": len(items),
            "waitlist": items,
        },
    )


def cancel_waitlist(identity, event):
    body = request_body(event) or {}

    path_parameters = event.get("pathParameters") or {}

    waitlist_id = (
        path_parameters.get("waitlist_id")
        or body.get("waitlist_id")
        or (
            event.get("queryStringParameters") or {}
        ).get("waitlist_id")
    )

    if not waitlist_id:
        return response(
            400,
            {"message": "waitlist_id is required"},
        )

    items = get_all_user_entries(identity["user_id"])

    entry = next(
        (
            item
            for item in items
            if item.get("waitlist_id") == waitlist_id
        ),
        None,
    )

    if not entry:
        return response(
            404,
            {"message": "Waitlist entry not found"},
        )

    waitlist_table.delete_item(
        Key={
            "slot_id": entry["slot_id"],
            "created_at": entry["created_at"],
        },
        ConditionExpression="user_id = :user_id",
        ExpressionAttributeValues={
            ":user_id": identity["user_id"],
        },
    )

    return response(
        200,
        {"message": "Waitlist entry cancelled"},
    )
```