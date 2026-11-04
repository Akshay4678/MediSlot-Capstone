"""Flask web app: role guards, session handling, token refresh and the doctor forms.
The REST API is replaced by a fake, so no network is used."""

import base64
import json
import re

import pytest

flask = pytest.importorskip("flask")

from web import api_client, sessions  # noqa: E402
from web.app import create_app  # noqa: E402


def fake_jwt(role):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'HS256'})}.{b64({'sub': 'u1', 'role': role})}.sig"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class FakeApi:
    """Records calls and answers like the MediSlot REST API."""

    def __init__(self):
        self.calls, self.expired_tokens = [], set()
        self.profile = {"doctor_id": "d1", "first_name": "Asha", "last_name": "Rao", "licence_number": "TS-12345",
                        "experience_years": 5, "qualifications": "MBBS", "timezone": "Asia/Kolkata",
                        "hospital_id": "h1", "hospital_name": "Demo Hospital", "hospital_city": "Hyderabad",
                        "specialisation_id": "s1", "specialisation_name": "Cardiology", "photo_url": None}

    def __call__(self, method, url, headers=None, json=None, params=None, timeout=None):
        path = url.split("/dev", 1)[-1]
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        self.calls.append((method, path, token, json, params))
        if path.endswith("/doctors/login"):
            return FakeResponse(200, {"access_token": fake_jwt("doctor"), "refresh_token": "R" * 40})
        if path.endswith("/patients/login"):
            return FakeResponse(200, {"access_token": fake_jwt("patient"), "refresh_token": "P" * 40})
        if path == "/auth/refresh":
            return FakeResponse(200, {"access_token": fake_jwt("doctor") + "2", "refresh_token": "N" * 40})
        if path == "/auth/logout":
            return FakeResponse(200, {"message": "ok"})
        if token in self.expired_tokens:
            return FakeResponse(401, {"error": "UNAUTHORIZED", "message": "expired"})
        if path == "/doctors/me" and method == "GET":
            return FakeResponse(200, {"profile": self.profile})
        if path == "/doctors/me" and method == "PUT":
            return FakeResponse(200, {"profile": self.profile})
        if path == "/hospitals":
            return FakeResponse(200, {"items": [{"hospital_id": "h1", "hospital_name": "Demo Hospital", "city": "Hyderabad"}]})
        if path == "/specialisations":
            return FakeResponse(200, {"items": [{"specialisation_id": "s1", "specialisation_name": "Cardiology"}]})
        if path == "/doctors/me/schedule":
            day = {"date": "2026-11-02", "weekday": "Monday", "blackout": False, "blackout_reason": "", "slots": [{
                "slot_id": "x", "start_time": "09:00", "end_time": "09:30", "type_name": "Standard Consult", "capacity": 1,
                "booked_count": 1, "status": "FULL", "appointments": [{"patient_name": "Pat One", "patient_phone": "1", "status": "CONFIRMED"}]}]}
            return FakeResponse(200, {"timezone": "Asia/Kolkata", "from": "2026-11-02", "to": "2026-11-02", "date": "2026-11-02",
                                      "days": [day], "slots": day["slots"], "blackout": False})
        if path == "/doctors/me/consultation-types":
            return FakeResponse(200, {"items": [{"consultation_type_id": "t1", "type_name": "Standard Consult", "duration_minutes": 30, "fee": 500.0}],
                                      "allowed_types": [{"type_name": "Standard Consult", "duration_minutes": 30}]})
        if path == "/doctors/me/availability" and method == "GET":
            return FakeResponse(200, {"timezone": "Asia/Kolkata", "patterns": []})
        if path == "/doctors/me/availability" and method == "PUT":
            return FakeResponse(200, {"patterns": [], "slots": {"created": 6, "window_days": 30, "removed_unbooked": 0}})
        if path == "/doctors/me/blackouts":
            return FakeResponse(200, {"items": []})
        return FakeResponse(404, {"error": "ROUTE_NOT_FOUND", "message": "nope"})


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("API_BASE_URL", "https://api.test/dev")
    monkeypatch.setattr(api_client.requests, "request", fake)
    sessions.clear_all()
    return fake


@pytest.fixture
def client(api):
    return create_app().test_client()


