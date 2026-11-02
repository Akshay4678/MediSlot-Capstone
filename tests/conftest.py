
"""Shared test setup.

CI installs the real libraries. When a library is missing (for example on a laptop
without the AWS SDK) a tiny stand-in is registered so the pure business logic can
still be unit-tested. Tests never call AWS: they patch the module-level clients.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ["JWT_SECRET_PARAMETER_NAME"] = "/test/auth/jwt-secret"
os.environ["REFRESH_TOKEN_TABLE"] = "dev-medislot-refresh-tokens"
os.environ["TOKEN_ACTIVELIST_TABLE"] = "dev-medislot-token-activelist-v2"
os.environ["JWT_ISSUER"] = "medislot"
os.environ.pop("SSM_PREFIX", None)


def _stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _ClientError(Exception):
    def __init__(self, response, operation_name="op"):
        super().__init__(str(response))
        self.response = response


try:
    import boto3
    import botocore
except ImportError:
    _stub("boto3", client=lambda *a, **k: MagicMock(),
          resource=lambda *a, **k: MagicMock())
    _stub("boto3.dynamodb")
    _stub("boto3.dynamodb.conditions", Key=lambda name: MagicMock())
    _stub("botocore")
    _stub("botocore.config", Config=lambda **k: None)
    _stub("botocore.exceptions", ClientError=_ClientError)

try:
    import pymysql
except ImportError:
    class _IntegrityError(Exception):
        pass

    _stub(
        "pymysql",
        err=types.SimpleNamespace(IntegrityError=_IntegrityError),
        cursors=types.SimpleNamespace(DictCursor=object),
        connect=lambda **k: None
    )

try:
    import bcrypt
except ImportError:
    import hashlib

    _stub(
        "bcrypt",
        gensalt=lambda rounds=12: f"$2b${rounds:02d}$".encode(),
        hashpw=lambda pw, salt: salt + hashlib.sha256(pw).hexdigest().encode(),
        checkpw=lambda pw, h: h.endswith(
            hashlib.sha256(pw).hexdigest().encode()
        )
    )
