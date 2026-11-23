"""The role deny rules must block the other role's routes and NOTHING else.

IAM resource matching lets "*" match "/", so a rule like "*/appointments/*" once blocked
GET /doctors/me/appointments. This test evaluates the generated policy the way IAM does
(explicit Deny wins; otherwise Allow) for every booking route.
"""

from fnmatch import fnmatchcase

import pytest

from application.authorizer import lambda_handler as az

ARN = "arn:aws:execute-api:ap-south-1:123456789012:abc123/dev/{method}/{path}"


def allowed(role, method, path):
    policy = az.build_policy({"sub": "u1", "role": role, "jti": "j"}, ARN.format(method=method, path=path))
    target = ARN.format(method=method, path=path)
    verdict = None
    for st in policy["policyDocument"]["Statement"]:
        resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        if any(fnmatchcase(target, r) for r in resources):
            if st["Effect"] == "Deny":
                return False
            verdict = True
    return bool(verdict)


PATIENT_ROUTES = [
    ("POST", "appointments"), ("GET", "appointments"), ("GET", "appointments/a1"),
    ("POST", "appointments/a1/cancel"), ("GET", "refunds"), ("GET", "patient/me"), ("PUT", "patient/me"),
    ("GET", "doctors/search"), ("GET", "doctors/d1"), ("GET", "doctors/d1/slots"),
]
DOCTOR_ROUTES = [
    ("GET", "doctors/me"), ("PUT", "doctors/me"), ("GET", "doctors/me/appointments"),
    ("GET", "doctors/me/appointments/a1"), ("POST", "doctors/me/slots/cancel"), ("GET", "doctors/me/schedule"),
    ("PUT", "doctors/me/availability"), ("POST", "doctors/me/consultation-types"),
]


@pytest.mark.parametrize("method,path", PATIENT_ROUTES)
def test_patient_can_use_patient_routes(method, path):
    assert allowed("patient", method, path)


@pytest.mark.parametrize("method,path", DOCTOR_ROUTES)
def test_doctor_can_use_doctor_routes(method, path):
    assert allowed("doctor", method, path)


@pytest.mark.parametrize("method,path", [r for r in PATIENT_ROUTES if r[1].split("/")[0] in ("appointments", "refunds", "patient")])
def test_doctor_cannot_use_patient_only_routes(method, path):
    assert not allowed("doctor", method, path)


@pytest.mark.parametrize("method,path", DOCTOR_ROUTES)
def test_patient_cannot_use_doctor_routes(method, path):
    assert not allowed("patient", method, path)
