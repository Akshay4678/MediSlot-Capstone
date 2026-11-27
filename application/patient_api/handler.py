"""Patient-facing API for Milestone 4: profile, doctor search, public doctor profile, slot availability.

Milestone 5 adds booking: POST/GET /appointments, GET /appointments/{appointment_id}, POST
/appointments/{appointment_id}/cancel and GET /refunds (logic in application/booking/service.py).

Milestone 6 adds PATCH /appointments/{appointment_id} (reschedule saga), POST/DELETE /slots/{slot_id}/waitlist,
GET /patient/me/waitlists and POST /waitlist-offers/{offer_id}/accept.

Routes are kept in one Lambda so API Gateway has a single patient-domain integration. Identity is
taken only from the authorizer context, never from the URL, query string or body.
"""

import logging
import os
from urllib.parse import unquote

from application.common.api import ApiError
from application.common.auth_context import claims
from application.common.http import request_body, request_method, response
from application.booking import service as booking
from application.booking.deps import default_deps
from application.booking.errors import is_missing_table
from application.database.connection import get_connection
from application.patient_api import discovery, profile as patient_profile
from application.reschedule import service as reschedule
from application.waitlist import service as waitlist

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


def _mark_booked_by_me(conn, user_id, body):
    """Flag the slots this patient already holds a CONFIRMED appointment in (the page then offers no Book and no
    "join waitlist" button for them)."""
    slot_ids = {s["slot_id"] for s in body.get("slots", []) + body.get("full_slots", [])}
    mine = set()
    if slot_ids:
        with conn.cursor() as cur:
            marks = ", ".join(["%s"] * len(slot_ids))
            cur.execute("SELECT a.slot_id FROM appointments a JOIN patients p ON p.patient_id = a.patient_id "
                        f"WHERE p.user_id = %s AND a.status = 'CONFIRMED' AND a.slot_id IN ({marks})",
                        (user_id, *sorted(slot_ids)))
            mine = {r["slot_id"] for r in cur.fetchall()} & slot_ids
        conn.rollback()
    for key in ("slots", "full_slots"):
        for s in body.get(key, []):
            s["booked_by_me"] = s["slot_id"] in mine
    for key in ("days", "full_days"):
        for day in body.get(key, []):
            for s in day.get("slots", []):
                s["booked_by_me"] = s["slot_id"] in mine
    body["my_booked_slot_ids"] = sorted(mine)


def _availability(event, user_id):
    q = event.get("queryStringParameters") or {}
    doctor_id = (((event.get("pathParameters") or {}).get("doctor_id")) or q.get("doctor_id") or "").strip()

    def work(conn):
        status, body = discovery.slots(conn, doctor_id, q)
        if status == 200:
            _mark_booked_by_me(conn, user_id, body)
        return status, body
    return _with_connection(work)


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


def _path(event, name):
    # slot ids contain "#", which travels percent-encoded (%23) in a URL path
    return unquote(((event.get("pathParameters") or {}).get(name) or "").strip())


def _reschedule_waitlist_route(method, resource, user_id, event):
    def work(conn):
        deps = default_deps()
        if resource == "/appointments/{appointment_id}":
            return reschedule.reschedule(conn, deps, user_id, _path(event, "appointment_id"), request_body(event))
        if resource == "/slots/{slot_id}/waitlist" and method == "POST":
            return waitlist.join(conn, deps, user_id, _path(event, "slot_id"))
        if resource == "/slots/{slot_id}/waitlist":
            return waitlist.leave(conn, deps, user_id, _path(event, "slot_id"))
        if resource == "/patient/me/waitlists":
            return waitlist.list_mine(conn, deps, user_id)
        return waitlist.accept(conn, deps, user_id, _path(event, "offer_id"))
    return _with_connection(work)


_RESCHEDULE_WAITLIST_ROUTES = {
    ("PATCH", "/appointments/{appointment_id}"), ("POST", "/slots/{slot_id}/waitlist"),
    ("DELETE", "/slots/{slot_id}/waitlist"), ("GET", "/patient/me/waitlists"),
    ("POST", "/waitlist-offers/{offer_id}/accept"),
}

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
        return _availability(event, user_id)
    if method == "GET" and resource == "/doctors/{doctor_id}":
        return _doctor_profile(event)
    if (method, resource) in _BOOKING_ROUTES:
        return _booking_route(method, resource, user_id, event)
    if (method, resource) in _RESCHEDULE_WAITLIST_ROUTES:
        return _reschedule_waitlist_route(method, resource, user_id, event)
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
            log.error("A DynamoDB table named in the Lambda settings does not exist: idempotency=%s slot_capacity=%s "
                      "waitlist=%s hold_offers=%s", os.environ.get("IDEMPOTENCY_TABLE_NAME"),
                      os.environ.get("SLOT_CAPACITY_TABLE_NAME"), os.environ.get("WAITLIST_TABLE_NAME"),
                      os.environ.get("HOLD_OFFERS_TABLE_NAME"))
            return _error(503, "BOOKING_STORE_UNAVAILABLE", "Booking is temporarily unavailable. Please try again later.")
        # In dev only, include the real cause so it shows on the web page without opening CloudWatch.
        detail = f" [{type(exc).__name__}: {exc}]" if os.environ.get("ENVIRONMENT_NAME") == "dev" else ""
        return _error(500, "INTERNAL_SERVER_ERROR", "The request could not be completed." + detail)
