
"""Offline tests of the access-token chain: active list, rotation, logout, authorizer."""

import base64
import copy
import hashlib
import hmac
import json
import time

import pytest

from application.auth import handler as ah
from application.authorizer import lambda_handler as az
from application.common import config

SECRET = b"test-secret-" * 5

USER = {
    "user_id": "user-1",
    "email": "pat@example.com",
    "password_hash": "HASH:Patient@12345",
    "role": "PATIENT",
    "is_active": True
}

DOCTOR = {
    "user_id": "doc-1",
    "email": "doc@example.com",
    "password_hash": "HASH:Doctor@12345",
    "role": "DOCTOR",
    "is_active": True
}


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeKey:
    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return (self.name, value)


class FakeTable:
    def __init__(self, store, key):
        self.items = store
        self.key = key

    def get_item(self, Key, **kwargs):
        item = self.items.get(Key[self.key])
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, ReturnValues=None, **kwargs):
        old = self.items.get(Item[self.key])
        self.items[Item[self.key]] = copy.deepcopy(Item)

        return {
            "Attributes": copy.deepcopy(old)
        } if ReturnValues == "ALL_OLD" and old else {}

    def delete_item(
        self,
        Key,
        ConditionExpression=None,
        ExpressionAttributeValues=None,
        ReturnValues=None,
        **kw
    ):
        item = self.items.get(Key[self.key])

        if ConditionExpression:
            if (
                not item
                or item.get("expires_at", 0)
                <= ExpressionAttributeValues[":now"]
            ):
                raise ClientError("ConditionalCheckFailedException")

        self.items.pop(Key[self.key], None)

        return {
            "Attributes": item
        } if ReturnValues == "ALL_OLD" and item else {}

    def query(self, **kwargs):
        _, user_id = kwargs["KeyConditionExpression"]

        return {
            "Items": [
                copy.deepcopy(v)
                for v in self.items.values()
                if v.get("user_id") == user_id
            ]
        }


class FakeDDB:
    def __init__(self):
        self.stores = {
            "dev-medislot-refresh-tokens": {},
            "dev-medislot-token-activelist-v2": {}
        }

        self.keys = {
            "dev-medislot-refresh-tokens": "token_hash",
            "dev-medislot-token-activelist-v2": "user_id"
        }

    def Table(self, name):
        return FakeTable(self.stores[name], self.keys[name])


class FakeBcrypt:
    rounds_seen = []

    @staticmethod
    def gensalt(rounds=12):
        FakeBcrypt.rounds_seen.append(rounds)
        return b"salt"

    @staticmethod
    def hashpw(password, salt):
        return b"HASH:" + password

    @staticmethod
    def checkpw(password, hashed):
        return hashed == b"HASH:" + password


class FakeCursor:
    def __init__(self, users):
        self.users = users
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, args=()):
        if "WHERE email = %s" in sql:
            self.row = self.users.get(args[0])

        elif "WHERE user_id = %s" in sql:
            self.row = next(
                (
                    u for u in self.users.values()
                    if u["user_id"] == args[0]
                ),
                None
            )

        else:
            self.row = None

    def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, users):
        self.users = users

    def cursor(self):
        return FakeCursor(self.users)

    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture(autouse=True)
def env(monkeypatch):
    # Isolate environment variables from other test modules.
    monkeypatch.setenv(
        "REFRESH_TOKEN_TABLE",
        "dev-medislot-refresh-tokens"
    )
    monkeypatch.setenv(
        "TOKEN_ACTIVELIST_TABLE",
        "dev-medislot-token-activelist-v2"
    )
    monkeypatch.setenv("JWT_ISSUER", "medislot")

    fake = FakeDDB()

    users = {
        "pat@example.com": USER,
        "doc@example.com": DOCTOR
    }

    monkeypatch.setattr(ah, "ddb", fake)
    monkeypatch.setattr(az, "dynamodb", fake)
    monkeypatch.setattr(ah, "Key", FakeKey)
    monkeypatch.setattr(ah, "bcrypt", FakeBcrypt)
    monkeypatch.setattr(ah, "ClientError", ClientError)

    monkeypatch.setattr(
        ah,
        "get_connection",
        lambda: FakeConn(users)
    )

    monkeypatch.setattr(ah, "get_jwt_secret", lambda: SECRET)
    monkeypatch.setattr(az, "get_jwt_secret", lambda: SECRET)

    monkeypatch.setattr(
        config,
        "get_optional_secret",
        lambda name: None
    )

    FakeBcrypt.rounds_seen.clear()

    return fake






































