"""RDS MySQL connection helper. Requires PyMySQL packaged with Lambda."""
import json, os, boto3, pymysql
_secrets = boto3.client("secretsmanager")
def get_connection():
    secret_arn=os.environ["DATABASE_SECRET_ARN"]
    secret=json.loads(_secrets.get_secret_value(SecretId=secret_arn)["SecretString"])
    return pymysql.connect(
        host=os.environ["DB_HOST"], user=secret["username"], password=secret["password"],
        database=os.environ["DB_NAME"], port=int(os.environ.get("DB_PORT","3306")),
        connect_timeout=5, read_timeout=10, write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        ssl={"check_hostname": True}
    )
