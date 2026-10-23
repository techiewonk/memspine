from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from memspine import Engine
from memspine.config.loader import load_config
from memspine.config.schema.providers import (
    FastembedEmbeddingSettings,
    HashEmbeddingSettings,
    LiteLLMEmbeddingSettings,
    SentenceTransformersEmbeddingSettings,
    StaticEmbeddingSettings,
)
from memspine.providers.builtin.generations import (
    BuiltinProjectionGenerationOpener,
    OpenedProjection,
)
from memspine.providers.changes import (
    ChangeCoordinator,
    ChangeStatus,
    SourceCompleteness,
    SqlChangeStore,
)
from memspine.providers.contracts import ProviderRef
from memspine.providers.placement import generation_component
from memspine.providers.projection_backend import EventLogProjectionBackend
from memspine.providers.projection_bindings import (
    projection_target_bindings,
    projection_target_fingerprint,
)
from memspine.providers.registry import (
    ProviderRegistryError,
    get_registry,
    provider_target_identity,
    validate_settings,
)
from memspine.providers.settings import Neo4jSettings
from memspine.providers.switching import (
    CleanupEvidence,
    GenerationStatus,
    ProjectionGeneration,
    ProjectionSwitcher,
    ProjectionSwitchStrategy,
    ProjectionVerificationEvidence,
    SqlProjectionSwitchStore,
)
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, SourceInfo


class _ConfigSource:
    def __init__(self, config) -> None:
        self._config = config

    async def resolve(self, generation: ProjectionGeneration) -> OpenedProjection:
        del generation
        return OpenedProjection(self._config)


class _Policy:
    def __init__(self, expected_record_id: str | None = None) -> None:
        self._expected_record_id = expected_record_id

    async def verify(
        self,
        *,
        category,
        source,
        target,
        service,
        source_head_seq,
        erase_checkpoint_seq,
    ) -> ProjectionVerificationEvidence:
        if self._expected_record_id is not None:
            assert category == "lexical"
            hits = await service.search("switch-test", "camping", top_k=5)
            assert any(hit.record_id == self._expected_record_id for hit in hits), (
                [hit.record_id for hit in hits],
                self._expected_record_id,
            )
        return ProjectionVerificationEvidence(
            report_id="tantivy-local-verification",
            source_head_seq=source_head_seq,
            target_checkpoint_seq=target.checkpoint_seq,
            erase_checkpoint_seq=erase_checkpoint_seq,
            owner_id=target.owner_id,
            target_fingerprint=target.target_fingerprint,
            successful_probes=("lexical_record_exists",),
        )

    async def activate(self, **_kwargs) -> None:
        return None

    async def freeze(self, **_kwargs) -> None:
        return None

    async def has_live_references(self, **_kwargs) -> bool:
        return False

    async def delete_owned(self, **_kwargs) -> CleanupEvidence:
        return CleanupEvidence(
            report_id="cleanup",
            owner_id=_kwargs["generation"].owner_id,
            generation_id=_kwargs["generation"].generation_id,
            target_fingerprint=_kwargs["generation"].target_fingerprint,
            deleted=True,
            remaining_references=0,
        )


class _RevisionGuard:
    async def current_revision(self, logical_service_id: str) -> str:
        assert logical_service_id == "lexical"
        return "tantivy-active-r1"

    async def current_target_fingerprint(self, config_id: str) -> str:
        assert config_id == "tantivy-target-v1"
        return self.fingerprint

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint


def _record() -> MemoryRecord:
    return MemoryRecord(
        record_id="generation-record",
        namespace="generation-test",
        memory_type="semantic",
        content="Tantivy generation persists camping records",
        source=SourceInfo(role="user"),
    )


