"""Real PostgreSQL pgvector operations, enabled only by the local contract runner."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.providers.health import ProbeStatus
from memspine.providers.lifecycle import LifecycleState


def _require_postgres_url() -> str:
    value = os.environ.get("MEMSPINE_TEST_LIVE_POSTGRES_URL")
    if not value:
        pytest.skip("local pgvector service not configured")
    return value


@pytest.mark.asyncio
@pytest.mark.provider_live
async def test_pgvector_native_projection_contract(tmp_path: Path) -> None:
    """Persist, filter, rank, erase, probe and reopen a real pgvector target."""
    url = _require_postgres_url()
    deployment_id = f"pgvector-contract-{uuid.uuid4().hex}"
    table_names: list[str] = []

    def build_engine(*, provision_projection: bool = True) -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            deployment={"data_dir": str(tmp_path / "derived"), "id": deployment_id},
            storage={
                "records": {
                    "provider": "postgres",
                    "settings": {
                        "url": url,
                        "connect_timeout_s": 10.0,
                        "statement_timeout_s": 10.0,
                    },
                },
                "vector": {
                    "provider": "pgvector",
                    "settings": {
                        "provision_projection": provision_projection,
                        "create_extension": provision_projection,
                    },
                },
                "cache": {"provider": "memory", "settings": {}},
            },
            embedding={"provider": "hash", "settings": {"dim": 64}},
            memories={"semantic": {"enabled": True}},
        )

    engine = build_engine()
    await engine.start()
    try:
        report = await engine.provider_health()
        vector_health = [
            item
            for item in report.providers
            if item.category == "vector" and item.provider == "pgvector"
        ]
        assert vector_health and all(item.status is ProbeStatus.HEALTHY for item in vector_health)
        assert engine._vector is not None
        assert engine._embedder is not None
        store = engine._vector
        embedder_id = engine._embedder.embedder_id
        table_names.append(store._table.name)
        vector_a = [1.0, 0.0] + [0.0] * 62
        vector_b = [0.0, 1.0] + [0.0] * 62
        await store.upsert("pg-a", "tenant-a", embedder_id, vector_a)
        await store.upsert("pg-b", "tenant-b", embedder_id, vector_a)
        await store.upsert("pg-c", "tenant-a", embedder_id, vector_b)

        hits = await store.query("tenant-a", vector_a, embedder_id, top_k=5)
        assert [hit.record_id for hit in hits] == ["pg-a", "pg-c"]
        assert hits[0].score == pytest.approx(1.0, abs=1e-5)
        assert hits[1].score == pytest.approx(0.0, abs=1e-5)
        tenant_b_hits = await store.query("tenant-b", vector_a, embedder_id)
        assert len(tenant_b_hits) == 1
        assert tenant_b_hits[0].record_id == "pg-b"
        assert tenant_b_hits[0].score == pytest.approx(1.0, abs=1e-5)
        assert await store.exists("pg-b")
        with pytest.raises(ConfigError, match="dimension mismatch"):
            await store.query("tenant-a", [1.0], embedder_id)

        await store.delete("pg-a")
        assert not await store.exists("pg-a")
        assert await store.history_absent("pg-a") is None
        await store.drop_namespace("tenant-b")
        assert not await store.exists("pg-b")
        assert await store.purge_deleted() is False
    finally:
        await engine.stop()
    assert engine._provider_resources is not None
    assert all(
        state is LifecycleState.STOPPED for state in engine._provider_resources.states.values()
    )

    reopened = build_engine(provision_projection=False)
    await reopened.start()
    try:
        assert reopened._vector is not None
        assert reopened._embedder is not None
        assert reopened._vector._table.name == table_names[0]
        persisted = await reopened._vector.query(
            "tenant-a",
            [0.0, 1.0] + [0.0] * 62,
            reopened._embedder.embedder_id,
        )
        assert [hit.record_id for hit in persisted] == ["pg-c"]
        assert await reopened._vector.delete_all() is None
    finally:
        await reopened.stop()
    assert reopened._provider_resources is not None
    assert all(
        state is LifecycleState.STOPPED for state in reopened._provider_resources.states.values()
    )
