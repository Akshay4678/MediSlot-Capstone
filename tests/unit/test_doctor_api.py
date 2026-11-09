"""Doctor API: validation rules, role enforcement and routing (no AWS, no database)."""

import json

import pytest

from application.common import rules
from application.common.api import ApiError, as_time, fmt_time
from application.doctor_api import availability, consultation_types, handler, profile

DOC = "11111111-1111-4111-8111-111111111111"
T_QUICK = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T_STD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
T_CAMP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

TYPES = {
    T_QUICK: {
        "consultation_type_id": T_QUICK,
        "type_name": "Quick Consult",
        "duration_minutes": 15,
    },
    T_STD: {
        "consultation_type_id": T_STD,
        "type_name": "Standard Consult",
        "duration_minutes": 30,
    },
    T_CAMP: {
        "consultation_type_id": T_CAMP,
        "type_name": "Health Camp Session",
        "duration_minutes": 60,
    },
}


def item(
    type_id=T_STD,
    day=1,
    start="09:00",
    end="12:00",
    **extra,
):
    return dict(
        {
            "consultation_type_id": type_id,
            "day_of_week": day,
            "start_time": start,
            "end_time": end,
        },
        **extra,
    )


def code_of(call):
    try:
        call()
    except ApiError as exc:
        return exc.code

    return None


# ------------------------------------------------------------------
# rules (SSM)
def test_frozen_durations_come_from_the_rules_module():
    assert rules.canonical_type("quick consult") == (
        "Quick Consult",
        15,
    )

    assert rules.canonical_type("STANDARD CONSULT") == (
        "Standard Consult",
        30,
    )

    assert rules.canonical_type("Detailed Consult") == (
        "Detailed Consult",
        60,
    )

    assert rules.canonical_type("Health Camp Session") == (
        "Health Camp Session",
        60,
    )

    assert rules.canonical_type("Magic Consult") is None

    assert (
        rules.health_camp_capacity_bounds() == (2, 10)
        and rules.booking_window_days() == 30
    )


def test_time_helpers_accept_mysql_timedelta():
    from datetime import timedelta

    assert (
        fmt_time(timedelta(hours=9, minutes=5)) == "09:05"
        and as_time("17:30:00").hour == 17
    )


# ------------------------------------------------------------
# availability rules
def test_valid_patterns_are_accepted_and_defaults_applied():
    clean = availability.validate(
        [
            item(),
            item(
                T_CAMP,
                2,
                "10:00",
                "12:00",
            ),
        ],
        TYPES,
        set(),
    )

    # Normal consultation = 1 seat.
    # Health Camp default = 5 seats.
    assert [p["capacity"] for p in clean] == [1, 5]


