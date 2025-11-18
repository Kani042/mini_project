import os
import sqlite3
from pathlib import Path

# Try to import psycopg2 only when needed
try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None

BASEDIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_PATH = BASEDIR / "local.db"

# Determine driver from env
_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    # normalize prefix if needed
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    DRIVER = "pg"
else:
    DRIVER = "sqlite"
    _db_url = str(DEFAULT_SQLITE_PATH)

# Connections (singletons)
_sqlite_conn = None
_pg_conn = None

def _ensure_connections():
    global _sqlite_conn, _pg_conn
    if DRIVER == "sqlite":
        if _sqlite_conn is None:
            DEFAULT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _sqlite_conn = sqlite3.connect(str(DEFAULT_SQLITE_PATH), check_same_thread=False)
            _sqlite_conn.row_factory = sqlite3.Row
    else:
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary is required for PostgreSQL. Install it in requirements.")
        if _pg_conn is None:
            _pg_conn = psycopg2.connect(_db_url)

_ensure_connections()

# Lightweight cursor/result wrapper to match .fetchone()/.fetchall() usage
class CursorWrapper:
    def __init__(self, cur, driver):
        self._cur = cur
        self.driver = driver

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if self.driver == "sqlite":
            return dict(row)
        return dict(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        if self.driver == "sqlite":
            return [dict(r) for r in rows]
        return [dict(r) for r in rows]

# Connection-like object with execute() and commit()
class DBConnection:
    def __init__(self):
        self.driver = DRIVER
        self._conn = _sqlite_conn if DRIVER == "sqlite" else _pg_conn

    def execute(self, sql, params=()):
        # sqlite uses ? placeholders, psycopg2 uses %s
        if self.driver == "sqlite":
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return CursorWrapper(cur, "sqlite")
        else:
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if "?" in sql:
                sql = sql.replace("?", "%s")
            cur.execute(sql, params)
            return CursorWrapper(cur, "pg")

    def commit(self):
        self._conn.commit()

    def executemany(self, sql, seq):
        if self.driver == "sqlite":
            cur = self._conn.cursor()
            cur.executemany(sql, seq)
            return CursorWrapper(cur, "sqlite")
        else:
            cur = self._conn.cursor()
            if "?" in sql:
                sql = sql.replace("?", "%s")
            cur.executemany(sql, seq)
            return CursorWrapper(cur, "pg")

def get_db():
    return DBConnection()

def init_db():
    """
    Run schema.sql if present (inventory_app/schema.sql or project root schema.sql).
    Safe to call on startup; if tables exist, errors are ignored.
    """
    schema_candidates = [
        BASEDIR / "schema.sql",
        BASEDIR.parent / "schema.sql",
    ]
    schema_path = next((p for p in schema_candidates if p.exists()), None)
    if not schema_path:
        return

    sql = schema_path.read_text(encoding="utf-8")
    if DRIVER == "sqlite":
        # executescript supports multiple statements
        _sqlite_conn.executescript(sql)
    else:
        cur = _pg_conn.cursor()
        # psycopg2 can execute multi-statement if autocommit or split; do simple execute
        try:
            cur.execute(sql)
        except Exception:
            # Try splitting statements (simple fallback)
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass
        _pg_conn.commit()
