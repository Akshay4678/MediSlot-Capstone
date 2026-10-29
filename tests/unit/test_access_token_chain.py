"""Unit tests: only the NEWEST access token of a login is valid.

Login remembers the access token's jti on the refresh-token record. Every
refresh denylists the previous jti and records the new one; logout denylists
the newest jti and deletes the refresh token. Denylist entries expire with
the token they describe.

Runs offline: DynamoDB, SSM, MySQL and bcrypt are in-memory fakes; the real
authorizer and auth-handler code runs.   python -m pytest tests/unit
"""
import base64, copy, hashlib, hmac, json, os, sys, time, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

# ---------------- fakes for libraries not available offline ----------------
class ClientError(Exception):
    def __init__(self, resp, op):
        self.response = resp
        super().__init__(str(resp))

for _n in ("botocore", "botocore.config", "botocore.exceptions"):
    sys.modules[_n] = types.ModuleType(_n)
sys.modules["botocore.config"].Config = lambda **k: k
sys.modules["botocore.exceptions"].ClientError = ClientError

SECRET = "s3cret-" * 8
STORE = {}                      # table name -> {key: item}
HOOKS = {}                      # name -> callable run at a chosen moment
FAIL_PUT = set()                # tables whose put_item raises
KEYS = {"dev-medislot-refresh-tokens": "token_hash", "dev-medislot-token-denylist": "jti"}


class FakeSSM:
    def get_parameter(self, **k):
        return {"Parameter": {"Value": SECRET}}


def _cond_fail():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


class Table:
    def __init__(self, name):
        self.name, self.k = name, KEYS[name]
        STORE.setdefault(name, {})

    def get_item(self, Key, ConsistentRead=False, **kw):
        assert ConsistentRead is True, "revocation/refresh reads must be strongly consistent"
        item = STORE[self.name].get(Key[self.k])
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, **kw):
        if self.name in FAIL_PUT:
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "PutItem")
        STORE[self.name][Item[self.k]] = copy.deepcopy(Item)

    def delete_item(self, Key, ReturnValues=None, **kw):
        if "before_delete" in HOOKS:
            HOOKS.pop("before_delete")()
        old = STORE[self.name].pop(Key[self.k], None)
        return {"Attributes": old} if (old and ReturnValues == "ALL_OLD") else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None, **kw):
        if "before_update" in HOOKS:
            HOOKS["before_update"]()          # may fire every time or pop itself
        item = STORE[self.name].get(Key[self.k])
        c = ConditionExpression or ""
        if "attribute_exists(token_hash)" in c and not item:
            raise _cond_fail()
        if "current_jti = :old" in c and item.get("current_jti") != ExpressionAttributeValues[":old"]:
            raise _cond_fail()
        if "attribute_not_exists(current_jti)" in c and "current_jti" in item:
            raise _cond_fail()
        for part in UpdateExpression[len("SET "):].split(", "):
            lhs, val = [x.strip() for x in part.split("=")]
            item[lhs] = ExpressionAttributeValues[val]
        return {}


class FakeDDB:
    def Table(self, n):
        return Table(n)


boto3 = types.ModuleType("boto3")
boto3.client = lambda *a, **k: FakeSSM()
boto3.resource = lambda *a, **k: FakeDDB()
sys.modules["boto3"] = boto3
bc = types.ModuleType("bcrypt")
bc.checkpw = lambda pw, h: h == b"HASH:" + pw
sys.modules["bcrypt"] = bc
pm = types.ModuleType("pymysql")
pm.cursors = types.SimpleNamespace(DictCursor=object)
sys.modules["pymysql"] = pm
os.environ.update(
    JWT_SECRET_PARAMETER_NAME="/p",
    REFRESH_TOKEN_TABLE="dev-medislot-refresh-tokens",
    TOKEN_DENYLIST_TABLE="dev-medislot-token-denylist",
)

import application.authorizer.lambda_handler as az   # noqa: E402
import application.auth.handler as ah                 # noqa: E402

