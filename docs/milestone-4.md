# Milestone 4 - Patient domain: profiles, search and discovery

## Endpoints (all need a patient token; a doctor token gets 403)
| Method and path | Purpose |
|---|---|
| `POST /patient/me` | Create the profile (registration already creates one, so this answers 409 `PROFILE_EXISTS` unless it was deleted) |
| `GET /patient/me` | Read **own** profile |
| `PUT /patient/me` | Update name, date of birth, gender, phone, address, city, emergency contact. Any other field (for example `patient_id`, `user_id`) is refused with 400 `UNKNOWN_FIELD` |
| `DELETE /patient/me` | Close the account: personal details are erased and login is disabled. Refused (409 `HAS_ACTIVE_APPOINTMENTS`) while upcoming confirmed appointments exist |
| `GET /doctors/search?name=&hospital=&specialisation=&city=&limit=&offset=` | Filtered search with pagination (`count`, `total`, `has_more`) |
| `GET /doctors/{doctor_id}` | Public profile with consultation types and fees |
| `GET /doctors/{doctor_id}/slots?date=` or `?from=&to=` (max 14 days) | Open slots with `available_seats`, grouped by day |

## Patient data isolation
The patient is identified only from the authorizer context (`userId`). There is no patient id in any URL, query string or body, so one patient cannot ask for another's profile. Proven by unit tests (`tests/unit/test_patient_domain.py`) and by the deployed smoke test, which registers two patients, sends patient A's id as a query parameter and in a body as patient B, and checks B still only ever receives B's own data.

## Search indexes and why
Search joins `doctors`, `hospitals`, `specialisations` and `users`. Name, hospital and city filters are **prefix** matches (`LIKE 'abc%'`) because a B-tree index can serve a prefix but never a leading wildcard. User input has `%` and `_` escaped.

| Filter | Query shape | Index used | Why this index |
|---|---|---|---|
| name (one word) | `last_name LIKE 'x%' OR first_name LIKE 'x%'` | `ix_doctors_name (last_name, first_name)` and `ix_doctors_first_name (first_name)` | The composite index leads with `last_name`; a first-name prefix cannot use it, so `first_name` has its own index. MySQL combines the two with an index merge |
| name (two words) | `first LIKE a% AND last LIKE b%` (either order) | `ix_doctors_name` | Both columns are in the composite index |
| hospital | `hospital_name LIKE 'x%'` | `ix_hospitals_name` | Prefix lookup on the hospital, then join to doctors through `ix_doctors_hospital_active` |
| city | `city LIKE 'x%'` | `ix_hospitals_city_active (city, is_active)` | `city` is the leading column; `is_active` is the other condition applied to every search |
| specialisation | `specialisation_name LIKE '%x%'` | `uq_specialisations_name`, then `ix_doctors_specialisation_active (specialisation_id, is_active)` | The specialisations table is a short reference list, so a contains-match on it is cheap; the join into doctors is by the indexed id |
| paging | `ORDER BY last_name, first_name, doctor_id LIMIT/OFFSET` | `ix_doctors_name` | Same order as the index, and `doctor_id` makes the order stable between pages |
| slot lookup | `doctor_id = ? AND date BETWEEN ? AND ?` | `ix_slots_doctor_date (doctor_id, date)` | Equality column first, range column second |
| patient's upcoming with a doctor | `patient_id = ? AND doctor_id = ? AND status = 'CONFIRMED'` | `ix_appointments_patient_status (patient_id, status)` | A patient has few appointments, so filtering the remaining column is cheap |

Verify with `EXPLAIN` on the dev database after loading realistic data; the Milestone 4 checkpoint reviews these against the real query patterns. A leading-wildcard name search (`%abc`) is deliberately not offered: it would force a full scan.

## Scope
This milestone stops at discovery: profiles, search, public doctor profile and slot availability. Booking, cancellation, refunds, reschedule, waitlist and notifications belong to Milestones 5-6 and are not part of this change set.

## Known differences from the brief's endpoint names
The brief lists `/patients/me` and `GET /doctors?name=...`. This project uses `/patient/me` and `/doctors/search`. Behaviour is the same; align the names if the mentor wants the exact paths.
