"""Flask web app: role guards, session handling, token refresh and doctor forms.
The REST API is replaced by a fake, so no network is used.
"""

import base64
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

from web import api_client, sessions  # noqa: E402
from web.app import create_app  # noqa: E402


def fake_jwt(role):
    def b64(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode()
        ).rstrip(b"=").decode()

    return (
        f"{b64({'alg': 'HS256'})}."
        f"{b64({'sub': 'u1', 'role': role})}.sig"
    )


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class FakeApi:
    """Records calls and answers like the MediSlot REST API."""

    def __init__(self):
        self.calls = []
        self.expired_tokens = set()
        self.put_error = None  # (status, body) to make PUT /doctors/me fail
        self.extra_types = []  # extra consultation types returned by the types endpoint
        self.has_bookings_on_blackout = False  # POST /doctors/me/blackouts needs a keep/cancel choice
        self.headers_seen = []  # (method, path, headers) of every call
        self.book_error = None  # (status, body) to make POST /appointments fail

        self.profile = {
            "doctor_id": "d1",
            "first_name": "Asha",
            "last_name": "Rao",
            "licence_number": "TS-12345",
            "experience_years": 5,
            "qualifications": "MBBS",
            "timezone": "Asia/Kolkata",
            "hospital_id": "h1",
            "hospital_name": "Demo Hospital",
            "hospital_city": "Hyderabad",
            "specialisation_id": "s1",
            "specialisation_name": "Cardiology",
            "photo_url": None,
        }

    def __call__(
        self,
        method,
        url,
        headers=None,
        json=None,
        params=None,
        timeout=None,
    ):
        path = url.split("/dev", 1)[-1]
        token = (headers or {}).get(
            "Authorization", ""
        ).replace("Bearer ", "")

        self.calls.append((method, path, token, json, params))
        self.headers_seen.append((method, path, dict(headers or {})))

        if path.endswith("/doctors/login"):
            return FakeResponse(
                200,
                {
                    "access_token": fake_jwt("doctor"),
                    "refresh_token": "R" * 40,
                },
            )

        if path.endswith("/patients/login"):
            return FakeResponse(
                200,
                {
                    "access_token": fake_jwt("patient"),
                    "refresh_token": "P" * 40,
                },
            )

        if path == "/auth/refresh":
            return FakeResponse(
                200,
                {
                    "access_token": fake_jwt("doctor") + "2",
                    "refresh_token": "N" * 40,
                },
            )

        if path == "/auth/logout":
            return FakeResponse(200, {"message": "ok"})

        if token in self.expired_tokens:
            return FakeResponse(
                401,
                {
                    "error": "UNAUTHORIZED",
                    "message": "expired",
                },
            )

        if path == "/doctors/me" and method == "GET":
            if self.profile is None:
                return FakeResponse(
                    404,
                    {
                        "error": "PROFILE_NOT_FOUND",
                        "message": "Doctor profile not found.",
                    },
                )

            return FakeResponse(
                200,
                {"profile": self.profile},
            )

        if path == "/doctors/me" and method == "PUT":
            if self.put_error:
                return FakeResponse(*self.put_error)

            return FakeResponse(
                200,
                {"profile": self.profile},
            )

        if path == "/hospitals":
            return FakeResponse(
                200,
                {
                    "items": [
                        {
                            "hospital_id": "h1",
                            "hospital_name": "Demo Hospital",
                            "city": "Hyderabad",
                        }
                    ]
                },
            )

        if path == "/specialisations":
            return FakeResponse(
                200,
                {
                    "items": [
                        {
                            "specialisation_id": "s1",
                            "specialisation_name": "Cardiology",
                        }
                    ]
                },
            )

        if path == "/doctors/me/schedule":
            day = {
                "date": "2026-11-02",
                "weekday": "Monday",
                "blackout": False,
                "blackout_reason": "",
                "slots": [
                    {
                        "slot_id": "x",
                        "start_time": "09:00",
                        "end_time": "09:30",
                        "type_name": "Standard Consult",
                        "capacity": 1,
                        "booked_count": 1,
                        "status": "FULL",
                        "appointments": [
                            {
                                "appointment_id": "appt-1",
                                "can_mark_attendance": True,
                                "patient_name": "Pat One",
                                "patient_phone": "1",
                                "status": "CONFIRMED",
                            }
                        ],
                    }
                ],
            }

            return FakeResponse(
                200,
                {
                    "timezone": "Asia/Kolkata",
                    "from": "2026-11-02",
                    "to": "2026-11-02",
                    "date": "2026-11-02",
                    "days": [day],
                    "slots": day["slots"],
                    "blackout": False,
                },
            )

        if path == "/doctors/me/consultation-types":
            return FakeResponse(
                200,
                {
                    "items": [
                        {
                            "consultation_type_id": "t1",
                            "type_name": "Standard Consult",
                            "duration_minutes": 30,
                            "fee": 500.0,
                        }
                    ] + self.extra_types,
                    "allowed_types": [
                        {
                            "type_name": "Standard Consult",
                            "duration_minutes": 30,
                        }
                    ],
                },
            )

        if path == "/doctors/me/availability" and method == "GET":
            return FakeResponse(
                200,
                {
                    "timezone": "Asia/Kolkata",
                    "patterns": [],
                    "health_camp_capacity": {"min": 2, "max": 10, "default": 5},
                },
            )

        if path == "/doctors/me/availability" and method == "PUT":
            return FakeResponse(
                200,
                {
                    "patterns": [],
                    "slots": {
                        "created": 6,
                        "window_days": 30,
                        "removed_unbooked": 0,
                    },
                },
            )

        if path == "/doctors/search" and method == "GET":
            return FakeResponse(
                200,
                {
                    "items": [{"doctor_id": "d9", "first_name": "Asha", "last_name": "Rao", "specialisation": "Cardiology",
                               "hospital_name": "Apollo", "city": "Hyderabad", "experience_years": 5}],
                    "count": 1, "total": 25, "limit": 10, "offset": (params or {}).get("offset", 0), "has_more": True,
                },
            )

        appt = {"appointment_id": "a1", "status": "CONFIRMED", "slot_id": "slot#1", "date": "2026-11-13",
                "start_time": "09:00", "end_time": "09:30", "start_utc": "2026-11-13T03:30:00Z", "timezone": "Asia/Kolkata",
                "fee": 500.0, "notes": "", "cancelled_at": None, "cancellation_reason": None, "refund": None,
                "consultation_type": {"consultation_type_id": "t1", "type_name": "Standard Consult"},
                "doctor": {"doctor_id": "d9", "name": "Dr. Asha Rao", "hospital_name": "Apollo", "city": "Hyderabad",
                           "specialisation": "Cardiology"},
                "can_cancel": True, "refund_preview": {"tier": "FULL", "percentage": 100, "amount": 500.0}}

        if path == "/appointments" and method == "POST":
            if self.book_error:
                return FakeResponse(*self.book_error)
            return FakeResponse(201, {"appointment": appt})

        if path == "/appointments" and method == "GET":
            return FakeResponse(200, {"items": [appt], "count": 1, "total": 1, "limit": 10, "offset": 0, "has_more": False})

        if path == "/appointments/a1" and method == "GET":
            return FakeResponse(200, {"appointment": appt})

        if path == "/appointments/a1/cancel" and method == "POST":
            done = dict(appt, status="CANCELLED_BY_PATIENT", can_cancel=False,
                        refund={"tier": "FULL", "percentage": 100.0, "amount": 500.0, "status": "CALCULATED"})
            return FakeResponse(200, {"appointment": done})

        if path == "/refunds" and method == "GET":
            return FakeResponse(200, {"items": [{"refund_id": "r1", "appointment_id": "a1", "tier": "TIER_75",
                                                 "percentage": 75.0, "amount": 375.0, "fee": 500.0, "status": "CALCULATED",
                                                 "appointment_status": "CANCELLED_BY_PATIENT", "doctor_name": "Dr. Asha Rao",
                                                 "type_name": "Standard Consult", "appointment_date": "2026-11-13",
                                                 "appointment_start": "09:00", "created_at": "2026-11-10T06:00:00Z"}],
                                      "count": 1, "total": 1, "limit": 10, "offset": 0, "has_more": False,
                                      "total_refunded": 375.0})

        if path == "/doctors/d9/slots" and method == "GET":
            if (params or {}).get("to", "") > "2027":
                return FakeResponse(400, {"error": "OUTSIDE_BOOKING_WINDOW",
                                          "message": "Slots can only be viewed up to 2026-12-23 (30 days ahead)."})
            slot = {"slot_id": "slot#1", "date": "2026-11-13", "start_time": "09:00", "end_time": "09:30",
                    "capacity": 3, "booked_count": 2, "available_seats": 1, "status": "OPEN", "type_name": "Standard Consult",
                    "duration_minutes": 30, "fee": 500.0, "consultation_type_id": "t1"}
            return FakeResponse(200, {"doctor_id": "d9", "timezone": "Asia/Kolkata", "slots": [slot],
                                      "days": [{"date": "2026-11-13", "slots": [slot]}],
                                      "consultation_types": ["Quick Consult", "Standard Consult"]})

        if path == "/doctors/me/appointments" and method == "GET":
            return FakeResponse(200, {"items": [dict(appt, patient={"patient_id": "p1", "name": "Pat One", "phone": "1"})],
                                      "count": 1, "total": 1, "limit": 15, "offset": 0, "has_more": False})

        if path == "/doctors/me/slots/cancel" and method == "POST":
            return FakeResponse(200, {"slot_id": (json or {}).get("slot_id"), "status": "CLOSED",
                                      "cancelled_appointments": 2, "refunds_total": 1000.0})

        if path == "/patient/me" and method == "GET":
            return FakeResponse(200, {"patient": {"first_name": "Pat", "last_name": "One", "phone": "+91 98765 43210",
                                                  "city": "Pune", "gender": "Male", "date_of_birth": "1990-05-04",
                                                  "address": None, "emergency_contact": None}})

        if path == "/patient/me" and method == "PUT":
            if self.put_error:
                return FakeResponse(*self.put_error)
            return FakeResponse(200, {"patient": json})

        if path == "/patient/me" and method == "DELETE":
            return FakeResponse(200, {"message": "deleted"})

        if path == "/doctors/d9" and method == "GET":
            return FakeResponse(
                200,
                {
                    "doctor": {"doctor_id": "d9", "first_name": "Asha", "last_name": "Rao", "specialisation": "Cardiology",
                               "hospital_name": "Apollo", "city": "Hyderabad", "qualifications": "MBBS", "experience_years": 5},
                    "consultation_types": [{"type_name": "Standard Consult", "duration_minutes": 30, "fee": 500.0}],
                },
            )

        if path == "/doctors/me/blackouts" and method == "POST":
            body = json or {}
            if self.has_bookings_on_blackout and "existing_bookings" not in body:
                return FakeResponse(
                    409,
                    {
                        "error": "BLACKOUT_HAS_BOOKINGS",
                        "message": "1 appointment(s) are already booked.",
                    },
                )
            return FakeResponse(
                201,
                {
                    "blackout": {"blackout_id": "b1"},
                    "slots_blocked": 4,
                    "slots_with_bookings": 1,
                    "appointments_kept": 1 if body.get("existing_bookings") == "keep" else 0,
                },
            )

        if path == "/doctors/me/blackouts":
            return FakeResponse(
                200,
                {"items": []},
            )

        return FakeResponse(
            404,
            {
                "error": "ROUTE_NOT_FOUND",
                "message": "nope",
            },
        )


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()

    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv(
        "API_BASE_URL",
        "https://api.test/dev",
    )

    monkeypatch.setattr(
        api_client.requests,
        "request",
        fake,
    )

    sessions.clear_all()

    return fake


