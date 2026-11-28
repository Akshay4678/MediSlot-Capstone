# Latest fixes

## Sign-in showed "Your session form expired" even with valid details
* The CSRF check only ran when a server session already existed, but the login and register forms carried no token.
  Once a browser had any session, every later login was rejected. The CSRF token now lives in the signed browser cookie,
  is part of every form (login and register included) and is checked on every POST.
* gunicorn ran 2 workers, each with its own in-memory sessions, so a login on one worker was unknown to the other.
  It now runs 1 worker with 8 threads (`web.yaml`).
* Opening a login page while already signed in now goes straight to that portal; a new login replaces the old session.

## No manual security-group changes any more (`web.yaml`)
* Port 80 is open to everyone; port 22 is open only to the EC2 Instance Connect range (`InstanceConnectCidr`, default
  ap-south-1; empty value closes it). Session Manager works with no inbound rule at all.
* The instance bootstrap no longer fails on Amazon Linux (plain `pip install` clashed with OS-owned packages and left the
  service uninstalled). Dependencies are installed into `/opt/medislot/lib`.
* The stack now waits for `/healthz` before succeeding (CreationPolicy + cfn-signal), and the pipeline fetches both login
  pages. A broken boot fails the deployment with a reason instead of leaving a dead site.

## Repository mix-ups corrected
* `requirements-dev.txt` was empty (which is why `flake8: command not found`), `README.md` contained requirements text and
  `.gitignore` was empty. Correct versions are included.
* Old unused files came back in your checkout (see `docs/runbooks/README.md` for the `git rm` list). `doctor.yaml` is back to
  generated names.

## Milestones 6 and 7 (latest)
* Availability change never cancels bookings: booked slots are kept as they are, new slots that would overlap them are skipped and reported to the doctor; doctor no-show / completed on the schedule.
* Milestone 6: reschedule saga (Step Functions), waitlist + hold offers, notifications pipeline, reminders, no-show sweeper, new API routes and portal pages.
* Milestone 7: `observability.yaml` (dashboard, 17 alarms, budget), cost-allocation tags on every stack, 7-day RDS backups, DynamoDB PITR, `scripts/detect_drift.sh`, `scripts/dr_drill.sh`, docs (architecture, API, runbook, test report, DR drill, cost review), infrastructure audit tests.
* `remove-leftovers.sh` updated: keeps application/reschedule and application/waitlist, removes old dirs and stub templates.
* Run `./remove-leftovers.sh` after unzipping, commit, push.
* Waitlist rows carry a TTL (expire_time = slot end); patients are not offered Book/waitlist for slots they booked; consultation type filter moved from the landing page to the slots page.
* Slots page consultation filter lists all four types, including Health Camp Session.
* Doctor dashboard shows today's patients with Attended / No-show (closes the slot); edge-case tests (tests/unit/test_edge_cases.py).
* Attended / No-show buttons are always visible for CONFIRMED appointments (locked until the start time); every patient and doctor is subscribed (SNS e-mail + filter policy on recipient_id) with the e-mail they registered with; SNS VPC endpoint added.
* Attendance: Attended / No-show are active only while the slot is running (start to end). Unmarked = attended: shown as COMPLETED the moment the slot ends (lazy), stored by a 1-minute sweeper (jobs/attendance.py).
* X-Ray removed (mentor's instruction): no tracing code, IAM, endpoint or stack settings remain. Ended slots on the doctor schedule show 'Slot over' / COMPLETED / NOT BOOKED instead of OPEN; the attendance hint is a hover tooltip.
