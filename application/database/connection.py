"""RDS MySQL connection helper for MediSlot Lambda functions."""
import json
import os

import boto3
import pymysql
from botocore.config import Config

# Uses the regional Secrets Manager endpoint. In private subnets, ensure a
# Secrets Manager interface VPC endpoint exists or configure NAT routing.
_secrets = boto3.client(
    "secretsmanager",
    config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 3, "mode": "standard"}),
)

def get_connection():
    secret_arn = os.environ.get("DATABASE_SECRET_ARN")
    db_host = os.environ.get("DB_HOST")
    db_name = os.environ.get("DB_NAME")
    if not secret_arn or not db_host or not db_name:
        raise RuntimeError(
            "DATABASE_SECRET_ARN, DB_HOST, and DB_NAME must all be configured."
        )

    response = _secrets.get_secret_value(SecretId=secret_arn)
    secret_string = response.get("SecretString")
    if not secret_string:
        raise RuntimeError("The database secret does not contain SecretString.")

    try:
        secret = json.loads(secret_string)
        username = secret["username"]
        password = secret["password"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "The database secret must be JSON containing username and password fields."
        ) from exc

    return pymysql.connect(
        host=db_host,
        user=username,
        password=password,
        database=db_name,
        port=int(os.environ.get("DB_PORT", "3306")),
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        ssl={"check_hostname": True},
    )