@pytest.mark.parametrize(
    "bad,expected",
    [
        (
            item(day=0),
            "INVALID_DAY",
        ),
        (
            item(day=8),
            "INVALID_DAY",
        ),
        (
            item(day="x"),
            "INVALID_DAY",
        ),
        (
            item(start="9:00"),
            "INVALID_TIME",
        ),
        (
            item(end="25:00"),
            "INVALID_TIME",
        ),
        (
            item(
                start="12:00",
                end="09:00",
            ),
            "INVALID_TIME_RANGE",
        ),
        (
            item(
                T_STD,
                1,
                "09:00",
                "09:20",
            ),
            "WINDOW_TOO_SHORT",
        ),
        (
            item(
                "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            ),
            "UNKNOWN_CONSULTATION_TYPE",
        ),
        (
            item(
                availability_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            ),
            "UNKNOWN_AVAILABILITY",
        ),
    ],
)
def test_invalid_patterns_are_rejected(bad, expected):
    assert (
        code_of(
            lambda: availability.validate(
                [bad],
                TYPES,
                set(),
            )
        )
        == expected
    )


def test_health_camp_capacity_is_not_rejected_by_availability_validation():
    """Health Camp seat enforcement is handled by the booking engine."""

    for capacity in (1, 2, 5, 6, 10, 20):
        clean = availability.validate(
            [
                item(
                    T_CAMP,
                    start="09:00",
                    end="10:00",
                    capacity=capacity,
                )
            ],
            TYPES,
            set(),
        )

        assert clean[0]["capacity"] == capacity


def test_health_camp_capacity_defaults_to_five():
    clean = availability.validate(
        [
            item(
                T_CAMP,
                start="09:00",
                end="10:00",
            )
        ],
        TYPES,
        set(),
    )

    assert clean[0]["capacity"] == 5


def test_normal_consultation_capacity_is_always_one():
    clean = availability.validate(
        [
            item(
                T_STD,
                start="09:00",
                end="10:00",
                capacity=5,
            )
        ],
        TYPES,
        set(),
    )

    assert clean[0]["capacity"] == 1


def test_overlapping_patterns_on_the_same_day_are_rejected_but_adjacent_ones_are_fine():
    assert (
        code_of(
            lambda: availability.validate(
                [
                    item(
                        start="09:00",
                        end="12:00",
                    ),
                    item(
                        T_QUICK,
                        start="11:30",
                        end="13:00",
                    ),
                ],
                TYPES,
                set(),
            )
        )
        == "OVERLAPPING_PATTERNS"
    )

    assert availability.validate(
        [
            item(
                start="09:00",
                end="12:00",
            ),
            item(
                T_QUICK,
                start="12:00",
                end="13:00",
            ),
        ],
        TYPES,
        set(),
    )

    assert availability.validate(
        [
            item(day=1),
            item(day=2),
        ],
        TYPES,
        set(),
    )


def test_pattern_limit():
    many = [
        item(
            day=1 + i % 7,
            start="09:00",
            end="09:30",
            type_id=T_QUICK,
        )
        for i in range(51)
    ]

    assert (
        code_of(
            lambda: availability.validate(
                many,
                TYPES,
                set(),
            )
        )
        == "TOO_MANY_PATTERNS"
    )


# -------------------------------------------------------------------
# profile
FULL = {
    "first_name": "Asha",
    "last_name": "Rao",
    "licence_number": "ts-12345",
    "hospital_id": "00000000-0000-4000-8000-000000000001",
    "specialisation_id": "10000000-0000-4000-8000-000000000001",
    "timezone": "Asia/Kolkata",
}


def test_profile_create_requires_all_core_fields():
    for missing in (
        "first_name",
        "last_name",
        "licence_number",
        "hospital_id",
        "specialisation_id",
        "timezone",
    ):
        body = {
            k: v
            for k, v in FULL.items()
            if k != missing
        }

        assert (
            code_of(
                lambda: profile._validate(
                    body,
                    creating=True,
                )
            )
            == "MISSING_FIELD"
        ), missing


def test_profile_validation_normalises_and_rejects_bad_values():
    values = profile._validate(
        dict(
            FULL,
            experience_years="12",
        ),
        creating=True,
    )

    assert (
        values["licence_number"] == "TS-12345"
        and values["experience_years"] == 12
    )

    assert (
        code_of(
            lambda: profile._validate(
                {"timezone": "Mars/Base"},
                creating=False,
            )
        )
        == "INVALID_TIMEZONE"
    )

    assert (
        code_of(
            lambda: profile._validate(
                {"licence_number": "x"},
                creating=False,
            )
        )
        == "INVALID_LICENCE"
    )

    assert (
        code_of(
            lambda: profile._validate(
                {"experience_years": 99},
                creating=False,
            )
        )
        == "INVALID_FIELD"
    )

    assert (
        code_of(
            lambda: profile._validate(
                {"hospital_id": "nope"},
                creating=False,
            )
        )
        == "INVALID_ID"
    )

    assert profile._validate(
        {"first_name": "New"},
        creating=False,
    ) == {"first_name": "New"}


def test_profile_cannot_set_photo_or_other_protected_columns():
    assert (
        profile._validate(
            {
                "photo_url": "x",
                "is_active": False,
                "user_id": "x",
            },
            creating=False,
        )
        == {}
    )


# -------------------------------------------------------
# consultation types
class Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.conn.executed.append(
            (sql, params)
        )

        self.rows = next(
            (
                rows
                for key, rows in self.conn.script
                if key in sql
            ),
            [],
        )

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, script=()):
        self.script = list(script)
        self.executed = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeCtx:
    def __init__(
        self,
        body=None,
        params=None,
        conn=None,
    ):
        self.body = body or {}
        self.params = params or {}
        self.conn = conn or Conn()
        self.query = {}
        self.user_id = "u-1"

    def doctor(self):
        return {
            "doctor_id": DOC,
            "timezone": "Asia/Kolkata",
            "is_active": True,
        }