USERS = {
    "pat@x.com": dict(user_id="user-1", email="pat@x.com", password_hash="HASH:Patient@12345", role="PATIENT", is_active=True),
    "doc@x.com": dict(user_id="user-2", email="doc@x.com", password_hash="HASH:Doctor@12345", role="DOCTOR", is_active=True),
}


class Cur:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, args=()): self.r = USERS.get(args[0])
    def fetchone(self): return self.r


class Conn:
    def cursor(self): return Cur()
    def close(self): pass


ah.get_connection = lambda: Conn()


# ---------------- helpers ----------------
def call(path, body, headers=None):
    r = ah.lambda_handler({"requestContext": {"http": {"method": "POST"}}, "rawPath": path,
                           "body": json.dumps(body), "headers": headers or {}}, None)
    return r["statusCode"], json.loads(r["body"])


def valid(tok):
    return az.lambda_handler({"version": "2.0", "headers": {"authorization": "Bearer " + tok}}, None).get("isAuthorized") is True


def claims(t):
    return json.loads(base64.urlsafe_b64decode(t.split(".")[1] + "=="))


def login(email="pat@x.com", pw="Patient@12345"):
    c, d = call("/auth/login", {"email": email, "password": pw})
    assert c == 200, d
    return d["access_token"], d["refresh_token"]


def record(refresh_token):
    return STORE["dev-medislot-refresh-tokens"][ah._hash_token(refresh_token)]


DENY = lambda: STORE["dev-medislot-token-denylist"]


def reset():
    STORE.clear(); HOOKS.clear(); FAIL_PUT.clear()
    STORE["dev-medislot-refresh-tokens"] = {}; STORE["dev-medislot-token-denylist"] = {}


# ---------------- tests ----------------
def test_ten_tokens_nine_denylisted_one_valid():
    reset()
    tokens = []
    a, r = login(); tokens.append(a)
    for _ in range(9):                                   # 1 login + 9 refreshes = 10 tokens
        c, d = call("/auth/refresh", {"refresh_token": r}); assert c == 200
        tokens.append(d["access_token"])
    assert len(set(claims(t)["jti"] for t in tokens)) == 10, "every token has its own jti"
    assert [valid(t) for t in tokens] == [False] * 9 + [True], "only the newest token is valid"
    assert len(DENY()) == 9 and claims(tokens[-1])["jti"] not in DENY()
    assert record(r)["current_jti"] == claims(tokens[-1])["jti"]


def test_denylist_entries_expire_with_their_token():
    reset()
    a, r = login(); call("/auth/refresh", {"refresh_token": r})
    entry = DENY()[claims(a)["jti"]]
    assert entry["expires_at"] == claims(a)["exp"] and entry["ttl"] == claims(a)["exp"]
    assert claims(a)["exp"] - claims(a)["iat"] == 900, "15-minute access tokens"


def test_logout_denies_the_latest_token_and_kills_refresh():
    reset()
    a, r = login(); _, d = call("/auth/refresh", {"refresh_token": r}); latest = d["access_token"]
    c, _ = call("/auth/logout", {"refresh_token": r})            # NO bearer sent
    assert c == 200
    assert not valid(latest), "latest token rejected after logout (found via the refresh record)"
    assert not valid(a)
    assert call("/auth/refresh", {"refresh_token": r})[0] == 401
    assert ah._hash_token(r) not in STORE["dev-medislot-refresh-tokens"]


def test_logout_twice_is_ok_and_two_logins_are_independent():
    reset()
    a1, r1 = login(); a2, r2 = login()
    assert call("/auth/logout", {"refresh_token": r1})[0] == 200
    assert call("/auth/logout", {"refresh_token": r1})[0] == 200
    assert not valid(a1) and valid(a2)
    assert call("/auth/refresh", {"refresh_token": r2})[0] == 200


