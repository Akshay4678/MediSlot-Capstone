# Milestone 6 — Reschedule Saga, Waitlist and Notifications

This implementation excludes DST/time-zone scheduling changes and X-Ray tracing as requested.

## Reschedule

`POST /appointments/{appointment_id}/reschedule` validates the six-hour cutoff and the maximum reschedule attempts, then starts the Standard Step Functions state machine.

State flow:

`ClaimTarget -> CommitReschedule -> Succeeded`

If commit fails after the target seat has been claimed:

`CommitReschedule -> Compensate -> Compensated`

The target seat is claimed in `application/booking/slot_store.py`. The appointment and RDS slot counters are updated in `application/workflow/handler.py`. The old seat is released only after the RDS transaction commits. `FORCE_RELEASE_FAILURE=1` can be used in a controlled test to force the compensation branch.

## Waitlist

The DynamoDB `WaitlistTable` uses `slot_id#patient_id` as the row key, with GSIs for `slot_id + joined_at` and `patient_id + joined_at`. A `slot_id#META` row atomically enforces the five-entry cap.

`HoldOffersTable` uses `slot_id` as its primary key. That makes the slot itself the uniqueness boundary: only one live offer can exist for a slot.

When a seat is freed, EventBridge sends `SlotSeatFreed` to the workflow Lambda. The worker selects the first waiting patient, reserves the seat in `SlotCapacityTable`, increments the RDS `materialised_slots.booked_count`, and creates the live hold. Normal booking sees the hold and returns `SLOT_HELD`.

The hold expiry sweeper runs every five minutes. It marks the offer expired, returns the waitlist entry to `WAITING`, releases the DynamoDB reservation, frees the RDS seat, and publishes `SlotSeatFreed` so the next patient can be promoted.

## Notifications

The eight appointment-change events are:

1. `AppointmentBooked`
2. `AppointmentCancelled`
3. `AppointmentRescheduled`
4. `WaitlistJoined`
5. `WaitlistLeft`
6. `WaitlistHoldOffered`
7. `WaitlistOfferAccepted`
8. `OffersExpired`

Each event has its own SQS queue and DLQ. The queues invoke the workflow Lambda, which publishes to SNS. `NotificationDispatchTable` is the dispatch ledger keyed by the event ID so duplicate SQS deliveries are ignored after a successful dispatch.

## Scheduled workers

- Hold expiry sweeper: every 5 minutes.
- Reminder dispatcher: every 5 minutes; dispatches reminders around 24 hours and 2 hours before confirmed appointments.
- No-show sweeper: every 15 minutes; marks confirmed appointments that are more than 15 minutes past start as `NO_SHOW`, frees the seat and publishes the seat-freed event.
