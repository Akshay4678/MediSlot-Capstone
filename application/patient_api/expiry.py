"""Expire 15-minute waitlist hold offers."""

import os
import time

import boto3
from botocore.config import Config
from boto3.dynamodb.conditions import Attr

cfg = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})
ddb = boto3.resource("dynamodb", config=cfg)


def lambda_handler(event, context):
    table_name = os.environ.get("HOLD_OFFERS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("HOLD_OFFERS_TABLE_NAME is not configured")
    table = ddb.Table(table_name)
    now = int(time.time())
    expired = 0
    scan_kwargs = {"FilterExpression": Attr("status").eq("PENDING") & Attr("expires_at").lte(now)}
    while True:
        page = table.scan(**scan_kwargs)
        for item in page.get("Items", []):
            table.update_item(
                Key={"offer_id": item["offer_id"]},
                UpdateExpression="SET #s = :s",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "EXPIRED", ":pending": "PENDING"},
            )
            expired += 1
        if "LastEvaluatedKey" not in page:
            break
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return {"statusCode": 200, "expired": expired}