def page_token(client, path="/patient/register"):
    """The CSRF token the browser would get from a form page (register pages never redirect)."""
    html = client.get(path).get_data(as_text=True)
    match = re.search(
        r'<input\\b(?=[^>]*\\bname=["\\']csrf_token["\\'])'
        r'(?=[^>]*\\bvalue=["\\']([^"\\']+)["\\'])[^>]*>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, (
        "CSRF input was not found in the rendered HTML. "
        f"HTML begins with: {html[:500]!r}"
    )
    return match.group(1)


def login(client, role, email="a@b.co"):
    return client.post(f"/{role}/login", data={"email": email, "password": "Passw0rd123",
                                               "csrf_token": page_token(client)})


def csrf(client):
    html = client.get("/doctor/profile").get_data(as_text=True)
    match = re.search(
        r'<input\\b(?=[^>]*\\bname=["\\']csrf_token["\\'])'
        r'(?=[^>]*\\bvalue=["\\']([^"\\']+)["\\'])[^>]*>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, (
        "CSRF input was not found in the rendered HTML. "
        f"HTML begins with: {html[:500]!r}"
    )
    return match.group(1)


def test_landing_offers_both_roles(client):
    html = client.get("/").get_data(as_text=True)
    assert "Login as Doctor" in html and "Login as Patient" in html


def test_login_pages_link_to_registration(client):
    assert "/doctor/register" in client.get("/doctor/login").get_data(as_text=True)
    assert "/patient/register" in client.get("/patient/login").get_data(as_text=True)


def test_logged_out_visitors_are_sent_to_the_login_of_that_portal(client):
    assert client.get("/doctor/").headers["Location"].endswith("/doctor/login")
    assert client.get("/patient/").headers["Location"].endswith("/patient/login")


def test_route_guards_work_in_both_directions(client):
    assert login(client, "patient").headers["Location"].endswith("/patient/")
    for path in ("/doctor/", "/doctor/profile", "/doctor/availability", "/doctor/schedule"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"] == "/", path
    client.post("/patient/logout", data={"csrf_token": ""})  # no csrf -> rejected, session stays
    client2 = create_app().test_client()
    assert login(client2, "doctor").headers["Location"].endswith("/doctor/")
    assert client2.get("/patient/").headers["Location"] == "/"


def test_pages_only_show_the_matching_navigation(client):
    login(client, "doctor")
    html = client.get("/doctor/profile").get_data(as_text=True)
    assert "Availability" in html and "/patient/" not in html


def test_logout_revokes_tokens_through_the_api_and_clears_the_session(client, api):
    login(client, "doctor")
    token = csrf(client)
    response = client.post("/doctor/logout", data={"csrf_token": token})
    assert response.status_code == 302
    logout_call = [c for c in api.calls if c[1] == "/auth/logout"][0]
    assert logout_call[2] == fake_jwt("doctor") and logout_call[3] == {"refresh_token": "R" * 40}
    assert client.get("/doctor/").headers["Location"].endswith("/doctor/login")


def test_forms_without_a_csrf_token_are_rejected(client, api):
    login(client, "doctor")
    client.post("/doctor/profile", data={"first_name": "X"})
    assert not [c for c in api.calls if c[0] == "PUT"]


def test_expired_access_token_is_refreshed_once_and_the_request_retried(client, api):
    login(client, "doctor")
    api.expired_tokens.add(fake_jwt("doctor"))
    assert client.get("/doctor/profile").status_code == 200
    assert any(c[1] == "/auth/refresh" for c in api.calls)
    assert [c[2] for c in api.calls if c[1] == "/doctors/me"][-1].endswith("2")


def test_unrefreshable_session_returns_to_the_landing_page(client, api, monkeypatch):
    login(client, "doctor")
    api.expired_tokens.add(fake_jwt("doctor"))
    real = api.__call__
    monkeypatch.setattr(api_client.requests, "request", lambda m, u, **k: FakeResponse(401, {}) if u.endswith("/auth/refresh") else real(m, u, **k))
    assert client.get("/doctor/profile").headers["Location"] == "/"


def test_profile_save_sends_only_the_form_fields(client, api):
    login(client, "doctor")
    token = csrf(client)
    client.post("/doctor/profile", data={"csrf_token": token, "first_name": "Asha", "last_name": "Rao", "licence_number": "TS-12345",
                                         "hospital_id": "h1", "specialisation_id": "s1", "timezone": "Asia/Kolkata",
                                         "experience_years": "5", "qualifications": ""})
    body = [c[3] for c in api.calls if c[0] == "PUT" and c[1] == "/doctors/me"][0]
    assert body["licence_number"] == "TS-12345" and "role" not in body and "user_id" not in body


def test_availability_form_becomes_a_patterns_payload(client, api):
    login(client, "doctor")
    token = csrf(client)
    client.post("/doctor/availability", data={
        "csrf_token": token, "row_count": "3",
        "p0_id": "", "p0_type": "t1", "p0_day": "1", "p0_start": "09:00", "p0_end": "12:00", "p0_capacity": "1",
        "p1_id": "a1", "p1_type": "t1", "p1_day": "2", "p1_start": "09:00", "p1_end": "12:00", "p1_capacity": "1", "p1_remove": "on",
        "p2_id": "", "p2_type": "", "p2_day": "", "p2_start": "", "p2_end": "", "p2_capacity": "1"})
    put = [c[3] for c in api.calls if c[0] == "PUT" and c[1] == "/doctors/me/availability"][0]
    assert put == {"patterns": [{"consultation_type_id": "t1", "day_of_week": "1", "start_time": "09:00",
                                 "end_time": "12:00", "capacity": "1"}]}


@pytest.mark.parametrize("path", ["/doctor/", "/doctor/profile", "/doctor/consultation-types", "/doctor/availability",
                                  "/doctor/blackouts", "/doctor/schedule", "/doctor/schedule?view=week&date=2026-11-02"])
def test_every_doctor_page_renders(client, path):
    login(client, "doctor")
    assert client.get(path).status_code == 200


def test_schedule_lists_booked_patients(client):
    login(client, "doctor")
    html = client.get("/doctor/schedule").get_data(as_text=True)
    assert "Pat One" in html and "09:00 - 09:30" in html


def test_week_view_requests_a_monday_to_sunday_range(client, api):
    login(client, "doctor")
    client.get("/doctor/schedule?view=week&date=2026-11-04")
    params = [c[4] for c in api.calls if c[1] == "/doctors/me/schedule"][-1]
    assert params == {"from": "2026-11-02", "to": "2026-11-08"}


def test_registration_forms_post_to_the_role_specific_api_route(client, api):
    token = page_token(client)
    client.post("/patient/register", data={"email": "p@b.co", "password": "Passw0rd123", "first_name": "P",
                                           "last_name": "Q", "csrf_token": token})
    client.post("/doctor/register", data={"email": "d@b.co", "password": "Passw0rd123", "csrf_token": token})
    paths = [c[1] for c in api.calls]
    assert "/auth/patients/register" in paths and "/auth/doctors/register" in paths


# ------------------------------------------------------- login / CSRF regressions
def test_login_and_register_forms_carry_a_csrf_token(client):
    for path in ("/doctor/login", "/patient/login", "/doctor/register", "/patient/register"):
        assert 'name="csrf_token"' in client.get(path).get_data(as_text=True), path


def test_login_without_a_csrf_token_is_rejected_and_never_calls_the_api(client, api):
    response = client.post("/doctor/login", data={"email": "a@b.co", "password": "Passw0rd123"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/doctor/login")
    assert not [c for c in api.calls if c[1].endswith("/login")]


def test_login_with_a_tampered_csrf_token_is_rejected(client, api):
    page_token(client)
    client.post("/doctor/login", data={"email": "a@b.co", "password": "Passw0rd123", "csrf_token": "forged"})
    assert not [c for c in api.calls if c[1].endswith("/login")]


def test_signing_in_works_when_the_browser_already_has_a_session(client, api):
    """Regression: a leftover session used to make every later login fail with 'session form expired'."""
    assert login(client, "doctor").headers["Location"].endswith("/doctor/")
    assert client.get("/doctor/login").headers["Location"].endswith("/doctor/")   # already signed in
    response = client.post("/doctor/login", data={"email": "a@b.co", "password": "Passw0rd123",
                                                  "csrf_token": csrf(client)})
    assert response.status_code == 302 and response.headers["Location"].endswith("/doctor/")
    assert len(sessions._STORE) == 1                                               # old session replaced, not leaked


def test_a_patient_can_sign_in_while_a_doctor_session_exists(client):
    login(client, "doctor")
    token = csrf(client)
    response = client.post("/patient/login", data={"email": "p@b.co", "password": "Passw0rd123", "csrf_token": token})
    assert response.headers["Location"].endswith("/patient/")


def test_wrong_credentials_show_the_api_message_and_keep_the_form(client, api, monkeypatch):
    real = api.__call__
    monkeypatch.setattr(api_client.requests, "request", lambda m, u, **k: FakeResponse(
        401, {"error": "INVALID_CREDENTIALS", "message": "Email or password is incorrect."}) if u.endswith("/doctors/login") else real(m, u, **k))
    response = client.post("/doctor/login", data={"email": "a@b.co", "password": "bad", "csrf_token": page_token(client)})
    assert "Email or password is incorrect." in response.get_data(as_text=True)


def test_logout_then_login_again_works(client, api):
    login(client, "doctor")
    client.post("/doctor/logout", data={"csrf_token": csrf(client)})
    assert client.get("/doctor/").headers["Location"].endswith("/doctor/login")
    assert login(client, "doctor").headers["Location"].endswith("/doctor/")
