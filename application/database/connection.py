"""
MediSlot RDS MySQL connection helper.

Required Lambda environment variables:
    DB_HOST
    DB_NAME
    DATABASE_SECRET_ARN

Optional:
    DB_PORT (defaults to 3306)

The Secrets Manager secret must contain:
    {
        "username": "...",
        "password": "..."
    }
"""

import json
import os

import boto3
import pymysql


_secrets_client = boto3.client("secretsmanager")


def get_connection():
    """Create and return a connection to the MediSlot RDS database."""

    required_variables = (
        "DB_HOST",
        "DB_NAME",
        "DATABASE_SECRET_ARN",
    )

    missing_variables = [
        name for name in required_variables
        if not os.environ.get(name)
    ]

    if missing_variables:
        raise RuntimeError(
            "Missing required database environment variables: "
            + ", ".join(missing_variables)
        )

    secret_arn = os.environ["DATABASE_SECRET_ARN"]

    secret_response = _secrets_client.get_secret_value(
        SecretId=secret_arn
    )

    secret_string = secret_response.get("SecretString")

    if not secret_string:
        raise RuntimeError(
            "The database secret does not contain a SecretString."
        )

    try:
        credentials = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "The database secret is not valid JSON."
        ) from exc

    username = credentials.get("username")
    password = credentials.get("password")

    if not username or not password:
        raise RuntimeError(
            "The database secret must contain username and password."
        )

    try:
        port = int(os.environ.get("DB_PORT", "3306"))
    except ValueError as exc:
        raise RuntimeError(
            "DB_PORT must be a valid integer."
        ) from exc

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=username,
        password=password,
        database=os.environ["DB_NAME"],
        port=port,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        ssl={"check_hostname": True},
    )