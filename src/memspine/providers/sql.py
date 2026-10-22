"""SQL capabilities and owned sync leases, independent of database vendor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.providers.async_utils import run_blocking_drained
from memspine.providers.contracts import LifecycleManager, SyncSqlLease


class ManagedSqlAccess:
    """Expose SQL operations without leaking a concrete client into Engine.

    A durable database has ONE sync engine (``sync_engine``, registered by the native
    factory under ``sync_resource_id``); each purpose (outbox, pgvector, sleep leases) is
    a label resource that depends on it, so every purpose shares one pool (ADR-079).
    A ``:memory:`` database cannot be reached by a second connection, so it keeps a
    private sync database per purpose built by ``sync_factory``.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        is_memory: bool,
        capabilities: frozenset[str],
        checkpoint: Callable[[], Awaitable[None]],
        owner_resource_id: str,
        sync_engine: Engine | None = None,
        sync_resource_id: str | None = None,
        sync_factory: Callable[[], Engine] | None = None,
        connector: object | None = None,
    ) -> None:
        if (sync_engine is None) != (sync_resource_id is None):
            raise ValueError("sync_engine and sync_resource_id are configured together")
        if (sync_engine is None) == (sync_factory is None):
            raise ValueError("configure exactly one of sync_engine or sync_factory")
        self.engine = engine
        #: The opened SQL connector (``clients/{sqlite,postgres}``) the storage factory holds,
        #: lent to adapters that share the database (pgvector, RM-113 item 11); never closed
        #: here: the storage factory's registry lease owns it.
        self.connector = connector
        self.is_memory = is_memory
        self.capabilities = capabilities
        self._checkpoint = checkpoint
        self._sync_engine = sync_engine
        self._sync_resource_id = sync_resource_id
        self._sync_factory = sync_factory
        self._owner_resource_id = owner_resource_id
        self._leases: dict[str, SyncSqlLease] = {}

    async def checkpoint(self) -> None:
        await self._checkpoint()

    def sync_lease(self, resources: LifecycleManager, purpose: str) -> SyncSqlLease:
        """Return the sync engine for ``purpose``; opening connections is the caller's work."""
        if not purpose or not purpose.strip():
            raise ValueError("sync SQL purpose must be nonempty")
        if purpose in self._leases:
            return self._leases[purpose]
        if self._sync_engine is not None:
            assert self._sync_resource_id is not None
            shared = self._sync_engine

            async def release_label() -> None:
                # The shared engine closes with its own resource, after every label.
                return None

            resource = resources.own(
                f"{self._sync_resource_id}:{purpose}",
                release_label,
                depends_on=(self._sync_resource_id,),
            )
            lease = SyncSqlLease(shared, resource)
            self._leases[purpose] = lease
            return lease
        assert self._sync_factory is not None
        engine = self._sync_factory()

        async def close() -> None:
            await run_blocking_drained(engine.dispose)

        try:
            resource = resources.own(
                f"{self._owner_resource_id}:sync:{purpose}",
                close,
                depends_on=(self._owner_resource_id,),
            )
        except BaseException:
            engine.dispose()
            raise
        lease = SyncSqlLease(engine, resource)
        self._leases[purpose] = lease
        return lease

    async def acquire_sync(self, resources: LifecycleManager, purpose: str) -> SyncSqlLease:
        return self.sync_lease(resources, purpose)
