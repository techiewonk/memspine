"""Shared Alembic environment for SQLite and PostgreSQL.

Runtime callers lend a configured connection; URL configuration supports CLI/tests.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from memspine.services.storage.baseline import schema_metadata

config = context.config
target_metadata = schema_metadata()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # #52: a caller that must open the database itself (SQLCipher: the connection
    # needs its key first) hands Alembic a live connection instead of a URL.
    given = config.attributes.get("connection")
    if given is not None:
        context.configure(connection=given, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
