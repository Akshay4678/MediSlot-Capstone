# Milestone 7 - operations, portals, hardening

| Requirement | Where |
|---|---|
| `medislot-operations` dashboard | `infrastructure/cloudformation/observability.yaml` (API, Lambda, bookings, reschedules, waitlist, notifications, queues, Step Functions, RDS, DynamoDB) |
| Custom metrics | `application/booking/metrics.py` (namespace `MediSlot`): BookingsConfirmed, BookingsRejected{Reason}, BookingConflictRetries, CancellationsByTier{Tier}, ReschedulesSucceeded, ReschedulesCompensated, OffersIssued, OffersExpired, WaitlistPromotions, NotificationsDispatched{Event,Recipient}, NoShows, NoShowRate, SlotUtilisationPercent |
| Alarms (6 categories -> SNS topic `medislot-<env>-alarms`) | API (5xx, p99 latency); Lambda errors (5 functions); queues (2 DLQs); database (CPU, storage, connections); business (compensations, conflict retries, no-show rate); workflow and DynamoDB (failed executions, seat-table throttles) |
| X-Ray | Lambda Active tracing + `aws-xray-sdk` (`application/common/tracing.py`), API Gateway and Step Functions tracing, VPC endpoints for `xray` and `sync-states`. A booking and a reschedule (including `compensate`) appear as one trace |
| Doctor portal | profile, consultation types, availability, blackouts, day/week schedule with patients per slot, outcome marking (completed / no-show), slot cancellation |
| Patient portal | profile, doctor search, slot browser, booking, upcoming/past, reschedule, waitlists with offers, refund history |
| Landing page + route guards | role selection on `/`; a doctor cannot open patient routes and vice versa (`tests/unit/test_web.py`), and the authorizer denies the other role's API routes |
| IAM audit | no wildcard actions on Allow statements; `Resource: *` only for PutMetricData (namespace condition), kms:Decrypt via SSM, X-Ray and log-delivery actions (no resource-level support). Enforced by `tests/unit/test_infrastructure_audit.py` |
| Drift detection | `scripts/detect_drift.sh [env]` |
| Backups | RDS automated backups 7 days (`rds.yaml`) + manual `scripts/backup_db.sh`; DynamoDB point-in-time recovery on all five tables |
| DR drill | `scripts/dr_drill.sh [env]`, record in `docs/dr-drill.md` |
| Budget and cost tags | AWS Budget on `Project=medislot` (80% actual, 100% forecast -> SNS); every stack deployed with `--tags Project, Environment, ManagedBy`. **Activate the `Project` tag once in Billing -> Cost allocation tags** (AWS requires this manual step) |
| Cost review | `docs/cost-review.md` |
| Teardown and redeploy | `docs/runbook.md` |

Pipeline variables (optional): `MEDISLOT_ALARM_EMAIL` (subscribes an e-mail to the alarm topic; confirm the e-mail AWS sends),
`MEDISLOT_MONTHLY_BUDGET_USD` (default 50).
