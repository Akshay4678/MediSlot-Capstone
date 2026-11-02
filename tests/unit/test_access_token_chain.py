
"""Offline tests for the current MediSlot access-token chain."""

import base64
import copy
import json
import os
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class ClientError(Exception):
    def __init__(self, response, operation_name="operation"):
        self.response = response
        super().__init__(str(response))


for name in ("botocore", "botocore.config", "botocore.exceptions"):
    sys.modules[name] = types.ModuleType(name)

sys.modules["botocore.config"].Config = lambda **kwargs: kwargs
sys.modules["botocore.exceptions"].ClientError = ClientError

SECRET = "test-secret-" * 5
STORE = {}

TABLE_KEYS = {
    "dev-medislot-refresh-tokens-m3": "token_hash",
    "dev-medislot-token-activelist-m3": "user_id",
}


class FakeSSM:
    def get_parameter(self, **kwargs):
        return {"Parameter": {"Value": SECRET}}


class FakeAttr:
    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return self.name, value


class Table:
    def __init__(self, name):
        self.name = name
        self.key = TABLE_KEYS[name]
        STORE.setdefault(name, {})

    def get_item(self, Key, ConsistentRead=False, **kwargs):
        item = STORE[self.name].get(Key[self.key])
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, **kwargs):
        STORE[self.name][Item[self.key]] = copy.deepcopy(Item)
        return {}

    def delete_item(
        self,
        Key,
        ConditionExpression=None,
        ExpressionAttributeValues=None,
        ReturnValues=None,
        **kwargs
    ):
        item = STORE[self.name].get(Key[self.key])

        if ConditionExpression:
            now = ExpressionAttributeValues[":now"]

            if not item or int(item.get("expires_at", 0)) <= int(now):
                raise ClientError({
                    "Error": {
                        "Code": "ConditionalCheckFailedException"
                    }
                })

        removed = STORE[self.name].pop(Key[self.key], None)

        if ReturnValues == "ALL_OLD" and removed:
            return {"Attributes": copy.deepcopy(removed)}

        return {}

    def query(self, KeyConditionExpression=None, **kwargs):
        field, value = KeyConditionExpression

        items = [
            copy.deepcopy(item)
            for item in STORE[self.name].values()
            if str(item.get(field)) == str(value)
        ]

        return {"Items": items}


class FakeDDB:
    def Table(self, name):
        return Table(name)


boto3 = types.ModuleType("boto3")
boto3.client = lambda *args, **kwargs: FakeSSM()
boto3.resource = lambda *args, **kwargs: FakeDDB()
sys.modules["boto3"] = boto3

boto3_dynamodb = types.ModuleType("boto3.dynamodb")
boto3_conditions = types.ModuleType(
    "boto3.dynamodb.conditions"
)


class FakeKey:
    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return self.name, value


boto3_conditions.Key = FakeKey
boto3_conditions.Attr = FakeAttr

sys.modules["boto3.dynamodb"] = boto3_dynamodb
sys.modules["boto3.dynamodb.conditions"] = boto3_conditions

bcrypt = types.ModuleType("bcrypt")
bcrypt.hashpw = lambda password, salt: b"HASH:" + password
bcrypt.gensalt = lambda **kwargs: b"salt"
bcrypt.checkpw = lambda password, hashed: (
    hashed == b"HASH:" + password
)
sys.modules["bcrypt"] = bcrypt

pymysql = types.ModuleType("pymysql")
pymysql.err = types.SimpleNamespace(
    IntegrityError=type("IntegrityError", (Exception,), {})
)
pymysql.cursors = types.SimpleNamespace(DictCursor=object)
sys.modules["pymysql"] = pymysql

os.environ.update(
    JWT_SECRET_PARAMETER_NAME="/test/jwt",
    REFRESH_TOKEN_TABLE="dev-medislot-refresh-tokens-m3",
    TOKEN_ACTIVELIST_TABLE="dev-medislot-token-activelist-m3",
    JWT_ISSUER="medislot",
)

import application.auth.handler as ah
import application.authorizer.lambda_handler as az


USERS = {
    "pat@example.com": {
        "user_id": 1001,
        "email": "pat@example.com",
        "password_hash": "HASH:Patient@12345",
        "role": "PATIENT",
        "is_active": True,
    }
}


class Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, args=()):
        if "WHERE email = %s" in sql:
            self.row = USERS.get(args[0])

        elif "WHERE user_id = %s" in sql:
            self.row = next(
                (
                    user for user in USERS.values()
                    if str(user["user_id"]) == str(args[0])
                ),
                None,
            )
        else:
            self.row = None

    def fetchone(self):
        return self.row


class Connection:
    def cursor(self):
        return Cursor()

    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


