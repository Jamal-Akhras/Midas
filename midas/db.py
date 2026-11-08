"""Engine/session factory and schema management. Single user, sqlite by
default; set DATABASE_URL for Postgres.

Schema changes are Alembic migrations (midas/migrations). `init_db` brings
any database to the current revision:

- empty database: create_all, then stamp head (same result, no replay)
- pre-Alembic database (ledgers from July 2026): add the two columns that
  were once added by hand, create tables added since, stamp head
- versioned database: upgrade head
"""

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from midas.config import settings
from midas.models import Base

if TYPE_CHECKING:
    from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        kwargs: dict = {}
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


def alembic_config() -> "Config":
    """Programmatic Alembic config pointing at the packaged migrations and
    the configured database (the CLI's alembic.ini resolves the same way)."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    return cfg


# Columns added to `orders` by hand in July 2026, before migrations existed.
# A database without alembic_version may still lack them.
_LEGACY_ORDER_COLUMNS = {
    "alpaca_order_id": "ALTER TABLE orders ADD COLUMN alpaca_order_id VARCHAR(64) DEFAULT ''",
    "fees_usd": "ALTER TABLE orders ADD COLUMN fees_usd FLOAT DEFAULT 0.0",
}


def _adopt_legacy_schema(conn: Connection) -> None:
    present = {c["name"] for c in inspect(conn).get_columns("orders")}
    for name, ddl in _LEGACY_ORDER_COLUMNS.items():
        if name not in present:
            conn.exec_driver_sql(ddl)
    Base.metadata.create_all(conn)  # tables added after the ledger was created


def init_db() -> None:
    """Bring the configured database to the current schema revision."""
    from alembic import command

    engine = get_engine()
    tables = set(inspect(engine).get_table_names())
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn  # env.py reuses this connection
        if not tables:
            Base.metadata.create_all(conn)
            command.stamp(cfg, "head")
        elif "alembic_version" not in tables:
            _adopt_legacy_schema(conn)
            command.stamp(cfg, "head")
        else:
            command.upgrade(cfg, "head")
