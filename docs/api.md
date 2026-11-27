# API reference (all JSON; `Authorization: Bearer <access token>` except /auth/*)

## Auth
| Method | Path | Notes |
|---|---|---|
| POST | /auth/register, /auth/login, /auth/refresh, /auth/logout | role is `patient` or `doctor` |

## Public catalogue
GET /hospitals, GET /specialisations, GET /doctors/search?..., GET /doctors/{doctor_id}, GET /doctors/{doctor_id}/slots?from=&to=
(only OPEN, unheld slots; a slot that is full is returned with `full: true` so the patient can join the waitlist).

## Doctor (role doctor)
| Method | Path | Purpose |
|---|---|---|
| GET/PUT | /doctors/me | profile (a timezone change reconciles slots) |
| POST | /doctors/me/photo | pre-signed upload |
| GET/POST/PUT/DELETE | /doctors/me/consultation-types[/{id}] | Quick / Standard / Detailed durations and fees |
| GET/PUT | /doctors/me/availability | weekly patterns; PUT never cancels a booking: booked slots stay as they are, new slots that would overlap them are skipped and listed in `slots.skipped_overlapping` (`kept_booked` counts the kept ones) |
| GET/POST/DELETE | /doctors/me/blackouts[/{id}] | blackout dates (cancel booked slots with refunds) |
| GET | /doctors/me/schedule?from=&to= | day/week schedule with patients per slot |
| GET | /doctors/me/appointments[/{appointment_id}] | list and detail |
| POST | /doctors/me/appointments/{id}/no-show | patient did not come; allowed from the appointment's start until 30 minutes after it (`rules/attendance-window-minutes`); closes the slot when nothing else is confirmed. Afterwards 409 `ATTENDANCE_WINDOW_CLOSED` |
| POST | /doctors/me/appointments/{id}/complete | outcome attended, same window. An appointment the doctor did not mark is recorded as COMPLETED automatically when the window ends |
| POST | /doctors/me/slots/cancel | body `{slot_id}`; cancels bookings (100% refund), slot becomes CLOSED |

## Patient (role patient)
| Method | Path | Purpose |
|---|---|---|
| GET/POST/PUT/DELETE | /patient/me | profile |
| POST | /appointments | book (`Idempotency-Key` header) |
| GET | /appointments[?when=upcoming\|past] , /appointments/{id} | list and detail (`can_reschedule`, `reschedule_count`) |
| POST | /appointments/{id}/cancel | refund tier by notice |
| PATCH | /appointments/{id} | reschedule `{slot_id}` (saga; 409 on limit/cutoff/full) |
| GET | /refunds | refund history |
| POST/DELETE | /slots/{slot_id}/waitlist | join (max 5, 409 WAITLIST_FULL) / leave. `slot_id` contains `#`: URL-encode it |
| GET | /patient/me/waitlists | my waitlists with any active offer |
| POST | /waitlist-offers/{offer_id}/accept | convert the held seat into an appointment |

Errors: `{"error": {"code": "...", "message": "..."}}`; codes include SLOT_UNAVAILABLE, SLOT_FULL, RESCHEDULE_LIMIT,
RESCHEDULE_TOO_LATE, WAITLIST_FULL, OFFER_EXPIRED, IDEMPOTENCY_CONFLICT.
