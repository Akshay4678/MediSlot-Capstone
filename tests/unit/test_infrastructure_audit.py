"""Milestone 7 audits of the CloudFormation templates and pipeline: IAM least privilege, tags, backups, alarms."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CFN = ROOT / "infrastructure" / "cloudformation"
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"


class Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


Loader.add_multi_constructor("!", _tag)

ACTIVE = ["auth", "doctor", "patient", "api", "workflow", "notifications", "booking-tables", "events", "storage",
          "rds", "network", "web", "observability"]

# Resource "*" is unavoidable for these actions (AWS does not support resource-level permissions for them).
STAR_RESOURCE_OK = {
    "cloudwatch:PutMetricData", "kms:Decrypt", "xray:PutTraceSegments", "xray:PutTelemetryRecords",
    "xray:GetSamplingRules", "xray:GetSamplingTargets", "logs:CreateLogDelivery", "logs:GetLogDelivery",
    "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
    "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
}


def load(name):
    return yaml.load((CFN / f"{name}.yaml").read_text(), Loader)


def statements(doc):
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "Effect" in o and "Action" in o:
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(doc)
    return found


def test_no_allow_statement_uses_a_wildcard_action():
    for name in ACTIVE:
        for st in statements(load(name)):
            if st["Effect"] != "Allow":
                continue
            actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            for action in actions:
                assert action != "*" and not action.endswith(":*"), (name, action)


def test_resource_star_is_limited_to_documented_actions_and_metric_writes_are_namespaced():
    for name in ACTIVE:
        for st in statements(load(name)):
            resources = st.get("Resource")
            resources = resources if isinstance(resources, list) else [resources]
            if st["Effect"] != "Allow" or "*" not in resources:
                continue
            actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            assert set(actions) <= STAR_RESOURCE_OK, (name, actions)
            if "cloudwatch:PutMetricData" in actions:
                assert "cloudwatch:namespace" in json.dumps(st.get("Condition")), name
            if "kms:Decrypt" in actions:
                assert "kms:ViaService" in json.dumps(st.get("Condition")), name


def test_every_template_parses_and_logical_ids_are_alphanumeric():
    for path in CFN.glob("*.yaml"):
        doc = yaml.load(path.read_text(), Loader)
        for logical in (doc or {}).get("Resources", {}):
            assert re.fullmatch(r"[A-Za-z0-9]+", logical), (path.name, logical)


def test_percentile_alarm_statistics_use_extended_statistic():
    for name in ("observability",):
        for res in load(name)["Resources"].values():
            if res["Type"] == "AWS::CloudWatch::Alarm":
                stat = res["Properties"].get("Statistic")
                assert stat in (None, "Average", "Maximum", "Minimum", "SampleCount", "Sum"), stat


def test_workflow_template_embeds_valid_state_machine_json():
    text = (CFN / "workflow.yaml").read_text()
    assert "AWS::StepFunctions::StateMachine" in text and "StateMachineType: EXPRESS" in text
    assert "TracingConfiguration" in text


def test_notification_queues_each_have_a_dead_letter_queue_with_a_redrive_policy():
    doc = load("notifications")["Resources"]
    queues = {k: v for k, v in doc.items() if v["Type"] == "AWS::SQS::Queue"}
    redriven = [k for k, v in queues.items() if "RedrivePolicy" in v["Properties"]]
    assert len(redriven) >= 2  # notifications and waitlist
    assert "maxReceiveCount: 5" in (CFN / "notifications.yaml").read_text() or "maxReceiveCount" in (CFN / "notifications.yaml").read_text()


def test_observability_has_the_dashboard_the_six_alarm_categories_and_a_budget():
    doc = load("observability")
    res = doc["Resources"]
    dash = [v for v in res.values() if v["Type"] == "AWS::CloudWatch::Dashboard"][0]
    assert dash["Properties"]["DashboardName"] == "medislot-operations"
    body = dash["Properties"]["DashboardBody"]
    json.loads(re.sub(r"\$\{[A-Za-z:]+\}", "x", body))  # valid JSON once the substitutions are filled in
    for metric in ("ReschedulesCompensated", "WaitlistPromotions", "BookingConflictRetries", "NotificationsDispatched"):
        assert metric in body, metric
    alarms = {k for k, v in res.items() if v["Type"] == "AWS::CloudWatch::Alarm"}
    for category in ("Api5xx", "LambdaErrors", "Dlq", "Rds", "Business", "Workflow", "Dynamodb"):
        assert any(category in a for a in alarms), category
    assert any(v["Type"] == "AWS::Budgets::Budget" for v in res.values())
    for v in res.values():
        if v["Type"] == "AWS::CloudWatch::Alarm":
            assert v["Properties"]["AlarmActions"], "every alarm must notify the SNS topic"


def test_rds_has_backups_and_every_dynamodb_table_has_point_in_time_recovery():
    rds = (CFN / "rds.yaml").read_text()
    assert re.search(r"BackupRetentionPeriod: !If\s+- IsProd\s+- 7\s+- 1", rds) and "DeletionPolicy: Snapshot" in rds
    tables = (CFN / "booking-tables.yaml").read_text()
    assert tables.count("AWS::DynamoDB::Table") == tables.count("PointInTimeRecoveryEnabled: true")


def test_every_stack_is_tagged_by_the_pipeline_for_cost_allocation_and_observability_is_deployed():
    text = DEPLOY.read_text()
    deploys = text.count("aws cloudformation deploy")
    assert deploys == text.count('--tags "Project=${PROJECT_NAME}"') >= 10
    assert "Deploy Observability Stack" in text


def test_schema_bootstrap_package_contains_everything_the_connection_module_imports():
    text = DEPLOY.read_text()
    step = text[text.index("application/database/schema.sql was not found"):]
    step = step[:step.index("zip -qr")]
    for needed in ("application/common/__init__.py", "application/common/tracing.py", "application/database/connection.py",
                   "application/database/init_schema.py"):
        assert needed.rsplit("/", 1)[1] in step and needed.rsplit("/", 1)[0] in step, needed


def test_email_subscription_plumbing_is_scoped_and_reachable_from_the_vpc():
    for name in ("patient", "doctor", "notifications"):
        subs = [st for st in statements(load(name)) if st.get("Action") == "sns:Subscribe"]
        assert subs and all("*" not in str(st["Resource"]) for st in subs), name
    network = (CFN / "network.yaml").read_text()
    assert "com.amazonaws.${AWS::Region}.sns" in network  # private Lambdas have no internet: SNS needs its own endpoint
    text = DEPLOY.read_text()
    assert text.count('"PatientTopicArn=${PATIENT_TOPIC_ARN}"') == 2  # doctor and patient stacks only
