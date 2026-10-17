from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine

from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.sql import ManagedSqlAccess
from memspine.runtime.outbox import memory_engine, sqlite_engine


async def test_sync_outbox_lease_is_reused_and_closes_before_storage() -> None:
    order: list[str] = []
    async_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    resources = ProviderLifecycleManager(close_timeout_s=2.0)

    async def close_storage() -> None:
        order.append("storage")
        await async_engine.dispose()

    owner = resources.own("sql", close_storage)
    access = ManagedSqlAccess(
        async_engine,
        is_memory=True,
        capabilities=frozenset({"sql", "sync_sql"}),
        checkpoint=lambda: _noop(),
        sync_factory=memory_engine,
        owner_resource_id=owner.resource_id,
    )

    lease = access.sync_lease(resources, "outbox")
    assert access.sync_lease(resources, "outbox") is lease
    original_dispose = lease.engine.dispose

    def close_sync(*args: object, **kwargs: object) -> None:
        order.append("outbox")
        original_dispose(*args, **kwargs)

    lease.engine.dispose = close_sync  # type: ignore[method-assign]
    report = await resources.close_all()

    assert report.complete
    assert order == ["outbox", "storage"]


def test_sync_sqlite_engine_uses_configured_busy_timeout(tmp_path) -> None:
    engine = sqlite_engine(str(tmp_path / "busy.db"), busy_timeout_s=0.25)
    try:
        with engine.connect() as connection:
            value = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        assert value == 250
    finally:
        engine.dispose()


async def _noop() -> None:
    return None
