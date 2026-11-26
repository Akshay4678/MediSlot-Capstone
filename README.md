# MediSlot - Capstone

Doctor appointment booking platform on AWS (Lambda, API Gateway, RDS MySQL, DynamoDB, EventBridge, S3, EC2).

* `application/` - Lambda source (auth, authorizer, doctor API, slot materialiser, shared helpers)
* `web/` - Flask web application (doctor and patient portals), deployed to EC2
* `infrastructure/cloudformation/` - network, data, dynamodb, storage, auth, doctor, api, web stacks
* `.github/workflows/` - CI (lint, cfn-lint, gitleaks, unit tests) and the deployment pipeline
* `scripts/smoke_test.py` - end-to-end API check used by the pipeline
* `docs/` - architecture, data model, API, runbook, test report, DR drill, cost review, milestone notes

Run the tests locally: `pip install -r requirements-dev.txt && python -m pytest tests/unit -v`

## Milestone 5
Booking, cancellation and refunds: see docs/milestone-5.md and docs/concurrency-design-note.md. Live checks: scripts/smoke_test.py, scripts/concurrency_check.py.

## Milestones 6 and 7
Reschedule saga, waitlist with hold offers, notifications and reminders: `docs/milestone-6.md`.
Observability, portals, hardening, backups, drift and DR: `docs/milestone-7.md`. Operations: `docs/runbook.md`.

## Submission note
* Web URL: `<fill in: printed by the Deploy Web step>`
* API base URL: `<fill in: ApiUrl output of the -api stack>`
* Dashboard: CloudWatch -> `medislot-operations`; alarms topic `medislot-<env>-alarms`
* Demo accounts: register one doctor and one patient from the landing page
* Documents: `docs/architecture.md`, `docs/data_model/`, `docs/concurrency-design-note.md`, `docs/api.md`, `docs/runbook.md`,
  `docs/test-report.md`, `docs/dr-drill.md` (fill in the measured restore time), `docs/cost-review.md`
