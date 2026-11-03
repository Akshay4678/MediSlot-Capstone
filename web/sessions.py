"""Server-side sessions.

The browser only receives a signed cookie holding a random session id. The JWT access token
and the refresh token stay on the server. The store lives in this process's memory, so
gunicorn MUST run exactly one worker (with threads): two workers would each hold different
sessions and logins would appear to fail at random. Restarting the service logs users out.

The CSRF token is NOT kept here. It lives in the signed Flask cookie, so it also protects the
login and registration forms, which are submitted before any server-side session exists.
"""

import base64
import hmac
import json
import secrets
import threading
import time

from flask import session as cookie

IDLE_SECONDS = 12 * 60 * 60
_STORE = {}
_LOCK = threading.Lock()


def _purge(now):
    for sid in [k for k, v in _STORE.items() if v["expires"] <= now]:
        del _STORE[sid]


def role_from_token(token):
    """Read the role claim for UI routing. The API re-verifies every token on every call."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        role = claims.get("role")
        return role if role in ("doctor", "patient") else None
    except (IndexError, ValueError, AttributeError):
        return None


def create(tokens):
    """Start a session from a login/refresh response."""
    now = time.time()
    sid = secrets.token_urlsafe(32)
    data = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "role": role_from_token(tokens["access_token"]),
        "expires": now + IDLE_SECONDS,
    }
    previous = cookie.get("sid")
    with _LOCK:
        _purge(now)
        if previous:
            _STORE.pop(previous, None)  # a new login replaces any earlier session of this browser
        _STORE[sid] = data
    cookie.clear()
    cookie["sid"] = sid
    return data


def current():
    sid = cookie.get("sid")
    if not sid:
        return None
    now = time.time()
    with _LOCK:
        data = _STORE.get(sid)
        if not data or data["expires"] <= now:
            _STORE.pop(sid, None)
            return None
        data["expires"] = now + IDLE_SECONDS
        return data


def destroy():
    sid = cookie.pop("sid", None)
    if sid:
        with _LOCK:
            _STORE.pop(sid, None)
    cookie.clear()


def csrf_token():
    """The CSRF token of this browser (created on first use, kept in the signed cookie)."""
    if "csrf" not in cookie:
        cookie["csrf"] = secrets.token_urlsafe(24)
    return cookie["csrf"]


def csrf_ok(submitted):
    expected = cookie.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(str(submitted), str(expected)))


def clear_all():
    """Test helper."""
    with _LOCK:
        _STORE.clear()