def test_consultation_type_rules():
    ctx = lambda body: FakeCtx(
        body,
        conn=Conn(
            [
                (
                    "FROM consultation_types",
                    [
                        {
                            "consultation_type_id": "x",
                            "duration_minutes": 15,
                        }
                    ],
                ),
                (
                    "FROM doctor_consultation_types",
                    [
                        {
                            "consultation_type_id": "x",
                            "type_name": "Quick Consult",
                            "duration_minutes": 15,
                            "fee": 400,
                        }
                    ],
                ),
            ]
        ),
    )

    assert (
        code_of(
            lambda: consultation_types.create(
                ctx(
                    {
                        "type_name": "Spa Day",
                        "fee": 10,
                    }
                )
            )
        )
        == "INVALID_TYPE_NAME"
    )

    assert (
        code_of(
            lambda: consultation_types.create(
                ctx(
                    {
                        "type_name": "Quick Consult",
                        "fee": 10,
                        "duration_minutes": 20,
                    }
                )
            )
        )
        == "INVALID_DURATION"
    )

    assert (
        code_of(
            lambda: consultation_types.create(
                ctx(
                    {
                        "type_name": "Quick Consult",
                    }
                )
            )
        )
        == "MISSING_FIELD"
    )

    assert (
        code_of(
            lambda: consultation_types.create(
                ctx(
                    {
                        "type_name": "Quick Consult",
                        "fee": -1,
                    }
                )
            )
        )
        == "INVALID_FEE"
    )

    assert (
        code_of(
            lambda: consultation_types.create(
                ctx(
                    {
                        "type_name": "Quick Consult",
                        "fee": "abc",
                    }
                )
            )
        )
        == "INVALID_FEE"
    )

    status, body = consultation_types.create(
        ctx(
            {
                "type_name": "quick consult",
                "fee": "400",
            }
        )
    )

    assert (
        status == 201
        and body["consultation_type"]["duration_minutes"] == 15
    )

    insert = [
        p
        for s, p in ctx({}).conn.executed
        if s.startswith("INSERT")
    ]

    assert insert == []


def test_consultation_type_create_adds_a_catalogue_row_and_the_callers_own_fee():
    conn = Conn(
        [
            (
                "FROM consultation_types",
                [
                    {
                        "consultation_type_id": "cat-1",
                        "duration_minutes": 30,
                    }
                ],
            ),
            (
                "FROM doctor_consultation_types",
                [
                    {
                        "consultation_type_id": "cat-1",
                        "type_name": "Standard Consult",
                        "duration_minutes": 30,
                        "fee": 500,
                    }
                ],
            ),
        ]
    )

    consultation_types.create(
        FakeCtx(
            {
                "type_name": "Standard Consult",
                "fee": 500,
            },
            conn=conn,
        )
    )

    inserts = [
        e
        for e in conn.executed
        if e[0].startswith("INSERT")
    ]

    catalogue = [
        e
        for e in inserts
        if "INTO consultation_types" in e[0]
    ][0]

    offering = [
        e
        for e in inserts
        if "INTO doctor_consultation_types" in e[0]
    ][0]

    assert "doctor_id" not in catalogue[0]
    assert "fee" not in catalogue[0]

    assert (
        catalogue[1][1] == "Standard Consult"
        and catalogue[1][2] == 30
    )

    assert (
        offering[1][1] == DOC
        and offering[1][2] == "cat-1"
        and str(offering[1][3]) == "500.00"
    )