def call(path, body=None, headers=None, method="POST", context=None):
    event = {
        "httpMethod": method,
        "path": path,
        "body": json.dumps(body or {}),
        "headers": headers or {},
        "requestContext": {
            "authorizer": context or {}
        }
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


def authorize(token, path="patients/me/x"):
    event = {
        "headers": {
            "Authorization": "Bearer " + token
        } if token else {},

        "methodArn": (
            f"arn:aws:execute-api:ap-south-1:123:api/dev/GET/{path}"
        )
    }

    return az.lambda_handler(event, None)


def valid(token):
    try:
        authorize(token)
        return True
    except Exception:
        return False


def login(
    path="/auth/patients/login",
    email="pat@example.com",
    password="Patient@12345"
):
    status, data = call(
        path,
        {"email": email, "password": password}
    )

    assert status == 200, data

    return data


def test_login_returns_tokens_only_no_user_details():
    data = login()

    assert set(data) == {
        "access_token",
        "token_type",
        "expires_in",
        "refresh_token",
        "refresh_expires_in"
    }

    assert "user" not in data
    assert "email" not in json.dumps(data)


def test_login_stores_one_active_token_and_a_hashed_refresh_record(env):
    data = login()
    c = claims(data["access_token"])

    active = env.stores["dev-medislot-token-activelist-v2"]

    assert list(active) == ["user-1"]
    assert active["user-1"]["jti"] == c["jti"]
    assert active["user-1"]["ttl"] == c["exp"]

    refresh = env.stores["dev-medislot-refresh-tokens"][
        ah._hash_token(data["refresh_token"])
    ]

    assert set(refresh) == {
        "token_hash",
        "user_id",
        "created_at",
        "expires_at"
    }

    assert data["refresh_token"] not in json.dumps(refresh)
    assert valid(data["access_token"])


def test_second_login_replaces_the_first_access_token(env):
    first = login()["access_token"]
    second = login()["access_token"]

    assert not valid(first)
    assert valid(second)

    assert len(
        env.stores["dev-medislot-token-activelist-v2"]
    ) == 1


def test_refresh_rotates_refresh_token_and_replaces_access_token(env):
    first = login()

    status, data = call(
        "/auth/refresh",
        {"refresh_token": first["refresh_token"]}
    )

    assert status == 200, data
    assert not valid(first["access_token"])
    assert valid(data["access_token"])

    assert data["refresh_token"] != first["refresh_token"]

    assert len(
        env.stores["dev-medislot-token-activelist-v2"]
    ) == 1

    assert call(
        "/auth/refresh",
        {"refresh_token": first["refresh_token"]}
    )[0] == 401


def test_unknown_refresh_token_rejected():
    assert call(
        "/auth/refresh",
        {"refresh_token": "not-a-real-token"}
    )[0] == 401


def test_logout_removes_active_access_and_all_refresh_tokens_for_user(env):
    data = login()

    other = "another-refresh-token"

    env.stores["dev-medislot-refresh-tokens"][
        ah._hash_token(other)
    ] = {
        "token_hash": ah._hash_token(other),
        "user_id": "user-1",
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + 3600
    }

    env.stores["dev-medislot-refresh-tokens"]["foreign"] = {
        "token_hash": "foreign",
        "user_id": "someone-else",
        "expires_at": int(time.time()) + 3600
    }

    status, body = call(
        "/auth/logout",
        {"refresh_token": data["refresh_token"]},
        {"Authorization": "Bearer " + data["access_token"]}
    )

    assert status == 200, body
    assert not valid(data["access_token"])

    assert env.stores["dev-medislot-token-activelist-v2"] == {}

    assert list(
        env.stores["dev-medislot-refresh-tokens"]
    ) == ["foreign"]

    assert call(
        "/auth/refresh",
        {"refresh_token": data["refresh_token"]}
    )[0] == 401


def test_logout_requires_a_valid_token():
    assert call("/auth/logout")[0] == 401

    assert call(
        "/auth/logout",
        headers={"Authorization": "Bearer abc.def.ghi"}
    )[0] == 401


def test_tampered_missing_and_wrong_issuer_tokens_are_rejected(monkeypatch):
    token = login()["access_token"]

    assert valid(token)
    assert not valid(token[:-3] + "xyz")
    assert not valid(None)
    assert not valid("not-a-jwt")

    monkeypatch.setenv("JWT_ISSUER", "someone-else")

    assert not valid(token)


def test_expired_token_is_rejected():
    now = int(time.time())

    header = base64.urlsafe_b64encode(
        b'{"alg":"HS256","typ":"JWT"}'
    ).rstrip(b"=").decode()

    body = base64.urlsafe_b64encode(
        json.dumps({
            "sub": "user-1",
            "role": "patient",
            "iat": now - 2000,
            "exp": now - 1000,
            "jti": "j",
            "iss": "medislot"
        }).encode()
    ).rstrip(b"=").decode()

    sig = base64.urlsafe_b64encode(
        hmac.new(
            SECRET,
            f"{header}.{body}".encode(),
            hashlib.sha256
        ).digest()
    ).rstrip(b"=").decode()

    assert not valid(f"{header}.{body}.{sig}")


def test_authorizer_requires_active_list_entry(env):
    token = login()["access_token"]

    assert valid(token)

    env.stores["dev-medislot-token-activelist-v2"].pop("user-1")

    assert not valid(token)


def test_wrong_role_gets_explicit_deny_and_context_carries_identity():
    patient = login()["access_token"]

    policy = authorize(patient, "doctors/me")

    deny = [
        s for s in policy["policyDocument"]["Statement"]
        if s["Effect"] == "Deny"
    ][0]

    assert any(
        r.endswith("/doctors/me")
        for r in deny["Resource"]
    )

    assert policy["context"] == {
        "userId": "user-1",
        "role": "patient",
        "jti": claims(patient)["jti"]
    }

    doctor = login(
        "/auth/doctors/login",
        "doc@example.com",
        "Doctor@12345"
    )["access_token"]

    deny = [
        s for s in authorize(
            doctor,
            "appointments"
        )["policyDocument"]["Statement"]
        if s["Effect"] == "Deny"
    ][0]

    assert any(
        r.endswith("/appointments")
        for r in deny["Resource"]
    )


def test_role_specific_login_rejects_the_other_role():
    assert call(
        "/auth/doctors/login",
        {
            "email": "pat@example.com",
            "password": "Patient@12345"
        }
    )[0] == 401


def test_bad_password_is_401():
    assert call(
        "/auth/patients/login",
        {
            "email": "pat@example.com",
            "password": "wrong-password-1"
        }
    )[0] == 401


def test_register_uses_work_factor_12_and_rejects_weak_passwords(monkeypatch):
    monkeypatch.setattr(
        ah,
        "get_connection",
        lambda: FakeConn({})
    )

    assert call(
        "/auth/patients/register",
        {
            "email": "a@b.co",
            "password": "short1",
            "first_name": "A",
            "last_name": "B"
        }
    )[0] == 400

    status, data = call(
        "/auth/patients/register",
        {
            "email": "a@b.co",
            "password": "Passw0rd123",
            "first_name": "A",
            "last_name": "B"
        }
    )

    assert status == 201
    assert data["role"] == "patient"
    assert min(FakeBcrypt.rounds_seen) >= 12


def test_role_cannot_be_chosen_in_the_body():
    status, _ = call(
        "/auth/register",
        {
            "email": "a@b.co",
            "password": "Passw0rd123",
            "role": "DOCTOR"
        }
    )

    assert status == 403


def test_doctor_registration_code_gate(monkeypatch):
    monkeypatch.setattr(
        ah,
        "get_connection",
        lambda: FakeConn({})
    )

    monkeypatch.setattr(
        config,
        "get_optional_secret",
        lambda name: "invite-123"
    )

    body = {
        "email": "d@b.co",
        "password": "Passw0rd123"
    }

    assert call(
        "/auth/doctors/register",
        body
    )[0] == 403

    assert call(
        "/auth/doctors/register",
        dict(body, registration_code="nope")
    )[0] == 403

    assert call(
        "/auth/doctors/register",
        dict(body, registration_code="invite-123")
    )[0] == 201


def test_protected_test_token_route_requires_authorizer_context():
    assert call(
        "/auth/test-token",
        method="GET"
    )[0] == 401

    status, data = call(
        "/auth/test-token",
        method="GET",
        context={
            "userId": "user-1",
            "role": "patient"
        }
    )

    assert status == 200
    assert data["user_id"] == "user-1"
