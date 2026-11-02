# Milestone 3 - Doctor domain: profiles, availability and slots

## What was fixed in the Milestone 2 code first
| Problem | Fix |
|---|---|
| The pipeline's packaging step ran `cp application/auth/* application/auth/` (source = destination), so new code never reached Lambda | Packaging copies the whole `application/` tree into the zip with Linux wheels |
| `GET /auth/test-token` was routed but the handler only knew `/auth/test-access`, so it always answered 405 | Handler accepts both paths |
| HTTP API cannot return 401 for a bad token and 403 for the wrong role | Migrated to a REST API with a REQUEST authorizer: 401 = missing/expired/tampered/replaced/logged-out, 403 = wrong role |
| Access token never reached the active list in the deployed path | Login and refresh always write the user's single row in `TOKEN_ACTIVELIST` (and return the replaced jti in the log) |
| Refresh token was reusable | Refresh now **rotates**: the old token is consumed with a conditional delete and a new one is issued |
| Login results | Login returns tokens only (`access_token`, `token_type`, `expires_in`, `refresh_token`, `refresh_expires_in`) - no user details |
| Logout | Deletes the user's active access token and **every** refresh token of that user |

## Token rules as implemented
* One active access token per user. A new login or refresh replaces the old one; the old token is rejected at once (authorizer cache TTL = 0, justified: the active-list check must be immediate; cost is one strongly consistent DynamoDB `GetItem` per request).
* Refresh tokens are opaque, stored only as SHA-256 hashes, expire by DynamoDB TTL, rotate on use.
* Lifetimes and bcrypt cost come from SSM: `auth/access-token-ttl-seconds` (900), `auth/refresh-token-ttl-seconds` (43200 = 12 h, your earlier choice; the brief says 7 days = 604800), `auth/bcrypt-work-factor` (12, never lower).
* Optional doctor registration gate: create SecureString `/medislot/<env>/auth/doctor-registration-code` to require `registration_code` on doctor sign-up.

## API added in Milestone 3 (all need a doctor token unless stated)
| Method and path | Purpose |
|---|---|
| `GET /doctors/me` | Own profile (404 until created) with a short-lived photo URL |
| `PUT /doctors/me` | Create (first call, needs first_name, last_name, licence_number, hospital_id, specialisation_id, timezone) or update the profile |
| `POST /doctors/me/photo` | Pre-signed S3 POST (content type pinned, size limited) |
| `GET/POST /doctors/me/consultation-types` | List / create (`Quick Consult` 15, `Standard Consult` 30, `Detailed Consult` 60, `Health Camp Session` 60 - from SSM) |
| `PUT/DELETE /doctors/me/consultation-types/{id}` | Change fee / delete (409 while used) |
| `GET/PUT /doctors/me/availability` | Weekly patterns; PUT replaces the schedule and rebuilds unbooked slots |
| `GET/POST /doctors/me/blackouts`, `DELETE .../{id}` | Leave dates; adding blocks that date's unbooked slots, removing reopens them |
| `GET /doctors/me/schedule?date=` or `?from=&to=` (max 14 days) | Day / week view with the patients booked into each slot |
| `GET /hospitals`, `GET /specialisations` | Reference lists (any signed-in user) |
| `GET /auth/test-token` | Any signed-in user: shows the identity the authorizer resolved |

Identity is read only from the authorizer context; the body, query string and headers are never used to decide who is acting (there is a unit test for this).

## SSM parameters (created once by the pipeline, never overwritten)
`rules/quick-consult-minutes`, `rules/standard-consult-minutes`, `rules/detailed-consult-minutes`, `rules/health-camp-minutes`,
`rules/health-camp-min-capacity` (2), `rules/health-camp-max-capacity` (10), `rules/booking-window-days` (30), `rules/photo-max-bytes`.

## Infrastructure added
`storage.yaml` (private photo bucket), `doctor.yaml` (doctor API Lambda, slot materialiser Lambda, EventBridge Scheduler
schedule `cron(15 0 * * ? *)`, IAM roles, X-Ray active tracing), `web.yaml` (one t2.micro running Flask with an Elastic IP;
disposable: it pulls the app from S3 on boot), `api.yaml` rewritten as a REST API.

## How to demo the checkpoint
1. Pipeline summary: lint, unit tests, deploy, **"Run slot materialiser twice"** (second run inserted 0), **"Smoke test"** (about 50 checks).
2. DST: `python -m pytest tests/unit/test_materialiser.py -v` - worked cases for New York, London, the spring-forward gap and Kolkata.
3. Idempotency by hand: invoke `medislot-<env>-slot-materialiser` twice (optionally with `{"as_of":"2026-10-30T00:00:00Z"}`) and compare `inserted`.
4. Web: open the `WebUrl` stack output, Login as Doctor, edit profile (+ photo), add a consultation type, set availability, open Schedule.

## Known limits / next milestones
* Web sessions are kept in memory (one gunicorn worker); restarting the service signs users out. The site is plain HTTP; put CloudFront or an ALB with a certificate in front for production.
* Slot cancellation, appointment outcome marking and the booking engine are Milestones 5-6. Handlers under `application/booking`, `patient`, `availability`, `refund`, `waitlist`, `notification`, `doctor` are earlier placeholders that still use the pre-ER column names; they are not deployed.
* The smoke test creates one throw-away patient and doctor per run (`@smoke.medislot.test`); delete them periodically in dev.
* The OIDC role used by GitHub Actions needs permission for the new resources (S3, EventBridge Scheduler, EC2 + Elastic IP, API Gateway deployments, Lambda).
