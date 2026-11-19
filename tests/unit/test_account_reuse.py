"""Schema upgrade for soft delete + email reuse (application/database/init_schema.py).

The upgrade runs on every deploy against a database that already holds data, so each step must
check the current state first and be safe to repeat. A scripted cursor stands in for MySQL.
"""

from application.database import init_schema as db


class ScriptedCursor:
    """Answers the information_schema look-ups from a small state and records every DDL statement."""

    def __init__(self, tables, columns, indexes):
        self.tables, self.columns, self.indexes = set(tables), set(columns), set(indexes)
        self.ddl = []
        self._row = None

    def execute(self, sql, params=()):
        if "information_schema.tables" in sql:
            self._row = {"n": int(params[0] in self.tables)}
        elif "information_schema.columns" in sql:
            self._row = {"n": int((params[0], params[1]) in self.columns)}
        elif "information_schema.statistics" in sql:
            self._row = {"n": int((params[0], params[1]) in self.indexes)}
        else:
            self.ddl.append(" ".join(sql.split()))
            if "ADD COLUMN active_email" in sql:
                self.columns.add(("users", "active_email"))
            if "ADD COLUMN is_active" in sql:
                self.columns.add(("patients", "is_active"))
            if "CREATE UNIQUE INDEX uq_users_active_email" in sql:
                self.indexes.add(("users", "uq_users_active_email"))
            if "CREATE INDEX ix_users_email" in sql:
                self.indexes.add(("users", "ix_users_email"))
            if "DROP INDEX uq_users_email" in sql:
                self.indexes.discard(("users", "uq_users_email"))

    def fetchone(self):
        return self._row


def old_database():
    return ScriptedCursor(
        tables={"users", "patients"},
        columns={("users", "is_active"), ("patients", "emergency_contact")},
        indexes={("users", "uq_users_email")},
    )


def test_upgrade_adds_the_columns_and_swaps_the_email_rule():
    cur = old_database()
    db._ensure_patient_is_active(cur)
    db._ensure_email_reuse(cur)

    text = "\n".join(cur.ddl)
    assert "ALTER TABLE patients ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE" in text
    # Accounts that were closed before the upgrade keep their closed state.
    assert "UPDATE patients p JOIN users u ON u.user_id = p.user_id SET p.is_active = u.is_active" in text
    assert "GENERATED ALWAYS AS (IF(is_active, email, NULL)) VIRTUAL" in text
    # The new rule is created BEFORE the old one is dropped, so email uniqueness is never unprotected.
    new_rule = next(i for i, s in enumerate(cur.ddl) if "CREATE UNIQUE INDEX uq_users_active_email" in s)
    drop_old = next(i for i, s in enumerate(cur.ddl) if "DROP INDEX uq_users_email" in s)
    assert new_rule < drop_old
    # Logins still look the email up by index.
    assert any("CREATE INDEX ix_users_email" in s for s in cur.ddl)


def test_upgrade_is_safe_to_run_again_on_every_deploy():
    cur = old_database()
    db._ensure_patient_is_active(cur)
    db._ensure_email_reuse(cur)
    cur.ddl.clear()

    db._ensure_patient_is_active(cur)
    db._ensure_email_reuse(cur)
    assert cur.ddl == []


def test_upgrade_does_nothing_when_the_tables_do_not_exist_yet():
    cur = ScriptedCursor(tables=set(), columns=set(), indexes=set())
    db._ensure_patient_is_active(cur)
    db._ensure_email_reuse(cur)
    assert cur.ddl == []


def test_schema_sql_matches_the_upgrade_for_a_fresh_database():
    sql = db.load_schema()
    assert "active_email VARCHAR(254) GENERATED ALWAYS AS (IF(is_active, email, NULL)) VIRTUAL" in sql
    assert "UNIQUE KEY uq_users_active_email (active_email)" in sql
    assert "UNIQUE KEY uq_users_email" not in sql
    patients = sql.split("CREATE TABLE IF NOT EXISTS patients")[1].split("ENGINE=InnoDB")[0]
    assert "is_active BOOLEAN NOT NULL DEFAULT TRUE" in patients
