"""Configuration read from SSM Parameter Store.

Every threshold, window and duration lives in Parameter Store (never hard-coded
in business logic). The code-side default is only a safety net so that a
missing parameter or a short SSM outage cannot take the whole API down; it is
logged as a warning so the gap is visible.

Parameters live under ``$SSM_PREFIX`` (for example ``/medislot/dev``).
"""

import logging
import os
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

_AWS = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})
_ssm = boto3.client("ssm", config=_AWS)


def _name(relative):
    prefix = os.environ.get("SSM_PREFIX", "").rstrip("/")
    return f"{prefix}/{relative}" if prefix else ""


@lru_cache(maxsize=64)
def get_str(relative, default=None, decrypt=False):
    name = _name(relative)
    if not name:
        return default
    try:
        return _ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 - never let config break a request
        log.warning("SSM parameter %s unavailable (%s); using default %r", name, type(exc).__name__, default)
        return default


def get_int(relative, default):
    value = get_str(relative, None)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("SSM parameter %s is not an integer; using default %r", relative, default)
        return default


def get_optional_secret(relative):
    """Return a SecureString value, or None only when the parameter does not exist.

    Any other failure (access denied, network...) is raised so that security gates
    built on this value fail closed instead of silently opening.
    """
    name = _name(relative)
    if not name:
        return None
    try:
        return _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"] or None
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
