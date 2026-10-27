"""MediSlot auth routes: register, login, refresh, logout.
Environment:
DATABASE_SECRET_ARN, DB_HOST, DB_NAME, JWT_SECRET_PARAMETER_NAME,
REFRESH_TOKEN_TABLE, TOKEN_DENYLIST_TABLE, AWS_REGION, optional JWT_ISSUER.
Requires bcrypt. Uses RDS users/patients tables and DynamoDB token-state tables.
"""
import base64, hashlib, hmac, json, os, re, secrets, time, uuid
from datetime import datetime, timezone
import boto3
import bcrypt
from application.common.http import response, request_method, request_path, request_body
from application.database.connection import get_connection

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACCESS_TTL = 900
REFRESH_TTL = 7 * 24 * 60 * 60

def _secret():
    name = os.environ["JWT_SECRET_PARAMETER_NAME"]
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].encode()

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _jwt(payload):
    header = _b64(json.dumps({"alg":"HS256","typ":"JWT"}, separators=(",",":")).encode())
    body = _b64(json.dumps(payload, separators=(",",":"), default=str).encode())
    signing = f"{header}.{body}".encode()
    sig = _b64(hmac.new(_secret(), signing, hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"

def _issue_access(user):
    now = int(time.time())
    return _jwt({
        "sub": str(user["user_id"]), "role": str(user["role"]).lower(),
        "iat": now, "exp": now + ACCESS_TTL, "jti": str(uuid.uuid4()),
        "iss": os.environ.get("JWT_ISSUER", "medislot")
    })

def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def _table(env_name):
    name = os.environ.get(env_name)
    if not name:
        raise RuntimeError(f"Missing required environment variable {env_name}")
    return ddb.Table(name)

def _json_error(status, code, message):
    return response(status, {"error": code, "message": message})

def _register(body):
    email = str(body.get("email","")).strip().lower()
    password = body.get("password")
    role = str(body.get("role","PATIENT")).upper()
    if not EMAIL_RE.match(email):
        return _json_error(400, "INVALID_EMAIL", "Provide a valid email address.")
    if not isinstance(password, str) or len(password) < 10 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return _json_error(400, "WEAK_PASSWORD", "Password must be at least 10 characters and contain a letter and a digit.")
    # Self-registration is limited to patients. Doctor/admin accounts must be provisioned by an administrator.
    if role != "PATIENT":
        return _json_error(403, "ROLE_NOT_ALLOWED", "Self-registration is only available for patient accounts.")
    first = str(body.get("first_name","")).strip()
    last = str(body.get("last_name","")).strip()
    if not first or not last:
        return _json_error(400, "MISSING_NAME", "first_name and last_name are required.")
    user_id, patient_id = str(uuid.uuid4()), str(uuid.uuid4())
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE email=%s LIMIT 1", (email,))
            if cur.fetchone():
                return _json_error(409, "EMAIL_EXISTS", "An account with this email already exists.")
            cur.execute(
                "INSERT INTO users (user_id,email,password_hash,role,is_active,email_verified) VALUES (%s,%s,%s,'PATIENT',TRUE,FALSE)",
                (user_id,email,password_hash))
            cur.execute(
                "INSERT INTO patients (patient_id,user_id,first_name,last_name,phone) VALUES (%s,%s,%s,%s,%s)",
                (patient_id,user_id,first,last,str(body.get("phone","")).strip() or None))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return response(201, {"message":"Registration successful.","user_id":user_id,"role":"patient"})

def _login(body):
    email = str(body.get("email","")).strip().lower()
    password = body.get("password")
    if not email or not isinstance(password,str):
        return _json_error(400,"MISSING_CREDENTIALS","email and password are required.")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id,email,password_hash,role,is_active FROM users WHERE email=%s LIMIT 1",(email,))
            user = cur.fetchone()
    finally:
        conn.close()
    if not user or not user.get("is_active") or not bcrypt.checkpw(password.encode(), str(user["password_hash"]).encode()):
        return _json_error(401,"INVALID_CREDENTIALS","Email or password is incorrect.")
    access = _issue_access(user)
    refresh = secrets.token_urlsafe(48)
    now = int(time.time())
    _table("REFRESH_TOKEN_TABLE").put_item(Item={
        "token_hash": _hash_token(refresh), "user_id": str(user["user_id"]),
        "role": str(user["role"]).lower(), "created_at": now,
        "expires_at": now + REFRESH_TTL, "ttl": now + REFRESH_TTL, "revoked": False
    })
    return response(200, {
        "access_token":access,"token_type":"Bearer","expires_in":ACCESS_TTL,
        "refresh_token":refresh,"refresh_expires_in":REFRESH_TTL,
        "user":{"user_id":str(user["user_id"]),"email":user["email"],"role":str(user["role"]).lower()}
    })

def _refresh(body):
    token = body.get("refresh_token")
    if not isinstance(token,str) or not token:
        return _json_error(400,"MISSING_REFRESH_TOKEN","refresh_token is required.")
    table = _table("REFRESH_TOKEN_TABLE")
    key = _hash_token(token)
    result = table.get_item(Key={"token_hash":key}, ConsistentRead=True)
    item = result.get("Item")
    now = int(time.time())
    if not item or item.get("revoked") or int(item.get("expires_at",0)) <= now:
        return _json_error(401,"INVALID_REFRESH_TOKEN","Refresh token is invalid, expired, or revoked.")
    # Rotate the refresh token so an old token cannot be reused.
    new_refresh = secrets.token_urlsafe(48)
    try:
        table.delete_item(Key={"token_hash":key}, ConditionExpression="attribute_exists(token_hash)")
        table.put_item(Item={
            "token_hash":_hash_token(new_refresh),"user_id":item["user_id"],"role":item["role"],
            "created_at":now,"expires_at":now+REFRESH_TTL,"ttl":now+REFRESH_TTL,"revoked":False
        }, ConditionExpression="attribute_not_exists(token_hash)")
    except Exception:
        return _json_error(401,"REFRESH_REPLAY_OR_RACE","Refresh token was already used. Please log in again.")
    return response(200, {
        "access_token":_jwt({"sub":str(item["user_id"]),"role":str(item["role"]).lower(),
            "iat":now,"exp":now+ACCESS_TTL,"jti":str(uuid.uuid4()),
            "iss":os.environ.get("JWT_ISSUER","medislot")}),
        "token_type":"Bearer","expires_in":ACCESS_TTL,
        "refresh_token":new_refresh,"refresh_expires_in":REFRESH_TTL
    })

def _logout(event, body):
    token = body.get("refresh_token")
    if not isinstance(token,str) or not token:
        return _json_error(400,"MISSING_REFRESH_TOKEN","refresh_token is required.")
    _table("REFRESH_TOKEN_TABLE").update_item(
        Key={"token_hash":_hash_token(token)},
        UpdateExpression="SET revoked = :r",
        ExpressionAttributeValues={":r":True},
        ConditionExpression="attribute_exists(token_hash)"
    )
    # If an access token was supplied, denylist its jti until its exp.
    auth = event.get("headers",{}).get("authorization") or event.get("headers",{}).get("Authorization","")
    if auth.lower().startswith("bearer "):
        parts = auth.split()[1].split(".")
        if len(parts) == 3:
            try:
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                jti, exp = payload.get("jti"), int(payload.get("exp",0))
                if jti and exp > int(time.time()):
                    _table("TOKEN_DENYLIST_TABLE").put_item(Item={"jti":jti,"expires_at":exp,"ttl":exp})
            except Exception:
                pass
    return response(200,{"message":"Logged out successfully."})

def lambda_handler(event, context):
    method = (event.get("requestContext",{}).get("http",{}).get("method") or event.get("httpMethod","GET")).upper()
    path = event.get("rawPath") or event.get("path") or "/"
    try:
        body = request_body(event) if method in ("POST","PUT","PATCH") else {}
    except ValueError as exc:
        return _json_error(400,"INVALID_JSON",str(exc))
    try:
        if method != "POST":
            return response(405,{"error":"METHOD_NOT_ALLOWED"})
        if path.endswith("/auth/register") or path.endswith("/register"):
            return _register(body)
        if path.endswith("/auth/login") or path.endswith("/login"):
            return _login(body)
        if path.endswith("/auth/refresh") or path.endswith("/refresh"):
            return _refresh(body)
        if path.endswith("/auth/logout") or path.endswith("/logout"):
            return _logout(event,body)
        return response(404,{"error":"ROUTE_NOT_FOUND"})
    except Exception:
        # Full details are intentionally not returned to clients.
        print("Authentication request failed; inspect CloudWatch logs for the exception.")
        return response(500,{"error":"INTERNAL_SERVER_ERROR","message":"Authentication service failed."})
