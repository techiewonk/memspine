from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.clients.lancedb import LanceDBClient, LanceDBSpec
from memspine.exceptions import ConfigError
from memspine.models.embedding_profiles import EmbeddingProfile, check_or_pin
from memspine.services.embedding.base import EmbeddingService
from memspine.services.storage.provider_metadata import SqlProjectionBindingStore
from memspine.services.storage.records_port import ProjectionTargetBinding
from memspine.services.storage.sqlite.schema import metadata, projector_offsets
from memspine.services.vector.lancedb_store import LanceDBVectorStore, _table_name
from memspine.store.vector_spaces import InMemorySpaceState, VectorSpace, VersionedSpaceStore


def test_pgvector_name_matches_original_including_pinned_profile_hash() -> None:
    from memspine.providers.builtin.pgvector import _table_name as pg_table_name

    old_profile = EmbeddingProfile("config", "fastembed", "model", 384)
    old = SimpleNamespace(
        shared=SimpleNamespace(owner_id="owner"),
        generation_id="active",
        embedding=SimpleNamespace(embedder_id="fastembed:model", dim=384),
        profile_meta=old_profile.to_dict(),
    )
    new = SimpleNamespace(
        shared=old.shared,
        generation_id="active",
        embedding=SimpleNamespace(
            embedder_id="embedder:model", dim=384, legacy_embedder_id="fastembed:model"
        ),
        profile_meta=replace(old_profile, provider="onnx").to_dict(),
    )
    assert pg_table_name(cast(Any, old)) == pg_table_name(cast(Any, new))
    new.embedding.legacy_embedder_id = None
    assert pg_table_name(cast(Any, old)) != pg_table_name(cast(Any, new))


def test_catalogue_only_allows_proven_unmodified_models() -> None:
    from memspine.clients.weights.onnx_catalogue import legacy_embedder_id

    model = "BAAI/bge-small-en-v1.5"
    assert legacy_embedder_id(model) == f"fastembed:{model}"
    assert legacy_embedder_id(model, query_instruction="changed") is None
    assert legacy_embedder_id("unknown-model") is None


async def test_binding_adoption_checks_exact_source_and_preserves_offset(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        store = SqlProjectionBindingStore(engine)
        old = ProjectionTargetBinding("owner", "vectors", "vector:lancedb:v1", "old")
        await store.ensure(old)
        async with engine.begin() as connection:
            await connection.execute(
                projector_offsets.insert().values(
                    projector_name="vectors", last_seq=17, updated_at="2026-10-23"
                )
            )
        new = replace(old, target_fingerprint="new")
        assert not await store.adopt_equivalent_target(new, "different-source")
        with pytest.raises(ConfigError):
            await store.ensure(new)
        assert await store.adopt_equivalent_target(new, "old")
        assert not await store.adopt_equivalent_target(new, "old")
        assert await store.ensure(new) == "active"
        async with engine.connect() as connection:
            assert await connection.scalar(select(projector_offsets.c.last_seq)) == 17
    finally:
        await engine.dispose()


async def test_lance_adoption_keeps_table_and_vector_values(tmp_path: Path) -> None:
    old_id, new_id = "fastembed:certified-model", "embedder:certified-model"
    client = LanceDBClient(LanceDBSpec(tmp_path / "vectors.lance"))
    await client.open()
    try:
        old = cast(EmbeddingService, SimpleNamespace(embedder_id=old_id, dim=3))
        before = LanceDBVectorStore(client, old)
        await before.upsert("r1", "tenant", old_id, [1.0, 0.0, 0.0])
        expected = await before.query("tenant", [1.0, 0.0, 0.0], old_id)
        table = client.db.open_table(_table_name(old_id))
        version = table.version
        vectors = table.to_arrow().to_pylist()
        certified = cast(
            EmbeddingService, SimpleNamespace(embedder_id=new_id, dim=3, legacy_embedder_id=old_id)
        )
        after = LanceDBVectorStore(client, certified)
        assert await after.query("tenant", [1.0, 0.0, 0.0], new_id) == expected
        assert list(client.db.table_names()) == [_table_name(old_id)]
        assert client.db.open_table(_table_name(old_id)).version == version
        assert client.db.open_table(_table_name(old_id)).to_arrow().to_pylist() == vectors
    finally:
        await client.close()


def test_versioned_adoption_preserves_physical_id_and_active_state() -> None:
    state = InMemorySpaceState()
    old = VectorSpace("fastembed:model", 3)
    state.save({"spaces": {old.id: old.to_dict()}, "active": {"tenant": old.id}})
    new = replace(old, model_id="embedder:model")
    factory: Any = lambda _: None
    store = VersionedSpaceStore(factory, new, state, legacy_model_id=old.model_id)
    assert store.current.qualified_id == old.qualified_id
    assert store.active_space("tenant") == store.current
    assert store.current.model_id == "embedder:model"
    snapshot = state.load()
    reopened = VersionedSpaceStore(factory, new, state, legacy_model_id=old.model_id)
    assert reopened.current.qualified_id == old.qualified_id
    assert state.load() == snapshot


def test_changed_instruction_does_not_adopt_versioned_space() -> None:
    state = InMemorySpaceState()
    old = VectorSpace("fastembed:model", 3)
    state.save({"spaces": {old.id: old.to_dict()}, "active": {"tenant": old.id}})
    new = VectorSpace("embedder:model", 3, query_instruction="different")
    store = VersionedSpaceStore(lambda _: cast(Any, None), new, state, legacy_model_id=old.model_id)
    assert store.active_space("tenant") == old
    assert store.current == new


def test_pin_adoption_requires_exact_metadata_and_certification(tmp_path: Path) -> None:
    path = str(tmp_path / "profile.json")
    old = EmbeddingProfile("config", "fastembed", "model", 384)
    assert check_or_pin(path, old) == "pinned"
    new = replace(old, provider="onnx")
    with pytest.raises(ConfigError):
        check_or_pin(path, new)
    with pytest.raises(ConfigError):
        check_or_pin(path, replace(new, query_instruction="changed"), certified_legacy=True)
    assert check_or_pin(path, new, certified_legacy=True) == "adopted"
    assert check_or_pin(path, new, certified_legacy=True) == "ok"
