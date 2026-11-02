"""Thin client for the MediSlot REST API. The web app is a pure API client: it holds no
database credentials and every read and write goes through API Gateway."""

import os

import requests

from web import sessions

TIMEOUT = (3.05, 25)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class SessionExpired(Exception):
    """The access token could not be used or refreshed; the user must log in again."""


def base_url():
    return os.environ.get("API_BASE_URL", "").rstrip("/")


def _send(method, path, token=None, json_body=None, params=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(method, base_url() + path, headers=headers, json=json_body,
                                    params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ApiError(503, "API_UNREACHABLE", "The MediSlot API could not be reached. Please try again.") from exc
    try:
        data = response.json() if response.content else {}
    except ValueError:
        data = {}
    return response.status_code, data if isinstance(data, dict) else {}


def _raise(status, data):
    raise ApiError(status, data.get("error", "ERROR"), data.get("message") or "The request failed.")


def public(method, path, json_body=None):
    """Call an endpoint that needs no token (login, register)."""
    status, data = _send(method, path, json_body=json_body)
    if status >= 400:
        _raise(status, data)
    return data


def call(method, path, json_body=None, params=None):
    """Call a protected endpoint with the session's token; refresh once on 401."""
    session = sessions.current()
    if not session:
        raise SessionExpired()

    status, data = _send(method, path, session["access_token"], json_body, params)
    if status == 401 and session.get("refresh_token"):
        r_status, r_data = _send("POST", "/auth/refresh", json_body={"refresh_token": session["refresh_token"]})
        if r_status == 200 and r_data.get("access_token"):
            session["access_token"] = r_data["access_token"]
            session["refresh_token"] = r_data.get("refresh_token", session["refresh_token"])
            status, data = _send(method, path, session["access_token"], json_body, params)
    if status == 401:
        sessions.destroy()
        raise SessionExpired()
    if status >= 400:
        _raise(status, data)
    return data


def logout():
    """Revoke every token server-side (best effort), then drop the local session."""
    session = sessions.current()
    if session:
        try:
            _send("POST", "/auth/logout", session["access_token"], {"refresh_token": session.get("refresh_token")})
        except ApiError:
            pass
    sessions.destroy()
