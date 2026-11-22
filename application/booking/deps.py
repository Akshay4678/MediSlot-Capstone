"""Wires the booking engine to the real AWS services (created lazily, once per Lambda container)."""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from application.booking.events import EventPublisher
from application.booking.idempotency import IdempotencyStore
from application.booking.metrics import Metrics
from application.booking.slot_store import SlotStore
from application.common import rules


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Deps:
    slots: SlotStore
    idempotency: IdempotencyStore
    events: object
    metrics: object
    now: object = _utcnow  # naive UTC "now"; tests replace it with a fixed clock


_cached = None


def default_deps():
    global _cached
    if _cached is None:
        import boto3
        from botocore.config import Config

        cfg = Config(connect_timeout=2, read_timeout=4, retries={"total_max_attempts": 2, "mode": "standard"})
        dynamodb = boto3.resource("dynamodb", config=cfg)
        env = os.environ.get("ENVIRONMENT_NAME", "dev")
        _cached = Deps(
            slots=SlotStore(dynamodb.Table(os.environ["SLOT_CAPACITY_TABLE_NAME"]),
                            max_attempts=rules.booking_max_attempts(), backoff_ms=rules.booking_backoff_ms()),
            idempotency=IdempotencyStore(dynamodb.Table(os.environ["IDEMPOTENCY_TABLE_NAME"]),
                                         ttl_hours=rules.idempotency_ttl_hours()),
            events=EventPublisher(boto3.client("events", config=cfg), os.environ.get("EVENT_BUS_NAME", "default")),
            metrics=Metrics(boto3.client("cloudwatch", config=cfg), environment=env),
        )
    return _cached