def test_two_doctors_can_offer_the_same_type_with_different_fees():
    def create_for(doctor_id, fee):
        conn = Conn(
            [
                (
                    "FROM consultation_types",
                    [
                        {
                            "consultation_type_id": "cat-1",
                            "duration_minutes": 30,
                        }
                    ],
                ),
                (
                    "FROM doctor_consultation_types",
                    [
                        {
                            "consultation_type_id": "cat-1",
                            "type_name": "Standard Consult",
                            "duration_minutes": 30,
                            "fee": fee,
                        }
                    ],
                ),
            ]
        )

        ctx = FakeCtx(
            {
                "type_name": "Standard Consult",
                "fee": fee,
            },
            conn=conn,
        )

        ctx.doctor = lambda: {
            "doctor_id": doctor_id,
            "timezone": "Asia/Kolkata",
            "is_active": True,
        }

        consultation_types.create(ctx)

        return [
            e
            for e in conn.executed
            if "INTO doctor_consultation_types" in e[0]
        ][0][1]

    first = create_for(DOC, 500)
    second = create_for(
        "22222222-2222-4222-8222-222222222222",
        900,
    )

    assert first[2] == second[2] == "cat-1"
    assert first[1] != second[1]
    assert first[3] != second[3]


def test_only_fee_can_be_updated():
    row = {
        "consultation_type_id": T_QUICK,
        "type_name": "Quick Consult",
        "duration_minutes": 15,
        "fee": 400,
    }

    conn = Conn(
        [
            (
                "FROM doctor_consultation_types",
                [row],
            )
        ]
    )

    assert (
        code_of(
            lambda: consultation_types.update(
                FakeCtx(
                    {"type_name": "Detailed Consult"},
                    {"id": T_QUICK},
                    conn,
                )
            )
        )
        == "TYPE_NAME_IMMUTABLE"
    )

    assert (
        code_of(
            lambda: consultation_types.update(
                {"duration_minutes": 60},
                {"id": T_QUICK},
                conn,
            )
        )
        == "INVALID_DURATION"
    )

    assert (
        code_of(
            lambda: consultation_types.update(
                {},
                {"id": T_QUICK},
                conn,
            )
        )
        == "NOTHING_TO_UPDATE"
    )

    assert (
        consultation_types.update(
            FakeCtx(
                {"fee": 450},
                {"id": T_QUICK},
                conn,
            )
        )[0]
        == 200
    )

    update_sql, update_params = [
        e
        for e in conn.executed
        if e[0].startswith("UPDATE")
    ][0]

    assert (
        "doctor_consultation_types" in update_sql
        and update_params[1:] == (DOC, T_QUICK)
    )


def test_delete_only_removes_the_callers_offering_not_the_shared_catalogue_row():
    row = {
        "consultation_type_id": T_QUICK,
        "type_name": "Quick Consult",
        "duration_minutes": 15,
        "fee": 400,
    }

    conn = Conn(
        [
            (
                "FROM doctor_consultation_types",
                [row],
            )
        ]
    )

    assert (
        consultation_types.delete(
            FakeCtx(
                {},
                {"id": T_QUICK},
                conn,
            )
        )[0]
        == 200
    )

    sql, params = [
        e
        for e in conn.executed
        if e[0].startswith("DELETE")
    ][0]

    assert (
        "doctor_consultation_types" in sql
        and "FROM consultation_types" not in sql
        and params == (DOC, T_QUICK)
    )

    empty = Conn(
        [
            (
                "FROM doctor_consultation_types",
                [],
            )
        ]
    )

    assert (
        code_of(
            lambda: consultation_types.delete(
                FakeCtx(
                    {},
                    {"id": T_QUICK},
                    empty,
                )
            )
        )
        == "CONSULTATION_TYPE_NOT_FOUND"
    )


# --------------------------------------------------------------------
# router
def event(
    method,
    resource,
    role="doctor",
    user="u-1",
    body=None,
    with_auth=True,
):
    auth = (
        {
            "userId": user,
            "role": role,
        }
        if with_auth
        else {}
    )

    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "body": json.dumps(body) if body else None,
        "requestContext": {
            "authorizer": auth,
        },
    }


