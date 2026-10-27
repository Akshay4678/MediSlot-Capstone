import logging
from application.common.auth_utils import body_json, response, refresh_table, token_hash, utc_epoch, create_tokens

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, context):
    try:
        data = body_json(event)
        refresh_token = data.get("refresh_token")
        if not isinstance(refresh_token, str) or len(refresh_token) < 32:
            return response(401, {"message": "Invalid or expired refresh token."})

        table = refresh_table()
        hashed = token_hash(refresh_token)
        item = table.get_item(Key={"token_hash": hashed}, ConsistentRead=True).get("Item")
        now = utc_epoch()
        if not item or int(item.get("expires_at", 0)) <= now:
            if item:
                table.delete_item(Key={"token_hash": hashed})
            return response(401, {"message": "Invalid or expired refresh token."})

        # Atomically consume the old token so it cannot be used twice.
        deleted = table.delete_item(
            Key={"token_hash": hashed},
            ConditionExpression="attribute_exists(token_hash) AND expires_at > :now",
            ExpressionAttributeValues={":now": now},
            ReturnValues="ALL_OLD",
        ).get("Attributes")
        if not deleted:
            return response(401, {"message": "Invalid or expired refresh token."})

        user = {"user_id": item["user_id"], "role": item["role"]}
        access_token, new_refresh_token, new_expiry = create_tokens(user)
        return response(200, {
            "message": "Token refreshed.",
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 900,
            "refresh_token": new_refresh_token,
            "refresh_token_expires_at": new_expiry,
        })
    except ValueError as exc:
        return response(400, {"message": str(exc)})
    except Exception:
        log.exception("Refresh failed")
        return response(500, {"message": "Internal server error."})