def test_neo4j_target_identity_tracks_resolved_endpoint_without_exposing_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMSPINE_NEO4J_URL", "neo4j+s://first.example:7687")
    monkeypatch.setenv("MEMSPINE_NEO4J_USER", "service-user")
    monkeypatch.setenv("MEMSPINE_NEO4J_PASSWORD", "private-password")
    settings = Neo4jSettings(connect_timeout_s=3.0, request_timeout_s=8.0)
    descriptor = get_registry("graph_stores").get("neo4j")

    first = provider_target_identity(descriptor, settings)
    assert first is not None
    assert first["host"] == "first.example"
    assert first["database"] == "neo4j"
    assert "private-password" not in repr(first)
    config = load_config(
        template="core",
        overrides={
            "storage": {
                "graph": {"provider": "neo4j", "settings": settings.model_dump(mode="python")}
            }
        },
    ).config
    fingerprint1 = projection_target_fingerprint(
        config,
        category="graph",
        provider=descriptor.ref,
        settings=settings,
        deployment_id="deployment-a",
    )

    monkeypatch.setenv("MEMSPINE_NEO4J_URL", "neo4j+s://second.example:7687")
    second = provider_target_identity(descriptor, settings)
    assert second is not None
    assert second["host"] == "second.example"
    assert second["url_digest"] != first["url_digest"]
    fingerprint2 = projection_target_fingerprint(
        config,
        category="graph",
        provider=descriptor.ref,
        settings=settings,
        deployment_id="deployment-a",
    )
    assert fingerprint2 != fingerprint1


def test_embedding_target_identity_requires_exact_dimension_without_loading_models() -> None:
    fastembed = get_registry("embedders").get("fastembed")
    known = provider_target_identity(fastembed, FastembedEmbeddingSettings())
    assert known == {
        "embedder_id": "fastembed:BAAI/bge-small-en-v1.5",
        "dimension": 384,
    }

    unknown = FastembedEmbeddingSettings(model="organization/private-model", dim=512)
    identity = provider_target_identity(fastembed, unknown)
    assert identity == {
        "embedder_id": "fastembed:organization/private-model",
        "dimension": 512,
    }
    with pytest.raises(ValidationError, match="settings.dim is required"):
        FastembedEmbeddingSettings(model="organization/private-model")

    static = get_registry("embedders").get("static")
    settings = validate_settings(static, {"model": "org/static", "dim": 768})
    assert provider_target_identity(static, settings) == {
        "embedder_id": "static:org/static",
        "dimension": 768,
    }
    with pytest.raises(ProviderRegistryError, match="invalid settings"):
        validate_settings(static, {"model": "org/static"})


def test_builtin_embedding_defaults_are_model_specific() -> None:
    assert FastembedEmbeddingSettings().dim == 384
    assert FastembedEmbeddingSettings(model="BAAI/bge-base-en-v1.5").dim == 768
    assert FastembedEmbeddingSettings(model="organization/private-model", dim=512).dim == 512
    with pytest.raises(ValidationError, match="settings.dim is required"):
        FastembedEmbeddingSettings(model="organization/private-model")

    assert HashEmbeddingSettings().dim == 64
    assert HashEmbeddingSettings(dim=768).dim == 768
    from memspine.config.schema import EmbeddingConfig
    from memspine.models.embedding_profiles import EmbeddingProfile

    profiled_hash = EmbeddingProfile(
        name="hash-768", provider="hash", model_id="ignored", dim=768
    ).apply(EmbeddingConfig())
    assert profiled_hash.settings["dim"] == 768

    assert StaticEmbeddingSettings().dim == 256
    with pytest.raises(ValidationError, match="settings.dim is required"):
        StaticEmbeddingSettings(model="organization/static-model")
    with pytest.raises(ValidationError, match="conflicts with the known static"):
        StaticEmbeddingSettings(dim=384)
    assert StaticEmbeddingSettings(model="organization/static-model", dim=512).dim == 512

    assert SentenceTransformersEmbeddingSettings().dim == 384
    assert SentenceTransformersEmbeddingSettings(model="Qwen/Qwen3-Embedding-0.6B").dim == 1024
    assert (
        SentenceTransformersEmbeddingSettings(model="Qwen/Qwen3-Embedding-0.6B", dim=256).dim == 256
    )
    with pytest.raises(ValidationError, match="settings.dim is required"):
        SentenceTransformersEmbeddingSettings(model="organization/model")
    assert SentenceTransformersEmbeddingSettings(model="organization/model", dim=768).dim == 768

    litellm_defaults = LiteLLMEmbeddingSettings(request_timeout_s=10.0)
    assert (litellm_defaults.model, litellm_defaults.dim) == (
        "openai/text-embedding-3-small",
        1536,
    )
    assert (
        LiteLLMEmbeddingSettings(model="openai/text-embedding-3-large", request_timeout_s=10.0).dim
        == 3072
    )
    with pytest.raises(ValidationError, match="settings.dim is required"):
        LiteLLMEmbeddingSettings(model="custom/provider-model", request_timeout_s=10.0)
    assert (
        LiteLLMEmbeddingSettings(
            model="custom/provider-model", dim=1024, request_timeout_s=10.0
        ).dim
        == 1024
    )


