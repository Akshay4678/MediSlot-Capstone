"""The booking tables live in their own stack so the pipeline can re-create them if they are deleted by hand."""

import re
from pathlib import Path

CFN = Path(__file__).resolve().parents[2] / "infrastructure" / "cloudformation"
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"


def test_booking_stack_defines_both_tables_and_outputs_ending_in_tablename():
    text = (CFN / "booking-tables.yaml").read_text()
    assert "SlotCapacityTable:" in text and "IdempotencyTable:" in text
    # the self-heal script only checks outputs whose key ends with TableName
    assert re.search(r"^  SlotCapacityTableName:", text, re.M) and re.search(r"^  IdempotencyTableName:", text, re.M)
    assert "AttributeName: expires_at" in text  # idempotency TTL


def test_tables_are_not_defined_twice():
    assert "IdempotencyTable:" not in (CFN / "rds.yaml").read_text()
    assert "SlotCapacityTable:" not in (CFN / "rds.yaml").read_text()


def test_pipeline_heals_then_deploys_the_booking_stack_before_reading_its_outputs():
    text = WORKFLOW.read_text()
    heal = text.index('ensure_dynamodb_stack.sh "${STACK_NAME}" "${AWS_REGION}"', text.index("Deploy Booking Tables"))
    read = text.index('BOOKING_STACK="${PROJECT_NAME}-${ENVIRONMENT_NAME}-booking"')
    assert heal < read


def test_smoke_test_is_the_last_job():
    text = WORKFLOW.read_text()
    assert text.rindex("\n  smoke-test:") > text.index("\n  deploy-web:")
    assert "needs: [deploy-infrastructure, deploy-web]" in text
