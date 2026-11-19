#!/usr/bin/env bash
# Self-heal the authentication DynamoDB stack before it is deployed.
#
# Usage: scripts/ensure_dynamodb_stack.sh <stack-name> <region>
#
# Problem this solves: if a table of this stack is deleted by hand (console / CLI), CloudFormation
# does not notice. The next "deploy" finds nothing to change, the table is never re-created, and
# every login fails with "Authentication service failed." (HTTP 500).
#
# What it does: if the stack exists but one of its tables is gone, the stack is deleted so the
# normal deploy step re-creates it. The stack holds only the two token tables (no user accounts,
# which live in RDS), so the only effect is that people have to sign in again. The new table names
# are read from the stack outputs later in the pipeline and handed to the Lambda stacks.
#
# Safety: a table is treated as missing ONLY when DynamoDB answers ResourceNotFoundException.
# Any other error (for example missing permissions) is reported and the stack is left alone.
set -euo pipefail

STACK="${1:?stack name required}"
REGION="${2:?region required}"

STATUS="$(aws cloudformation describe-stacks --stack-name "${STACK}" --region "${REGION}" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"

if [[ -z "${STATUS}" || "${STATUS}" == "None" ]]; then
  echo "Stack ${STACK} does not exist yet; nothing to check."
  exit 0
fi

RECREATE=false

case "${STATUS}" in
  ROLLBACK_COMPLETE|ROLLBACK_FAILED|DELETE_FAILED)
    echo "::warning::Stack ${STACK} is in state ${STATUS} and cannot be updated; it will be re-created."
    RECREATE=true
    ;;
  *_IN_PROGRESS)
    echo "Stack ${STACK} is busy (${STATUS}); skipping the table check."
    exit 0
    ;;
esac

if [[ "${RECREATE}" == "false" ]]; then
  TABLES="$(aws cloudformation describe-stacks --stack-name "${STACK}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?ends_with(OutputKey, 'TableName')].OutputValue" --output text)"

  for table in ${TABLES}; do
    if RESULT="$(aws dynamodb describe-table --table-name "${table}" --region "${REGION}" 2>&1)"; then
      echo "Table ${table} exists."
    elif grep -q "ResourceNotFoundException" <<<"${RESULT}"; then
      echo "::warning::Table ${table} was deleted outside CloudFormation; the stack will be re-created."
      RECREATE=true
    else
      echo "::warning::Could not check table ${table}; leaving the stack as it is. ${RESULT}"
    fi
  done
fi

if [[ "${RECREATE}" == "true" ]]; then
  echo "Deleting stack ${STACK} so that it is created again with fresh tables."
  aws cloudformation delete-stack --stack-name "${STACK}" --region "${REGION}"
  aws cloudformation wait stack-delete-complete --stack-name "${STACK}" --region "${REGION}"
  echo "Stack ${STACK} deleted."
else
  echo "All tables of ${STACK} are present."
fi