@pytest.fixture
def client(api):
    return create_app().test_client()


# -------------------------------------------------------
# CSRF HTML parser
# -------------------------------------------------------

class CSRFInputParser(HTMLParser):
    """Extract CSRF input values regardless of line breaks or attribute order."""

    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "input":
            return

        attributes = dict(attrs)

        if attributes.get("name") == "csrf_token":
            self.token = attributes.get("value")


def extract_csrf_token(html, path):
    parser = CSRFInputParser()
    parser.feed(html)

    assert parser.token, (
        f"CSRF token was not found on {path}. "
        f"HTML begins with: {html[:500]!r}"
    )

    return parser.token


def page_token(client, path="/patient/register"):
    """Get the CSRF token from a registration page."""

    response = client.get(path)
    html = response.get_data(as_text=True)

    return extract_csrf_token(html, path)


def csrf(client):
    """Get the CSRF token from an authenticated doctor page."""

    response = client.get("/doctor/profile")
    html = response.get_data(as_text=True)

    return extract_csrf_token(
        html,
        "/doctor/profile",
    )


def login(client, role, email="a@b.co"):
    return client.post(
        f"/{role}/login",
        data={
            "email": email,
            "password": "Passw0rd123",
            "csrf_token": page_token(client),
        },
    )


# -------------------------------------------------------
# Landing and login tests
# -------------------------------------------------------

