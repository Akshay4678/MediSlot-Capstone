# MediSlot - Capstone

Doctor appointment booking platform on AWS (Lambda, API Gateway, RDS MySQL, DynamoDB, EventBridge, S3, EC2).

* `application/` - Lambda source (auth, authorizer, doctor API, slot materialiser, shared helpers)
* `web/` - Flask web application (doctor and patient portals), deployed to EC2
* `infrastructure/cloudformation/` - network, data, dynamodb, storage, auth, doctor, api, web stacks
* `.github/workflows/` - CI (lint, cfn-lint, gitleaks, unit tests) and the deployment pipeline
* `scripts/smoke_test.py` - end-to-end API check used by the pipeline
* `docs/` - data model, milestone notes (`docs/milestone-3.md`)

Run the tests locally: `pip install -r requirements-dev.txt && python -m pytest tests/unit -v`
