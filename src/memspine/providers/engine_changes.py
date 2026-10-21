"""Explicit construction helpers for Engine-owned change coordination.

The Engine integration stays generic: callers supply resolved snapshots,
service revisions, strategies and a finite plan TTL. This module never creates
production tables; those are installed by the storage migrations.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.providers.changes import (
    ChangeCategory,
    ChangeCoordinator,
    ChangeStrategy,
    RevisionGuard,
    SqlChangeStore,
)
from memspine.providers.contracts import SqlAccess
from memspine.providers.switching import SqlProjectionSwitchStore


class ServiceRevisionSource(Protocol):
    async def current_revision(self, logical_service_id: str) -> str: ...


class ResolvedConfigSource(Protocol):
    async def get_resolved_config(self, config_id: str) -> BaseModel | Mapping[str, object]: ...


def resolved_config_fingerprint(config: BaseModel | Mapping[str, object]) -> str:
    """Hash a caller-selected resolved provider config into a stable identity.

    Pass an immutable selected-provider snapshot or versioned secret references.
    Credentials contribute to the hash without being persisted or returned, so
    changing credentials invalidates a previously prepared plan.
    """

    def canonical(value: object) -> object:
        # Masked display serialization would make different credentials hash alike.
        # Raw values are consumed only in memory for the digest, never persisted.
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        if isinstance(value, BaseModel):
            return {key: canonical(getattr(value, key)) for key in type(value).model_fields}
        if isinstance(value, Mapping):
            return {str(key): canonical(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [canonical(item) for item in value]
        return value

    encoded = json.dumps(canonical(config), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ResolvedConfigRevisionGuard(RevisionGuard):
    """Re-read source revisions and target snapshots immediately before execution."""

    def __init__(
        self,
        *,
        services: ServiceRevisionSource,
        configs: ResolvedConfigSource,
    ) -> None:
        self._services = services
        self._configs = configs

    async def current_revision(self, logical_service_id: str) -> str:
        return await self._services.current_revision(logical_service_id)

    async def current_target_fingerprint(self, config_id: str) -> str:
        snapshot = await self._configs.get_resolved_config(config_id)
        return resolved_config_fingerprint(snapshot)


def build_engine_change_support(
    *,
    sql: SqlAccess,
    revision_guard: RevisionGuard,
    strategies: Mapping[tuple[ChangeCategory, str], ChangeStrategy],
    plan_ttl_s: float,
) -> tuple[ChangeCoordinator, SqlProjectionSwitchStore]:
    """Construct the durable change API over an Engine's shared SQL access.

    ``plan_ttl_s`` is required and validated by :class:`ChangeCoordinator`;
    callers install the production tables through Alembic before use. Tests and
    isolated local tools may call ``create_schema`` explicitly on the returned
    stores.
    """
    coordinator = ChangeCoordinator(
        store=SqlChangeStore(sql.engine),
        revision_guard=revision_guard,
        strategies=strategies,
        plan_ttl_s=plan_ttl_s,
    )
    return coordinator, SqlProjectionSwitchStore(sql.engine)


def build_test_change_support(
    *,
    engine: AsyncEngine,
    revision_guard: RevisionGuard,
    strategies: Mapping[tuple[ChangeCategory, str], ChangeStrategy],
    plan_ttl_s: float,
) -> tuple[ChangeCoordinator, SqlProjectionSwitchStore]:
    """Explicit local/test helper when no :class:`SqlAccess` wrapper exists."""
    coordinator = ChangeCoordinator(
        store=SqlChangeStore(engine),
        revision_guard=revision_guard,
        strategies=strategies,
        plan_ttl_s=plan_ttl_s,
    )
    return coordinator, SqlProjectionSwitchStore(engine)


__all__ = [
    "ResolvedConfigRevisionGuard",
    "ResolvedConfigSource",
    "ServiceRevisionSource",
    "build_engine_change_support",
    "build_test_change_support",
    "resolved_config_fingerprint",
]
