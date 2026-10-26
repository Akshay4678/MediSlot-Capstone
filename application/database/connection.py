"""RDS connection helper. Requires PyMySQL to be included in the Lambda package/layer.

Set DATABASE_SECRET_ARN to a Secrets Manager secret containing username/password,
and set DB_HOST and DB_NAME as Lambda environment variables.
"""
import json
import os
import boto3
import pymysql

_secrets = boto3.client("secretsmanager")

def get_connection():
    secret_arn = os.environ["DATABASE_SECRET_ARN"]
    secret = json.loads(
        _secrets.get_secret_value(SecretId=secret_arn)["SecretString"]
    )
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=secret["username"],
        password=secret["password"],
        database=os.environ["DB_NAME"],
        port=int(os.environ.get("DB_PORT", "3306")),
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        ssl={"check_hostname": True},
    )
