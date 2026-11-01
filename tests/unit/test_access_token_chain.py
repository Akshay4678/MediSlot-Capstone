"""Offline tests for the ERD-aligned jti-keyed active-token list and 12-hour refresh tokens."""
import base64
import copy
import json
import os
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class ClientError(Exception):
    def __init__(self, response, operation_name):
        self.response = response
        super().__init__(str(response))

for name in ("botocore", "botocore.config", "botocore.exceptions"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["botocore.config"].Config = lambda **kwargs: kwargs
sys.modules["botocore.exceptions"].ClientError = ClientError

SECRET = "test-secret-" * 5
STORE = {}
TABLE_KEYS = {"dev-medislot-refresh-tokens-m3": "token_hash", "dev-medislot-token-activelist-m3": "jti"}

class FakeSSM:
    def get_parameter(self, **kwargs):
        return {"Parameter": {"Value": SECRET}}

class FakeAttr:
    def __init__(self, name): self.name = name
    def eq(self, value): return (self.name, value)

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
    def delete_item(self, Key, **kwargs):
        STORE[self.name].pop(Key[self.key], None)
        return {}
    def scan(self, FilterExpression=None, **kwargs):
        items = list(STORE[self.name].values())
        if isinstance(FilterExpression, tuple):
            field, value = FilterExpression
            items = [x for x in items if str(x.get(field)) == str(value)]
        return {"Items": copy.deepcopy(items)}

class FakeDDB:
    def Table(self, name): return Table(name)

boto3 = types.ModuleType("boto3")
boto3.client = lambda *a, **k: FakeSSM()
boto3.resource = lambda *a, **k: FakeDDB()
sys.modules["boto3"] = boto3
boto3_dynamodb = types.ModuleType("boto3.dynamodb")
boto3_conditions = types.ModuleType("boto3.dynamodb.conditions")
class FakeKey:
    def __init__(self, name): self.name = name
    def eq(self, value): return (self.name, value)
boto3_conditions.Key = FakeKey
boto3_conditions.Attr = FakeAttr
sys.modules["boto3.dynamodb"] = boto3_dynamodb
sys.modules["boto3.dynamodb.conditions"] = boto3_conditions

bcrypt = types.ModuleType("bcrypt")
bcrypt.hashpw = lambda pw, salt: b"HASH:" + pw
bcrypt.gensalt = lambda **kwargs: b"salt"
bcrypt.checkpw = lambda pw, hashed: hashed == b"HASH:" + pw
sys.modules["bcrypt"] = bcrypt
pymysql = types.ModuleType("pymysql")
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

USERS = {"pat@example.com": dict(user_id=1001, email="pat@example.com", password_hash="HASH:Patient@12345", role="PATIENT", is_active=True)}

class Cursor:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, args=()):
        if "WHERE email = %s" in sql:
            self.row = USERS.get(args[0])
        elif "WHERE user_id = %s" in sql:
            self.row = next((u for u in USERS.values() if str(u["user_id"]) == str(args[0])), None)
        else:
            self.row = None
    def fetchone(self): return self.row
class Connection:
    def cursor(self): return Cursor()
    def close(self): pass
    def commit(self): pass
    def rollback(self): pass
ah.get_connection = lambda: Connection()


def call(path, body=None, headers=None, method="POST", context=None):
    event = {"requestContext": {"http": {"method": method}, "authorizer": context or {}},
             "rawPath": path, "body": json.dumps(body or {}), "headers": headers or {}}
    result = ah.lambda_handler(event, None)
    return result["statusCode"], json.loads(result["body"])


def claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * ((4-len(part)%4)%4)))


def valid(token):
    return az.lambda_handler({"version":"2.0", "headers":{"authorization":"Bearer "+token}}, None).get("isAuthorized") is True


def login():
    status, data = call("/auth/login", {"email":"pat@example.com", "password":"Patient@12345"})
    assert status == 200 and "user" not in data, data
    return data["access_token"], data["refresh_token"]


def reset():
    STORE.clear()
    STORE["dev-medislot-refresh-tokens-m3"] = {}
    STORE["dev-medislot-token-activelist-m3"] = {}


def test_login_stores_one_active_token_and_12_hour_refresh_record():
    reset(); access, refresh = login(); token_claims = claims(access)
    refresh_record = STORE["dev-medislot-refresh-tokens-m3"][ah._hash_token(refresh)]
    created = datetime.fromisoformat(refresh_record["created_at"])
    expires = datetime.fromisoformat(refresh_record["expires_at"])
    assert set(refresh_record) == {"token_hash", "user_id", "created_at", "expires_at", "revoked_at"}
    assert len(STORE["dev-medislot-token-activelist-m3"]) == 1
    active = STORE["dev-medislot-token-activelist-m3"][token_claims["jti"]]
    assert active["user_id"] == 1001 and active["ttl"] == token_claims["exp"]
    assert (expires-created).total_seconds() == 12 * 60 * 60
    assert valid(access)


def test_refresh_replaces_previous_active_access_token():
    reset(); old, refresh = login()
    status, data = call("/auth/refresh", {"refresh_token":refresh})
    assert status == 200, data
    new = data["access_token"]
    assert not valid(old) and valid(new)
    assert len(STORE["dev-medislot-token-activelist-m3"]) == 1
    assert STORE["dev-medislot-token-activelist-m3"][claims(new)["jti"]]["user_id"] == 1001


def test_expired_or_unknown_refresh_token_rejected():
    reset()
    assert call("/auth/refresh", {"refresh_token":"not-a-real-token"})[0] == 401


def test_logout_removes_active_access_and_all_refresh_tokens_for_user():
    reset(); access, refresh = login()
    second = "another-refresh-token"
    now = int(time.time())
    STORE["dev-medislot-refresh-tokens-m3"][ah._hash_token(second)] = {
        "token_hash": ah._hash_token(second), "user_id":1001,
        "created_at":datetime.fromtimestamp(now,timezone.utc).isoformat(),
        "expires_at":datetime.fromtimestamp(now+43200,timezone.utc).isoformat(), "revoked_at":""
    }
    status, data = call("/auth/logout", {}, {"authorization":"Bearer "+access})
    assert status == 200, data
    assert not valid(access)
    assert STORE["dev-medislot-refresh-tokens-m3"] == {}
    assert STORE["dev-medislot-token-activelist-m3"] == {}
    assert call("/auth/refresh", {"refresh_token":refresh})[0] == 401


def test_tampered_token_rejected():
    reset(); access, _ = login()
    assert valid(access)
    assert not valid(access[:-3] + "xyz")


def test_protected_dummy_endpoint_requires_authorizer_context():
    reset()
    assert call("/auth/test-access", method="GET")[0] == 401
    status, data = call("/auth/test-access", method="GET", context={"lambda":{"userId":"1001","role":"patient"}})
    assert status == 200 and data["user_id"] == "1001"


def test_authorizer_requires_active_list_entry():
    reset(); access, _ = login(); jti = claims(access)["jti"]
    assert valid(access)
    STORE["dev-medislot-token-activelist-m3"].pop(jti)
    assert not valid(access)
