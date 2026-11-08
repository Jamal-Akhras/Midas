"""Engine/session factory. Single user, sqlite by default; set DATABASE_URL
for Postgres. create_all is the migration story for now (personal tool);
switch to Alembic if the schema starts churning."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from midas.config import settings
from midas.models import Base

_engine = None
_factory = None


def get_engine():
    global _engine
    if _engine is None:
        kwargs = {}
        if settings.database_url.startswith("sqlite"):
            # The UI server runs pipeline work in background threads; sqlite
            # connections must be allowed to cross thread boundaries.
            # timeout: wait for a competing writer instead of erroring.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(settings.database_url, **kwargs)
        if settings.database_url.startswith("sqlite"):
            # Two processes (monitor + UI) share this file. WAL lets readers
            # proceed alongside a writer; busy_timeout queues writers instead
            # of raising 'database is locked'.
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
    return _engine


def session_factory() -> Session:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _factory()


def init_db() -> None:
    Base.metadata.create_all(get_engine())
    # lightweight migration: columns added after first release
    if settings.database_url.startswith("sqlite"):

        with get_engine().connect() as conn:
            for ddl in (
                "ALTER TABLE orders ADD COLUMN alpaca_order_id VARCHAR(64) DEFAULT ''",
                "ALTER TABLE orders ADD COLUMN fees_usd FLOAT DEFAULT 0.0",
            ):
                try:
                    conn.exec_driver_sql(ddl)
                    conn.commit()
                except Exception:
                    pass  # already present
