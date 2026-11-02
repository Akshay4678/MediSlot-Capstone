"""Request/response helpers shared by the MediSlot domain Lambdas."""

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone


class ApiError(Exception):
    """Raised by handlers; the router turns it into a JSON error response."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = int(status), code, message


# ------------------------------------------------------------------- formatting
def as_time(value):
    """MySQL TIME comes back as timedelta (PyMySQL), time or str; normalise to time."""
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        return (datetime.min + value).time()
    if isinstance(value, str):
        parts = [int(p) for p in value.split(":")]
        return time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)
    raise TypeError(f"Unsupported TIME value: {value!r}")


def fmt_time(value):
    return None if value is None else as_time(value).strftime("%H:%M")


def fmt_utc(value):
    """DATETIME (naive, stored as UTC) -> ISO-8601 with Z."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    return str(value)


def as_date(value):
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def utc_now():
    return datetime.now(timezone.utc)


def money(value):
    return None if value is None else float(value)


# -------------------------------------------------------------------- validation
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def require_uuid(value, field):
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise ApiError(400, "INVALID_ID", f"{field} must be a valid id.")
    return value.lower()


def new_id():
    return str(uuid.uuid4())


def parse_hhmm(value, field):
    match = _HHMM_RE.match(value) if isinstance(value, str) else None
    if not match:
        raise ApiError(400, "INVALID_TIME", f"{field} must be a local time in HH:MM format.")
    return time(int(match.group(1)), int(match.group(2)))


def parse_date(value, field):
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "INVALID_DATE", f"{field} must use YYYY-MM-DD.") from None


def clean_text(value, field, max_len, required=False):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ApiError(400, "MISSING_FIELD", f"{field} is required.")
        return None
    if not isinstance(value, str):
        raise ApiError(400, "INVALID_FIELD", f"{field} must be text.")
    value = value.strip()
    if len(value) > max_len:
        raise ApiError(400, "INVALID_FIELD", f"{field} must be at most {max_len} characters.")
    return value
