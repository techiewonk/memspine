"""Stable deployment ownership, persisted with authoritative SQL metadata."""

from uuid import uuid4

from sqlalchemy import Column, Integer, String, Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from memspine.exceptions import ConfigError
from memspine.providers.contracts import SqlAccess
from memspine.services.storage.sqlite.schema import metadata

DEPLOYMENT_METADATA = Table(
    "provider_deployment_identity",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("deployment_id", String(256), nullable=False),
)


async def resolve_deployment(
    sql: SqlAccess,
    configured_id: str | None,
    environment: str,
) -> str:
    """Create ownership once; reject an identity change against existing data."""
    if environment == "production" and not configured_id:
        raise ConfigError("production requires runtime.deployment_id")
    candidate = configured_id or str(uuid4())
    async with sql.engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "sqlite":
            await conn.execute(
                sqlite_insert(DEPLOYMENT_METADATA)
                .values(id=1, deployment_id=candidate)
                .on_conflict_do_nothing(index_elements=["id"])
            )
        elif dialect == "postgresql":
            await conn.execute(
                pg_insert(DEPLOYMENT_METADATA)
                .values(id=1, deployment_id=candidate)
                .on_conflict_do_nothing(index_elements=["id"])
            )
        else:
            raise ConfigError(
                "storage SQL dialect does not support deployment identity initialization"
            )
        existing: str = (
            await conn.execute(
                select(DEPLOYMENT_METADATA.c.deployment_id).where(DEPLOYMENT_METADATA.c.id == 1)
            )
        ).scalar_one()
        if configured_id is not None and configured_id != existing:
            raise ConfigError("runtime.deployment_id conflicts with the stored deployment identity")
    return str(existing)
