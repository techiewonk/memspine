"""SQL capabilities and owned sync leases, independent of database vendor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.providers.async_utils import run_blocking_drained
from memspine.providers.contracts import LifecycleManager, SyncSqlLease


class ManagedSqlAccess:
    """Expose SQL operations without leaking a concrete client into Engine.

    Native factories supply the transport-specific sync creator and checkpoint.
    Every sync engine is registered before consumers can use it.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        is_memory: bool,
        capabilities: frozenset[str],
        checkpoint: Callable[[], Awaitable[None]],
        sync_factory: Callable[[], Engine],
        owner_resource_id: str,
    ) -> None:
        self.engine = engine
        self.is_memory = is_memory
        self.capabilities = capabilities
        self._checkpoint = checkpoint
        self._sync_factory = sync_factory
        self._owner_resource_id = owner_resource_id
        self._leases: dict[str, SyncSqlLease] = {}

    async def checkpoint(self) -> None:
        await self._checkpoint()

    def sync_lease(self, resources: LifecycleManager, purpose: str) -> SyncSqlLease:
        """Construct a lazy SQLAlchemy engine; opening connections is the caller's work."""
        if not purpose or not purpose.strip():
            raise ValueError("sync SQL purpose must be nonempty")
        if purpose in self._leases:
            return self._leases[purpose]
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