def test_landing_offers_both_roles(client):
    html = client.get("/").get_data(as_text=True)

    assert "Login as Doctor" in html
    assert "Login as Patient" in html


def test_login_pages_link_to_registration(client):
    doctor_html = client.get(
        "/doctor/login"
    ).get_data(as_text=True)

    patient_html = client.get(
        "/patient/login"
    ).get_data(as_text=True)

    assert "/doctor/register" in doctor_html
    assert "/patient/register" in patient_html


def test_logged_out_visitors_are_sent_to_the_login_of_that_portal(client):
    assert client.get(
        "/doctor/"
    ).headers["Location"].endswith("/doctor/login")

    assert client.get(
        "/patient/"
    ).headers["Location"].endswith("/patient/login")


# -------------------------------------------------------
# Role guard tests
# -------------------------------------------------------

def test_route_guards_work_in_both_directions(client):
    response = login(client, "patient")

    assert response.headers["Location"].endswith("/patient/")

    for path in (
        "/doctor/",
        "/doctor/profile",
        "/doctor/availability",
        "/doctor/schedule",
    ):
        response = client.get(path)

        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    client.post(
        "/patient/logout",
        data={"csrf_token": ""},
    )

    client2 = create_app().test_client()

    response = login(client2, "doctor")

    assert response.headers["Location"].endswith("/doctor/")
    assert client2.get("/patient/").headers["Location"] == "/"


def test_pages_only_show_the_matching_navigation(client):
    login(client, "doctor")

    html = client.get(
        "/doctor/profile"
    ).get_data(as_text=True)

    assert "Availability" in html
    assert "/patient/" not in html


# -------------------------------------------------------
# Logout and session tests
# -------------------------------------------------------

