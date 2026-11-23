# Milestone 5 — Booking, cancellation, refunds

| Requirement | Where |
|---|---|
| Booking endpoint, full validation | `POST /appointments` -> `application/booking/service.py` |
| OCC with conditional writes + version | `booking/slot_store.py` |
| Bounded retry + backoff, retries as metric | `slot_store.reserve`, `BookingConflictRetries` |
| Exact capacity incl. Health Camp, 409 when full | same conditional update, `SLOT_FULL` |
| Idempotency store | `booking/idempotency.py` |
| System of record in RDS | `appointments`, `refund_ledger` (schema.sql / init_schema.py) |
| Events | `booking/events.py`, `infrastructure/cloudformation/events.yaml` |
| Listing and detail scoped to caller | `GET /appointments`, `GET /appointments/{id}`, doctor: `/doctors/me/appointments` |
| Cancellation + refund tiers (SSM) | `POST /appointments/{id}/cancel`, `booking/refunds.py` |
| Doctor slot cancellation | `POST /doctors/me/slots/cancel` |
| Refund ledger endpoint | `GET /refunds` |
| UI polish, consultation-type filter, one shared slot reader | `web/`, `application/common/slot_query.py` |

Run: `pytest tests/unit` (273 tests). Live: `API_BASE_URL=... python scripts/smoke_test.py` and `python scripts/concurrency_check.py`.
See `docs/concurrency-design-note.md`.

## Deployment note
The data stack (`rds.yaml`) now holds only RDS and the buckets. Waitlist, hold-offer and notification tables belong to later milestones and are not deployed yet.
The two booking tables are in `infrastructure/cloudformation/booking-tables.yaml` (stack `<project>-<env>-booking`). The pipeline re-creates that stack automatically if a table was deleted by hand. The smoke test is the last job, after the web stack.
