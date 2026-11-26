"""AWS X-Ray, switched on only inside Lambda and only if the SDK is installed (unit tests run without it).

The Lambda functions have Active tracing (CloudFormation `TracingConfig: Mode: Active`), which gives one
segment per invocation. enable() additionally patches boto3 / botocore (DynamoDB, EventBridge, SSM, Secrets
Manager, SNS, Step Functions ...) and PyMySQL, so every call a booking or a reschedule makes shows up as a
sub-segment. API Gateway tracing and the Step Functions state machine tracing join them into ONE trace:
API -> patient-api Lambda -> state machine -> step Lambdas (validate, claim, move, release, COMPENSATE ...).
"""

import logging
import os

log = logging.getLogger()
_enabled = False


def enable():
    global _enabled
    if _enabled or not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return _enabled
    try:
        from aws_xray_sdk.core import patch_all
        patch_all()
        _enabled = True
    except Exception:  # noqa: BLE001 - tracing must never stop a request
        log.warning("X-Ray SDK not available: running without sub-segments")
    return _enabled


def annotate(key, value):
    """Searchable annotation on the current segment (for example compensated=true). Never raises."""
    if not _enabled:
        return
    try:
        from aws_xray_sdk.core import xray_recorder
        segment = xray_recorder.current_subsegment() or xray_recorder.current_segment()
        segment.put_annotation(key, value)
    except Exception:  # noqa: BLE001
        pass
