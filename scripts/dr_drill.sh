#!/usr/bin/env bash
# Disaster-recovery drill (Milestone 7): prove the database can be restored and time it.
# Usage: scripts/dr_drill.sh [environment]       (default: dev)
# What it does (nothing touches the live database):
#   1. RDS: restores the newest automated snapshot into a throw-away instance, waits until it is available,
#      prints the elapsed time (record it in docs/dr-drill.md), then deletes the throw-away instance.
#   2. DynamoDB: prints the earliest/latest restorable time of every table (point-in-time recovery proof).
# Costs a few cents (a db.t3.micro for roughly 10-20 minutes).
set -euo pipefail
ENVIRONMENT="${1:-dev}"
PROJECT="${PROJECT_NAME:-medislot}"
REGION="${AWS_REGION:-ap-south-1}"
DB_ID="$(aws cloudformation describe-stack-resource --stack-name "${PROJECT}-${ENVIRONMENT}-data" \
  --logical-resource-id MediSlotDatabase --region "${REGION}" --query StackResourceDetail.PhysicalResourceId --output text)"
SNAP="$(aws rds describe-db-snapshots --db-instance-identifier "${DB_ID}" --region "${REGION}" \
  --query 'reverse(sort_by(DBSnapshots[?Status==`available`], &SnapshotCreateTime))[0].DBSnapshotIdentifier' --output text)"
if [[ -z "${SNAP}" || "${SNAP}" == "None" ]]; then
  echo "No snapshot found. Run scripts/backup_db.sh ${ENVIRONMENT} first."; exit 1
fi
DRILL="${PROJECT}-${ENVIRONMENT}-dr-drill-$(date -u +%Y%m%d%H%M)"
echo "Restoring snapshot ${SNAP} into ${DRILL}"
START="$(date +%s)"
aws rds restore-db-instance-from-db-snapshot --db-instance-identifier "${DRILL}" --db-snapshot-identifier "${SNAP}" \
  --db-instance-class db.t3.micro --no-publicly-accessible --region "${REGION}" \
  --tags Key=Project,Value="${PROJECT}" Key=Environment,Value="${ENVIRONMENT}" Key=Purpose,Value=dr-drill >/dev/null
aws rds wait db-instance-available --db-instance-identifier "${DRILL}" --region "${REGION}"
ELAPSED=$(( $(date +%s) - START ))
echo "RESTORE TIME: ${ELAPSED} seconds (snapshot ${SNAP}) -> write this in docs/dr-drill.md"
aws rds delete-db-instance --db-instance-identifier "${DRILL}" --skip-final-snapshot --region "${REGION}" >/dev/null
echo "Throw-away instance ${DRILL} is being deleted."
echo
echo "DynamoDB point-in-time recovery window:"
for suffix in slot-capacity idempotency waitlist hold-offers dispatch-log; do
  aws dynamodb list-tables --region "${REGION}" --query "TableNames[?contains(@, '${PROJECT}-${ENVIRONMENT}') && contains(@, '${suffix}')]" --output text | tr '\t' '\n' | while read -r T; do
    [[ -z "${T}" ]] && continue
    aws dynamodb describe-continuous-backups --table-name "${T}" --region "${REGION}" \
      --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.[PointInTimeRecoveryStatus,EarliestRestorableDateTime,LatestRestorableDateTime]' --output text | sed "s|^|${T}  |"
  done
done
