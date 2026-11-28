# Cost review (estimate, ap-south-1, light usage; check Cost Explorer for real values)

| Component | Driver | Notes |
|---|---|---|
| RDS MySQL (db.t3.micro) | instance hours + storage + backups | largest fixed cost; snapshots retained after teardown cost storage only |
| EC2 web (t3.micro) + Elastic IP | instance hours | stop/terminate when idle |
| NAT / VPC endpoints | hourly per endpoint | interface endpoints (secrets, sns, sync-states, ...) charge per hour: review which are still needed |
| Lambda, API Gateway, Step Functions EXPRESS, SQS, SNS, EventBridge | per request | near zero at capstone volumes |
| DynamoDB PAY_PER_REQUEST + PITR | requests + PITR storage | cents per month |
| CloudWatch | dashboard (3 free), alarms (10 free, then 0.10 each: 17 alarms), custom metrics (0.30 each per dimension set), logs | dimensioned metrics (Tier, Reason, Event x Recipient) create several series each |
| AWS Budgets | first 2 budgets free | budget `medislot-<env>-monthly` alerts at 80% actual / 100% forecast |

Actions: (1) tag filter `Project=medislot` in Cost Explorer after activating the cost allocation tag; (2) terminate the
stack set when not demoing (`docs/runbook.md`); (3) consider removing unused interface VPC endpoints; (4) reduce log retention to 14 days.
