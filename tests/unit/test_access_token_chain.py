"""Offline tests for MediSlot's active access-token list and refresh records."""
import base64, copy, hashlib, hmac, json, os, sys, time, types
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
TABLE_KEYS = {"dev-medislot-refresh-tokens": "token_hash", "dev-medislot-token-activelist": "jti"}

class FakeSSM:
    def get_parameter(self, **kwargs): return {"Parameter": {"Value": SECRET}}

class Table:
    def __init__(self, name): self.name=name; self.key=TABLE_KEYS[name]; STORE.setdefault(name,{})
    def get_item(self, Key, ConsistentRead=False, **kwargs):
        item=STORE[self.name].get(Key[self.key]); return {"Item":copy.deepcopy(item)} if item else {}
    def put_item(self, Item, **kwargs): STORE[self.name][Item[self.key]]=copy.deepcopy(Item); return {}
    def delete_item(self, Key, **kwargs): STORE[self.name].pop(Key[self.key],None); return {}
    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None, **kwargs):
        item=STORE[self.name].get(Key[self.key])
        if not item or (ConditionExpression and "attribute_not_exists(revoked_at)" in ConditionExpression and "revoked_at" in item):
            raise ClientError({"Error":{"Code":"ConditionalCheckFailedException"}}, "UpdateItem")
        if UpdateExpression.startswith("SET revoked_at = :now"):
            item["revoked_at"]=ExpressionAttributeValues[":now"]
        return {}

class FakeDDB:
    def Table(self, name): return Table(name)
boto3=types.ModuleType("boto3"); boto3.client=lambda *a,**k:FakeSSM(); boto3.resource=lambda *a,**k:FakeDDB(); sys.modules["boto3"]=boto3
bc=types.ModuleType("bcrypt"); bc.hashpw=lambda pw,salt:b"HASH:"+pw; bc.gensalt=lambda **kwargs:b"salt"; bc.checkpw=lambda pw,h:h==b"HASH:"+pw; sys.modules["bcrypt"]=bc
pm=types.ModuleType("pymysql"); pm.cursors=types.SimpleNamespace(DictCursor=object); sys.modules["pymysql"]=pm
os.environ.update(JWT_SECRET_PARAMETER_NAME="/test/jwt", REFRESH_TOKEN_TABLE="dev-medislot-refresh-tokens", TOKEN_ACTIVELIST_TABLE="dev-medislot-token-activelist", JWT_ISSUER="medislot")

import application.auth.handler as ah
import application.authorizer.lambda_handler as az

USERS={"pat@example.com":dict(user_id="user-1",email="pat@example.com",password_hash="HASH:Patient@12345",role="PATIENT",is_active=True)}
class Cursor:
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def execute(self, sql, args=()):
        if "WHERE email = %s" in sql: self.row=USERS.get(args[0])
        elif "WHERE user_id = %s" in sql: self.row=next((u for u in USERS.values() if u["user_id"]==args[0]),None)
        else: self.row=None
    def fetchone(self): return self.row
class Connection:
    def cursor(self): return Cursor()
    def close(self): pass
    def commit(self): pass
    def rollback(self): pass
ah.get_connection=lambda:Connection()

def call(path, body=None, headers=None, method="POST", context=None):
    event={"requestContext":{"http":{"method":method},"authorizer":context or {}},"rawPath":path,"body":json.dumps(body or {}),"headers":headers or {}}
    result=ah.lambda_handler(event,None); return result["statusCode"],json.loads(result["body"])
def claims(token):
    part=token.split(".")[1]; return json.loads(base64.urlsafe_b64decode(part+"="*((4-len(part)%4)%4)))
def valid(token):
    return az.lambda_handler({"version":"2.0","headers":{"authorization":"Bearer "+token}},None).get("isAuthorized") is True
def login():
    status,data=call("/auth/login",{"email":"pat@example.com","password":"Patient@12345"}); assert status==200,data; return data["access_token"],data["refresh_token"]
def reset():
    STORE.clear(); STORE["dev-medislot-refresh-tokens"]={}; STORE["dev-medislot-token-activelist"]={}

def test_login_stores_refresh_schema_and_active_jti():
    reset(); access,refresh=login(); c=claims(access)
    refresh_record=STORE["dev-medislot-refresh-tokens"][ah._hash_token(refresh)]
    assert set(refresh_record)=={"token_hash","user_id","created_at","expires_at"}
    assert c["jti"] in STORE["dev-medislot-token-activelist"] and valid(access)
    assert STORE["dev-medislot-token-activelist"][c["jti"]]["ttl"]==c["exp"]

def test_refresh_issues_new_active_token_without_jti_in_refresh_record():
    reset(); old,refresh=login(); status,data=call("/auth/refresh",{"refresh_token":refresh}); assert status==200,data
    new=data["access_token"]
    assert valid(old) and valid(new)  # no refresh-record JTI means no per-session association
    assert claims(old)["jti"] in STORE["dev-medislot-token-activelist"]
    assert claims(new)["jti"] in STORE["dev-medislot-token-activelist"]
    assert "current_jti" not in STORE["dev-medislot-refresh-tokens"][ah._hash_token(refresh)]

def test_expired_or_unknown_refresh_token_rejected():
    reset(); status,_=call("/auth/refresh",{"refresh_token":"not-a-real-token"}); assert status==401

def test_logout_revokes_refresh_and_removes_presented_access_jti():
    reset(); access,refresh=login(); token_jti=claims(access)["jti"]
    status,data=call("/auth/logout",{"refresh_token":refresh},{"authorization":"Bearer "+access}); assert status==200,data
    assert not valid(access)
    record=STORE["dev-medislot-refresh-tokens"][ah._hash_token(refresh)]
    assert record["revoked_at"] > 0
    assert call("/auth/refresh",{"refresh_token":refresh})[0]==401
    assert token_jti not in STORE["dev-medislot-token-activelist"]

def test_tampered_token_rejected():
    reset(); access,_=login(); assert valid(access); assert not valid(access[:-3]+"xyz")

def test_protected_dummy_endpoint_requires_authorizer_context():
    reset(); assert call("/auth/test-access",method="GET")[0]==401
    status,data=call("/auth/test-access",method="GET",context={"lambda":{"userId":"user-1","role":"patient"}})
    assert status==200 and data["user_id"]=="user-1"

def test_authorizer_requires_active_list_entry():
    reset(); access,_=login(); jti=claims(access)["jti"]; assert valid(access)
    STORE["dev-medislot-token-activelist"].pop(jti); assert not valid(access)
