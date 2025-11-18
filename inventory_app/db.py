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

_db_url = os.environ.get("DATABASE_URL")
if _db_url and _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

DRIVER = "pg" if _db_url else "sqlite"
if DRIVER == "sqlite":
    _sqlite_path = DEFAULT_SQLITE_PATH
else:
    # keep DSN for per-connection use
    _pg_dsn = _db_url

# initialize sqlite singleton connection only
_sqlite_conn = None
if DRIVER == "sqlite":
    _sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    _sqlite_conn = sqlite3.connect(str(_sqlite_path), check_same_thread=False)
    _sqlite_conn.row_factory = sqlite3.Row

# Lightweight cursor/result wrapper to match .fetchone()/.fetchall() usage
class CursorWrapper:
    def __init__(self, cur, driver):
        self._cur = cur
        self.driver = driver

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        return [dict(r) for r in rows]

# Connection-like object with execute() and commit()
class DBConnection:
    def __init__(self):
        self.driver = DRIVER
        if self.driver == "sqlite":
            self._conn = _sqlite_conn
        else:
            if psycopg2 is None:
                raise RuntimeError("psycopg2-binary is required for Postgres. Install it.")
            # create a fresh connection for this DBConnection instance
            # this prevents aborted transactions from leaking between requests
            self._conn = psycopg2.connect(_pg_dsn)

    def execute(self, sql, params=()):
        # Normalize placeholders depending on backend
        if self.driver == "sqlite":
            # sqlite expects '?' placeholders; convert '%s' -> '?'
            if "%s" in sql:
                sql = sql.replace("%s", "?")
        else:
            # postgres (psycopg2) expects '%s' placeholders; convert '?' -> '%s'
            if "?" in sql:
                sql = sql.replace("?", "%s")

        if self.driver == "sqlite":
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return CursorWrapper(cur, "sqlite")
        else:
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(sql, params)
                return CursorWrapper(cur, "pg")
            except Exception:
                # rollback & close to clear aborted transaction
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                try:
                    self._conn.close()
                except Exception:
                    pass
                raise

    def executemany(self, sql, seq):
        # Normalize placeholders depending on backend
        if self.driver == "sqlite":
            if "%s" in sql:
                sql = sql.replace("%s", "?")
        else:
            if "?" in sql:
                sql = sql.replace("?", "%s")

        if self.driver == "sqlite":
            cur = self._conn.cursor()
            cur.executemany(sql, seq)
            return CursorWrapper(cur, "sqlite")
        else:
            cur = self._conn.cursor()
            try:
                cur.executemany(sql, seq)
                return CursorWrapper(cur, "pg")
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                try:
                    self._conn.close()
                except Exception:
                    pass
                raise

    def commit(self):
        try:
            self._conn.commit()
        finally:
            # close Postgres connection after commit to avoid reusing an in-session connection
            if self.driver != "sqlite":
                try:
                    self._conn.close()
                except Exception:
                    pass

    def rollback(self):
        """Public rollback to clear current transaction and close connection (Postgres)."""
        try:
            self._conn.rollback()
        except Exception:
            pass
        if self.driver != "sqlite":
            try:
                self._conn.close()
            except Exception:
                pass

# Public API
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
        # create a temporary connection for schema application
        tmp_conn = psycopg2.connect(_pg_dsn)
        cur = tmp_conn.cursor()
        try:
            cur.execute(sql)
            tmp_conn.commit()
        except Exception:
            # fallback: split statements and run one by one (best-effort)
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass
            try:
                tmp_conn.commit()
            except Exception:
                pass
        finally:
            try:
                tmp_conn.close()
            except Exception:
                pass
