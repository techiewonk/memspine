"""Open real built-in provider stores as isolated projection generations.

The caller supplies resolved configuration by persisted fingerprint. This keeps
credentials outside switch metadata and makes the target config an explicit
operator/workflow input. Verification and retained-generation cleanup remain
explicit callbacks because they are provider and deployment policy decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.memories.associative.entities import EntityPolicy
from memspine.memories.associative.projector import GraphProjector
from memspine.models.encoders.embedding.base import EmbeddingService
from memspine.providers.contracts import SqlAccess
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.projection_bindings import projection_target_fingerprint
from memspine.providers.registry import LEXICAL_INDEXES, VECTOR_STORES
from memspine.providers.switching import (
    CleanupEvidence,
    ProjectionGeneration,
    ProjectionVerificationEvidence,
)
from memspine.runtime.container import (
    Capabilities,
    Wiring,
    _descriptor,
    _graph_descriptor,
    _settings,
    build_graph_store,
    build_lexical_store,
    build_vector_store,
    lexical_projector_name,
    memory_policy,
)
from memspine.services.lexical.projector import LexicalProjector
from memspine.services.vector.projector import VectorProjector
from memspine.store.projections.projector import Projector

ProjectionCategory = Literal["graph", "vector", "lexical"]


@dataclass(frozen=True, slots=True)
class OpenedProjection:
    """A generation-scoped built-in service and its canonical event projector."""

    config: MemspineConfig
    embedder: EmbeddingService | None = None
    sql: SqlAccess | None = None


class ProjectionConfigSource(Protocol):
    async def resolve(self, generation: ProjectionGeneration) -> OpenedProjection: ...


class ProjectionGenerationPolicy(Protocol):
    async def verify(
        self,
        *,
        category: ProjectionCategory,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        service: object,
        source_head_seq: int,
        erase_checkpoint_seq: int,
    ) -> ProjectionVerificationEvidence: ...

    async def activate(
        self,
        *,
        category: ProjectionCategory,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        service: object,
        expected_source_revision: str,
        evidence: ProjectionVerificationEvidence,
    ) -> None: ...

    async def freeze(
        self, *, category: ProjectionCategory, generation: ProjectionGeneration, service: object
    ) -> None: ...

    async def has_live_references(
        self, *, category: ProjectionCategory, generation: ProjectionGeneration, service: object
    ) -> bool: ...

    async def delete_owned(
        self, *, category: ProjectionCategory, generation: ProjectionGeneration, service: object
    ) -> CleanupEvidence: ...


class BuiltinProjectionGenerationOpener:
    """Build an isolated provider through its registered descriptor.

    ``configs`` resolves each generation's persisted target fingerprint to an
    explicit secret-bearing in-memory config. No config or credential is
    serialized by this opener. Activation is a durable configuration cutover;
    callers stop writes and restart Engine with the verified target config after
    the operation. The backend closes this handle when activation returns, so
    this adapter does not claim a live in-process hot swap.
    """

    def __init__(
        self,
        *,
        category: ProjectionCategory,
        configs: ProjectionConfigSource,
        policy: ProjectionGenerationPolicy,
        batch_size: int = 500,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._category = category
        self._configs = configs
        self._policy = policy
        self._batch_size = batch_size

    async def __call__(self, generation: ProjectionGeneration) -> BuiltinProjectionGenerationHandle:
        opened = await self._configs.resolve(generation)
        config = opened.config
        resources = ProviderLifecycleManager(
            close_timeout_s=config.runtime.provider_services.close_timeout_s
        )
        wiring = Wiring(
            resources=resources,
            health_timeout_s=config.runtime.provider_services.health_timeout_s,
            startup_timeout_s=config.runtime.provider_services.startup_timeout_s,
            owner_id=generation.owner_id,
            explicit_owner_id=True,
            sql=opened.sql,
            projection_routes={
                {"graph": "graph", "vector": "vectors", "lexical": "lexical"}[
                    self._category
                ]: generation
            },
        )
        try:
            descriptor, raw_settings = self._descriptor_for(config, wiring)
            if descriptor.ref.name != generation.provider:
                raise ConfigError(
                    "resolved projection config provider does not match its generation"
                )
            settings = _settings(descriptor, raw_settings)
            expected_fingerprint = projection_target_fingerprint(
                config,
                category=self._category,
                provider=descriptor.ref,
                settings=settings,
                deployment_id=generation.deployment_id,
                embedder=opened.embedder,
            )
            if expected_fingerprint != generation.target_fingerprint:
                raise ConfigError(
                    "resolved projection config does not match the generation fingerprint"
                )
            service, projector = await self._build(config, opened.embedder, wiring)
        except BaseException:
            await resources.close_all()
            raise
        return BuiltinProjectionGenerationHandle(
            category=self._category,
            generation=generation,
            service=service,
            projector=projector,
            resources=resources,
            policy=self._policy,
        )

    def _descriptor_for(
        self, config: MemspineConfig, wiring: Wiring
    ) -> tuple[Any, object]:
        if self._category == "graph":
            descriptor = _graph_descriptor(config, wiring)
            raw_settings: object = config.graph.settings
        elif self._category == "vector":
            descriptor = _descriptor(VECTOR_STORES, config.vector.provider, "vector.provider")
            raw_settings = config.vector.settings
        else:
            descriptor = _descriptor(
                LEXICAL_INDEXES, config.read.lexical_provider, "read.lexical_provider"
            )
            raw_settings = config.read.lexical_settings
        return descriptor, raw_settings

    async def _build(
        self,
        config: MemspineConfig,
        embedder: EmbeddingService | None,
        wiring: Wiring,
    ) -> tuple[object, Projector]:
        if self._category == "graph":
            service_graph = await build_graph_store(config, wiring)
            entities = EntityPolicy.from_policy(
                memory_policy(config, "associative").get("entity_nodes")
            )
            projector = GraphProjector(
                service_graph,
                entities,
                embed=embedder.embed if config.graph.entity_embeddings and embedder else None,
            )
            return service_graph, projector
        if self._category == "vector":
            if embedder is None:
                raise ConfigError("vector generation requires its resolved embedding service")
            service_vector = await build_vector_store(config, embedder, wiring)
            return service_vector, VectorProjector(service_vector, embedder)
        service_lexical = await build_lexical_store(config, Capabilities(set(), []), wiring)
        return service_lexical, LexicalProjector(
            service_lexical, name=lexical_projector_name(config)
        )


class BuiltinProjectionGenerationHandle:
    """Lifecycle adapter around an actual descriptor-built projection store."""

    def __init__(
        self,
        *,
        category: ProjectionCategory,
        generation: ProjectionGeneration,
        service: object,
        projector: Projector,
        resources: ProviderLifecycleManager,
        policy: ProjectionGenerationPolicy,
    ) -> None:
        self._category = category
        self._generation = generation
        self._service = service
        self._projector = projector
        self._resources = resources
        self._policy = policy

    @property
    def projector(self) -> Projector:
        return self._projector

    async def verify(
        self,
        *,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        source_head_seq: int,
        erase_checkpoint_seq: int,
    ) -> ProjectionVerificationEvidence:
        self._require_generation(target)
        return await self._policy.verify(
            category=self._category,
            source=source,
            target=target,
            service=self._service,
            source_head_seq=source_head_seq,
            erase_checkpoint_seq=erase_checkpoint_seq,
        )

    async def activate(
        self,
        *,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        expected_source_revision: str,
        evidence: ProjectionVerificationEvidence,
    ) -> None:
        self._require_generation(target)
        await self._policy.activate(
            category=self._category,
            source=source,
            target=target,
            service=self._service,
            expected_source_revision=expected_source_revision,
            evidence=evidence,
        )

    async def freeze(self, generation: ProjectionGeneration) -> None:
        self._require_generation(generation)
        await self._policy.freeze(
            category=self._category, generation=generation, service=self._service
        )

    async def has_live_references(self, generation: ProjectionGeneration) -> bool:
        self._require_generation(generation)
        return await self._policy.has_live_references(
            category=self._category, generation=generation, service=self._service
        )

    async def delete_owned(self, generation: ProjectionGeneration) -> CleanupEvidence:
        self._require_generation(generation)
        try:
            return await self._policy.delete_owned(
                category=self._category, generation=generation, service=self._service
            )
        finally:
            await self.close()

    async def close(self) -> None:
        report = await self._resources.close_all()
        if report.failures:
            raise ConfigError("projection generation provider resources did not close cleanly")

    def _require_generation(self, generation: ProjectionGeneration) -> None:
        if (
            generation.deployment_id != self._generation.deployment_id
            or generation.logical_service_id != self._generation.logical_service_id
            or generation.generation_id != self._generation.generation_id
            or generation.owner_id != self._generation.owner_id
            or generation.target_fingerprint != self._generation.target_fingerprint
            or generation.provider != self._generation.provider
        ):
            raise ConfigError("projection handle generation ownership does not match")


__all__ = [
    "BuiltinProjectionGenerationHandle",
    "BuiltinProjectionGenerationOpener",
    "OpenedProjection",
    "ProjectionConfigSource",
    "ProjectionGenerationPolicy",
]
