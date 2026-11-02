"""Optional AWS X-Ray instrumentation (boto3, pymysql, requests are patched when the SDK is present)."""

import logging

log = logging.getLogger()


def enable():
    try:
        from aws_xray_sdk.core import patch_all  # type: ignore

        patch_all()
        return True
    except Exception as exc:  # noqa: BLE001 - tracing must never break the function
        log.info("X-Ray SDK not active (%s)", type(exc).__name__)
        return False
