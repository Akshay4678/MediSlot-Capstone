"""Patient-facing API for Milestone 4: profile, doctor search, public doctor profile, slot availability.

Routes are kept in one Lambda so API Gateway has a single patient-domain integration. Identity is
taken only from the authorizer context, never from the URL, query string or body. Booking,
cancellation, refunds, reschedule, waitlist and notifications are later milestones and are not here.
"""

import logging
import os

from application.common.api import ApiError
from application.common.auth_context import claims
from application.common.http import request_body, request_method, response
from application.database.connection import get_connection
from application.patient_api import discovery, profile as patient_profile

log = logging.getLogger()
log.setLevel(logging.INFO)


def _error(status, code, message):
    return response(status, {"error": code, "message": message})


def _with_connection(work):
    conn = get_connection()
    try:
        status, body = work(conn)
        return response(status, body)
    finally:
        conn.close()


def _doctor_search(event):
    q = event.get("queryStringParameters") or {}
    return _with_connection(lambda conn: discovery.search(conn, q))


def _doctor_profile(event):
    doctor_id = ((event.get("pathParameters") or {}).get("doctor_id") or "").strip()
    return _with_connection(lambda conn: discovery.doctor_profile(conn, doctor_id))


def _availability(event):
    q = event.get("queryStringParameters") or {}
    doctor_id = (((event.get("pathParameters") or {}).get("doctor_id")) or q.get("doctor_id") or "").strip()
    return _with_connection(lambda conn: discovery.slots(conn, doctor_id, q))


def _profile_route(method, user_id, event):
    def work(conn):
        if method == "GET":
            return patient_profile.get(conn, user_id)
        if method == "POST":
            return patient_profile.create(conn, user_id, request_body(event))
        if method == "PUT":
            return patient_profile.update(conn, user_id, request_body(event))
        return patient_profile.delete(conn, user_id)
    return _with_connection(work)


def _route(event, user_id):
    method = request_method(event)
    resource = event.get("resource") or ""

    if method == "GET" and resource == "/doctors/search":
        return _doctor_search(event)
    if method == "GET" and resource == "/doctors/{doctor_id}/slots":
        return _availability(event)
    if method == "GET" and resource == "/doctors/{doctor_id}":
        return _doctor_profile(event)
    if resource == "/patient/me" and method in ("GET", "POST", "PUT", "DELETE"):
        return _profile_route(method, user_id, event)
    return _error(404, "ROUTE_NOT_FOUND", "Unsupported patient route.")


def lambda_handler(event, context):
    identity = claims(event)
    user_id, role = identity.get("user_id"), identity.get("role")
    if not user_id:
        return _error(401, "UNAUTHORIZED", "A valid access token is required.")
    if role != "patient":
        return _error(403, "FORBIDDEN", "This endpoint is for patients only.")
    try:
        return _route(event, str(user_id))
    except ApiError as exc:
        return _error(exc.status, exc.code, exc.message)
    except ValueError as exc:
        if str(exc) == "INVALID_JSON":
            return _error(400, "INVALID_JSON", "Request body must be valid JSON.")
        log.exception("Patient API validation failed")
        return _error(400, "INVALID_REQUEST", str(exc))
    except Exception as exc:
        log.exception("Patient API failed")
        # In dev only, include the real cause so it shows on the web page without opening CloudWatch.
        detail = f" [{type(exc).__name__}: {exc}]" if os.environ.get("ENVIRONMENT_NAME") == "dev" else ""
        return _error(500, "INTERNAL_SERVER_ERROR", "The request could not be completed." + detail)
