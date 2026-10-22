from __future__ import annotations

import pytest

from memspine.config.schema.providers import DiskCacheSettings
from memspine.providers.builtin.cache import DiskBuilder
from memspine.providers.contexts import CacheBuildContext, SharedBuildContext
from memspine.providers.contracts import ProjectionPlacement
from memspine.providers.lifecycle import ProviderLifecycleManager


@pytest.mark.asyncio
async def test_shared_cache_separates_deployments_and_reuses_stable_owner_on_reopen(
    monkeypatch, tmp_path
) -> None:
    values: dict[str, bytes] = {}
    closes: list[str] = []

    class Backend:
        async def get(self, key: str) -> bytes | None:
            return values.get(key)

        async def set(self, key: str, value: bytes, **kwargs: object) -> None:
            values[key] = value

        async def delete(self, key: str) -> None:
            values.pop(key, None)

    class Client:
        def __init__(self, spec: object) -> None:
            self.cache = Backend()

        async def open(self) -> None:
            pass

        async def close(self) -> None:
            closes.append("closed")

    monkeypatch.setattr("memspine.clients.cashews.CashewsClient", Client)
    owners: list[ProviderLifecycleManager] = []

    async def build(owner: str):
        resources = ProviderLifecycleManager(close_timeout_s=1.0)
        owners.append(resources)
        ctx = CacheBuildContext(
            SharedBuildContext(resources, owner, 1.0), ProjectionPlacement(tmp_path, False, owner)
        )
        return (
            await DiskBuilder().build(
                DiskCacheSettings(path=str(tmp_path / "shared"), namespace="app"), ctx
            )
        ).service

    first, second = await build("deployment-a"), await build("deployment-b")
    try:
        await first.set("same-key", b"private-a")
        assert await second.get("same-key") is None
        await second.set("same-key", b"private-b")
        assert await first.get("same-key") == b"private-a"
        await first.delete("same-key")
        assert await second.get("same-key") == b"private-b"
        await owners[0].close_all()
        reopened = await build("deployment-b")
        assert await reopened.get("same-key") == b"private-b"
        assert all(key.startswith("app:deployment:") for key in values)
        assert "deployment-a" not in repr(values)
    finally:
        for resources in owners:
            await resources.close_all()
    assert len(closes) == 3
