"""Complete first-cycle SQL schema used by the shared fresh Alembic baseline.

Provider management DDL is pinned here beside the substrate schema. There is no
historical upgrade/adoption chain. Current projector and provider-change state
remain part of the fresh database contract.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.engine import Connection

from memspine.services.assets.sql_registry import asset_metadata
from memspine.services.storage.intake_tables import intake_metadata
from memspine.services.storage.outbox_tables import outbox_metadata
from memspine.services.storage.provider_metadata import DEPLOYMENT_METADATA
from memspine.services.storage.provider_tables import provider_metadata
from memspine.store.side_tables import side_metadata


def schema_metadata() -> list[MetaData]:
    """All current table collections for creation and Alembic inspection."""
    assert provider_metadata is DEPLOYMENT_METADATA.metadata
    return [
        DEPLOYMENT_METADATA.metadata,
        outbox_metadata,
        side_metadata,
        asset_metadata,
        intake_metadata,
    ]


def create_schema(connection: Connection) -> None:
    """Create the complete schema in an empty database; existing tables are an error."""
    for schema in schema_metadata():
        schema.create_all(connection, checkfirst=False)


def drop_schema(connection: Connection) -> None:
    """Drop the complete baseline schema."""
    for schema in reversed(schema_metadata()):
        schema.drop_all(connection, checkfirst=False)
