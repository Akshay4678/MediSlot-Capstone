"""RDS MySQL connection helper for MediSlot Lambda functions."""

import json
import logging
import os
from functools import lru_cache

import boto3
import pymysql
from botocore.config import Config

log = logging.getLogger()
log.setLevel(logging.INFO)

# Short timeouts: a missing network route must fail fast with a clear log line
# instead of hanging until the Lambda times out.
aws_config = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})
_secrets = boto3.client("secretsmanager", config=aws_config)


@lru_cache(maxsize=1)
def _credentials():
    secret_arn = os.environ.get("DATABASE_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("DATABASE_SECRET_ARN must be configured.")
    log.info("Retrieving database credentials from Secrets Manager")
    secret_string = _secrets.get_secret_value(SecretId=secret_arn).get("SecretString")
    if not secret_string:
        raise RuntimeError("The database secret does not contain SecretString.")
    secret = json.loads(secret_string)
    username, password = secret.get("username"), secret.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise RuntimeError("The database secret needs string username and password fields.")
    return username, password


def get_connection():
    """Open a new MySQL connection (the caller closes it)."""
    db_host, db_name = os.environ.get("DB_HOST"), os.environ.get("DB_NAME")
    if not db_host or not db_name:
        raise RuntimeError("DB_HOST and DB_NAME must be configured.")
    username, password = _credentials()
    try:
        connection = pymysql.connect(
            host=db_host,
            user=username,
            password=password,
            database=db_name,
            port=int(os.environ.get("DB_PORT", "3306")),
            connect_timeout=5,
            read_timeout=15,
            write_timeout=15,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            ssl={"check_hostname": True},
        )
    except Exception:
        log.exception("Failed to connect to RDS")
        raise
    return connection
