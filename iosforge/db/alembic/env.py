"""Alembic environment (T-2.2, SPEC §10).

The database URL is taken from application settings (``Settings.database_url``)
rather than hardcoded in ``alembic.ini``, and ``target_metadata`` is the shared
:class:`Base.metadata` so ``alembic revision --autogenerate`` sees every model.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool, text

from iosforge.common.config import get_settings
from iosforge.db import models as _models  # noqa: F401  (register all tables)
from iosforge.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime database URL from settings (env/.env/toml -> no secrets in ini).
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata

# Optional isolation schema (used by the test suite to apply migrations into a
# throwaway schema without touching the public one).
_SCHEMA = os.environ.get("IOSFORGE_DB_SCHEMA")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    if _SCHEMA:
        # Create the isolation schema in its own committed transaction so it
        # survives independently of the migration's transactional DDL.
        with connectable.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"'))

        # Pin search_path on every DBAPI connection from this engine, so ALL
        # unqualified DDL — create_table AND alter ops (add_column/create_foreign_key,
        # which schema_translate_map does not reliably qualify) — lands in the
        # isolation schema instead of public.
        @event.listens_for(connectable, "connect")
        def _set_search_path(dbapi_conn: object, _record: object) -> None:
            cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
            cur.execute(f'SET search_path TO "{_SCHEMA}"')
            cur.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table_schema=_SCHEMA or None,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