async def test_mock_embedding_dimensions_must_match_the_declared_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.services.embedding.fastembed import FastembedEmbedding

    class FakeFastembedModel:
        def embed(self, _texts: list[str]) -> list[list[float]]:
            return [[1.0, 2.0, 3.0]]

    fast = FastembedEmbedding(model="organization/private-model", dim=3)
    fast._model = FakeFastembedModel()
    assert await fast.embed(["no download"]) == [[1.0, 2.0, 3.0]]

    wrong_fast = FastembedEmbedding(model="organization/private-model", dim=4)
    wrong_fast._model = FakeFastembedModel()
    with pytest.raises(ValueError, match="output dimension"):
        await wrong_fast.embed(["no download"])

    import sys
    from types import ModuleType

    monkeypatch.setitem(sys.modules, "model2vec", ModuleType("model2vec"))
    from memspine.services.embedding.static import StaticEmbedder

    class FakeStaticModel:
        def encode(self, _texts: list[str]) -> list[list[float]]:
            return [[3.0, 4.0, 0.0]]

    static = StaticEmbedder(model="org/static", dim=3)
    static._model = FakeStaticModel()
    assert len((await static.embed(["no download"]))[0]) == 3
    wrong_static = StaticEmbedder(model="org/static", dim=2)
    wrong_static._model = FakeStaticModel()
    with pytest.raises(ValueError, match="output dimension"):
        await wrong_static.embed(["no download"])


async def test_builtin_tantivy_generation_opener_reopens_owned_file_generation(
    tmp_path: Path,
) -> None:
    config = load_config(
        template="core",
        overrides={
            "storage": {
                "records": {
                    "provider": "sqlite",
                    "settings": {
                        "path": str(tmp_path / "events.sqlite"),
                        "busy_timeout_s": 5.0,
                    },
                },
                "lexical": {
                    "provider": "tantivy",
                    "settings": {"analyzer": "default", "date_tokens": False},
                },
            },
        },
    ).config
    descriptor = get_registry("lexical_indexes").get("tantivy")
    settings = validate_settings(descriptor, config.storage.lexical.settings)
    fingerprint = projection_target_fingerprint(
        config,
        category="lexical",
        provider=ProviderRef("lexical", "tantivy"),
        settings=settings,
        deployment_id="deployment-a",
    )
    opener = BuiltinProjectionGenerationOpener(
        category="lexical",
        configs=_ConfigSource(config),
        policy=_Policy(),
    )

    async def open_generation(generation_id: str):
        return await opener(
            ProjectionGeneration(
                deployment_id="deployment-a",
                logical_service_id="lexical",
                generation_id=generation_id,
                provider="tantivy",
                target_fingerprint=fingerprint,
                owner_id="deployment-a",
                status=GenerationStatus.BUILDING,
                updated_at=datetime.now(UTC),
            )
        )

    source = await open_generation("active")
    await source.projector.apply(
        MemoryEvent(
            seq=1,
            kind=EventKind.WRITE,
            namespace="generation-test",
            payload={"record": _record().model_dump(mode="json")},
        )
    )
    await source.close()

    reopened_source = await open_generation("active")
    source_hits = await reopened_source._service.search("generation-test", "camping", top_k=5)
    await reopened_source.close()
    assert [hit.record_id for hit in source_hits] == ["generation-record"]

    target = await open_generation("../../unsafe/path")
    assert not await target._service.exists("generation-record")
    await target.projector.apply(
        MemoryEvent(
            seq=1,
            kind=EventKind.WRITE,
            namespace="generation-test",
            payload={"record": _record().model_dump(mode="json")},
        )
    )
    await target.close()

    reopened_target = await open_generation("../../unsafe/path")
    target_hits = await reopened_target._service.search("generation-test", "camping", top_k=5)
    await reopened_target.close()
    assert [hit.record_id for hit in target_hits] == ["generation-record"]
    assert generation_component("../../unsafe/path") not in ("../../unsafe/path", "active")


