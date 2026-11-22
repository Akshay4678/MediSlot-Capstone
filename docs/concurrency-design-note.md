# Concurrency Design Note — booking a seat

## Problem
Many patients can press "Book" for the same slot at the same instant. A Health Camp Session has several seats
(capacity N); a normal consultation has one. The system must never confirm more than N, must not hand a patient two
appointments for one click, and must stay correct under retries.

## Where the truth lives
| Concern | Store | Why |
|---|---|---|
| Seats taken right now (hot counter) | DynamoDB `SlotCapacity` (PK `slot_id`; `booked_count`, `version`) | single-item conditional writes are atomic and cheap |
| Appointment, refund ledger (system of record) | RDS MySQL | relational, auditable |
| Capacity of a slot | RDS `materialised_slots.capacity` (never copied into DynamoDB) | one source of truth, so a changed capacity applies at once |
| Replay protection | DynamoDB `Idempotency` + RDS UNIQUE(patient_id, idempotency_key) | fast path plus durable backstop |

## Optimistic concurrency control (OCC)
1. Strongly-consistent read of the slot item: `booked_count = c`, `version = v` (absent = 0 / 0).
2. If `c >= capacity` -> reject, **409 SLOT_FULL**.
3. One conditional update: `SET booked_count = booked_count + 1, version = version + 1`
   `IF version = v AND booked_count < capacity`. First booking creates the item with `attribute_not_exists(slot_id)`.
4. `ConditionalCheckFailed` means somebody else won between our read and write -> **conflict**: count it, sleep
   (exponential backoff with full jitter, base 25 ms, cap 1 s), re-read, try again.
5. After `rules/booking-max-attempts` (default 6) failed attempts -> 503 `CONTENTION` with Retry-After; nothing was reserved.

Capacity is enforced twice by the same condition (`booked_count < capacity`), so even if the version check were
removed the seat count could not exceed capacity. The version makes the contention visible and the protocol explicit.

### Why the slot row in RDS is NOT locked
The only RDS lock is `SELECT ... FROM patients WHERE patient_id = ? FOR UPDATE`, which serialises *one patient's own*
requests (so double-click and overlapping-time checks are race-free). Patients competing for the same slot do **not**
queue behind a database lock; they genuinely collide in DynamoDB. That is what makes `BookingConflictRetries`
meaningful: it cannot be zero by accident of serialisation.

## Order of operations in a booking
1. Validate body and `Idempotency-Key` header. 2. `IdempotencyStore.begin` (NEW / REPLAY / 409 in progress / 422 key reused).
3. RDS: lock patient row, validate slot (exists, OPEN, not started, inside booking window), patient rules (max 3 upcoming
with a doctor, no overlap, not already booked).
4. DynamoDB: reserve the seat (OCC above). 5. RDS: insert appointment (+ slot `booked_count`, status FULL when full), **commit**.
6. Only after commit: publish `AppointmentBooked`, emit metrics, mark idempotency COMPLETED.
If step 5 fails the seat is released (`booked_count - 1 IF booked_count >= 1`, version bumped) and the key abandoned.

## Idempotency
Key = `<user_id>#<Idempotency-Key>`. Stored with a SHA-256 of the request, state IN_PROGRESS (30 s lock) then COMPLETED
with the original response, TTL `rules/idempotency-ttl-hours` (24 h). Same key + same request -> original appointment
returned (`idempotent_replay: true`, no second seat). Same key + different request -> 422 `IDEMPOTENCY_KEY_REUSED`.
Concurrent duplicate while in progress -> 409 `REQUEST_IN_PROGRESS`. A crashed worker's lock expires after 30 s and can
be taken over. If the DynamoDB record expired, the RDS UNIQUE(patient_id, idempotency_key) still returns the original.

## Cancellation and seat release
Patient cancel: refund tier from exact instants (>=48h FULL 100%, >=24h 75%, >=6h 50%, else none; windows in SSM
`rules/refund-full-hours`, `refund-75-hours`, `refund-50-hours`; a boundary instant belongs to the higher tier).
Appointment -> CANCELLED, ledger row, seat released in DynamoDB, slot reopened, events `AppointmentCancelled` + `SlotSeatFreed`.
Doctor cancels a slot: slot -> CLOSED (kept by the slot rebuild), every CONFIRMED patient refunded 100% (`DOCTOR_CANCELLED`).

## Observability
CloudWatch namespace `MediSlot`: `BookingConflictRetries` (one per OCC conflict), `CancellationsByTier` (dim Tier),
`BookingsConfirmed`, `BookingsRejected` (dim Reason). Events (source `medislot.appointments`) go to the bus
`<project>-<env>-events`; the rule copies them to log group `/aws/events/<project>-<env>-appointments` so they can be read.

Check contention after `python scripts/concurrency_check.py`:
`aws cloudwatch get-metric-statistics --namespace MediSlot --metric-name BookingConflictRetries --dimensions Name=Environment,Value=dev --statistics Sum Maximum --period 60 --start-time ... --end-time ...`
With 24 patients on 3 seats expect a clearly non-zero Sum.

## Known limitations (honest list)
- A process crash between seat reservation and RDS commit leaks one DynamoDB seat; no reconciliation job yet
  (design: periodically recompute `booked_count` from RDS and correct DynamoDB).
- Event publish and metrics are best-effort after commit (no outbox); a failure is logged, not retried.
- Contention above the retry budget returns 503 rather than queueing.

## Tests
`tests/unit/test_booking_engine.py` — MANDATORY 1 (24 simultaneous bookings, capacity 3: exactly 3 succeed, DynamoDB count 3,
retries recorded) and MANDATORY 2 (same key replayed: one appointment). `tests/unit/test_cancellation_refunds.py` — every refund
boundary to the microsecond. Both run in CI (`pytest tests/unit`). Live check: `scripts/concurrency_check.py`.
