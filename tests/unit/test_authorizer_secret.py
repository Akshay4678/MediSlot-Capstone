"""The authorizer (and the auth Lambda, which imports it) must say clearly when the JWT secret is unreachable."""

import logging

import pytest

from application.authorizer import lambda_handler as az


class MissingParameterSsm:
    def get_parameter(self, **kwargs):
        raise az.ClientError({"Error": {"Code": "ParameterNotFound", "Message": ""}}, "GetParameter")


class WorkingSsm:
    calls = 0

    def get_parameter(self, **kwargs):
        WorkingSsm.calls += 1
        return {"Parameter": {"Value": "s3cret"}}


@pytest.fixture(autouse=True)
def fresh_secret_cache():
    az.get_jwt_secret.cache_clear()
    yield
    az.get_jwt_secret.cache_clear()


def test_missing_parameter_is_logged_with_its_name_and_never_cached(monkeypatch, caplog):
    monkeypatch.setenv("JWT_SECRET_PARAMETER_NAME", "/medislot/dev/auth/jwt-secret")
    monkeypatch.setattr(az, "ssm", MissingParameterSsm())
    with caplog.at_level(logging.ERROR), pytest.raises(az.ClientError):
        az.get_jwt_secret()
    assert "/medislot/dev/auth/jwt-secret" in caplog.text and "ParameterNotFound" in caplog.text

    # once the parameter exists the very next call works: a failure must not be cached
    monkeypatch.setattr(az, "ssm", WorkingSsm())
    assert az.get_jwt_secret() == b"s3cret"


def test_unset_parameter_name_is_reported(monkeypatch, caplog):
    monkeypatch.delenv("JWT_SECRET_PARAMETER_NAME", raising=False)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        az.get_jwt_secret()
    assert "JWT_SECRET_PARAMETER_NAME" in caplog.text


def test_a_dependency_failure_still_fails_closed_but_is_logged_with_a_traceback(monkeypatch, caplog):
    def boom(token):
        raise RuntimeError("DynamoDB unavailable")
    monkeypatch.setattr(az, "verify_access_token", boom)
    event = {"headers": {"Authorization": "Bearer abc.def.ghi"}, "methodArn": "arn:aws:execute-api:r:a:id/dev/GET/x"}
    with caplog.at_level(logging.ERROR), pytest.raises(Exception, match="Unauthorized"):
        az.lambda_handler(event, None)
    assert "dependency failed" in caplog.text and "DynamoDB unavailable" in caplog.text


def test_a_bad_token_is_a_quiet_401(monkeypatch, caplog):
    def bad(token):
        raise ValueError("Invalid signature")
    monkeypatch.setattr(az, "verify_access_token", bad)
    event = {"headers": {"Authorization": "Bearer abc.def.ghi"}, "methodArn": "arn:aws:execute-api:r:a:id/dev/GET/x"}
    with caplog.at_level(logging.ERROR), pytest.raises(Exception, match="Unauthorized"):
        az.lambda_handler(event, None)
    assert caplog.text == ""  # expected 401s (smoke test, expired tokens) stay out of the error logs