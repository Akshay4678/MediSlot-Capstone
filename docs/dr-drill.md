# Disaster-recovery drill record

Targets (proposed): RDS restore within 30 minutes (RTO), at most 24 h of data loss from the daily automated backup, or
5 minutes with point-in-time restore (RPO). DynamoDB: point-in-time recovery, 35 days.

## Procedure
1. `scripts/backup_db.sh dev` (fresh snapshot) then `scripts/dr_drill.sh dev`.
2. The script restores the newest snapshot into a throw-away instance, prints `RESTORE TIME`, deletes the instance and lists
   every DynamoDB table's restorable window.
3. Fill in the table below with the real values and keep this file in the repository.

## Result (fill in after running the drill)
| Item | Value |
|---|---|
| Date / who | |
| Snapshot id | |
| RDS restore time (seconds) | |
| Row-count check (appointments) source vs restored | |
| DynamoDB earliest restorable time | |
| DynamoDB latest restorable time | |
| Problems found | |

The measured numbers are not filled in here because the drill needs your AWS account; running it takes about 20 minutes.
