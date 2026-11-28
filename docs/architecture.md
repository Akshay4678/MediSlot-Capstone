# Architecture

```
Browser -> EC2 (Flask web: patient + doctor portals)
              |  HTTPS/JSON + JWT
              v
        API Gateway (REST)  -- Lambda authorizer (role + route rules)
              |
   +----------+-----------+------------------+
   v                      v                  v
 auth Lambda        doctor-api Lambda   patient-api Lambda ----(sync EXPRESS)----> Step Functions: reschedule saga
   |                      |                  |                                      validate -> claim_target -> move_appointment
   |                      |                  |                                      -> release_original -> publish (compensate on failure)
   v                      v                  v                                      each step = reschedule-step Lambda
 RDS MySQL  <------ appointments, refunds, doctors, patients, slots (source of truth for money and state)
 DynamoDB   <------ SlotCapacity (seat counter, optimistic version), Idempotency, Waitlist, HoldOffers, DispatchLog
              |
              v   events (AppointmentBooked/Rescheduled/Cancelled, SlotSeatFreed, WaitlistOfferIssued/Expired, ReminderDue)
        EventBridge bus --rule--> SQS notifications (+DLQ) --> notification-consumer Lambda --> SNS patient/doctor topics
                        --rule--> SQS waitlist (+DLQ)      --> jobs Lambda (offer the freed seat to the next patient)
        EventBridge Scheduler --> jobs Lambda: expire_offers (1 min), reminders (5 min), attendance (1 min)
        CloudWatch: dashboard medislot-operations, 17 alarms -> SNS alarm topic, AWS Budget
```

## Why two data stores
* **RDS (MySQL)** holds everything that needs transactions and reports: appointments, refunds, doctors, patients, slots.
* **DynamoDB** holds the hot, contended counter (`SlotCapacity`): one conditional update per booking decides who gets a
  seat (`booked_count < capacity AND version = :seen`). RDS and DynamoDB are kept consistent by ordering (claim the seat
  first, commit RDS second, release the seat if RDS fails) and by idempotency keys.

## Flows
* **Book**: validate -> idempotency claim -> reserve seat (DynamoDB) -> insert appointment (RDS tx) -> publish AppointmentBooked.
* **Cancel**: refund tier from the notice period (100/75/50/0%) -> RDS tx -> release seat -> SlotSeatFreed (waitlist).
* **Reschedule**: Step Functions saga with compensation (`docs/milestone-6.md`). At most 2 attempts, not within 6 hours of the start.
* **Waitlist**: max 5 per slot; a freed seat is *held* for the first patient (real seat, invisible to others) for a limited
  time; accept converts the hold, expiry offers the next patient.
* **Availability change**: booked slots are never touched. Unbooked slots are regenerated from the new patterns, and a new
  slot that would overlap a booked one is skipped and reported to the doctor. CLOSED slots are never regenerated.
* **Attendance**: while the slot is running (start to end) the doctor can mark NO_SHOW (fee kept, slot closed) or COMPLETED (attended). If nothing is chosen the appointment counts as attended: every API shows it as COMPLETED the moment the slot ends, and a 1-minute sweeper stores it.

## Security
JWT access tokens (short lived) + refresh tokens (DynamoDB, rotated). The authorizer lets doctors use `/doctors/me/*`
and patients `/patient/me/*`, `/appointments*`, `/slots/*/waitlist`; the web app guards routes in both directions.
IAM roles are resource-scoped with no wildcard actions (audited by `tests/unit/test_infrastructure_audit.py`).
