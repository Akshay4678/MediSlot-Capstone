import logging
import jwt

from application.common.auth_utils import (
    body_json, response, get_bearer_token, decode_access_token,
    refresh_table, token_hash, save_jti_to_denylist, utc_epoch
)

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, context):
    try:
        # API Gateway should protect this route with Lambda Authorizer too.
        access_token = get_bearer_token(event)
        if not access_token:
            return response(401, {"message": "Bearer access token is required"})

        claims = decode_access_token(access_token, check_denylist=False)
        data = body_json(event)
        refresh_token = data.get("refresh_token")

        if isinstance(refresh_token, str) and refresh_token:
            hashed = token_hash(refresh_token)
            table = refresh_table()
            item = table.get_item(Key={"token_hash": hashed}, ConsistentRead=True).get("Item")
            # Do not allow a user to revoke another user's refresh session.
            if item and str(item.get("user_id")) == str(claims["sub"]):
                table.delete_item(
                    Key={"token_hash": hashed},
                    ConditionExpression="attribute_exists(token_hash) AND user_id = :uid",
                    ExpressionAttributeValues={":uid": str(claims["sub"])},
                )

        save_jti_to_denylist(claims["jti"], int(claims["exp"]), claims["sub"])
        return response(200, {"message": "Logout successful"})
    except jwt.ExpiredSignatureError:
        return response(401, {"message": "Access token has expired"})
    except jwt.InvalidTokenError:
        return response(401, {"message": "Invalid access token"})
    except Exception:
        log.exception("Logout failed")
        return response(500, {"message": "Internal server error"})
