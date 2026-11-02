"""
Initialize the MediSlot MySQL schema.

This module is intended to run as a one-time deployment task.
It does not run automatically when connection.py is imported.

Required:
    application/database/schema.sql
"""

import logging
import re
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



def migrate_availability_pattern_identifier(cursor):
    """
    Align an existing availability_patterns table with the approved schema.

    Older development databases may have used a different numeric primary-key
    name (for example, id or availability_pattern_id). CREATE TABLE IF NOT
    EXISTS does not alter an existing table, so rename that existing identifier
    to availability_id before materialised_slots adds its foreign key.

    Existing row identifiers are preserved. If the table has no suitable
    numeric identifier, a new AUTO_INCREMENT identifier is added.
    """

    cursor.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'availability_patterns'
        """
    )
    table_row = cursor.fetchone()
    if not table_row or int(table_row["table_count"]) == 0:
        # A fresh database will create the table from schema.sql.
        return

    cursor.execute(
        """
        SELECT COLUMN_NAME, COLUMN_TYPE, EXTRA, COLUMN_KEY, ORDINAL_POSITION
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'availability_patterns'
        ORDER BY ORDINAL_POSITION
        """
    )
    rows = cursor.fetchall()
    columns = {row["COLUMN_NAME"]: row for row in rows}

    def ensure_unique_identifier(column_name, column_row):
        if column_row["COLUMN_KEY"] not in ("PRI", "UNI"):
            cursor.execute(
                f"ALTER TABLE availability_patterns "
                f"ADD UNIQUE KEY uq_availability_patterns_{column_name} "
                f"(`{column_name}`)"
            )

    if "availability_id" in columns:
        identifier = columns["availability_id"]
        ensure_unique_identifier("availability_id", identifier)

        if "auto_increment" not in (identifier.get("EXTRA") or "").lower():
            other_auto_increment = [
                row["COLUMN_NAME"]
                for row in rows
                if row["COLUMN_NAME"] != "availability_id"
                and "auto_increment" in (row.get("EXTRA") or "").lower()
            ]
            if other_auto_increment:
                raise RuntimeError(
                    "availability_patterns.availability_id is not AUTO_INCREMENT "
                    "and another AUTO_INCREMENT column already exists "
                    f"({other_auto_increment[0]}). Please inspect this legacy table "
                    "before deploying; no columns were removed."
                )
            cursor.execute(
                "ALTER TABLE availability_patterns "
                "MODIFY COLUMN availability_id BIGINT NOT NULL AUTO_INCREMENT"
            )
        return

    # Prefer the existing AUTO_INCREMENT identifier, then a single-column
    # numeric primary key. This preserves the IDs already referenced by rows.
    auto_increment_columns = [
        row for row in rows
        if "auto_increment" in (row.get("EXTRA") or "").lower()
        and re.fullmatch(r"(tinyint|smallint|mediumint|int|integer|bigint)( unsigned)?", row["COLUMN_TYPE"].lower())
    ]
    primary_columns = [row for row in rows if row["COLUMN_KEY"] == "PRI"]
    numeric_primary_columns = [
        row for row in primary_columns
        if re.fullmatch(r"(tinyint|smallint|mediumint|int|integer|bigint)( unsigned)?", row["COLUMN_TYPE"].lower())
    ]

    candidate = None
    if auto_increment_columns:
        candidate = auto_increment_columns[0]
    elif len(primary_columns) == 1 and numeric_primary_columns:
        candidate = numeric_primary_columns[0]

    if candidate:
        old_name = candidate["COLUMN_NAME"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", old_name):
            raise RuntimeError(
                "Unsupported legacy identifier name in availability_patterns."
            )

        other_auto_increment = [
            row["COLUMN_NAME"]
            for row in rows
            if row["COLUMN_NAME"] != old_name
            and "auto_increment" in (row.get("EXTRA") or "").lower()
        ]
        if other_auto_increment:
            raise RuntimeError(
                "Cannot safely migrate availability_patterns: more than one "
                "candidate identifier exists. Please inspect the table before "
                "deploying; no columns were removed."
            )

        cursor.execute(
            f"ALTER TABLE availability_patterns "
            f"CHANGE COLUMN `{old_name}` availability_id "
            f"BIGINT NOT NULL AUTO_INCREMENT"
        )
        cursor.execute(
            "SELECT COLUMN_KEY FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'availability_patterns' "
            "AND COLUMN_NAME = 'availability_id'"
        )
        migrated = cursor.fetchone()
        if not migrated or migrated["COLUMN_KEY"] not in ("PRI", "UNI"):
            ensure_unique_identifier("availability_id", {"COLUMN_KEY": None})
        logger.info(
            "Migrated availability_patterns.%s to availability_id; existing IDs preserved.",
            old_name,
        )
        return

    if auto_increment_columns:
        # Defensive branch; normally handled by candidate selection above.
        raise RuntimeError(
            "Could not identify a safe availability_patterns identifier."
        )

    # No numeric identifier exists. Add a new generated key without deleting
    # or rewriting any existing business columns.
    cursor.execute(
        """
        ALTER TABLE availability_patterns
        ADD COLUMN availability_id BIGINT NOT NULL AUTO_INCREMENT UNIQUE FIRST
        """
    )
    logger.info(
        "Added availability_patterns.availability_id to the existing legacy table."
    )


def initialize_database():
    """Execute the SQL statements from schema.sql."""

    sql_text = load_schema()
    statements = split_sql_statements(sql_text)

    if not statements:
        raise RuntimeError(
            "No SQL statements were found in schema.sql."
        )

    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            # Migrate the known legacy development schema before running the
            # current ERD-aligned CREATE TABLE and seed statements.
            cursor.execute(
                """SELECT COLUMN_NAME FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'hospitals'"""
            )
            hospital_columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
            if hospital_columns:
                if "hospital_name" not in hospital_columns:
                    if "name" in hospital_columns:
                        cursor.execute(
                            "ALTER TABLE hospitals CHANGE COLUMN name "
                            "hospital_name VARCHAR(180) NOT NULL"
                        )
                        logger.info("Migrated hospitals.name to hospitals.hospital_name")
                    else:
                        raise RuntimeError(
                            "Existing hospitals table does not match the approved schema: "
                            "expected hospital_name or legacy name. Recreate the development "
                            "database before initializing schema.sql."
                        )

                # CREATE TABLE IF NOT EXISTS does not add columns to an existing
                # table. Older MediSlot databases have a hospitals table without
                # the address field required by the current schema seed INSERTs.
                # Add the missing field and backfill existing rows without
                # deleting or rebuilding hospital records.
                if "address" not in hospital_columns:
                    cursor.execute(
                        "ALTER TABLE hospitals ADD COLUMN address VARCHAR(500) NULL"
                    )
                    cursor.execute(
                        "UPDATE hospitals SET address = '' WHERE address IS NULL"
                    )
                    cursor.execute(
                        "ALTER TABLE hospitals MODIFY COLUMN address VARCHAR(500) NOT NULL"
                    )
                    logger.info(
                        "Migrated hospitals.address; existing rows were preserved."
                    )

            # Migrate the legacy availability identifier before the schema
            # creates materialised_slots and its availability foreign key.
            migrate_availability_pattern_identifier(cursor)

            for number, statement in enumerate(statements, start=1):
                logger.info(
                    "Executing database schema statement %s of %s",
                    number,
                    len(statements),
                )

                cursor.execute(statement)

        connection.commit()

        logger.info(
            "Database schema initialization completed successfully."
        )

    except Exception:
        connection.rollback()
        logger.exception(
            "Database schema initialization failed."
        )
        raise

    finally:
        connection.close()


def lambda_handler(event, context):
    """AWS Lambda entry point for schema initialization."""

    initialize_database()

    return {
        "statusCode": 200,
        "body": "MediSlot database schema initialized successfully.",
    }