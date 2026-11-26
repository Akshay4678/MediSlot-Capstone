#!/usr/bin/env bash
# CloudFormation drift detection for every MediSlot stack of one environment (Milestone 7).
# Usage: scripts/detect_drift.sh [environment]       (default: dev)
# Exit code 0 = no drift, 1 = at least one stack drifted (so it can run in CI or a scheduled job).
# Drift means someone changed a resource in the console. Fix it by re-running the pipeline (it re-applies the
# template), or by updating the template if the console change was intended.
set -euo pipefail
ENVIRONMENT="${1:-dev}"
PROJECT="${PROJECT_NAME:-medislot}"
REGION="${AWS_REGION:-ap-south-1}"
drifted=0
for suffix in network data booking storage events workflow notifications doctor patient auth api observability web; do
  STACK="${PROJECT}-${ENVIRONMENT}-${suffix}"
  if ! aws cloudformation describe-stacks --stack-name "${STACK}" --region "${REGION}" >/dev/null 2>&1; then
    echo "SKIP   ${STACK} (does not exist)"; continue
  fi
  ID="$(aws cloudformation detect-stack-drift --stack-name "${STACK}" --region "${REGION}" --query StackDriftDetectionId --output text)"
  for _ in $(seq 1 60); do
    STATE="$(aws cloudformation describe-stack-drift-detection-status --stack-drift-detection-id "${ID}" --region "${REGION}" \
      --query DetectionStatus --output text)"
    [[ "${STATE}" != "DETECTION_IN_PROGRESS" ]] && break
    sleep 5
  done
  STATUS="$(aws cloudformation describe-stack-drift-detection-status --stack-drift-detection-id "${ID}" --region "${REGION}" \
    --query StackDriftStatus --output text)"
  if [[ "${STATUS}" == "IN_SYNC" ]]; then
    echo "OK     ${STACK}"
  else
    echo "DRIFT  ${STACK} (${STATUS}) - changed resources:"
    aws cloudformation describe-stack-resource-drifts --stack-name "${STACK}" --region "${REGION}" \
      --stack-resource-drift-status-filters MODIFIED DELETED \
      --query 'StackResourceDrifts[].[LogicalResourceId,ResourceType,StackResourceDriftStatus]' --output text | sed 's/^/         /'
    drifted=1
  fi
done
exit "${drifted}"
