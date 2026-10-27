import logging
from application.common.auth_utils import (
    body_json, response, get_bearer_token, decode_access_token,
    save_jti_to_denylist, refresh_table, token_hash, utc_epoch
)

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, context):
    try:
        token = get_bearer_token(event)
        if not token:
            return response(401, {"message": "Bearer access token is required."})
        claims = decode_access_token(token, check_denylist=True)
        data = body_json(event)
        refresh_token = data.get("refresh_token")

        save_jti_to_denylist(claims["jti"], claims["exp"], claims["sub"])

        if refresh_token:
            if not isinstance(refresh_token, str):
                return response(400, {"message": "refresh_token must be a string."})
            hashed = token_hash(refresh_token)
            table = refresh_table()
            item = table.get_item(Key={"token_hash": hashed}, ConsistentRead=True).get("Item")
            # Only revoke a refresh token owned by the authenticated user.
            if item and str(item.get("user_id")) == str(claims["sub"]):
                table.delete_item(
                    Key={"token_hash": hashed},
                    ConditionExpression="user_id = :uid",
                    ExpressionAttributeValues={":uid": str(claims["sub"])},
                )
        return response(200, {"message": "Logout successful."})
    except ValueError as exc:
        return response(400, {"message": str(exc)})
    except Exception:
        log.exception("Logout failed")
        return response(401, {"message": "Invalid or expired access token."})