def test_logout_revokes_tokens_through_the_api_and_clears_the_session(
    client,
    api,
):
    login(client, "doctor")

    token = csrf(client)

    response = client.post(
        "/doctor/logout",
        data={"csrf_token": token},
    )

    assert response.status_code == 302

    logout_call = [
        call for call in api.calls
        if call[1] == "/auth/logout"
    ][0]

    assert logout_call[2] == fake_jwt("doctor")

    assert logout_call[3] == {
        "refresh_token": "R" * 40,
    }

    assert client.get(
        "/doctor/"
    ).headers["Location"].endswith("/doctor/login")


def test_forms_without_a_csrf_token_are_rejected(client, api):
    login(client, "doctor")

    client.post(
        "/doctor/profile",
        data={"first_name": "X"},
    )

    assert not [
        call for call in api.calls
        if call[0] == "PUT"
    ]


# -------------------------------------------------------
# Token refresh tests
# -------------------------------------------------------

def test_expired_access_token_is_refreshed_once_and_the_request_retried(
    client,
    api,
):
    login(client, "doctor")

    api.expired_tokens.add(
        fake_jwt("doctor")
    )

    response = client.get("/doctor/profile")

    assert response.status_code == 200

    assert any(
        call[1] == "/auth/refresh"
        for call in api.calls
    )

    doctor_calls = [
        call[2]
        for call in api.calls
        if call[1] == "/doctors/me"
    ]

    assert doctor_calls[-1].endswith("2")


def test_unrefreshable_session_returns_to_the_landing_page(
    client,
    api,
    monkeypatch,
):
    login(client, "doctor")

    api.expired_tokens.add(
        fake_jwt("doctor")
    )

    real = api.__call__

    def fake_request(method, url, **kwargs):
        if url.endswith("/auth/refresh"):
            return FakeResponse(401, {})

        return real(method, url, **kwargs)

    monkeypatch.setattr(
        api_client.requests,
        "request",
        fake_request,
    )

    response = client.get("/doctor/profile")

    assert response.headers["Location"] == "/"


# -------------------------------------------------------
# Doctor profile tests
# -------------------------------------------------------

def test_profile_save_sends_only_the_form_fields(client, api):
    login(client, "doctor")

    token = csrf(client)

    client.post(
        "/doctor/profile",
        data={
            "csrf_token": token,
            "editing": "1",
            "first_name": "Asha",
            "last_name": "Rao",
            "licence_number": "TS-12345",
            "hospital_id": "h1",
            "specialisation_id": "s1",
            "timezone": "Asia/Kolkata",
            "experience_years": "5",
            "qualifications": "",
        },
    )

    body = [
        call[3]
        for call in api.calls
        if call[0] == "PUT"
        and call[1] == "/doctors/me"
    ][0]

    assert body["licence_number"] == "TS-12345"
    assert "role" not in body
    assert "user_id" not in body
    assert "editing" not in body  # the edit-mode flag never reaches the API


PROFILE_FORM = {
    "first_name": "Asha",
    "last_name": "Rao",
    "licence_number": "TS-12345",
    "hospital_id": "h1",
    "specialisation_id": "s1",
    "timezone": "Asia/Kolkata",
    "experience_years": "7",
    "qualifications": "MBBS, MD",
}


def _profile_puts(api):
    return [c for c in api.calls if c[0] == "PUT" and c[1] == "/doctors/me"]


def test_profile_opens_read_only_with_the_saved_details(client):
    login(client, "doctor")

    html = client.get("/doctor/profile").get_data(as_text=True)

    # every saved detail is shown ...
    for value in ("Asha", "Rao", "TS-12345", "MBBS", "Cardiology", "Demo Hospital", "Asia/Kolkata"):
        assert value in html
    # ... but the fields are locked: disabled fieldset, no edit flag, only "Edit profile" is offered
    assert '<fieldset id="profile-fields"' in html
    fieldset_tag = html.split('<fieldset id="profile-fields"', 1)[1].split(">", 1)[0]
    assert "disabled" in fieldset_tag
    edit_flag = html.split('id="editing-flag"', 1)[1].split(">", 1)[0]
    assert "disabled" in edit_flag
    edit_button = html.split('id="edit-profile"', 1)[1].split(">", 1)[0]
    assert "hidden" not in edit_button
    save_button = html.split('id="save-profile"', 1)[1].split(">", 1)[0]
    assert "hidden" in save_button and "disabled" in save_button


def test_profile_cannot_be_saved_without_clicking_edit(client, api):
    login(client, "doctor")
    token = csrf(client)

    response = client.post(
        "/doctor/profile",
        data=dict(PROFILE_FORM, csrf_token=token),  # no "editing" flag
    )

    assert response.status_code == 302 and response.headers["Location"].endswith("/doctor/profile")
    assert not _profile_puts(api)
    assert "Click Edit profile" in client.get("/doctor/profile").get_data(as_text=True)


