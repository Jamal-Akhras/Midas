"""Schema management: init_db brings any database to the current revision,
and the migration chain reproduces the models exactly (no drift)."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from midas import db as midas_db
from midas.config import settings
from midas.models import Base


@pytest.fixture()
def file_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(midas_db, "_engine", None)
    monkeypatch.setattr(midas_db, "_factory", None)
    yield url
    if midas_db._engine is not None:
        midas_db._engine.dispose()


def _head() -> str | None:
    return ScriptDirectory.from_config(midas_db.alembic_config()).get_current_head()


def _version(engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(text("select version_num from alembic_version")).scalar()


def test_fresh_database_is_created_and_stamped(file_db):
    midas_db.init_db()
    engine = midas_db.get_engine()
    tables = set(inspect(engine).get_table_names())
    assert {"pipeline_runs", "orders", "cost_events", "alembic_version"} <= tables
    assert _version(engine) == _head()


def test_init_db_is_idempotent(file_db):
    midas_db.init_db()
    midas_db.init_db()  # versioned database: upgrade head is a no-op
    assert _version(midas_db.get_engine()) == _head()


def test_pre_alembic_ledger_is_adopted(file_db):
    """A July-2026 ledger: every table exists, no alembic_version, and the
    orders table predates the two hand-added columns."""
    engine = create_engine(file_db)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE orders DROP COLUMN fees_usd"))
        conn.execute(text("ALTER TABLE orders DROP COLUMN alpaca_order_id"))
    engine.dispose()

    midas_db.init_db()
    engine = midas_db.get_engine()
    cols = {c["name"] for c in inspect(engine).get_columns("orders")}
    assert {"fees_usd", "alpaca_order_id"} <= cols
    assert _version(engine) == _head()


@pytest.mark.filterwarnings("ignore:Cannot correctly sort tables:sqlalchemy.exc.SAWarning")
def test_migration_chain_matches_models(file_db):
    """Upgrading an empty database through every migration must produce the
    schema the models describe: autogenerate would have nothing to add."""
    engine = create_engine(file_db)
    with engine.begin() as conn:
        cfg = midas_db.alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "render_as_batch": True})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == [], diff