ah.get_connection = lambda: Connection()


def call(
    path,
    body=None,
    headers=None,
    method="POST",
    context=None
):
    event = {
        "requestContext": {
            "http": {"method": method},
            "authorizer": context or {},
        },
        "rawPath": path,
        "body": json.dumps(body or {}),
        "headers": headers or {},
    }

    result = ah.lambda_handler(event, None)

    return result["statusCode"], json.loads(result["body"])


def claims(token):
    part = token.split(".")[1]

    return json.loads(
        base64.urlsafe_b64decode(
            part + "=" * (-len(part) % 4)
        )
    )


def valid(token):
    try:
        policy = az.lambda_handler(
            {
                "methodArn": (
                    "arn:aws:execute-api:ap-south-1:"
                    "123:api/dev/GET/test"
                ),
                "headers": {
                    "authorization": "Bearer " + token
                },
            },
            None,
        )

        return any(
            statement.get("Effect") == "Allow"
            for statement in policy["policyDocument"]["Statement"]
        )

    except Exception:
        return False


def login():
    status, data = call(
        "/auth/login",
        {
            "email": "pat@example.com",
            "password": "Patient@12345",
        },
    )

    assert status == 200 and "user" not in data, data

    return data["access_token"], data["refresh_token"]


def reset():
    STORE.clear()

    STORE["dev-medislot-refresh-tokens-m3"] = {}
    STORE["dev-medislot-token-activelist-m3"] = {}


def test_login_stores_one_active_token_and_12_hour_refresh_record():
    reset()

    access, refresh = login()
    token_claims = claims(access)

    refresh_record = STORE[
        "dev-medislot-refresh-tokens-m3"
    ][ah._hash_token(refresh)]

    assert set(refresh_record) == {
        "token_hash",
        "user_id",
        "created_at",
        "expires_at",
    }

    assert isinstance(refresh_record["created_at"], int)
    assert isinstance(refresh_record["expires_at"], int)

    assert (
        refresh_record["expires_at"]
        - refresh_record["created_at"]
    ) == 12 * 60 * 60

    active_records = STORE[
        "dev-medislot-token-activelist-m3"
    ]

    assert list(active_records) == ["1001"]

    active = active_records["1001"]

    assert active["jti"] == token_claims["jti"]
    assert active["user_id"] == "1001"
    assert active["ttl"] == token_claims["exp"]

    assert valid(access)


def test_refresh_replaces_previous_active_access_token():
    reset()

    old_access, refresh = login()

    status, data = call(
        "/auth/refresh",
        {"refresh_token": refresh},
    )

    assert status == 200, data

    new_access = data["access_token"]

    assert not valid(old_access)
    assert valid(new_access)

    assert len(
        STORE["dev-medislot-token-activelist-m3"]
    ) == 1

    assert (
        STORE["dev-medislot-token-activelist-m3"]["1001"]["jti"]
        == claims(new_access)["jti"]
    )


def test_expired_or_unknown_refresh_token_rejected():
    reset()

    assert call(
        "/auth/refresh",
        {"refresh_token": "not-a-real-token"},
    )[0] == 401


def test_logout_removes_active_access_and_all_refresh_tokens_for_user():
    reset()

    access, refresh = login()

    second = "another-refresh-token"
    now = int(time.time())

    STORE["dev-medislot-refresh-tokens-m3"][
        ah._hash_token(second)
    ] = {
        "token_hash": ah._hash_token(second),
        "user_id": "1001",
        "created_at": now,
        "expires_at": now + 43200,
    }

    status, data = call(
        "/auth/logout",
        {},
        {"authorization": "Bearer " + access},
    )

    assert status == 200, data
    assert not valid(access)

    assert STORE["dev-medislot-refresh-tokens-m3"] == {}
    assert STORE["dev-medislot-token-activelist-m3"] == {}

    assert call(
        "/auth/refresh",
        {"refresh_token": refresh},
    )[0] == 401


def test_tampered_token_rejected():
    reset()

    access, _ = login()

    assert valid(access)
    assert not valid(access[:-3] + "xyz")


def test_protected_dummy_endpoint_requires_authorizer_context():
    reset()

    assert call(
        "/auth/test-access",
        method="GET",
    )[0] == 401

    status, data = call(
        "/auth/test-access",
        method="GET",
        context={
            "lambda": {
                "userId": "1001",
                "role": "patient",
            }
        },
    )

    assert status == 200
    assert data["user_id"] == "1001"


def test_authorizer_requires_active_list_entry():
    reset()

    access, _ = login()

    assert valid(access)

    STORE["dev-medislot-token-activelist-m3"].pop("1001")

    assert not valid(access)
