import os
from pathlib import Path
from dotenv import load_dotenv

# Try to import psycopg2 and extras (required)
try:
    import psycopg2
    import psycopg2.extras
except Exception:
    raise RuntimeError("psycopg2-binary is required. Install it in your virtualenv (pip install psycopg2-binary).")

load_dotenv()  # load .env from project root so DATABASE_URL/PG* vars are available at import time

BASEDIR = Path(__file__).resolve().parent

# Read DATABASE_URL or assemble DSN from PG* env vars (host/db/user/password/port)
_pg_dsn = os.environ.get("DATABASE_URL", "").strip()

def _get_dsn():
    """Build and return PostgreSQL DSN from env or raise if not configured.
    Called lazily when a DB connection is created so import-time doesn't fail."""
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn:
        if dsn.startswith("postgres://"):
            dsn = dsn.replace("postgres://", "postgresql://", 1)
        return dsn
    # try individual vars
    pg_host = os.environ.get("PGHOST") or os.environ.get("DB_HOST")
    pg_db = os.environ.get("PGDATABASE") or os.environ.get("DB_NAME")
    pg_user = os.environ.get("PGUSER") or os.environ.get("DB_USER")
    pg_pass = os.environ.get("PGPASSWORD") or os.environ.get("DB_PASSWORD")
    pg_port = os.environ.get("PGPORT") or os.environ.get("DB_PORT") or "5432"
    if pg_host and pg_db and pg_user and pg_pass:
        return f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
    raise RuntimeError(
        "Postgres connection not configured. Set DATABASE_URL or PGHOST/PGDATABASE/PGUSER/PGPASSWORD (and optional PGPORT)."
    )

# Compatibility wrapper: dict-like but also allow attribute access (row.id)
class RowProxy(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)
    def __setattr__(self, name, value):
        self[name] = value
    def get(self, key, default=None):
        return super().get(key, default)

# Cursor/result wrapper returning RowProxy rows
class CursorWrapper:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        try:
            data = dict(row)
        except Exception:
            data = {desc[0]: row[idx] for idx, desc in enumerate(self._cur.description)}
        return RowProxy(data)

    def fetchall(self):
        rows = self._cur.fetchall()
        result = []
        for r in rows:
            try:
                data = dict(r)
            except Exception:
                data = {desc[0]: r[idx] for idx, desc in enumerate(self._cur.description)}
            result.append(RowProxy(data))
        return result

# Postgres-only connection wrapper
class DBConnection:
    def __init__(self):
        self.driver = "pg"
        try:
            dsn = _pg_dsn
            # if _pg_dsn empty, try to rebuild from env just in case
            if not dsn:
                from dotenv import load_dotenv as _ld; _ld()
                dsn = os.environ.get("DATABASE_URL", "").strip()
            if dsn.startswith("postgres://"):
                dsn = dsn.replace("postgres://", "postgresql://", 1)
            self._conn = psycopg2.connect(dsn)
        except Exception as conn_err:
            # provide a helpful message and re-raise
            raise RuntimeError(
                "Unable to connect to PostgreSQL. Check DATABASE_URL or PGHOST/PGDATABASE/PGUSER/PGPASSWORD. "
                f"Original error: {conn_err}"
            ) from conn_err

    def _normalize_sql(self, sql: str) -> str:
        # if code accidentally used sqlite-style '?' placeholders, convert them
        if "?" in sql:
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql, params=()):
        sql = self._normalize_sql(sql)
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(sql, params or ())
            return CursorWrapper(cur)
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

    def executemany(self, sql, seq):
        sql = self._normalize_sql(sql)
        cur = self._conn.cursor()
        try:
            cur.executemany(sql, seq)
            return CursorWrapper(cur)
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
            try:
                self._conn.close()
            except Exception:
                pass

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass
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

        # --- NEW: ensure invoice_number exists and is populated consistently ---
        try:
            # Postgres path
            if DRIVER == "pg":
                try:
                    # Add column if missing
                    cur.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS invoice_number TEXT;")
                except Exception:
                    # If ALTER ... IF NOT EXISTS not supported, ignore
                    pass
                # Populate missing invoice_number values using the reserved id format
                try:
                    cur.execute("""
                        UPDATE invoices
                        SET invoice_number = CONCAT('INV-', lpad(id::text, 6, '0'))
                        WHERE invoice_number IS NULL OR invoice_number = '';
                    """)
                except Exception:
                    pass
                # Enforce NOT NULL and unique index (safe if column now populated)
                try:
                    cur.execute("ALTER TABLE invoices ALTER COLUMN invoice_number SET NOT NULL;")
                except Exception:
                    # may fail if some rows lack values; ignore to avoid aborting init
                    pass
                try:
                    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_invoice_number ON invoices (invoice_number);")
                except Exception:
                    pass
                try:
                    tmp_conn.commit()
                except Exception:
                    pass

            else:
                # SQLite path
                # Check existing columns
                cur.execute("PRAGMA table_info(invoices);")
                cols = [r[1] for r in cur.fetchall()]
                if "invoice_number" not in cols:
                    try:
                        cur.execute("ALTER TABLE invoices ADD COLUMN invoice_number TEXT;")
                    except Exception:
                        pass
                # Populate invoice_number for existing rows
                try:
                    cur.execute("""
                        UPDATE invoices
                        SET invoice_number = 'INV-' || printf('%06d', id)
                        WHERE invoice_number IS NULL OR invoice_number = '';
                    """)
                except Exception:
                    pass
                try:
                    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_invoice_number ON invoices (invoice_number);")
                except Exception:
                    pass
                try:
                    tmp_conn.commit()
                except Exception:
                    pass
        except Exception:
            # any migration error should not break startup; print for diagnostics
            try:
                tmp_conn.rollback()
            except Exception:
                pass
        finally:
            try:
                tmp_conn.close()
            except Exception:
                pass

def test_connection():
    """Quick helper to test DB connectivity; returns True on success or raises on failure."""
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        pg_host = os.environ.get("PGHOST") or os.environ.get("DB_HOST")
        pg_db = os.environ.get("PGDATABASE") or os.environ.get("DB_NAME")
        pg_user = os.environ.get("PGUSER") or os.environ.get("DB_USER")
        pg_pass = os.environ.get("PGPASSWORD") or os.environ.get("DB_PASSWORD")
        pg_port = os.environ.get("PGPORT") or os.environ.get("DB_PORT") or "5432"
        if pg_host and pg_db and pg_user and pg_pass:
            dsn = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    try:
        conn = psycopg2.connect(dsn)
        conn.close()
        return True
    except Exception as e:
        raise RuntimeError(f"Postgres connectivity test failed: {e}") from e