def test_patient_token_is_rejected_on_every_doctor_route():
    for method, resource in handler.ROUTES:
        if resource.startswith("/doctors/me"):
            assert (
                handler.lambda_handler(
                    event(
                        method,
                        resource,
                        role="patient",
                    ),
                    None,
                )["statusCode"]
                == 403
            ), resource


def test_missing_authorizer_context_is_401_and_unknown_routes_are_404_405():
    assert (
        handler.lambda_handler(
            event(
                "GET",
                "/doctors/me",
                with_auth=False,
            ),
            None,
        )["statusCode"]
        == 401
    )

    assert (
        handler.lambda_handler(
            event(
                "GET",
                "/nope",
            ),
            None,
        )["statusCode"]
        == 404
    )

    assert (
        handler.lambda_handler(
            event(
                "PATCH",
                "/doctors/me",
            ),
            None,
        )["statusCode"]
        == 405
    )


def test_acting_user_comes_from_the_authorizer_not_the_request(monkeypatch):
    seen = {}

    conn = Conn(
        [
            (
                "FROM doctors WHERE user_id",
                [
                    {
                        "doctor_id": DOC,
                        "timezone": "Asia/Kolkata",
                        "is_active": 1,
                    }
                ],
            ),
            (
                "FROM availability_patterns",
                [],
            ),
        ]
    )

    monkeypatch.setattr(
        handler,
        "get_connection",
        lambda: conn,
    )

    monkeypatch.setitem(
        handler.ROUTES,
        ("GET", "/doctors/me/availability"),
        lambda ctx: seen.update(user=ctx.user_id) or (200, {}),
    )

    evt = event(
        "GET",
        "/doctors/me/availability",
        user="real-user",
    )

    evt["queryStringParameters"] = {
        "userId": "attacker",
        "user_id": "attacker",
    }

    evt["headers"] = {
        "X-User-Id": "attacker",
    }

    evt["body"] = json.dumps(
        {
            "user_id": "attacker",
            "role": "doctor",
        }
    )

    assert (
        handler.lambda_handler(evt, None)["statusCode"] == 200
    )

    assert (
        seen["user"] == "real-user"
        and conn.committed
        and conn.closed
    )


def test_api_errors_roll_back_and_unexpected_errors_are_500_without_details(
    monkeypatch,
):
    conn = Conn()

    monkeypatch.setattr(
        handler,
        "get_connection",
        lambda: conn,
    )

    def boom(ctx):
        raise ApiError(
            409,
            "PROFILE_REQUIRED",
            "nope",
        )

    monkeypatch.setitem(
        handler.ROUTES,
        ("GET", "/doctors/me/schedule"),
        boom,
    )

    result = handler.lambda_handler(
        event(
            "GET",
            "/doctors/me/schedule",
        ),
        None,
    )

    assert (
        result["statusCode"] == 409
        and json.loads(result["body"])["error"]
        == "PROFILE_REQUIRED"
        and conn.rolled_back
    )

    def crash(ctx):
        raise RuntimeError(
            "secret internal detail"
        )

    monkeypatch.setitem(
        handler.ROUTES,
        ("GET", "/doctors/me/schedule"),
        crash,
    )

    result = handler.lambda_handler(
        event(
            "GET",
            "/doctors/me/schedule",
        ),
        None,
    )

    assert (
        result["statusCode"] == 500
        and "secret internal detail"
        not in result["body"]
    )


def test_doctor_without_profile_gets_a_clear_409(monkeypatch):
    conn = Conn(
        [
            (
                "FROM doctors WHERE user_id",
                [],
            )
        ]
    )

    monkeypatch.setattr(
        handler,
        "get_connection",
        lambda: conn,
    )

    result = handler.lambda_handler(
        event(
            "GET",
            "/doctors/me/availability",
        ),
        None,
    )

    assert (
        result["statusCode"] == 409
        and json.loads(result["body"])["error"]
        == "PROFILE_REQUIRED"
    )