async def test_tantivy_switch_replays_then_engine_reopens_activated_generation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "engine.sqlite"

    def make_engine() -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            storage={
                "records": {
                    "provider": "sqlite",
                    "settings": {"path": str(database), "busy_timeout_s": 5.0},
                },
                "cache": {"provider": "memory", "settings": {}},
                "lexical": {
                    "provider": "tantivy",
                    "settings": {"analyzer": "default", "date_tokens": False},
                },
            },
            model={"embedding": {"provider": "hash", "settings": {}}},
            read={"hybrid": True},
        )

    engine = make_engine()
    await engine.start()
    record = await engine.write(
        "Tantivy generation switch preserves camping records",
        namespace="switch-test",
    )
    assert engine._sql is not None and engine._provider_owner_id is not None
    fingerprint = next(
        binding.target_fingerprint
        for binding in projection_target_bindings(
            engine._resolved.config,
            engine._provider_results,
            engine._projectors,
            engine._provider_owner_id,
            engine._embedder,
        )
        if binding.projector_name == "lexical"
    )
    config = engine._resolved.config
    opener = BuiltinProjectionGenerationOpener(
        category="lexical",
        configs=_ConfigSource(config),
        policy=_Policy(record.record_id),
    )
    switch_store = SqlProjectionSwitchStore(engine._sql.engine)
    backend = EventLogProjectionBackend(
        category="lexical",
        event_log=engine._storage,
        switch_store=switch_store,
        open_generation=opener,
        stop_admission=lambda _deployment, _epoch: _async_noop(),
        read_batch_size=16,
    )
    strategy = ProjectionSwitchStrategy(
        backend=backend,
        store=switch_store,
        owner_id=engine._provider_owner_id,
        target_provider="tantivy",
        target_fingerprint=fingerprint,
        batch_size=16,
    )
    coordinator = ChangeCoordinator(
        store=SqlChangeStore(engine._sql.engine),
        revision_guard=_RevisionGuard(fingerprint),
        strategies={("lexical", "projection_rebuild"): strategy},
        plan_ttl_s=3600,
    )
    switcher = ProjectionSwitcher(coordinator)
    plan = await switcher.plan_switch(
        category="lexical",
        logical_service_id="lexical",
        source_revision="tantivy-active-r1",
        target_revision="tantivy-generation-r2",
        deployment_id=engine._provider_owner_id,
        owner_id=engine._provider_owner_id,
        source_generation="active",
        target_generation="target-generation",
        source_target_fingerprint=fingerprint,
        target_fingerprint=fingerprint,
        source_provider="tantivy",
        target_provider="tantivy",
        config_id="tantivy-target-v1",
        source_completeness=SourceCompleteness(
            complete=True,
            evidence_id="event-log-rebuildable",
            source_checkpoint=0,
            source_head_seq=1,
        ),
        retention_s=3600,
        actor="test",
    )
    try:
        operation = await switcher.execute_switch(plan.plan_id)
        result = await switcher.verify_switch(operation.operation_id)
        assert result.status is ChangeStatus.ACTIVE
        await engine.stop()

        reopened = make_engine()
        await reopened.start()
        try:
            assert reopened._lexical is not None
            hits = await reopened._lexical.search("switch-test", "camping", top_k=5)
            assert [hit.record_id for hit in hits] == [record.record_id]
        finally:
            await reopened.stop()
    finally:
        if engine.is_started:
            await engine.stop()


async def _async_noop() -> None:
    return None
