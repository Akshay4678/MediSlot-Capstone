"""Milestone 5 at the API boundary: patient and doctor Lambda routes, schema upgrade, shared slot query, search filter."""

import json
from datetime import timedelta

import pytest
from booking_support import NOW, SCHEMA, add_slot, make_deps, seed
from sqlite_mysql import Conn

from application.database import init_schema as db_schema
from application.doctor_api import appointments as doctor_appointments, handler as doctor_handler, schedule
from application.patient_api import discovery, handler as patient_handler

START = NOW + timedelta(days=3, hours=3)


@pytest.fixture
def world(monkeypatch):
    conn = Conn(SCHEMA)
    seed(conn.db, patients=3)
    add_slot(conn.db, "slot", START, capacity=1)
    add_slot(conn.db, "camp", START + timedelta(hours=2), capacity=5, ctype="hc", minutes=60)
    deps = make_deps()
    conn.close = lambda: None  # the handlers close their connection; keep the in-memory database alive
    monkeypatch.setattr(patient_handler, "get_connection", lambda: conn)
    monkeypatch.setattr(patient_handler, "default_deps", lambda: deps)
    monkeypatch.setattr(doctor_handler, "get_connection", lambda: conn)
    monkeypatch.setattr(doctor_appointments, "default_deps", lambda: deps)
    return conn, deps


