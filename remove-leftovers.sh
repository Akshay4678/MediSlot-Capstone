#!/usr/bin/env bash
# Run from the repository root after copying the overlay files over your checkout.
# Removes old, unused files that break the lint step or conflict with the current stacks.
set -euo pipefail
git rm -f --ignore-unmatch \
  application/auth/login.py application/auth/logout.py application/auth/refresh.py application/auth/register.py \
  application/common/auth_utils.py \
  infrastructure/cloudformation/application.yaml infrastructure/cloudformation/iam.yaml infrastructure/cloudformation/scheduler.yaml \
  tests/unit/test_access_token_chain.py
echo "Done. Now: git add -A && git commit -m 'Fix login CSRF, single web worker, web stack ports and bootstrap'"
