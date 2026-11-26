# Test report

Run: `pip install -r requirements-dev.txt && python -m pytest tests/unit -q` (about 4 seconds).

| Area | File(s) | Highlights |
|---|---|---|
| Booking engine and concurrency | test_booking_engine, test_booking_api, concurrency_check | optimistic version conflicts, idempotency, no overbooking |
| Cancellation and refunds | test_cancellation_refunds | tiers 100/75/50/0, boundary instants, doctor cancel = 100% |
| Availability changes | test_availability_change | booked slot no longer in the new pattern is cancelled and refunded; CLOSED never regenerated |
| Reschedule | test_reschedule | **Mandatory 3** saga compensation (forced failure during release and DB move), limits |
| Waitlist | test_waitlist | **Mandatory 4** one freed seat, three waiting, one live offer; concurrent handlers create one offer; expiry |
| Notifications | test_notifications | eight notices, exactly-once, batch partial failure |
| Doctor/patient API, authorizer | test_doctor_api, test_milestone6_api, test_authorizer_policy | routes, role denial, no-show/complete |
| Web portals | test_web | role guards in both directions, forms |
| Infrastructure audit | test_infrastructure_audit, test_booking_tables_template | IAM wildcards, PITR, backups, tags, alarms, DLQs |

Result at packaging time: all tests pass (see the final output of the command above).
Not covered by automated tests (need a live AWS account): Step Functions execution of the ASL, EventBridge Scheduler
firing, SQS redrive, CloudWatch dashboard rendering. Use `scripts/smoke_test.py` and the runbook for those.
