"""Flask web app: role guards, session handling, token refresh and doctor forms.
The REST API is replaced by a fake, so no network is used.
"""

import base64
import json
from html.parser import HTMLParser

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

        if path in ("/doctors/search", "/appointments") and method == "GET":
            return FakeResponse(200, {"items": []})

        if path == "/waitlist" and method == "GET":
            return FakeResponse(
                200,
                {
                    "items": [
                        {"waitlist_id": "w1", "slot_id": "s1", "doctor_name": "Dr. Asha Rao",
                         "type_name": "Standard Consult", "date": "2026-11-20", "start_time": "10:00"}
                    ],
                    "offers": [
                        {"offer_id": "o1", "slot_id": "s2", "doctor_name": "Dr. Asha Rao",
                         "type_name": "Standard Consult", "date": "2026-11-21", "start_time": "11:00"}
                    ],
                },
            )

        if path == "/waitlist/w1" and method == "DELETE":
            return FakeResponse(200, {"message": "Removed from waitlist."})

        if path == "/waitlist/offers/o1/accept" and method == "POST":
            return FakeResponse(201, {"appointment_id": "a1", "status": "CONFIRMED"})

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
                    "appointments_cancelled": 1 if body.get("existing_bookings") == "cancel" else 0,
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
    assert "09:00 - 09:30" in html


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
    assert 'name="existing_bookings" value="cancel"' in html
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
            "existing_bookings": "cancel",
        },
    )

    posts = [c[3] for c in api.calls if c[0] == "POST" and c[1] == "/doctors/me/blackouts"]

    assert posts[-1]["existing_bookings"] == "cancel"


def test_patient_home_lists_waitlist_entries_and_held_offers(client, api):
    login(client, "patient")

    html = client.get("/patient/").get_data(as_text=True)

    assert "My waitlist" in html
    assert "A seat is being held for you" in html and "2026-11-21" in html
    assert "/patient/offers/o1/accept" in html
    assert "/patient/waitlist/w1/leave" in html


def test_patient_can_accept_an_offer_and_leave_the_waitlist(client, api):
    login(client, "patient")
    token = extract_csrf_token(client.get("/patient/").get_data(as_text=True), "/patient/")

    client.post("/patient/offers/o1/accept", data={"csrf_token": token})
    client.post("/patient/waitlist/w1/leave", data={"csrf_token": token})

    made = [(c[0], c[1]) for c in api.calls]

    assert ("POST", "/waitlist/offers/o1/accept") in made
    assert ("DELETE", "/waitlist/w1") in made
