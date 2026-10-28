"""RDS MySQL connection helper for MediSlot Lambda functions."""

import json
import logging
import os

import boto3
import pymysql
from botocore.config import Config

log = logging.getLogger()
log.setLevel(logging.INFO)

# Limit connection and read times so failures are easier to diagnose.
aws_config = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={
        "total_max_attempts": 1,
        "mode": "standard",
    },
)

_secrets = boto3.client(
    "secretsmanager",
    config=aws_config,
)


def get_connection():
    """Retrieve database credentials and connect to RDS MySQL."""

    secret_arn = os.environ.get("DATABASE_SECRET_ARN")
    db_host = os.environ.get("DB_HOST")
    db_name = os.environ.get("DB_NAME")

    if not secret_arn or not db_host or not db_name:
        raise RuntimeError(
            "DATABASE_SECRET_ARN, DB_HOST, and DB_NAME must be configured."
        )

    log.info("Retrieving database credentials from Secrets Manager")

    try:
        secret_response = _secrets.get_secret_value(
            SecretId=secret_arn
        )
    except Exception:
        log.exception("Failed to retrieve database credentials")
        raise

    secret_string = secret_response.get("SecretString")

    if not secret_string:
        raise RuntimeError(
            "The database secret does not contain SecretString."
        )

    try:
        secret = json.loads(secret_string)
        username = secret["username"]
        password = secret["password"]

        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("Invalid database credential types")

    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "The database secret must contain valid username and password fields."
        ) from exc

    log.info("Database credentials retrieved; connecting to RDS")

    try:
        connection = pymysql.connect(
            host=db_host,
            user=username,
            password=password,
            database=db_name,
            port=int(os.environ.get("DB_PORT", "3306")),
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            ssl={"check_hostname": True},
        )

        log.info("RDS connection established")
        return connection

    except Exception:
        log.exception("Failed to connect to RDS")
        raise