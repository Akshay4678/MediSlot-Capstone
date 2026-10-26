"""Shared helpers for API Gateway HTTP API payload format 2.0."""
import json

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }

def request_method(event):
    return (
        event.get("requestContext", {})
        .get("http", {})
        .get("method", "GET")
        .upper()
    )

def request_path(event):
    return event.get("rawPath", "/")

def request_body(event):
    raw = event.get("body")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("Request body must be valid JSON.")
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object.")
    return value

def not_implemented(feature):
    return response(501, {
        "error": "NOT_IMPLEMENTED",
        "message": f"{feature} business logic is not implemented yet."
    })