def evt(method, resource, user="u-1", role="patient", body=None, headers=None, path=None, query=None):
    return {"httpMethod": method, "resource": resource, "path": resource, "headers": headers or {},
            "pathParameters": path, "queryStringParameters": query, "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"userId": user, "role": role}}}


def call(handler, *args, **kw):
    result = handler.lambda_handler(evt(*args, **kw), None)
    return result["statusCode"], json.loads(result["body"])


# ------------------------------------------------------------------------------ patient routes
def test_full_patient_journey_through_the_lambda(world):
    conn, deps = world
    status, body = call(patient_handler, "POST", "/appointments", body={"slot_id": "slot"},
                        headers={"Idempotency-Key": "abc-1"})
    assert status == 201
    appt_id = body["appointment"]["appointment_id"]

    # the header name is case-insensitive and a replay returns the ORIGINAL appointment
    status, again = call(patient_handler, "POST", "/appointments", body={"slot_id": "slot"},
                         headers={"idempotency-key": "abc-1"})
    assert status == 201 and again["idempotent_replay"] is True and again["appointment"]["appointment_id"] == appt_id

    status, listing = call(patient_handler, "GET", "/appointments")
    assert status == 200 and listing["total"] == 1
    status, detail = call(patient_handler, "GET", "/appointments/{appointment_id}", path={"appointment_id": appt_id})
    assert status == 200 and detail["appointment"]["appointment_id"] == appt_id

    status, full = call(patient_handler, "POST", "/appointments", user="u-2", body={"slot_id": "slot"})
    assert (status, full["error"]) == (409, "SLOT_FULL")

    status, cancelled = call(patient_handler, "POST", "/appointments/{appointment_id}/cancel",
                             path={"appointment_id": appt_id}, body={"reason": "busy"})
    assert status == 200 and cancelled["appointment"]["refund"]["tier"] == "FULL"
    status, ledger = call(patient_handler, "GET", "/refunds")
    assert status == 200 and ledger["total_refunded"] == 500.0


def test_cancel_works_without_a_body(world):
    _, body = call(patient_handler, "POST", "/appointments", body={"slot_id": "slot"})
    status, _ = call(patient_handler, "POST", "/appointments/{appointment_id}/cancel",
                     path={"appointment_id": body["appointment"]["appointment_id"]})
    assert status == 200


def test_another_patient_gets_404_on_my_appointment(world):
    _, body = call(patient_handler, "POST", "/appointments", body={"slot_id": "slot"})
    appt_id = body["appointment"]["appointment_id"]
    for method, resource in (("GET", "/appointments/{appointment_id}"), ("POST", "/appointments/{appointment_id}/cancel")):
        status, err = call(patient_handler, method, resource, user="u-2", path={"appointment_id": appt_id})
        assert (status, err["error"]) == (404, "APPOINTMENT_NOT_FOUND")
    assert call(patient_handler, "GET", "/appointments", user="u-2")[1]["total"] == 0


def test_a_patient_cannot_pass_a_patient_id_to_book_for_someone_else(world):
    status, err = call(patient_handler, "POST", "/appointments", body={"slot_id": "slot", "patient_id": "p-2"})
    assert (status, err["error"]) == (400, "UNKNOWN_FIELD")


def test_bad_json_and_bad_key_are_400(world):
    bad = evt("POST", "/appointments")
    bad["body"] = "{not json"
    assert json.loads(patient_handler.lambda_handler(bad, None)["body"])["error"] == "INVALID_JSON"
    status, err = call(patient_handler, "POST", "/appointments", body={"slot_id": "slot"}, headers={"Idempotency-Key": "a b"})
    assert (status, err["error"]) == (400, "INVALID_IDEMPOTENCY_KEY")


def test_doctors_and_anonymous_callers_cannot_use_patient_booking_routes(world):
    for method, resource in (("POST", "/appointments"), ("GET", "/appointments"), ("GET", "/refunds"),
                             ("GET", "/appointments/{appointment_id}"), ("POST", "/appointments/{appointment_id}/cancel")):
        assert call(patient_handler, method, resource, user="u-d1", role="doctor", path={"appointment_id": "x"})[0] == 403
        anon = evt(method, resource)
        anon["requestContext"] = {"authorizer": {}}
        assert patient_handler.lambda_handler(anon, None)["statusCode"] == 401


def test_the_authorizer_denies_booking_routes_to_doctors():
    from application.authorizer.lambda_handler import DENIED_FOR_ROLE
    for pattern in ("appointments", "appointments/*", "refunds"):
        assert pattern in DENIED_FOR_ROLE["doctor"]
    assert "doctors/me/*" in DENIED_FOR_ROLE["patient"]  # and patients stay out of /doctors/me/appointments


# ------------------------------------------------------------------------------ doctor routes
def test_doctor_routes_are_registered_and_closed_to_patients():
    for key in (("GET", "/doctors/me/appointments"), ("GET", "/doctors/me/appointments/{appointment_id}"),
                ("POST", "/doctors/me/slots/cancel")):
        assert key in doctor_handler.ROUTES
        assert call(doctor_handler, key[0], key[1], role="patient")[0] == 403


def test_doctor_sees_bookings_and_cancels_a_slot_through_the_lambda(world):
    for user in ("u-1", "u-2"):
        assert call(patient_handler, "POST", "/appointments", user=user, body={"slot_id": "camp"})[0] == 201
    status, listing = call(doctor_handler, "GET", "/doctors/me/appointments", user="u-d1", role="doctor")
    assert status == 200 and listing["total"] == 2 and listing["items"][0]["patient"]["name"].startswith("Pat")
    appt_id = listing["items"][0]["appointment_id"]
    assert call(doctor_handler, "GET", "/doctors/me/appointments/{appointment_id}", user="u-d1", role="doctor",
                path={"appointment_id": appt_id})[0] == 200
    assert call(doctor_handler, "GET", "/doctors/me/appointments/{appointment_id}", user="u-d2", role="doctor",
                path={"appointment_id": appt_id})[0] == 404  # another doctor's appointment is invisible

    status, result = call(doctor_handler, "POST", "/doctors/me/slots/cancel", user="u-d1", role="doctor",
                          body={"slot_id": "camp", "reason": "Storm"})
    assert status == 200 and result["cancelled_appointments"] == 2 and result["refunds_total"] == 400.0
    assert call(patient_handler, "GET", "/refunds", user="u-2")[1]["total_refunded"] == 200.0


# ------------------------------------------------------------------------------ ONE query feeds both dashboards
def test_patient_and_doctor_dashboards_use_the_same_slot_query(world, monkeypatch):
    conn, deps = world
    from application.common import slot_query
    calls = []
    real = slot_query.fetch_slots
    monkeypatch.setattr(slot_query, "fetch_slots", lambda *a, **k: calls.append(a[1]) or real(*a, **k))
    discovery.slots(conn, "d1", {"date": "2026-11-13"}, now_utc=NOW)

    class Ctx:
        def __init__(self):
            self.conn, self.query = conn, {"date": "2026-11-13"}

        def doctor(self):
            return {"doctor_id": "d1", "timezone": "Asia/Kolkata"}

    schedule.get(Ctx())
    assert calls == ["d1", "d1"]


def test_both_dashboards_agree_on_the_same_slot_data(world):
    conn, deps = world
    call(patient_handler, "POST", "/appointments", body={"slot_id": "camp"})
    patient_view = {s["slot_id"]: s for s in discovery.slots(conn, "d1", {"date": "2026-11-13"}, now_utc=NOW)[1]["slots"]}

    class Ctx:
        def __init__(self):
            self.conn, self.query = conn, {"date": "2026-11-13"}

        def doctor(self):
            return {"doctor_id": "d1", "timezone": "Asia/Kolkata"}

    doctor_view = {s["slot_id"]: s for s in schedule.get(Ctx())[1]["slots"]}
    assert set(patient_view) <= set(doctor_view)
    for slot_id, p in patient_view.items():
        d = doctor_view[slot_id]
        shared = ("start_utc", "end_utc", "start_time", "end_time", "capacity", "booked_count", "available_seats",
                  "status", "type_name", "duration_minutes", "fee")
        assert {k: p[k] for k in shared} == {k: d[k] for k in shared}
    assert doctor_view["camp"]["appointments"][0]["patient_name"] == "Pat1 Test"  # doctors additionally see who booked


def test_the_patient_view_shows_full_slots_for_waitlist_but_hides_closed_slots(world):
    conn, deps = world
    call(patient_handler, "POST", "/appointments", body={"slot_id": "slot"})  # capacity 1 -> FULL
    add_slot(conn.db, "closed", START + timedelta(hours=5), status="CLOSED")

    class Ctx:
        def __init__(self):
            self.conn, self.query = conn, {"date": "2026-11-13"}

        def doctor(self):
            return {"doctor_id": "d1", "timezone": "Asia/Kolkata"}

    patient_ids = {s["slot_id"] for s in discovery.slots(conn, "d1", {"date": "2026-11-13"}, now_utc=NOW)[1]["slots"]}
    doctor_ids = {s["slot_id"] for s in schedule.get(Ctx())[1]["slots"]}
    assert patient_ids == {"slot", "camp"} and doctor_ids == {"slot", "camp", "closed"}


# ------------------------------------------------------------------------------ consultation type filter
def test_search_filters_by_consultation_type_and_lists_what_each_doctor_offers(world):
    conn, _ = world
    names = lambda q: [d["doctor_id"] for d in discovery.search(conn, q)[1]["items"]]  # noqa: E731
    assert names({"consultation_type": "Health Camp Session"}) == ["d1"]       # only d1 offers it
    assert sorted(names({"consultation_type": "Standard Consult"})) == ["d1", "d2"]
    assert names({"consultation_type": "Quick Consult"}) == []
    assert names({"consultation_type": "Standard Consult", "name": "Rav"}) == ["d2"]  # combines with other filters
    status, body = discovery.search(conn, {"consultation_type": "Standard Consult"})
    assert body["total"] == 2
    by_id = {d["doctor_id"]: d for d in body["items"]}
    assert by_id["d1"]["consultation_types"] == [{"type_name": "Standard Consult", "duration_minutes": 30, "fee": 500.0},
                                                {"type_name": "Health Camp Session", "duration_minutes": 60, "fee": 200.0}]
    assert by_id["d2"]["consultation_types"] == [{"type_name": "Standard Consult", "duration_minutes": 30, "fee": 400.0}]


def test_the_consultation_type_value_is_a_parameter_not_sql(world):
    conn, _ = world
    assert discovery.search(conn, {"consultation_type": "x' OR '1'='1"})[1]["total"] == 0


# ------------------------------------------------------------------------------ schema upgrade
class Scripted:
    def __init__(self, tables, columns, indexes):
        self.tables, self.columns, self.indexes, self.ddl, self._row = set(tables), set(columns), set(indexes), [], None

    def execute(self, sql, params=()):
        if "information_schema.tables" in sql:
            self._row = {"n": int(params[0] in self.tables)}
        elif "information_schema.columns" in sql:
            self._row = {"n": int((params[0], params[1]) in self.columns)}
        elif "information_schema.statistics" in sql:
            self._row = {"n": int((params[0], params[1]) in self.indexes)}
        else:
            text = " ".join(sql.split())
            self.ddl.append(text)
            if text.startswith("ALTER TABLE appointments ADD COLUMN"):
                self.columns.add(("appointments", text.split()[5]))
            if text.startswith("ALTER TABLE refund_ledger ADD COLUMN"):
                self.columns.add(("refund_ledger", text.split()[5]))
            if text.startswith("CREATE"):
                self.indexes.add(("appointments", text.split(" INDEX ")[1].split()[0]))

    def fetchone(self):
        return self._row


def test_booking_columns_are_added_to_an_existing_database_and_the_upgrade_repeats_safely():
    cur = Scripted({"appointments", "refund_ledger"}, {("appointments", "status")}, set())
    db_schema._ensure_booking_columns(cur)
    text = "\n".join(cur.ddl)
    for column in ("fee_amount", "idempotency_key", "starts_at_utc", "ends_at_utc", "local_date", "cancelled_at", "active_flag"):
        assert f"ADD COLUMN {column}" in text
    assert "ALTER TABLE refund_ledger ADD COLUMN refund_tier" in text and "ALTER TABLE refund_ledger ADD COLUMN fee_amount" in text
    assert "CREATE UNIQUE INDEX uq_appt_patient_idem ON appointments (patient_id, idempotency_key)" in text
    assert "CREATE UNIQUE INDEX uq_appt_patient_slot_active ON appointments (patient_id, slot_id, active_flag)" in text
    assert "CREATE INDEX ix_appointments_patient_start" in text
    first_run = len(cur.ddl)
    db_schema._ensure_booking_columns(cur)
    assert len(cur.ddl) == first_run  # nothing is repeated on the next deploy


def test_a_fresh_schema_and_an_upgraded_one_define_the_same_booking_columns():
    sql = db_schema.load_schema()
    for column, _ in db_schema.APPOINTMENT_COLUMNS:
        assert column in sql
    assert "active_flag TINYINT GENERATED ALWAYS AS (IF(status = 'CONFIRMED', 1, NULL)) VIRTUAL" in sql
    assert "UNIQUE KEY uq_appt_patient_idem (patient_id, idempotency_key)" in sql
    assert "refund_tier VARCHAR(20)" in sql
    for name, _, _ in db_schema.APPOINTMENT_INDEXES:
        assert name in sql


# ------------------------------------------------------------------------------ misconfigured tables
class _MissingTable(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException", "Message": "Requested resource not found"}}


def test_a_missing_dynamodb_table_is_a_clean_503_not_a_stack_trace(world, monkeypatch):
    conn, deps = world

    def boom(*args, **kwargs):
        raise _MissingTable()

    monkeypatch.setattr(deps.idempotency, "begin", boom)
    status, body = call(patient_handler, "POST", "/appointments", body={"slot_id": "slot"}, headers={"Idempotency-Key": "k-1"})
    assert (status, body["error"]) == (503, "BOOKING_STORE_UNAVAILABLE")
    assert "ResourceNotFound" not in json.dumps(body)
    # nothing was booked
    assert call(patient_handler, "GET", "/appointments")[1]["total"] == 0
