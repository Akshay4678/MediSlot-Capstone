"""Tiny SQLite stand-in for the MySQL cursor API used by the MediSlot SQL (unit tests only)."""

import re
import sqlite3
from datetime import date, datetime, time
from decimal import Decimal


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
        sql = sql.replace("%s", "?").replace("`", '"').replace("GREATEST(", "MAX(").replace(" FOR UPDATE", "")
        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE")
        # MySQL's LIKE escape character is a backslash by default; SQLite needs it spelled out.
        return re.sub(r"LIKE \?", "LIKE ? ESCAPE '\\\\'", sql)

    @staticmethod
    def _arg(value):
        if isinstance(value, Decimal):
            return float(value)  # PyMySQL sends Decimal natively; SQLite needs a float
        if isinstance(value, time):
            return value.strftime("%H:%M")
        if isinstance(value, datetime):
            return value.isoformat(sep=" ")
        if isinstance(value, date):
            return value.isoformat()
        return value

    def execute(self, sql, params=()):
        self._c.execute(self._sql(sql), [self._arg(v) for v in params])
        self.rowcount = self._c.rowcount

    def executemany(self, sql, rows):
        rows = [[self._arg(v) for v in row] for row in rows]
        self._c.executemany(self._sql(sql), rows)
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


class FileDb:
    """A SQLite FILE shared by many connections, for tests that run bookings on real threads.

    Every connection is its own transaction scope. A write transaction is opened lazily with
    BEGIN IMMEDIATE just before the first INSERT/UPDATE/DELETE, so the read-and-reserve phase of a
    booking (before it writes anything) really runs concurrently in every thread, as it does on MySQL.
    """

    def __init__(self, path, schema):
        self.path = str(path)
        boot = sqlite3.connect(self.path)
        boot.executescript(schema)
        boot.commit()
        boot.close()

    def connect(self):
        return _FileConn(self.path)


class _FileCursor(Cursor):
    def __init__(self, conn):
        self._conn = conn
        self._c = conn.db.cursor()
        self.rowcount = 0

    def execute(self, sql, params=()):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", sql, re.I) and not self._conn.in_tx:
            self._conn.db.execute("BEGIN IMMEDIATE")
            self._conn.in_tx = True
        super().execute(sql, params)


class _FileConn:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=60, isolation_level=None, check_same_thread=False)
        self.in_tx = False

    def cursor(self):
        return _FileCursor(self)

    def commit(self):
        if self.in_tx:
            self.db.execute("COMMIT")
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            self.db.execute("ROLLBACK")
            self.in_tx = False

    def close(self):
        self.rollback()
        self.db.close()

    def q(self, sql, *params):
        return self.db.execute(sql, params).fetchall()