def test_saving_returns_to_read_only_mode_with_a_success_message(client, api):
    login(client, "doctor")
    token = csrf(client)

    response = client.post(
        "/doctor/profile",
        data=dict(PROFILE_FORM, csrf_token=token, editing="1"),
    )

    assert response.status_code == 302 and response.headers["Location"].endswith("/doctor/profile")
    assert len(_profile_puts(api)) == 1

    html = client.get("/doctor/profile").get_data(as_text=True)
    assert "Profile saved." in html
    fieldset_tag = html.split('<fieldset id="profile-fields"', 1)[1].split(">", 1)[0]
    assert "disabled" in fieldset_tag  # back to read-only


def test_a_rejected_save_stays_in_edit_mode_with_the_values_kept(client, api):
    api.put_error = (400, {"error": "INVALID_LICENCE", "message": "licence_number is invalid."})
    login(client, "doctor")
    token = csrf(client)

    response = client.post(
        "/doctor/profile",
        data=dict(PROFILE_FORM, csrf_token=token, editing="1", licence_number="BAD"),
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200 and "licence_number is invalid." in html
    assert 'value="BAD"' in html
    fieldset_tag = html.split('<fieldset id="profile-fields"', 1)[1].split(">", 1)[0]
    assert "disabled" not in fieldset_tag
    save_button = html.split('id="save-profile"', 1)[1].split(">", 1)[0]
    assert "hidden" not in save_button


def test_a_doctor_without_a_profile_starts_in_edit_mode(client, api):
    api.profile = None
    login(client, "doctor")

    html = client.get("/doctor/profile").get_data(as_text=True)

    fieldset_tag = html.split('<fieldset id="profile-fields"', 1)[1].split(">", 1)[0]
    assert "disabled" not in fieldset_tag
    edit_button = html.split('id="edit-profile"', 1)[1].split(">", 1)[0]
    assert "hidden" in edit_button
    save_button = html.split('id="save-profile"', 1)[1].split(">", 1)[0]
    assert "hidden" not in save_button


# -------------------------------------------------------
# Availability tests
# -------------------------------------------------------

def test_availability_form_becomes_a_patterns_payload(client, api):
    login(client, "doctor")

    token = csrf(client)

    client.post(
        "/doctor/availability",
        data={
            "csrf_token": token,
            "row_count": "3",

            "p0_id": "",
            "p0_type": "t1",
            "p0_day": "1",
            "p0_start": "09:00",
            "p0_end": "12:00",
            "p0_capacity": "1",

            "p1_id": "a1",
            "p1_type": "t1",
            "p1_day": "2",
            "p1_start": "09:00",
            "p1_end": "12:00",
            "p1_capacity": "1",
            "p1_remove": "on",

            "p2_id": "",
            "p2_type": "",
            "p2_day": "",
            "p2_start": "",
            "p2_end": "",
            "p2_capacity": "1",
        },
    )

    put = [
        call[3]
        for call in api.calls
        if call[0] == "PUT"
        and call[1] == "/doctors/me/availability"
    ][0]

    assert put == {
        "patterns": [
            {
                "consultation_type_id": "t1",
                "day_of_week": "1",
                "start_time": "09:00",
                "end_time": "12:00",
                "capacity": "1",
            }
        ]
    }


# -------------------------------------------------------
# Doctor page rendering tests
# -------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/doctor/",
        "/doctor/profile",
        "/doctor/consultation-types",
        "/doctor/availability",
        "/doctor/blackouts",
        "/doctor/schedule",
        "/doctor/schedule?view=week&date=2026-11-02",
    ],
)
def test_every_doctor_page_renders(client, path):
    login(client, "doctor")

    response = client.get(path)

    assert response.status_code == 200


def test_schedule_lists_booked_patients(client):
    login(client, "doctor")

    html = client.get(
        "/doctor/schedule"
    ).get_data(as_text=True)

    assert "Pat One" in html
    assert "09:00 &ndash; 09:30" in html


def test_week_view_requests_a_monday_to_sunday_range(client, api):
    login(client, "doctor")

    client.get(
        "/doctor/schedule?view=week&date=2026-11-04"
    )

    params = [
        call[4]
        for call in api.calls
        if call[1] == "/doctors/me/schedule"
    ][-1]

    assert params == {
        "from": "2026-11-02",
        "to": "2026-11-08",
    }


# -------------------------------------------------------
# Registration tests
# -------------------------------------------------------

def test_registration_forms_post_to_the_role_specific_api_route(
    client,
    api,
):
    token = page_token(client)

    client.post(
        "/patient/register",
        data={
            "email": "p@b.co",
            "password": "Passw0rd123",
            "first_name": "P",
            "last_name": "Q",
            "csrf_token": token,
        },
    )

    client.post(
        "/doctor/register",
        data={
            "email": "d@b.co",
            "password": "Passw0rd123",
            "csrf_token": token,
        },
    )

    paths = [
        call[1]
        for call in api.calls
    ]

    assert "/auth/patients/register" in paths
    assert "/auth/doctors/register" in paths


# -------------------------------------------------------
# Login and CSRF regression tests
# -------------------------------------------------------

def test_login_and_register_forms_carry_a_csrf_token(client):
    paths = (
        "/doctor/login",
        "/patient/login",
        "/doctor/register",
        "/patient/register",
    )

    for path in paths:
        html = client.get(path).get_data(as_text=True)

        assert 'name="csrf_token"' in html, path


