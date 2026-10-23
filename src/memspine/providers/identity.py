"""Stable deployment ownership, persisted with the authoritative records store (ADR-081).

The identity is stored by the records store itself (``RecordsStore.claim_deployment_id``:
a table on SQLite/Postgres, a metadata document on MongoDB, RM-112), so it travels with the
data and every process on one store agrees on it."""

from uuid import uuid4

from memspine.exceptions import ConfigError
from memspine.services.storage.records_port import ProviderMetadataPort


async def resolve_deployment(
    store: ProviderMetadataPort,
    configured_id: str | None,
    environment: str,
) -> str:
    """Create ownership once; reject an identity change against existing data."""
    if environment == "production" and not configured_id:
        raise ConfigError("production requires deployment.id")
    existing = await store.claim_deployment_id(configured_id or str(uuid4()))
    if configured_id is not None and configured_id != existing:
        raise ConfigError("deployment.id conflicts with the stored deployment identity")
    return existing
