"""Consultation types are split into a catalogue and per-doctor fees (schema version 5)."""

import re
from pathlib import Path

from application.database import init_schema

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "application" / "database" / "schema.sql").read_text(encoding="utf-8")


def table_sql(name):
    return re.search(rf"CREATE TABLE IF NOT EXISTS {name} \((.*?)\) ENGINE", SCHEMA, re.S).group(1)


def test_catalogue_has_no_doctor_or_fee_and_offerings_hold_the_fee():
    catalogue, offering = table_sql("consultation_types"), table_sql("doctor_consultation_types")
    assert "doctor_id" not in catalogue and "fee" not in catalogue
    assert "UNIQUE KEY uq_consultation_type_name (type_name)" in catalogue
    assert "fee DECIMAL(10,2)" in offering
    assert "UNIQUE KEY uq_doctor_consultation_type (doctor_id, consultation_type_id)" in offering


def test_children_reference_the_doctor_and_type_pair():
    for table in ("availability_patterns", "materialised_slots", "appointments"):
        sql = table_sql(table)
        assert re.search(r"FOREIGN KEY \(doctor_id, consultation_type_id\)\s+REFERENCES doctor_consultation_types"
                         r"\(doctor_id, consultation_type_id\)", sql), table


def test_offerings_are_created_after_the_catalogue_and_before_their_children():
    order = [m.group(1) for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)]
    assert order.index("consultation_types") < order.index("doctor_consultation_types") < order.index("availability_patterns")
    assert init_schema.SCHEMA_VERSION == 5 and "doctor_consultation_types" in init_schema.APP_TABLES


def test_no_live_code_reads_a_fee_or_doctor_from_the_catalogue():
    for rel in ("application/doctor_api", "application/patient_api", "application/slot_materialiser"):
        for path in (ROOT / rel).glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"(?<![a-z_])ct\.fee", text), path
            assert not re.search(r"(?<!doctor_)consultation_types WHERE doctor_id", text), path


class MigrationCursor:
    """Answers the information_schema lookups for an old (version 4) database and records every statement."""

    def __init__(self, old_shape=True, v4_exists=False):
        self.statements, self.old_shape, self.v4_exists, self._rows = [], old_shape, v4_exists, []

    def execute(self, sql, params=()):
        self.statements.append(" ".join(sql.split()))
        sql = self.statements[-1]
        self._rows = []
        if "information_schema.tables" in sql:
            exists = params[0] == "consultation_types" or (params[0] == init_schema.OLD_TYPES_TABLE and self.v4_exists)
            self._rows = [{"n": 1 if exists else 0}]
        elif "information_schema.columns" in sql:
            self._rows = [{"n": 1 if self.old_shape else 0}]
        elif "information_schema.referential_constraints" in sql:
            self._rows = [{"child": "availability_patterns", "fk": "fk_availability_consultation_type"},
                          {"child": "materialised_slots", "fk": "fk_slots_consultation_type"},
                          {"child": "appointments", "fk": "fk_appointments_consultation_type"}]
        elif "information_schema.table_constraints" in sql:
            self._rows = [{"n": 0}]

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows


def test_migration_step_one_drops_child_keys_then_renames_the_old_table():
    cur = MigrationCursor(old_shape=True)
    cur.v4_exists = False
    # after the rename the old table exists
    original = cur.execute

    def execute(sql, params=()):
        original(sql, params)
        if sql.startswith("RENAME TABLE"):
            cur.v4_exists = True
    cur.execute = execute
    assert init_schema._prepare_consultation_type_split(cur) is True
    drops = [i for i, s in enumerate(cur.statements) if "DROP FOREIGN KEY" in s]
    rename = [i for i, s in enumerate(cur.statements) if s.startswith("RENAME TABLE consultation_types TO")][0]
    assert len(drops) == 3 and max(drops) < rename


def test_migration_step_one_is_a_no_op_when_already_split():
    cur = MigrationCursor(old_shape=False, v4_exists=False)
    assert init_schema._prepare_consultation_type_split(cur) is False
    assert not any(s.startswith(("RENAME", "ALTER")) for s in cur.statements)


def test_migration_step_two_copies_remaps_then_adds_keys_and_drops_the_old_table():
    cur = MigrationCursor()
    init_schema._finish_consultation_type_split(cur)
    run = [s for s in cur.statements if not s.startswith("SELECT")]
    assert run[0].startswith("INSERT IGNORE INTO consultation_types")
    assert run[1].startswith("INSERT IGNORE INTO doctor_consultation_types")
    assert run[2].startswith("UPDATE doctor_consultation_types") and "SET d.fee = o.fee" in run[2]
    remap = [i for i, s in enumerate(run) if s.startswith("UPDATE ") and "SET t.consultation_type_id" in s]
    alters = [i for i, s in enumerate(run) if s.startswith("ALTER TABLE") and "FOREIGN KEY (doctor_id, consultation_type_id)" in s]
    assert len(remap) == 3 and len(alters) == 3
    assert max(remap[:1]) < min(alters)  # remap before the new keys are enforced, per table
    assert run[-1] == f"DROP TABLE {init_schema.OLD_TYPES_TABLE}"
