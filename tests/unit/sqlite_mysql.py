"""Tiny SQLite stand-in for the MySQL cursor API used by the MediSlot SQL (unit tests only)."""

import re
import sqlite3
from datetime import date, datetime


class Cursor:
    def __init__(self, db):
        self._c = db.cursor()
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @staticmethod
    def _sql(sql):
        sql = sql.replace("%s", "?").replace("`", '"').replace("GREATEST(", "MAX(")
        # MySQL's LIKE escape character is a backslash by default; SQLite needs it spelled out.
        return re.sub(r"LIKE \?", "LIKE ? ESCAPE '\\\\'", sql)

    @staticmethod
    def _arg(value):
        if isinstance(value, datetime):
            return value.isoformat(sep=" ")
        if isinstance(value, date):
            return value.isoformat()
        return value

    def execute(self, sql, params=()):
        self._c.execute(self._sql(sql), [self._arg(v) for v in params])
        self.rowcount = self._c.rowcount

    def fetchone(self):
        row = self._c.fetchone()
        return dict(zip([d[0] for d in self._c.description], row)) if row else None

    def fetchall(self):
        cols = [d[0] for d in self._c.description]
        return [dict(zip(cols, r)) for r in self._c.fetchall()]


class Conn:
    def __init__(self, schema):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(schema)

    def cursor(self):
        return Cursor(self.db)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass

    def q(self, sql, *params):
        return self.db.execute(sql, params).fetchall()
