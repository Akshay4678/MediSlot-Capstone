#!/usr/bin/env bash
# Run from the repository root after copying the overlay files over your checkout.
set -euo pipefail
git rm -f --ignore-unmatch \
  application/auth/login.py application/auth/logout.py application/auth/refresh.py application/auth/register.py \
  application/common/auth_utils.py \
  infrastructure/cloudformation/application.yaml infrastructure/cloudformation/iam.yaml infrastructure/cloudformation/scheduler.yaml \
  tests/unit/test_access_token_chain.py
echo "Done. Now: git add -A && git commit -m 'Fix web instance replacement, login CSRF, single worker, ports'"
