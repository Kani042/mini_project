import os
from pathlib import Path

basedir = Path(__file__).resolve().parent

class Config:
    # General
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Local default sqlite (localdb)
    DEFAULT_SQLITE = os.environ.get("LOCAL_DB") or f"sqlite:///{basedir / 'local.db'}"

    # Use DATABASE_URL when provided (Render sets this when you attach a Postgres DB)
    _db_url = os.environ.get("DATABASE_URL") or DEFAULT_SQLITE

    # Normalize common prefix
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)

    SQLALCHEMY_DATABASE_URI = _db_url

    # Engine options to keep connections healthy on hosted DBs
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
    }

class DevelopmentConfig(Config):
    DEBUG = True
    # optionally force sqlite in development:
    # SQLALCHEMY_DATABASE_URI = Config.DEFAULT_SQLITE

class ProductionConfig(Config):
    DEBUG = False
