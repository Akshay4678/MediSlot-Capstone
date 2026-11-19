"""scripts/ensure_dynamodb_stack.sh re-creates the auth DynamoDB stack when a table was deleted by hand.

The real script runs against a fake `aws` command that logs every call, so no AWS access is needed.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ensure_dynamodb_stack.sh"

FAKE_AWS = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "cloudformation describe-stacks")
    if [[ "$*" == *StackStatus* ]]; then
      [[ -z "${FAKE_STATUS:-}" ]] && exit 255
      echo "$FAKE_STATUS"
    else
      echo "${FAKE_TABLES:-}"
    fi
    ;;
  "dynamodb describe-table")
    table="$4"
    if [[ " ${FAKE_MISSING:-} " == *" $table "* ]]; then
      echo "An error occurred (ResourceNotFoundException) when calling the DescribeTable operation" >&2
      exit 254
    fi
    if [[ " ${FAKE_DENIED:-} " == *" $table "* ]]; then
      echo "An error occurred (AccessDeniedException) when calling the DescribeTable operation" >&2
      exit 254
    fi
    echo '{"Table": {}}'
    ;;
esac
exit 0
"""


def run(tmp_path, **env):
    fake = tmp_path / "aws"
    fake.write_text(FAKE_AWS)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("")
    full_env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", FAKE_LOG=str(log), **env)
    result = subprocess.run(["bash", str(SCRIPT), "medislot-dev-dynamodb", "ap-south-1"],
                            env=full_env, capture_output=True, text=True, timeout=30)
    return result, log.read_text()


TABLES = "dev-refresh-abc dev-active-xyz"


def test_nothing_happens_when_the_stack_does_not_exist_yet(tmp_path):
    result, calls = run(tmp_path, FAKE_STATUS="")
    assert result.returncode == 0
    assert "delete-stack" not in calls


def test_a_healthy_stack_is_left_alone(tmp_path):
    result, calls = run(tmp_path, FAKE_STATUS="CREATE_COMPLETE", FAKE_TABLES=TABLES)
    assert result.returncode == 0, result.stderr
    assert "delete-stack" not in calls
    assert "All tables of medislot-dev-dynamodb are present." in result.stdout


def test_a_table_deleted_by_hand_makes_the_stack_be_re_created(tmp_path):
    result, calls = run(tmp_path, FAKE_STATUS="UPDATE_COMPLETE", FAKE_TABLES=TABLES,
                        FAKE_MISSING="dev-refresh-abc")
    assert result.returncode == 0, result.stderr
    assert "cloudformation delete-stack --stack-name medislot-dev-dynamodb" in calls
    assert "cloudformation wait stack-delete-complete --stack-name medislot-dev-dynamodb" in calls
    assert "deleted outside CloudFormation" in result.stdout


def test_both_tables_missing_deletes_the_stack_once(tmp_path):
    _, calls = run(tmp_path, FAKE_STATUS="CREATE_COMPLETE", FAKE_TABLES=TABLES,
                   FAKE_MISSING="dev-refresh-abc dev-active-xyz")
    assert calls.count("delete-stack") == 1


def test_an_access_error_never_deletes_a_healthy_stack(tmp_path):
    result, calls = run(tmp_path, FAKE_STATUS="CREATE_COMPLETE", FAKE_TABLES=TABLES,
                        FAKE_DENIED="dev-active-xyz")
    assert result.returncode == 0
    assert "delete-stack" not in calls
    assert "Could not check table dev-active-xyz" in result.stdout


@pytest.mark.parametrize("status", ["ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "DELETE_FAILED"])
def test_a_stack_that_cannot_be_updated_is_re_created(tmp_path, status):
    _, calls = run(tmp_path, FAKE_STATUS=status, FAKE_TABLES=TABLES)
    assert "delete-stack" in calls


def test_a_stack_that_is_busy_is_skipped(tmp_path):
    result, calls = run(tmp_path, FAKE_STATUS="UPDATE_IN_PROGRESS", FAKE_TABLES=TABLES,
                        FAKE_MISSING="dev-refresh-abc")
    assert result.returncode == 0
    assert "delete-stack" not in calls