def test_login_without_a_csrf_token_is_rejected_and_never_calls_the_api(
    client,
    api,
):
    response = client.post(
        "/doctor/login",
        data={
            "email": "a@b.co",
            "password": "Passw0rd123",
        },
    )

    assert response.status_code == 302

    assert response.headers[
        "Location"
    ].endswith("/doctor/login")

    assert not [
        call for call in api.calls
        if call[1].endswith("/login")
    ]


def test_login_with_a_tampered_csrf_token_is_rejected(client, api):
    page_token(client)

    client.post(
        "/doctor/login",
        data={
            "email": "a@b.co",
            "password": "Passw0rd123",
            "csrf_token": "forged",
        },
    )

    assert not [
        call for call in api.calls
        if call[1].endswith("/login")
    ]


def test_signing_in_works_when_the_browser_already_has_a_session(
    client,
    api,
):
    """A leftover session must not make later login fail."""

    response = login(client, "doctor")

    assert response.headers["Location"].endswith("/doctor/")

    response = client.get("/doctor/login")

    assert response.headers["Location"].endswith("/doctor/")

    response = client.post(
        "/doctor/login",
        data={
            "email": "a@b.co",
            "password": "Passw0rd123",
            "csrf_token": csrf(client),
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/doctor/")

    assert len(sessions._STORE) == 1


def test_a_patient_can_sign_in_while_a_doctor_session_exists(client):
    login(client, "doctor")

    token = csrf(client)

    response = client.post(
        "/patient/login",
        data={
            "email": "p@b.co",
            "password": "Passw0rd123",
            "csrf_token": token,
        },
    )

    assert response.headers["Location"].endswith("/patient/")


def test_wrong_credentials_show_the_api_message_and_keep_the_form(
    client,
    api,
    monkeypatch,
):
    real = api.__call__

    def fake_request(method, url, **kwargs):
        if url.endswith("/doctors/login"):
            return FakeResponse(
                401,
                {
                    "error": "INVALID_CREDENTIALS",
                    "message": "Email or password is incorrect.",
                },
            )

        return real(method, url, **kwargs)

    monkeypatch.setattr(
        api_client.requests,
        "request",
        fake_request,
    )

    response = client.post(
        "/doctor/login",
        data={
            "email": "a@b.co",
            "password": "bad",
            "csrf_token": page_token(client),
        },
    )

    assert "Email or password is incorrect." in (
        response.get_data(as_text=True)
    )


def test_logout_then_login_again_works(client, api):
    login(client, "doctor")

    client.post(
        "/doctor/logout",
        data={"csrf_token": csrf(client)},
    )

    assert client.get(
        "/doctor/"
    ).headers["Location"].endswith("/doctor/login")

    response = login(client, "doctor")

    assert response.headers["Location"].endswith("/doctor/")

# -------------------------------------------------------
# Health Camp seats per slot and blackout confirmation
# -------------------------------------------------------

CAMP_TYPE = {
    "consultation_type_id": "t2",
    "type_name": "Health Camp Session",
    "duration_minutes": 60,
    "fee": 200.0,
}


def test_availability_page_has_a_seats_input_limited_to_the_camp_range(client, api):
    api.extra_types = [CAMP_TYPE]
    login(client, "doctor")

    html = client.get("/doctor/availability").get_data(as_text=True)

    assert 'name="p__I___capacity"' in html
    assert 'min="2"' in html and 'max="10"' in html
    # Only the camp type is flagged so the input shows for it alone.
    assert 'data-camp="1"' in html and 'data-camp="0"' in html


def test_availability_form_sends_the_chosen_camp_capacity(client, api):
    api.extra_types = [CAMP_TYPE]
    login(client, "doctor")

    client.post(
        "/doctor/availability",
        data={
            "csrf_token": csrf(client),
            "row_count": "1",
            "p0_id": "",
            "p0_type": "t2",
            "p0_day": "6",
            "p0_start": "09:00",
            "p0_end": "12:00",
            "p0_capacity": "8",
        },
    )

    put = [c[3] for c in api.calls if c[0] == "PUT" and c[1] == "/doctors/me/availability"][0]

    assert put["patterns"][0]["capacity"] == "8"


def test_blackout_with_existing_bookings_asks_the_doctor_to_choose(client, api):
    api.has_bookings_on_blackout = True
    login(client, "doctor")

    response = client.post(
        "/doctor/blackouts",
        data={"csrf_token": csrf(client), "blackout_date": "2026-11-02", "reason": "Leave"},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "already booked on 2026-11-02" in html
    assert 'name="existing_bookings" value="keep"' in html
    assert "Pat One" in html


def test_blackout_confirmation_passes_the_choice_to_the_api(client, api):
    api.has_bookings_on_blackout = True
    login(client, "doctor")

    client.post(
        "/doctor/blackouts",
        data={
            "csrf_token": csrf(client),
            "blackout_date": "2026-11-02",
            "reason": "Leave",
            "existing_bookings": "keep",
        },
    )

    posts = [c[3] for c in api.calls if c[0] == "POST" and c[1] == "/doctors/me/blackouts"]

    assert posts[-1]["existing_bookings"] == "keep"


# -------------------------------------------------------
# Milestone 5: search filter, booking, appointments, refunds, doctor slot cancellation
# -------------------------------------------------------

def test_the_landing_search_has_no_consultation_type_filter(client):
    login(client, "patient")
    html = client.get("/patient/").get_data(as_text=True)
    assert 'name="consultation_type"' not in html and "Specialisation" in html


def test_the_slots_page_has_a_consultation_type_filter_that_is_sent_to_the_api(client, api):
    login(client, "patient")
    html = client.get("/patient/slots/d9?date=2026-11-13&consultation_type=Quick+Consult").get_data(as_text=True)
    assert 'name="consultation_type"' in html and "Standard Consult" in html
    for kind in ("Quick Consult", "Standard Consult", "Detailed Consult", "Health Camp Session"):
        assert f'value="{kind}"' in html, kind
    assert 'value="Quick Consult" selected' in html
    params = [c[4] for c in api.calls if c[1] == "/doctors/d9/slots"][-1]
    assert params["consultation_type"] == "Quick Consult"


def test_the_slots_page_has_a_book_button_with_a_csrf_token_and_an_idempotency_key(client):
    login(client, "patient")
    html = client.get("/patient/slots/d9?date=2026-11-13").get_data(as_text=True)
    assert 'action="/patient/book"' in html and 'name="slot_id" value="slot#1"' in html
    assert 'name="idempotency_key"' in html and 'name="csrf_token"' in html and "1 of 3 seats free" in html


def test_booking_sends_the_slot_and_an_idempotency_key_then_shows_the_appointment(client, api):
    login(client, "patient")
    token = extract_csrf_token(client.get("/patient/slots/d9?date=2026-11-13").get_data(as_text=True), "slots")
    response = client.post("/patient/book", data={"slot_id": "slot#1", "doctor_id": "d9", "idempotency_key": "key-1",
                                                 "csrf_token": token})
    # booking returns to the list; cancelling is done from there, not on the confirmation
    assert response.headers["Location"].endswith("/patient/appointments")
    method, path, _, body, _ = [c for c in api.calls if c[1] == "/appointments" and c[0] == "POST"][-1]
    assert body == {"slot_id": "slot#1"}
    sent = [h for m, p, h in api.headers_seen if (m, p) == ("POST", "/appointments")][-1]
    assert sent["Idempotency-Key"] == "key-1"
    page = client.get("/patient/appointments").get_data(as_text=True)
    assert "Your appointment is confirmed" in page and "Cancel appointment" not in page


def test_a_full_slot_shows_the_api_message_and_returns_to_the_slots(client, api):
    api.book_error = (409, {"error": "SLOT_FULL", "message": "This slot is full."})
    login(client, "patient")
    token = extract_csrf_token(client.get("/patient/slots/d9?date=2026-11-13").get_data(as_text=True), "slots")
    response = client.post("/patient/book", data={"slot_id": "slot#1", "doctor_id": "d9", "date": "2026-11-13",
                                                 "csrf_token": token})
    assert "/patient/slots/d9" in response.headers["Location"]
    assert "This slot is full." in client.get(response.headers["Location"]).get_data(as_text=True)


def test_booking_needs_a_csrf_token(client, api):
    login(client, "patient")
    client.post("/patient/book", data={"slot_id": "slot#1"})  # rejected before it reaches the route
    assert not [c for c in api.calls if c[1] == "/appointments" and c[0] == "POST"]


def test_my_appointments_page_lists_and_links_to_details(client, api):
    login(client, "patient")
    html = client.get("/patient/appointments").get_data(as_text=True)
    assert "Dr. Asha Rao" in html and "/patient/appointments/a1" in html and "Upcoming" in html
    assert [c[4] for c in api.calls if c[1] == "/appointments"][-1]["scope"] == "upcoming"
    client.get("/patient/appointments?scope=past&offset=10")
    assert [c[4] for c in api.calls if c[1] == "/appointments"][-1] == {"scope": "past", "limit": 10, "offset": 10}
    client.get("/patient/appointments?scope=bogus")
    assert [c[4] for c in api.calls if c[1] == "/appointments"][-1]["scope"] == "upcoming"


def test_appointment_detail_shows_the_refund_preview_and_a_cancel_form(client):
    login(client, "patient")
    html = client.get("/patient/appointments/a1").get_data(as_text=True)
    assert "&#8377;500.00" in html and "Cancel appointment" in html and "/patient/appointments/a1/cancel" in html
    assert "48 hours or more" in html


def test_cancelling_calls_the_api_and_reports_the_refund(client, api):
    login(client, "patient")
    token = extract_csrf_token(client.get("/patient/appointments/a1").get_data(as_text=True), "detail")
    response = client.post("/patient/appointments/a1/cancel", data={"reason": "plans changed", "csrf_token": token})
    assert response.headers["Location"].endswith("/patient/appointments/a1")
    assert [c[3] for c in api.calls if c[1] == "/appointments/a1/cancel"][-1] == {"reason": "plans changed"}
    assert "Refund: \u20b9500.00 (100%)" in client.get("/patient/appointments/a1").get_data(as_text=True)


def test_refund_ledger_page_shows_each_refund_and_the_total(client):
    login(client, "patient")
    html = client.get("/patient/refunds").get_data(as_text=True)
    assert 'id="refund-ledger"' in html and "&#8377;375.00" in html and "Tier 75" in html and "Dr. Asha Rao" in html


def test_navigation_links_to_the_new_pages(client):
    html = login(client, "patient") and client.get("/patient/").get_data(as_text=True)
    assert "/patient/appointments" in html and "/patient/refunds" in html
    c2 = create_app().test_client()
    login(c2, "doctor")
    assert "/doctor/appointments" in c2.get("/doctor/").get_data(as_text=True)


def test_doctor_appointments_page_lists_patients(client, api):
    login(client, "doctor")
    html = client.get("/doctor/appointments?status=CONFIRMED&from=2026-11-01").get_data(as_text=True)
    assert "Pat One" in html and 'id="doctor-appointments"' in html
    assert [c[4] for c in api.calls if c[1] == "/doctors/me/appointments"][-1] == {
        "limit": 15, "offset": 0, "from": "2026-11-01", "status": "CONFIRMED"}


def test_schedule_has_a_cancel_slot_form_and_cancelling_reports_the_refunds(client, api):
    login(client, "doctor")
    html = client.get("/doctor/schedule").get_data(as_text=True)
    assert 'action="/doctor/slots/cancel"' in html and "Cancel slot" in html
    token = extract_csrf_token(html, "schedule")
    response = client.post("/doctor/slots/cancel", data={"slot_id": "x", "reason": "Storm", "date": "2026-11-02",
                                                        "csrf_token": token})
    assert "/doctor/schedule" in response.headers["Location"]
    assert [c[3] for c in api.calls if c[1] == "/doctors/me/slots/cancel"][-1] == {"slot_id": "x", "reason": "Storm"}
    assert "2 appointment(s) refunded in full" in client.get(response.headers["Location"]).get_data(as_text=True)


def test_patients_cannot_open_the_doctor_pages_and_doctors_cannot_book(client):
    login(client, "patient")
    assert client.get("/doctor/appointments").status_code in (302, 403)
    c2 = create_app().test_client()
    login(c2, "doctor")
    assert c2.get("/patient/appointments").status_code in (302, 403)


def test_patient_and_doctor_pages_render_slots_with_the_same_shared_tile(client):
    login(client, "patient")
    patient_html = client.get("/patient/slots/d9?date=2026-11-13").get_data(as_text=True)
    assert 'class="slot-tile' in patient_html and 'data-slot="slot#1"' in patient_html
    template = (Path(__file__).resolve().parents[2] / "web" / "templates")
    for page in ("patient/slots.html", "doctor/schedule.html"):
        assert 'import slot_card' in (template / page).read_text(), page
        assert "slot-tile" not in (template / page).read_text(), "tile markup must live only in _slot_card.html"


def test_the_slots_page_asks_for_the_whole_30_day_window_by_default(client, api):
    login(client, "patient")
    html = client.get("/patient/slots/d9?date=2026-11-23").get_data(as_text=True)
    params = [c[4] for c in api.calls if c[1].endswith("/slots")][-1]
    assert params == {"from": "2026-11-23", "to": "2026-12-22"}
    assert 'value="2026-12-22"' in html
    client.get("/patient/slots/d9?date=2026-11-23&to=2026-11-25")
    assert [c[4] for c in api.calls if c[1].endswith("/slots")][-1] == {"from": "2026-11-23", "to": "2026-11-25"}


def test_a_date_beyond_the_booking_window_shows_the_message_and_the_default_window(client, api):
    login(client, "patient")
    html = client.get("/patient/slots/d9?date=2026-11-23&to=2028-01-01").get_data(as_text=True)
    assert "Slots can only be viewed up to 2026-12-23 (30 days ahead)." in html
    assert "Book" in html  # the page still lists the default window's slots


def test_the_doctor_dashboard_lists_todays_patients_with_a_no_show_button(client, api):
    login(client, "doctor")
    html = client.get("/doctor/").get_data(as_text=True)
    assert "Today's patients" in html and "Pat One" in html
    assert 'action="/doctor/appointments/appt-1/attendance"' in html and "No-show" in html and 'name="next" value="home"' in html


def test_marking_a_no_show_from_the_dashboard_returns_to_the_dashboard(client, api):
    login(client, "doctor")
    token = extract_csrf_token(client.get("/doctor/").get_data(as_text=True), "attendance")
    response = client.post("/doctor/appointments/appt-1/attendance",
                           data={"csrf_token": token, "outcome": "no-show", "next": "home"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/doctor/")
    assert [c for c in api.calls if c[1] == "/doctors/me/appointments/appt-1/no-show"]
