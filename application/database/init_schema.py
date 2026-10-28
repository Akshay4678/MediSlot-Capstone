"""
Initialize the MediSlot MySQL schema.

This module is intended to run as a one-time deployment task.
It does not run automatically when connection.py is imported.

Required:
    application/database/schema.sql
"""

import logging
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