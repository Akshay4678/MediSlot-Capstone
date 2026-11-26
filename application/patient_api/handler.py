"""Patient-facing API for Milestone 4: profile, doctor search, public doctor profile, slot availability.

Milestone 5 adds booking: POST/GET /appointments, GET /appointments/{appointment_id}, POST
/appointments/{appointment_id}/cancel and GET /refunds (logic in application/booking/service.py).

Routes are kept in one Lambda so API Gateway has a single patient-domain integration. Identity is
taken only from the authorizer context, never from the URL, query string or body. Milestone 6 adds
reschedule saga start, waitlist join/leave/offer acceptance and the asynchronous workflow hooks.
"""

import logging
import os

from application.common.api import ApiError
from application.common.auth_context import claims
from application.common.http import request_body, request_method, response
from application.booking import service as booking
from application.booking.deps import default_deps
from application.booking.errors import is_missing_table
from application.database.connection import get_connection
from application.patient_api import discovery, profile as patient_profile
from application.reschedule.service import start as start_reschedule
from application.waitlist import service as waitlist_service

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


def _header(event, name):
    name = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if str(key).lower() == name:
            return value
    return None


def _booking_route(method, resource, user_id, event):
    q = event.get("queryStringParameters") or {}
    appointment_id = ((event.get("pathParameters") or {}).get("appointment_id") or "").strip()

    def work(conn):
        deps = default_deps()
        if resource == "/appointments" and method == "POST":
            key = _header(event, "Idempotency-Key")
            return booking.book(conn, deps, user_id, request_body(event), key)
        if resource == "/appointments":
            return booking.list_for_patient(conn, deps, user_id, q)
        if resource == "/appointments/{appointment_id}":
            return booking.get_for_patient(conn, deps, user_id, appointment_id)
        if resource == "/appointments/{appointment_id}/cancel":
            return booking.cancel_by_patient(conn, deps, user_id, appointment_id, request_body(event))
        return booking.list_refunds(conn, deps, user_id, q)
    return _with_connection(work)



def _milestone6_route(method, resource, user_id, event):
    deps = default_deps()
    body = request_body(event)
    def work(conn):
        if resource == "/appointments/{appointment_id}/reschedule" and method == "POST":
            appointment_id = ((event.get("pathParameters") or {}).get("appointment_id") or "").strip()
            return start_reschedule(conn, user_id, appointment_id, body.get("target_slot_id"))
        if resource == "/waitlist" and method == "POST":
            return waitlist_service.join(conn, deps.waitlist, user_id, body.get("slot_id"), deps.events)
        if resource == "/waitlist" and method == "GET":
            return waitlist_service.list_for_patient(conn, deps.waitlist, user_id)
        if resource == "/waitlist/{slot_id}" and method == "DELETE":
            from urllib.parse import unquote
            slot_id = unquote(((event.get("pathParameters") or {}).get("slot_id") or "").strip())
            return waitlist_service.leave(deps.waitlist, user_id, slot_id, deps.events)
        if resource == "/waitlist/offers/{offer_id}/accept" and method == "POST":
            offer_id = ((event.get("pathParameters") or {}).get("offer_id") or "").strip()
            return waitlist_service.accept_offer(conn, deps, user_id, offer_id)
        return 404, {"error": "ROUTE_NOT_FOUND", "message": "Unsupported patient route."}
    return _with_connection(work)

_BOOKING_ROUTES = {
    ("POST", "/appointments"), ("GET", "/appointments"), ("GET", "/appointments/{appointment_id}"),
    ("POST", "/appointments/{appointment_id}/cancel"), ("GET", "/refunds"),
}


def _route(event, user_id):
    method = request_method(event)
    resource = event.get("resource") or ""

    if method == "GET" and resource == "/doctors/search":
        return _doctor_search(event)
    if method == "GET" and resource == "/doctors/{doctor_id}/slots":
        return _availability(event)
    if method == "GET" and resource == "/doctors/{doctor_id}":
        return _doctor_profile(event)
    if resource in {"/appointments/{appointment_id}/reschedule", "/waitlist", "/waitlist/{slot_id}", "/waitlist/offers/{offer_id}/accept"}:
        return _milestone6_route(method, resource, user_id, event)
    if (method, resource) in _BOOKING_ROUTES:
        return _booking_route(method, resource, user_id, event)
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
        if str(exc) == "INVALID_JSON" or "valid JSON" in str(exc):
            return _error(400, "INVALID_JSON", "Request body must be valid JSON.")
        log.exception("Patient API validation failed")
        return _error(400, "INVALID_REQUEST", str(exc))
    except Exception as exc:
        log.exception("Patient API failed")
        if is_missing_table(exc):
            log.error("A DynamoDB table named in the Lambda settings does not exist: idempotency=%s slot_capacity=%s",
                      os.environ.get("IDEMPOTENCY_TABLE_NAME"), os.environ.get("SLOT_CAPACITY_TABLE_NAME"))
            return _error(503, "BOOKING_STORE_UNAVAILABLE", "Booking is temporarily unavailable. Please try again later.")
        # In dev only, include the real cause so it shows on the web page without opening CloudWatch.
        detail = f" [{type(exc).__name__}: {exc}]" if os.environ.get("ENVIRONMENT_NAME") == "dev" else ""
        return _error(500, "INTERNAL_SERVER_ERROR", "The request could not be completed." + detail)
