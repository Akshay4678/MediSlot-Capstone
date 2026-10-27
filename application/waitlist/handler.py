```python
import os
import time
import uuid

import boto3

from application.common.http import (
    response,
    request_method,
    request_body,
)
from application.common.auth_context import claims


dynamodb = boto3.resource("dynamodb")


def lambda_handler(event, context):
    # Allow access only to authenticated patients
    identity = claims(event)

    if (
        not identity["user_id"]
        or identity["role"] != "patient"
    ):
        return response(
            401,
            {"error": "UNAUTHORIZED"},
        )

    # Get the waitlist table name from environment variables
    table_name = os.environ.get("WAITLIST_TABLE_NAME")

    if not table_name:
        return response(
            500,
            {"error": "WAITLIST_TABLE_NOT_CONFIGURED"},
        )

    table = dynamodb.Table(table_name)
    method = request_method(event)

    query_params = event.get("queryStringParameters") or {}

    # Add a patient to the waitlist
    if method == "POST":
        try:
            body = request_body(event)

        except ValueError as error:
            return response(
                400,
                {
                    "error": "INVALID_JSON",
                    "message": str(error),
                },
            )

        slot_id = str(body.get("slot_id", "")).strip()
        doctor_id = str(body.get("doctor_id", "")).strip()

        if not slot_id or not doctor_id:
            return response(
                400,
                {
                    "error": "MISSING_FIELDS",
                    "fields": ["slot_id", "doctor_id"],
                },
            )

        # Create the waitlist entry
        # The table must use waitlist_id as its String partition key.
        current_time = int(time.time())

        waitlist_item = {
            "waitlist_id": str(uuid.uuid4()),
            "user_id": identity["user_id"],
            "slot_id": slot_id,
            "doctor_id": doctor_id,
            "status": "WAITING",
            "created_at": current_time,
            "ttl": current_time + 90 * 86400,
        }

        try:
            table.put_item(
                Item=waitlist_item,
                ConditionExpression="attribute_not_exists(waitlist_id)",
            )

            return response(
                201,
                {
                    "message": "Added to waitlist.",
                    "waitlist_id": waitlist_item["waitlist_id"],
                },
            )

        except Exception:
            return response(
                500,
                {
                    "error": "WAITLIST_WRITE_FAILED",
                    "message": (
                        "Verify the table key schema and IAM permissions."
                    ),
                },
            )

    # Retrieve the current patient's waitlist entries
    if method == "GET":
        # A queryable user_id GSI is required; Scan is not used.
        index_name = os.environ.get(
            "WAITLIST_USER_INDEX",
            "user_id-index",
        )

        try:
            waitlist_entries = table.query(
                IndexName=index_name,
                KeyConditionExpression=(
                    boto3.dynamodb.conditions.Key("user_id").eq(
                        identity["user_id"]
                    )
                ),
            ).get("Items", [])

            return response(
                200,
                {
                    "items": waitlist_entries,
                    "count": len(waitlist_entries),
                },
            )

        except Exception:
            return response(
                500,
                {
                    "error": "WAITLIST_QUERY_FAILED",
                    "message": (
                        "Configure a GSI partitioned by user_id "
                        "or adjust WAITLIST_USER_INDEX."
                    ),
                },
            )

    # Remove a patient's waitlist entry
    if method == "DELETE":
        path_params = event.get("pathParameters") or {}

        waitlist_id = (
            path_params.get("waitlist_id")
            or query_params.get("waitlist_id")
        )

        if not waitlist_id:
            return response(
                400,
                {"error": "WAITLIST_ID_REQUIRED"},
            )

        try:
            table.delete_item(
                Key={"waitlist_id": waitlist_id},
                ConditionExpression="user_id = :u",
                ExpressionAttributeValues={
                    ":u": identity["user_id"],
                },
            )

            return response(
                200,
                {"message": "Waitlist entry removed."},
            )

        except Exception:
            return response(
                404,
                {"error": "WAITLIST_ENTRY_NOT_FOUND"},
            )

    return response(
        405,
        {"error": "METHOD_NOT_ALLOWED"},
    )
```