"""Doctor-domain Lambda (Milestone 3): profile, photo, consultation types, availability,
blackouts and schedule, plus the hospital / specialisation reference lists.

Identity comes ONLY from the API Gateway authorizer context (userId, role). The request
body, query string and headers are never used to decide who is acting.
"""

import json
import logging

from application.common.api import ApiError
from application.common.http import request_body, request_method, response
from application.database.connection import get_connection
from application.doctor_api import availability, blackouts, catalogue, consultation_types, photo, profile, schedule

log = logging.getLogger()
log.setLevel(logging.INFO)


class Ctx:
    """Per-request context handed to every route function."""

    def __init__(self, event, conn, user_id, role):
        self.event, self.conn, self.user_id, self.role = event, conn, user_id, role
        self.params = event.get("pathParameters") or {}
        self.query = event.get("queryStringParameters") or {}
        self._body = None
        self._doctor = None

    @property
    def body(self):
        if self._body is None:
            try:
                self._body = request_body(self.event)
            except ValueError as exc:
                raise ApiError(400, "INVALID_JSON", str(exc)) from None
        return self._body

    def find_doctor(self):
        """The caller's doctors row, or None when the profile has not been created yet."""
        if self._doctor is None:
            with self.conn.cursor() as cur:
                cur.execute("SELECT doctor_id, timezone, is_active FROM doctors WHERE user_id = %s", (self.user_id,))
                self._doctor = cur.fetchone() or False
        return self._doctor or None

    def doctor(self):
        doctor = self.find_doctor()
        if not doctor:
            raise ApiError(409, "PROFILE_REQUIRED", "Create your doctor profile first (PUT /doctors/me).")
        if not doctor["is_active"]:
            raise ApiError(403, "DOCTOR_INACTIVE", "This doctor account is not active.")
        return doctor


# (HTTP method, API Gateway resource path) -> function(ctx) returning (status, body)
ROUTES = {
    ("GET", "/doctors/me"): profile.get,
    ("PUT", "/doctors/me"): profile.put,
    ("POST", "/doctors/me/photo"): photo.create_upload,
    ("GET", "/doctors/me/consultation-types"): consultation_types.list_,
    ("POST", "/doctors/me/consultation-types"): consultation_types.create,
    ("PUT", "/doctors/me/consultation-types/{id}"): consultation_types.update,
    ("DELETE", "/doctors/me/consultation-types/{id}"): consultation_types.delete,
    ("GET", "/doctors/me/availability"): availability.get,
    ("PUT", "/doctors/me/availability"): availability.put,
    ("GET", "/doctors/me/blackouts"): blackouts.list_,
    ("POST", "/doctors/me/blackouts"): blackouts.create,
    ("DELETE", "/doctors/me/blackouts/{id}"): blackouts.delete,
    ("GET", "/doctors/me/schedule"): schedule.get,
    ("GET", "/hospitals"): catalogue.hospitals,
    ("GET", "/specialisations"): catalogue.specialisations,
}


def _error(status, code, message):
    return response(status, {"error": code, "message": message})


def lambda_handler(event, context):
    method, resource = request_method(event), event.get("resource") or ""
    route = ROUTES.get((method, resource))
    if route is None:
        known = any(r == resource for _, r in ROUTES)
        return _error(405 if known else 404, "METHOD_NOT_ALLOWED" if known else "ROUTE_NOT_FOUND", "Unsupported route.")

    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    user_id, role = authorizer.get("userId"), authorizer.get("role")
    if not user_id or not role:
        return _error(401, "UNAUTHORIZED", "A valid access token is required.")
    if resource.startswith("/doctors/me") and role != "doctor":
        return _error(403, "FORBIDDEN", "This endpoint is for doctors only.")

    conn = None
    try:
        conn = get_connection()
        status, payload = route(Ctx(event, conn, str(user_id), str(role)))
        conn.commit()
        return response(status, payload)
    except ApiError as exc:
        if conn:
            conn.rollback()
        return _error(exc.status, exc.code, exc.message)
    except Exception:  # noqa: BLE001 - details stay in CloudWatch
        if conn:
            conn.rollback()
        log.error(json.dumps({"event": "unhandled_error", "method": method, "resource": resource}), exc_info=True)
        return _error(500, "INTERNAL_SERVER_ERROR", "The request could not be completed.")
    finally:
        if conn:
            conn.close()
