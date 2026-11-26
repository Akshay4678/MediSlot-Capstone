# Milestone 6 - reschedule saga, waitlist, notifications

## Reschedule saga (Step Functions EXPRESS, synchronous)
validate -> claim_target (DynamoDB seat) -> move_appointment (one RDS transaction) -> release_original -> publish AppointmentRescheduled.
If a step after `claim_target` fails, `compensate` releases the claimed seat (and reverts the move if it had committed),
then the execution ends in the `RescheduleCompensated` Fail state; the API maps it to a clean 409 and the metric
`ReschedulesCompensated` is emitted. Rules: at most 2 reschedules per appointment, not within 6 hours of the start.
`RESCHEDULE_STATE_MACHINE_ARN` selects Step Functions; when unset (tests, local) the same saga runs in-process.

## Waitlist and hold offers
Join/leave/list (cap 5 per slot). `SlotSeatFreed` -> SQS -> jobs Lambda -> `offer_next`: reserves the seat for the first
waiting patient (WAITING->OFFERED conditional claim), publishes WaitlistOfferIssued. Held seats are not bookable by
anyone else. Accept converts the hold; the `expire_offers` schedule (every minute) expires old offers, frees the
seat and offers it to the next patient (the WaitlistPromotions metric counts patients promoted from the waitlist to a booking). `offer_next` is idempotent.

## Notifications
EventBridge rule -> SQS (+ DLQ, maxReceiveCount 5) -> Lambda (partial batch failures) -> SNS patient/doctor topics.
The eight notices: booked (patient, doctor), rescheduled (both), cancelled by patient (both), cancelled by doctor
(patient), offer issued, offer expired, reminder 24h, reminder 2h. Exactly-once: each notice claims a row in the
DynamoDB DispatchLog (conditional put, 60 s lease) before publishing; a duplicate delivery is skipped.

## Mandatory tests
Test 3 (reschedule compensation) and Test 4 (waitlist exclusivity) live in `tests/unit/test_reschedule.py` and
`tests/unit/test_waitlist.py`.
