# Runbook

## Daily checks
Open CloudWatch -> Dashboards -> `medislot-operations`. Both DLQ tiles must read 0 and `ReschedulesCompensated` should be flat.

## Alarm -> first action
| Alarm | Look at | Usual cause / fix |
|---|---|---|
| api-5xx, lambda-errors-* | CloudWatch Logs of that function | recent deploy; roll forward with a fix, re-run pipeline |
| dlq-notifications | `aws sqs receive-message` on the DLQ | SNS publish failure or malformed event. Fix, then redrive from the SQS console (Start DLQ redrive) |
| dlq-waitlist | same | `offer_next` failed repeatedly; check jobs Lambda logs; the 1-minute sweeper also recovers offers |
| rds-* | RDS console, Performance Insights | connection leak (Lambda concurrency) or storage; scale class/storage in `rds.yaml` |
| business-reschedule-compensated | Step Functions execution history | a downstream step failed; the saga already restored state, no manual repair needed |
| business-conflict-retries | hot slot | normal during a rush; if constant, check for a retry storm in a client |
| workflow-failed, dynamodb-seat-throttles | state machine / table metrics | switch table to higher capacity or investigate loops |

## Common operations
* **Replay a failed notification**: redrive the DLQ. The DynamoDB DispatchLog makes it safe: already-sent notices are skipped.
* **Drift check**: `scripts/detect_drift.sh dev`. Re-run the pipeline to re-apply templates.
* **Backup before risky work**: `scripts/backup_db.sh dev`.
* **Restore the database**: delete nothing; set repository variable `MEDISLOT_RESTORE_SNAPSHOT` to the snapshot id, then run the pipeline (see DR drill).
* **Restore a DynamoDB table**: `aws dynamodb restore-table-to-point-in-time --source-table-name X --target-table-name X-restored --restore-date-time <ISO time>`; verify, then swap the name in the booking stack or copy items back.

## Teardown (stop all costs)
1. `scripts/backup_db.sh dev` (keeps the data as a snapshot).
2. Delete stacks in reverse order: web, observability, api, auth, patient, doctor, notifications, workflow, events, storage (empty the photo bucket first), booking, data (empty the artifacts bucket first; the snapshot is retained), network.
3. Remove leftovers: RDS manual snapshots you no longer need, CloudWatch log groups, the `lambda artifacts` bucket.
4. Check Cost Explorer filtered by tag `Project=medislot` shows 0 the next day.

## Redeploy
Push to the deploy branch (or run the workflow manually). The pipeline recreates every stack in order, restores the newest
RDS snapshot automatically if the data stack is new, re-runs the smoke test, and prints the web URL.
Verify: dashboard exists, alarms are `OK`/`INSUFFICIENT_DATA`, `scripts/smoke_test.py` passes.
