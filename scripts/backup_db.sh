#!/usr/bin/env bash
# Take a manual RDS snapshot BEFORE you delete any stacks.
# Usage: scripts/backup_db.sh [environment]      (default: dev)
# The deploy pipeline restores the newest snapshot automatically when the data stack is created again.
set -euo pipefail
ENVIRONMENT="${1:-dev}"
PROJECT="${PROJECT_NAME:-medislot}"
REGION="${AWS_REGION:-ap-south-1}"
STACK="${PROJECT}-${ENVIRONMENT}-data"

DB_ID="$(aws cloudformation describe-stack-resource --stack-name "${STACK}" \
  --logical-resource-id MediSlotDatabase --region "${REGION}" \
  --query StackResourceDetail.PhysicalResourceId --output text)"
SNAP="${PROJECT}-${ENVIRONMENT}-backup-$(date -u +%Y%m%d-%H%M%S)"

aws rds create-db-snapshot --db-instance-identifier "${DB_ID}" \
  --db-snapshot-identifier "${SNAP}" --region "${REGION}" >/dev/null
echo "Creating snapshot ${SNAP} ... (waiting)"
aws rds wait db-snapshot-available --db-snapshot-identifier "${SNAP}" --region "${REGION}"
echo "Snapshot ready: ${SNAP}. You can now delete the stacks."
