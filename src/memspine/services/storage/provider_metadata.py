"""Provider metadata of a SQL records store (ADR-081, RM-112): deployment identity and
projector target bindings.

Both live in the authoritative database because they gate what a projector checkpoint may be
reused for. The records port exposes them as :meth:`RecordsStore.claim_deployment_id` and
:meth:`RecordsStore.projection_bindings` (``services/storage/records_port.py``); this module is
the SQL implementation (SQLite and Postgres). The MongoDB store implements the same contract in
:mod:`memspine.services.storage.mongodb`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, Integer, String, Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.exceptions import ConfigError
from memspine.observability.logging import get_logger
from memspine.services.storage.records_port import (
    ProjectionTargetBinding,
    binding_source_name,
    require_binding_match,
)
from memspine.services.storage.sqlite.schema import (
    metadata,
    projector_offsets,
    provider_projector_bindings,
)

__all__ = ["DEPLOYMENT_METADATA", "SqlProjectionBindingStore", "sql_claim_deployment_id"]

_log = get_logger(__name__)

DEPLOYMENT_METADATA = Table(
    "provider_deployment_identity",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("deployment_id", String(256), nullable=False),
)


def _insert_ignore(dialect: str, table: Table, index: list[str], **values: Any) -> Any:
    if dialect == "sqlite":
        return sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=index)
    if dialect == "postgresql":
        return pg_insert(table).values(**values).on_conflict_do_nothing(index_elements=index)
    raise ConfigError(f"storage SQL dialect {dialect!r} cannot persist provider metadata")


async def sql_claim_deployment_id(engine: AsyncEngine, candidate: str) -> str:
    """Store ``candidate`` as the deployment identity unless one exists; return the stored one."""
    async with engine.begin() as conn:
        await conn.execute(
            _insert_ignore(
                conn.dialect.name, DEPLOYMENT_METADATA, ["id"], id=1, deployment_id=candidate
            )
        )
        existing: str = (
            await conn.execute(
                select(DEPLOYMENT_METADATA.c.deployment_id).where(DEPLOYMENT_METADATA.c.id == 1)
            )
        ).scalar_one()
    return str(existing)


class SqlProjectionBindingStore:
    """Atomically bind a projector checkpoint to its provider target (one transaction each)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def adopt_equivalent_target(
        self, target: ProjectionTargetBinding, old_fingerprint: str
    ) -> bool:
        """ADR-084: CAS a proven-equivalent vector identity, preserving its checkpoint."""
        if target.projector_name != "vectors" or target.source_provider is not None:
            return False
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(provider_projector_bindings)
                .where(
                    provider_projector_bindings.c.deployment_id == target.deployment_id,
                    provider_projector_bindings.c.projector_name == target.projector_name,
                    provider_projector_bindings.c.provider == target.provider,
                    provider_projector_bindings.c.generation_id == target.generation_id,
                    provider_projector_bindings.c.target_fingerprint == old_fingerprint,
                )
                .values(
                    target_fingerprint=target.target_fingerprint,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
        adopted = bool(result.rowcount)
        if adopted:
            from memspine.observability import tap

            fields = dict(old=old_fingerprint, new=target.target_fingerprint)
            _log.info("provider.vector_identity_adopted", **fields)
            tap.event("provider.vector_identity_adopted", **fields)
        return adopted

    async def bound_provider(self, deployment_id: str, projector_name: str) -> str | None:
        """The provider NAME a projector is bound to (``graph:ladybug:v1`` -> ``ladybug``),
        or None when it has no binding yet. ADR-081 pins ``auto`` selections to it."""
        async with self._engine.connect() as conn:
            provider = await conn.scalar(
                select(provider_projector_bindings.c.provider).where(
                    provider_projector_bindings.c.deployment_id == deployment_id,
                    provider_projector_bindings.c.projector_name == projector_name,
                )
            )
        return None if provider is None else binding_source_name(str(provider))

    async def adopt_renamed_provider(
        self, target: ProjectionTargetBinding, old_provider: str
    ) -> bool:
        """ADR-081: rewrite a binding recorded under a renamed provider identity (for example
        ``graph:sqlite_adjacency:v1``) to ``target`` (compare-and-swap on the old identity and
        the active generation). Only for renames whose physical target is unchanged. Returns
        True when a row was adopted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(provider_projector_bindings)
                .where(
                    provider_projector_bindings.c.deployment_id == target.deployment_id,
                    provider_projector_bindings.c.projector_name == target.projector_name,
                    provider_projector_bindings.c.provider == old_provider,
                    provider_projector_bindings.c.generation_id == target.generation_id,
                )
                .values(
                    provider=target.provider,
                    target_fingerprint=target.target_fingerprint,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
        adopted = bool(result.rowcount)
        if adopted:
            _log.info(
                "provider.binding_renamed",
                projector=target.projector_name,
                old=old_provider,
                new=target.provider,
            )
        return adopted

    async def ensure(self, target: ProjectionTargetBinding) -> str:
        """Bind a target generation, allowing only a verified compare-and-swap.

        A non-zero legacy offset without a binding is ambiguous: the target may
        have been changed while keeping the same projector name. It must be
        rebuilt or moved through a verified switch before startup can continue.
        """
        bindings = provider_projector_bindings
        key = (
            bindings.c.deployment_id == target.deployment_id,
            bindings.c.projector_name == target.projector_name,
        )
        columns = (bindings.c.provider, bindings.c.target_fingerprint, bindings.c.generation_id)
        offset_of = select(projector_offsets.c.last_seq).where(
            projector_offsets.c.projector_name == target.projector_name
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(select(*columns).where(*key))).first()
            if row is not None:
                if (
                    row.provider == target.provider
                    and row.target_fingerprint == target.target_fingerprint
                    and row.generation_id == target.generation_id
                ):
                    return str(row.generation_id)
                if target.source_provider is None:
                    require_binding_match(target, row.provider, row.target_fingerprint)
                    raise ConfigError(
                        f"projector {target.projector_name!r} is bound to generation "
                        f"{row.generation_id!r}, but the selected generation is "
                        f"{target.generation_id!r}; a verified projection switch is required"
                    )
                source_matches = (
                    binding_source_name(row.provider) == target.source_provider
                    and row.target_fingerprint == target.source_target_fingerprint
                    and row.generation_id == target.source_generation_id
                )
                if not source_matches:
                    raise ConfigError(
                        f"projector {target.projector_name!r} no longer matches the verified "
                        "switch source; refusing to reuse or replace its offset"
                    )
                current_offset = int(await conn.scalar(offset_of) or 0)
                if current_offset != target.verified_checkpoint_seq:
                    raise ConfigError(
                        f"projector {target.projector_name!r} offset does not match the "
                        "verified target checkpoint; startup is blocked"
                    )
                result = await conn.execute(
                    update(bindings)
                    .where(
                        *key,
                        bindings.c.provider == row.provider,
                        bindings.c.target_fingerprint == row.target_fingerprint,
                        bindings.c.generation_id == row.generation_id,
                    )
                    .values(
                        provider=target.provider,
                        target_fingerprint=target.target_fingerprint,
                        generation_id=target.generation_id,
                        updated_at=datetime.now(UTC).isoformat(),
                    )
                )
                if result.rowcount != 1:
                    raise ConfigError("projector binding changed during verified activation")
                return target.generation_id

            offset = await conn.scalar(offset_of)
            if offset is not None and int(offset) > 0:
                raise ConfigError(
                    f"projector {target.projector_name!r} has a non-zero legacy offset "
                    "without a provider target binding; a verified projection rebuild or "
                    "switch is required before startup"
                )
            if target.source_provider is not None:
                raise ConfigError(
                    f"projector {target.projector_name!r} has no source binding for the verified "
                    "switch; refusing to create a target binding"
                )
            await conn.execute(
                _insert_ignore(
                    conn.dialect.name,
                    bindings,
                    ["deployment_id", "projector_name"],
                    deployment_id=target.deployment_id,
                    projector_name=target.projector_name,
                    provider=target.provider,
                    target_fingerprint=target.target_fingerprint,
                    generation_id=target.generation_id,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            saved = (await conn.execute(select(*columns).where(*key))).one()
            require_binding_match(target, saved.provider, saved.target_fingerprint)
            return str(saved.generation_id)
