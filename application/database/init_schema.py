"""
Initialize the MediSlot MySQL schema.

This module is intended to run as a one-time deployment task.
It does not run automatically when connection.py is imported.

Required:
    application/database/schema.sql
"""

import logging
import os
from pathlib import Path

from application.database.connection import get_connection


logger = logging.getLogger()
logger.setLevel(logging.INFO)


SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def load_schema():
    """Read the SQL schema file."""

    if not SCHEMA_FILE.is_file():
        raise FileNotFoundError(
            f"Schema file not found: {SCHEMA_FILE}"
        )

    sql_text = SCHEMA_FILE.read_text(encoding="utf-8")

    if not sql_text.strip():
        raise RuntimeError("The schema.sql file is empty.")

    return sql_text


def split_sql_statements(sql_text):
    """
    Split ordinary SQL statements separated by semicolons.

    This supports a schema containing CREATE TABLE and SET statements.
    It is not intended for stored procedures or triggers containing
    internal semicolons.
    """

    statements = []
    current_statement = []
    quote = None
    escaped = False
    line_comment = False
    block_comment = False

    index = 0

    while index < len(sql_text):
        char = sql_text[index]
        next_char = (
            sql_text[index + 1]
            if index + 1 < len(sql_text)
            else ""
        )

        if line_comment:
            if char == "\n":
                line_comment = False
                current_statement.append(char)
            index += 1
            continue

        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue

        if quote:
            current_statement.append(char)

            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None

            index += 1
            continue

        if char == "-" and next_char == "-":
            line_comment = True
            index += 2
            continue

        if char == "#":
            line_comment = True
            index += 1
            continue

        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue

        if char in ("'", '"', "`"):
            quote = char
            current_statement.append(char)
            index += 1
            continue

        if char == ";":
            statement = "".join(current_statement).strip()

            if statement:
                statements.append(statement)

            current_statement = []
            index += 1
            continue

        current_statement.append(char)
        index += 1

    if quote:
        raise RuntimeError(
            "The schema.sql file contains an unterminated quoted value."
        )

    if block_comment:
        raise RuntimeError(
            "The schema.sql file contains an unterminated block comment."
        )

    final_statement = "".join(current_statement).strip()

    if final_statement:
        statements.append(final_statement)

    return statements


SCHEMA_VERSION = 4

# Dependency order does not matter for DROP because foreign key checks are disabled,
# but the list must name every application table.
APP_TABLES = (
    "refund_ledger", "appointments", "materialised_slots", "blackout_dates",
    "availability_patterns", "consultation_types", "doctors", "patients",
    "specialisations", "hospitals", "users",
)


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (name,),
    )
    row = cursor.fetchone()
    return (row["n"] if isinstance(row, dict) else row[0]) > 0


def _current_version(cursor):
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        " id TINYINT NOT NULL PRIMARY KEY,"
        " version INT NOT NULL,"
        " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB"
    )
    cursor.execute("SELECT version FROM schema_meta WHERE id = 1")
    row = cursor.fetchone()
    if not row:
        return None
    return row["version"] if isinstance(row, dict) else row[0]


def initialize_database():
    """Create or upgrade the schema.

    * Fresh database                      -> create everything.
    * Same SCHEMA_VERSION                 -> idempotent no-op (CREATE IF NOT EXISTS, seed upserts).
    * Older / unversioned database (the pre-ER layout used by Milestone 2) is REBUILT:
      all application tables are dropped and re-created from schema.sql. This is a
      development-only convenience and is allowed only when ALLOW_SCHEMA_REBUILD=true,
      because it deletes data.
    """
    statements = split_sql_statements(load_schema())
    if not statements:
        raise RuntimeError("No SQL statements were found in schema.sql.")

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            version = _current_version(cursor)
            legacy = version is None and _table_exists(cursor, "users")
            outdated = version is not None and version < SCHEMA_VERSION

            # Milestone 3 -> 4 is a small additive migration. Keep production data intact.
            # Older/unversioned layouts are still rebuilt only when explicitly allowed.
            if version == 3:
                logger.info("Applying schema migration 3 -> 4")
                if _table_exists(cursor, "appointments"):
                    cursor.execute(
                        "ALTER TABLE appointments ADD COLUMN reschedule_count TINYINT UNSIGNED NOT NULL DEFAULT 0"
                    )
                version = 4
                cursor.execute(
                    "UPDATE schema_meta SET version = 4, applied_at = CURRENT_TIMESTAMP WHERE id = 1"
                )
                connection.commit()
                return

            if legacy or outdated:
                if os.environ.get("ALLOW_SCHEMA_REBUILD", "").lower() != "true":
                    raise RuntimeError(
                        "The database uses an older schema layout. Set ALLOW_SCHEMA_REBUILD=true "
                        "(development only) to drop and rebuild all application tables."
                    )
                logger.warning("Rebuilding schema: version %s -> %s (data is dropped)", version, SCHEMA_VERSION)
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                for table in APP_TABLES:
                    cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

            for number, statement in enumerate(statements, start=1):
                logger.info("Executing database schema statement %s of %s", number, len(statements))
                cursor.execute(statement)

            cursor.execute(
                "INSERT INTO schema_meta (id, version) VALUES (1, %s) "
                "ON DUPLICATE KEY UPDATE version = VALUES(version), applied_at = CURRENT_TIMESTAMP",
                (SCHEMA_VERSION,),
            )
        connection.commit()
        logger.info("Database schema is at version %s.", SCHEMA_VERSION)
    except Exception:
        connection.rollback()
        logger.exception("Database schema initialization failed.")
        raise
    finally:
        connection.close()


def lambda_handler(event, context):
    """AWS Lambda entry point for schema initialization."""
    initialize_database()
    return {"statusCode": 200, "body": "MediSlot database schema is up to date."}