def _sign(payload, secret):
    enc = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    h = enc(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    b = enc(json.dumps(payload).encode())
    return f"{h}.{b}." + enc(hmac.new(secret.encode(), f"{h}.{b}".encode(), hashlib.sha256).digest())


def test_logout_requires_refresh_token_and_ignores_forged_bearer():
    reset()
    assert call("/auth/logout", {})[0] == 400
    a, r = login()
    now = int(time.time())
    # Signed with the WRONG secret: an attacker must not be able to write
    # arbitrary jti values into the denylist through the logout endpoint.
    forged = _sign({"sub": "x", "role": "patient", "iat": now, "exp": now + 900,
                    "jti": "attacker-chosen-jti", "iss": "medislot"}, "wrong-secret")
    c, _ = call("/auth/logout", {"refresh_token": r}, {"authorization": "Bearer " + forged})
    assert c == 200
    assert "attacker-chosen-jti" not in DENY()
    assert not valid(a), "the real latest token was still revoked via the refresh record"


def test_tampered_or_unknown_tokens_rejected():
    reset()
    a, _ = login()
    assert valid(a)
    assert not valid(a[:-3] + "xyz")


def test_race_two_refreshes_leave_exactly_one_live_token():
    reset()
    a, r = login()
    state = {"n": 0}

    def other_refresh_wins_first():
        # Simulates a concurrent refresh that completes between our read and write.
        if state["n"] == 0:
            state["n"] = 1
            rec = record(r)
            rec["current_jti"], rec["current_jti_exp"] = "rival-token-jti", int(time.time()) + 900

    HOOKS["before_update"] = other_refresh_wins_first
    c, d = call("/auth/refresh", {"refresh_token": r})
    assert c == 200
    new = d["access_token"]
    assert valid(new) and not valid(a)
    assert "rival-token-jti" in DENY(), "the rival's token is denylisted, not left live"
    assert record(r)["current_jti"] == claims(new)["jti"]


def test_persistent_conflict_returns_409_not_two_live_tokens():
    reset()
    a, r = login()
    counter = {"i": 0}

    def always_interleaved():
        counter["i"] += 1
        rec = record(r); rec["current_jti"] = f"rival-{counter['i']}"; rec["current_jti_exp"] = int(time.time()) + 900

    HOOKS["before_update"] = always_interleaved
    c, d = call("/auth/refresh", {"refresh_token": r})
    assert c == 409 and d["error"] == "REFRESH_CONFLICT"


def test_race_refresh_slips_in_between_logout_steps():
    reset()
    a, r = login()

    def refresh_sneaks_in():
        rec = record(r); rec["current_jti"], rec["current_jti_exp"] = "sneaky-jti", int(time.time()) + 900

    HOOKS["before_delete"] = refresh_sneaks_in
    assert call("/auth/logout", {"refresh_token": r})[0] == 200
    assert "sneaky-jti" in DENY(), "token created during the logout race is also denylisted"
    assert call("/auth/refresh", {"refresh_token": r})[0] == 401


def test_fail_closed_when_denylist_write_fails():
    reset()
    a, r = login()
    FAIL_PUT.add("dev-medislot-token-denylist")
    c, _ = call("/auth/refresh", {"refresh_token": r}); assert c == 500
    assert valid(a), "refresh failed -> old token is still the one valid token"
    c, _ = call("/auth/logout", {"refresh_token": r}); assert c == 500, "never a false 'Logged out'"
    assert ah._hash_token(r) in STORE["dev-medislot-refresh-tokens"], "refresh token kept so logout can be retried"
    FAIL_PUT.clear()
    assert call("/auth/logout", {"refresh_token": r})[0] == 200 and not valid(a)


def test_login_record_created_before_this_feature_still_refreshes():
    reset()
    a, r = login()
    rec = record(r); rec.pop("current_jti"); rec.pop("current_jti_exp")      # legacy record
    c, d = call("/auth/refresh", {"refresh_token": r}); assert c == 200
    assert valid(d["access_token"]) and record(r)["current_jti"] == claims(d["access_token"])["jti"]


def test_refresh_token_never_stored_in_clear():
    reset()
    _, r = login()
    assert r not in json.dumps(STORE)
