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


def test_data_stack_holds_only_what_this_milestone_uses():
    # Waitlist, hold-offer and notification-dispatch tables belong to later milestones. They were deleted by hand,
    # and a retained-but-missing table made the data stack update fail ("Unable to retrieve Arn ... does not exist").
    text = (CFN / "rds.yaml").read_text()
    for gone in ("IdempotencyTable", "SlotCapacityTable", "WaitlistTable", "HoldOffersTable", "NotificationDispatchTable"):
        assert f"{gone}:" not in text and f"!GetAtt {gone}" not in text, gone
    assert "MediSlotDatabase:" in text and "ArtifactsBucket:" in text


def test_pipeline_requires_milestone6_tables_and_workflow_stack():
    text = WORKFLOW.read_text()
    for required in ("WAITLIST_TABLE_NAME", "NOTIFICATION_DISPATCH_TABLE_NAME", "HOLD_OFFERS_TABLE_NAME", "infrastructure/cloudformation/workflow.yaml"):
        assert required in text, required
    assert "RescheduleStateMachineArn" in text



def test_pipeline_heals_then_deploys_the_booking_stack_before_reading_its_outputs():
    text = WORKFLOW.read_text()
    heal = text.index('ensure_dynamodb_stack.sh "${STACK_NAME}" "${AWS_REGION}"', text.index("Deploy Booking Tables"))
    read = text.index('BOOKING_STACK="${PROJECT_NAME}-${ENVIRONMENT_NAME}-booking"')
    assert heal < read


def test_smoke_test_is_the_last_job():
    text = WORKFLOW.read_text()
    assert text.rindex("\n  smoke-test:") > text.index("\n  deploy-web:")
    assert "needs: [deploy-infrastructure, deploy-web]" in text
