"""Alembic environment for the memspine Postgres storage profile (F-16d, M-04).

Run programmatically via ``memspine.services.storage.postgres.migrations``. The revisions are
explicit, dialect-neutral DDL (no ``metadata.create_all``), so the tree can be replayed against
any SQLAlchemy dialect and compared with the SQLite tree column for column.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    given = config.attributes.get("connection")
    if given is not None:
        context.configure(connection=given, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
