import os
import json
import boto3

from application.common.http import (
    response,
    request_method,
    request_body,
)


sns_client = boto3.client("sns")


def lambda_handler(event, context):
    # Handle incoming SNS notifications
    if (
        "Records" in event
        and any(
            record.get("EventSource") == "aws:sns"
            for record in event["Records"]
        )
    ):
        print("Received SNS notification event.")

        return {
            "statusCode": 200,
            "body": "ok",
        }

    # Handle appointment events from EventBridge or Lambda
    if (
        event.get("source") == "medislot.appointments"
        or event.get("detail-type")
        in (
            "AppointmentCreated",
            "AppointmentCancelled",
            "AppointmentRescheduled",
        )
    ):
        topic_arn = os.environ.get("NOTIFICATION_TOPIC_ARN")

        if not topic_arn:
            raise RuntimeError(
                "NOTIFICATION_TOPIC_ARN is not configured"
            )

        event_details = event.get("detail", {})

        message = json.dumps(
            {
                "event": event.get("detail-type"),
                "detail": event_details,
            },
            default=str,
        )

        sns_client.publish(
            TopicArn=topic_arn,
            Subject="MediSlot appointment update",
            Message=message,
        )

        return {
            "statusCode": 200,
            "body": "notification published",
        }

    # Handle POST requests for administrative testing
    if request_method(event) == "POST":
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

        topic_arn = os.environ.get("NOTIFICATION_TOPIC_ARN")

        if not topic_arn:
            return response(
                500,
                {"error": "NOTIFICATION_TOPIC_NOT_CONFIGURED"},
            )

        if not body.get("message"):
            return response(
                400,
                {"error": "MESSAGE_REQUIRED"},
            )

        sns_client.publish(
            TopicArn=topic_arn,
            Subject=str(
                body.get("subject", "MediSlot notification")
            )[:100],
            Message=str(body["message"]),
        )

        return response(
            202,
            {"message": "Notification submitted."},
        )

    return response(
        405,
        {"error": "METHOD_NOT_ALLOWED"},
    )
