#!/usr/bin/env bash
# Run from the repository root AFTER copying the new files over your checkout.
# Unzipping over a folder never deletes old files, so this removes the dead code that the project no longer uses
# (including every place that still mentioned an "admin" role: MediSlot has only patients and doctors).
set -euo pipefail
git rm -rf --ignore-unmatch \
  application/appointment \
  application/auth/login.py application/auth/logout.py application/auth/refresh.py application/auth/register.py \
  application/common/auth_utils.py \
  infrastructure/cloudformation/application.yaml infrastructure/cloudformation/iam.yaml infrastructure/cloudformation/scheduler.yaml \
  infrastructure/cloudformation/eventbridge.yaml infrastructure/cloudformation/sns.yaml \
  tests/unit/test_access_token_chain.py
find application -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "Check nothing mentions admin any more (this should print nothing):"
grep -rniI "admin" application web scripts tests .github infrastructure 2>/dev/null | grep -v "medislot_admin\|AdministratorAccess" || true
echo "Done. Now: git add -A && git commit -m 'Remove the admin role and dead code'"
