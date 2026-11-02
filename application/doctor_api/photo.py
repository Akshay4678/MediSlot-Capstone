"""Profile photo upload: the browser/app uploads straight to S3 with a pre-signed POST.

Binary content never passes through API Gateway or Lambda. The POST policy pins the
content type and limits the size (rules/photo-max-bytes).
"""

import os
from functools import lru_cache

import boto3
from botocore.config import Config

from application.common import rules
from application.common.api import ApiError, new_id

ALLOWED = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
UPLOAD_EXPIRES = 300
VIEW_EXPIRES = 900


@lru_cache(maxsize=1)
def _s3():
    return boto3.client(
        "s3", region_name=os.environ.get("AWS_REGION"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"},
                      connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"}),
    )


def _bucket():
    bucket = os.environ.get("PHOTO_BUCKET")
    if not bucket:
        raise RuntimeError("PHOTO_BUCKET is not configured")
    return bucket


def view_url(key):
    """Short-lived pre-signed GET URL for a stored photo key (None if no photo)."""
    if not key:
        return None
    return _s3().generate_presigned_url("get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=VIEW_EXPIRES)


def create_upload(ctx):
    """POST /doctors/me/photo  {"content_type": "image/jpeg"} -> pre-signed POST."""
    doctor = ctx.doctor()
    content_type = str(ctx.body.get("content_type", "")).lower().strip()
    if content_type not in ALLOWED:
        raise ApiError(400, "UNSUPPORTED_CONTENT_TYPE", f"content_type must be one of {sorted(ALLOWED)}.")

    key = f"doctors/{doctor['doctor_id']}/{new_id()}.{ALLOWED[content_type]}"
    max_bytes = rules.photo_max_bytes()
    upload = _s3().generate_presigned_post(
        Bucket=_bucket(), Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=UPLOAD_EXPIRES,
    )
    with ctx.conn.cursor() as cur:
        cur.execute("UPDATE doctors SET photo_url = %s WHERE doctor_id = %s", (key, doctor["doctor_id"]))
    return 200, {"upload": upload, "photo_key": key, "max_bytes": max_bytes, "expires_in": UPLOAD_EXPIRES}
