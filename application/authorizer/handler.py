"""API Gateway HTTP API Lambda authorizer using HS256 JWTs.
Configure JWT_SECRET_PARAMETER_NAME, TOKEN_DENYLIST_TABLE, JWT_ISSUER.
Expected DynamoDB denylist partition key: jti (String).
"""
import base64, hashlib, hmac, json, os, time
import boto3

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")

def _b64decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

def _secret():
    name = os.environ["JWT_SECRET_PARAMETER_NAME"]
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].encode()

def _denylisted(jti):
    table_name = os.environ.get("TOKEN_DENYLIST_TABLE")
    if not table_name:
        raise RuntimeError("TOKEN_DENYLIST_TABLE is not configured")
    item = ddb.Table(table_name).get_item(Key={"jti":jti}, ConsistentRead=True).get("Item")
    return bool(item and int(item.get("expires_at",0)) > int(time.time()))

def _verify(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT")
    signing = f"{parts[0]}.{parts[1]}".encode()
    expected = hmac.new(_secret(), signing, hashlib.sha256).digest()
    supplied = _b64decode(parts[2])
    if not hmac.compare_digest(expected,supplied):
        raise ValueError("Invalid signature")
    header = json.loads(_b64decode(parts[0]))
    claims = json.loads(_b64decode(parts[1]))
    if header.get("alg") != "HS256":
        raise ValueError("Unsupported JWT algorithm")
    now = int(time.time())
    if int(claims.get("exp",0)) <= now or int(claims.get("iat",0)) > now + 60:
        raise ValueError("Expired or invalid token time")
    if claims.get("iss") != os.environ.get("JWT_ISSUER","medislot"):
        raise ValueError("Invalid issuer")
    if not claims.get("sub") or claims.get("role") not in ("patient","doctor","admin") or not claims.get("jti"):
        raise ValueError("Required claims missing")
    if _denylisted(claims["jti"]):
        raise ValueError("Token revoked")
    return claims

def lambda_handler(event, context):
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise Exception("Unauthorized")
    try:
        claims = _verify(auth.split(None,1)[1].strip())
    except Exception as exc:
        print(f"Authorizer rejected token: {type(exc).__name__}")
        raise Exception("Unauthorized")
    # HTTP API Lambda authorizer simple response format; enable simple responses in API config.
    if event.get("version") == "2.0":
        return {
            "isAuthorized": True,
            "context": {
                "userId": str(claims["sub"]),
                "role": str(claims["role"]),
                "jti": str(claims["jti"])
            }
        }
    # REST API / IAM-policy authorizer response format.
    method_arn = event.get("methodArn") or event.get("routeArn","*")
    resource_arn = method_arn.rsplit("/",2)[0] + "/*/*" if "/" in method_arn else "*"
    return {
        "principalId":str(claims["sub"]),
        "policyDocument":{"Version":"2012-10-17","Statement":[{
            "Action":"execute-api:Invoke","Effect":"Allow","Resource":resource_arn
        }]},
        "context":{"userId":str(claims["sub"]),"role":str(claims["role"]),"jti":str(claims["jti"])}
    }
