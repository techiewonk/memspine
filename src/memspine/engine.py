"""The Engine facade (D-01): one clean API over the event-sourced substrate.

Startup follows plan §4 (Phase-0 scope — runners join in Phase 1):

1. bootstrap secrets, then resolve config (two-phase, D-22)
2. validate the memory combination (C1b dependency closure) + required services (D-10)
3. construct services; missing service hard-fails naming the extra (D-10)
4. open the write door (event log) and register projectors
6. catch-up: projectors replay from their high-water marks
7. ``describe()`` returns the effective world
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Self, TypeVar

from memspine.clients.http import HTTPClient
from memspine.clients.sqlite import SQLiteClient
from memspine.config.loader import ResolvedConfig, load_config
from memspine.config.schema import MemspineConfig
from memspine.core.events import EventKind, EventLogMode, MemoryEvent
from memspine.core.namespace import validate_namespace
from memspine.core.policies.assembly import AssembledContext, AssemblyPolicy
from memspine.core.policies.scoring import ScoringPolicy
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord, PiiTier, SourceInfo
from memspine.core.registry import SERVICE_EXTRAS, dependency_closure, missing_services
from memspine.core.replay import catch_up
from memspine.core.replay import rebuild as replay_rebuild
from memspine.exceptions import ConfigError, MemspineError, MissingServiceError
from memspine.memories.working.manager import DEFAULT_PAGE_SIZE, WorkingMemory
from memspine.memories.working.persona import make_persona_record
from memspine.observability.logging import EVENT_REBUILD, EVENT_RETRIEVE, EVENT_WRITE, get_logger
from memspine.services.cache.base import MemoryKV
from memspine.services.cache.semantic import CachedEmbedding
from memspine.services.embedding.base import EmbeddingService
from memspine.services.llm.base import LLMRouter, LLMService
from memspine.services.llm.local import OpenAICompatLLM
from memspine.services.secrets.env import EnvSecrets
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.services.vector.base import VectorStore
from memspine.services.vector.projector import VectorProjector
from memspine.services.vector.sqlite_store import SQLiteVectorStore
from memspine.workers.inline import InlineRunner
from memspine.workers.pipelines import PIPELINES, PipelineContext
from memspine.workers.schedule import run_sleep_cycle

__all__ = ["Engine"]

_log = get_logger(__name__)

_T = TypeVar("_T")

#: Services the Phase-0 engine always constructs (core install, D-03).
_CORE_SERVICES = frozenset({"storage", "secrets"})


class Engine:
    """Async-first facade; thin sync wrappers on the public verbs (D-01)."""

    def __init__(
        self,
        template: str | None = None,
        user_config: str | Path | dict[str, Any] | None = None,
        dotenv_path: str | Path | None = ".env",
        **overrides: Any,
    ) -> None:
        self._template = template
        self._user_config = user_config
        self._dotenv_path = dotenv_path
        self._overrides = overrides
        self._resolved: ResolvedConfig | None = None
        self._enabled: set[str] = set()
        self._auto_enabled: list[str] = []
        self._client: SQLiteClient | None = None
        self._http: HTTPClient | None = None
        self._storage: SQLiteStorage | None = None
        self._projectors: list[Projector] = []
        self._embedder: EmbeddingService | None = None
        self._vector: VectorStore | None = None
        self._llm: LLMRouter | None = None
        self._working: WorkingMemory | None = None
        self._scoring: ScoringPolicy | None = None
        self._assembly: AssemblyPolicy | None = None
        self._runner: InlineRunner | None = None
        self._started = False
        self._sync_loop: asyncio.AbstractEventLoop | None = None
        self._sync_thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> Self:
        if self._started:
            return self

        # 1. secrets, then config (D-22 two-phase).
        secrets = EnvSecrets(dotenv_path=self._dotenv_path)
        self._resolved = load_config(
            template=self._template,
            user_config=self._user_config,
            env=os.environ,
            overrides=self._overrides,
            secret_resolver=secrets.get,
        )
        config = self._resolved.config

        # 2. validate combination (C1b) + required services (D-10).
        self._enabled, self._auto_enabled = dependency_closure(config.enabled_memories())
        gaps = missing_services(self._enabled, set(_CORE_SERVICES))
        if gaps:
            mem_type, services = next(iter(sorted(gaps.items())))
            service = sorted(services)[0]
            if config.strict_services:
                raise MissingServiceError(service, SERVICE_EXTRAS.get(service))
            _log.warning(
                "service.missing_ignored",
                detail="strict_services=false: engine starts degraded (D-10)",
                gaps={k: sorted(v) for k, v in gaps.items()},
                first_missing=f"{mem_type} needs {service}",
            )

        # 3./4. construct services + open the write door.
        self._client = SQLiteClient(config.storage.path)
        await self._client.connect()
        self._storage = SQLiteStorage(
            self._client,
            mode=config.event_log.mode,
            compress=config.event_log.compress,
        )
        await self._storage.start()
        if config.event_log.mode is EventLogMode.EPHEMERAL:
            _log.warning(
                "event_log.ephemeral_mode_active",
                detail="events are not persisted: rebuild and audit taint unavailable (D-45)",
            )

        # P1 services: embedding (E3-cached), vector store, LLM router, policies.
        self._embedder = CachedEmbedding(self._build_embedder(config), MemoryKV())
        self._vector = self._build_vector_store(config)
        self._llm = await self._build_llm_router(config)
        self._scoring = ScoringPolicy.bind(config.read.scoring)
        self._assembly = AssemblyPolicy.bind(config.read.assembly)
        self._working = WorkingMemory(
            append_event=self._append_and_project,
            page_size=int(
                self._memory_policy(config, "working").get("page_size", DEFAULT_PAGE_SIZE)
            ),
        )
        self._projectors = [
            RecordProjector(self._storage),
            VectorProjector(self._vector, self._embedder),
        ]

        # Background runner seam (D-16): inline default, pipelines from the table.
        self._runner = InlineRunner()
        for name, pipeline in PIPELINES.items():
            self._runner.register(name, pipeline)

        # 5.(minimal)/6. catch-up from high-water marks; rolling mode prunes on
        # boot via the pipeline (continuous scheduling joins the P3 sleep cycle).
        await catch_up(self._storage, list(self._projectors))
        if config.event_log.mode is EventLogMode.ROLLING:
            stats = await self._runner.run("event_log_prune", self._pipeline_ctx())
            if stats.get("pruned"):
                _log.info("event_log.pruned", **stats)
        self._started = True
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.close()
        if self._storage is not None:
            await self._storage.stop()
        if self._client is not None:
            await self._client.close()
        if self._http is not None:
            await self._http.close()
        self._started = False

    # ── public verbs (P0: write / retrieve / rebuild / describe) ─────────────

    async def write(
        self,
        content: str,
        namespace: str = "default",
        memory_type: str = "semantic",
        source: SourceInfo | None = None,
        pii_tier: PiiTier = PiiTier.NONE,
        actor: str = "user",
    ) -> MemoryRecord:
        """Append a WRITE event through the single door; projection materializes it."""
        storage = self._require_started()
        ns = validate_namespace(namespace)
        record = MemoryRecord(
            namespace=ns,
            memory_type=memory_type,
            content=content,
            source=source or SourceInfo(role=actor),
            pii_tier=pii_tier,
        )
        event = MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor=actor,
            payload={"record": record.model_dump(mode="json")},
        )
        await self._append_and_project(event)
        _log.info(EVENT_WRITE, namespace=ns, record_id=record.record_id)

        # Working memory keeps its hot window bounded (M13.1): overflow pages
        # out to episodic via DECAY_TRANSITION events through the same door.
        if memory_type == "working" and self._working is not None:
            active = await storage.list_records(ns, "working")
            await self._working.enforce(ns, active)
        return record

    async def retrieve(
        self, namespace: str = "default", memory_type: str | None = None
    ) -> list[MemoryRecord]:
        """P0 read path: relational listing. Hybrid retrieval lands in Phase 1."""
        storage = self._require_started()
        ns = validate_namespace(namespace)
        records = await storage.list_records(ns, memory_type)
        _log.info(EVENT_RETRIEVE, namespace=ns, memory_type=memory_type, count=len(records))
        return records

    async def search(
        self,
        query: str,
        namespace: str = "default",
        top_k: int = 8,
    ) -> list[tuple[MemoryRecord, float]]:
        """Semantic retrieval (P1): embed → vector search → composite scoring.

        Returns ``(record, score)`` pairs sorted by the M1 composite score
        (recency/relevance/importance + utility), not raw cosine — a stale
        near-duplicate loses to a fresher, proven-useful memory.
        """
        storage = self._require_started()
        if self._embedder is None or self._vector is None or self._scoring is None:
            raise MemspineError("retrieval services not constructed — engine not started?")
        ns = validate_namespace(namespace)
        [query_vector] = await self._embedder.embed([query])
        hits = await self._vector.query(ns, query_vector, top_k=top_k)
        scored: list[tuple[MemoryRecord, float]] = []
        for hit in hits:
            record = await storage.get_record(hit.record_id)
            if record is None:
                continue  # vector row outlived its record; rebuild reconciles
            scored.append((record, self._scoring.composite_score(record, relevance=hit.score)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        _log.info(EVENT_RETRIEVE, namespace=ns, query=True, count=len(scored))
        return scored

    async def assemble(
        self,
        query: str,
        namespace: str = "default",
        budget_tokens: int = 2048,
        top_k: int = 16,
    ) -> AssembledContext:
        """Retrieval + M12/E2 assembly: MMR-selected, cache-aware-ordered context.

        Persona and other stable records sit before ``boundary_index``; volatile
        episodic/working content after it — feed the prefix to provider caching.
        """
        if self._assembly is None:
            raise MemspineError("assembly policy not bound — engine not started?")
        scored = await self.search(query, namespace=namespace, top_k=top_k)
        # Persona is pinned context (E2): always a candidate, never query-gated.
        storage = self._require_started()
        ns = validate_namespace(namespace)
        for record in await storage.list_records(ns, "working"):
            if record.source.channel == "persona" and all(
                record.record_id != candidate.record_id for candidate, _ in scored
            ):
                scored.append((record, 1.0))
        return self._assembly.assemble(scored, budget_tokens=budget_tokens)

    async def set_persona(self, namespace: str, text: str) -> MemoryRecord:
        """Pin the persona block (M13.1): first token of the E2 stable prefix."""
        self._require_started()
        ns = validate_namespace(namespace)
        record = make_persona_record(ns, text)
        event = MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor="system",
            payload={"record": record.model_dump(mode="json")},
        )
        await self._append_and_project(event)
        return record

    def llm(self, role: str) -> LLMService:
        """The provider bound to a role (D-07/D-22): extract / judge / chat."""
        if self._llm is None:
            raise MemspineError("Engine not started — call start() first")
        return self._llm.for_role(role)

    async def sleep(self) -> dict[str, dict[str, object]]:
        """Run the maintenance sleep cycle now (M2/E7): consolidate → decay →
        compress → prune. All steps are idempotent; P3 gives them teeth."""
        self._require_started()
        assert self._runner is not None
        return await run_sleep_cycle(self._runner, self._pipeline_ctx())

    async def rebuild(self) -> dict[str, int]:
        """Rebuild every projector from seq 0 (D0.1). Raises off-window (D-45)."""
        storage = self._require_started()
        counts = {
            projector.name: await replay_rebuild(storage, projector)
            for projector in self._projectors
        }
        _log.info(EVENT_REBUILD, counts=counts)
        return counts

    def describe(self) -> dict[str, Any]:
        """The effective world (§4 step 7). Only meaningful on a started engine."""
        storage = self._require_started()
        assert self._resolved is not None
        config = self._resolved.config
        return {
            "profile": config.profile,
            "memories": {
                "enabled": sorted(self._enabled),
                "auto_enabled": list(self._auto_enabled),
            },
            "event_log": {
                "mode": config.event_log.mode.value,
                "compress": config.event_log.compress,
                "retention_days": config.event_log.retention_days,
                "rebuildable": storage.can_rebuild,
            },
            "storage": {"backend": "sqlite", "path": config.storage.path},
            "embedding": self._embedder.embedder_id if self._embedder else None,
            "vector": type(self._vector).__name__ if self._vector else None,
            "llm_roles": self._llm.roles if self._llm else [],
            "projectors": [projector.name for projector in self._projectors],
            "runner": "inline",
            "strict_services": config.strict_services,
        }

    # ── thin sync wrappers (D-01) ────────────────────────────────────────────
    #
    # All sync verbs dispatch onto ONE long-lived background loop so aiosqlite
    # connections stay bound to a living loop across calls (a fresh
    # asyncio.run() per verb would strand pooled connections on dead loops).

    def start_sync(self) -> Self:
        return self._run_sync(self.start())

    def write_sync(self, content: str, **kwargs: Any) -> MemoryRecord:
        return self._run_sync(self.write(content, **kwargs))

    def retrieve_sync(self, **kwargs: Any) -> list[MemoryRecord]:
        return self._run_sync(self.retrieve(**kwargs))

    def stop_sync(self) -> None:
        self._run_sync(self.stop())
        self._close_sync_loop()

    def _run_sync(self, coro: Coroutine[Any, Any, _T]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            raise MemspineError(
                "sync wrapper called from inside a running event loop — "
                "use the async API (await engine.<verb>()) here"
            )
        if self._sync_loop is None or self._sync_loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="memspine-sync", daemon=True)
            thread.start()
            self._sync_loop, self._sync_thread = loop, thread
        return asyncio.run_coroutine_threadsafe(coro, self._sync_loop).result()

    def _close_sync_loop(self) -> None:
        if self._sync_loop is not None and not self._sync_loop.is_closed():
            self._sync_loop.call_soon_threadsafe(self._sync_loop.stop)
            if self._sync_thread is not None:
                self._sync_thread.join(timeout=5)
            self._sync_loop.close()
        self._sync_loop = None
        self._sync_thread = None

    # ── internals ────────────────────────────────────────────────────────────

    def _require_started(self) -> SQLiteStorage:
        if not self._started or self._storage is None:
            raise MemspineError("Engine not started — call start() first")
        return self._storage

    async def _append_and_project(self, event: MemoryEvent) -> None:
        """The one internal path from an event to its projections.

        Inline-runner semantics (P1): the appended event is in hand — apply it
        directly in every mode (startup catch-up already brought projectors to
        the head; set_offset is advance-only, so races cannot regress marks).
        """
        assert self._storage is not None
        appended = await self._storage.append_event(event)
        if appended.seq is None:  # pragma: no cover - write door always assigns seq
            raise MemspineError("write door returned an event without seq")
        for projector in self._projectors:
            await projector.apply(appended)
            await self._storage.set_offset(projector.name, appended.seq)

    def _pipeline_ctx(self) -> PipelineContext:
        assert self._storage is not None and self._resolved is not None
        return PipelineContext(storage=self._storage, config=self._resolved.config)

    @staticmethod
    def _memory_policy(config: MemspineConfig, memory_type: str) -> dict[str, Any]:
        mem = config.memories.get(memory_type)
        return dict(mem.policies) if mem is not None else {}

    def _build_embedder(self, config: MemspineConfig) -> EmbeddingService:
        if config.embedding.provider == "hash":
            from memspine.services.embedding.hash_local import HashEmbedding

            return HashEmbedding()
        if config.embedding.provider == "fastembed":
            from memspine.services.embedding.fastembed_local import FastembedEmbedding

            return FastembedEmbedding(model=config.embedding.model)
        raise ConfigError(
            f"unknown embedding.provider {config.embedding.provider!r} (valid: fastembed, hash)"
        )

    def _build_vector_store(self, config: MemspineConfig) -> VectorStore:
        assert self._client is not None and self._embedder is not None
        backend = config.vector.backend
        if backend == "auto":
            try:
                import lancedb  # noqa: F401

                backend = "lance"
            except ImportError:
                backend = "sqlite"
        if backend == "sqlite":
            return SQLiteVectorStore(self._client)
        if backend == "lance":
            from memspine.services.vector.lancedb_store import LanceDBVectorStore

            lance_dir = (
                f"{config.storage.path}.lance"
                if config.storage.path != ":memory:"
                else "./memspine.lance"
            )
            return LanceDBVectorStore(lance_dir, self._embedder.embedder_id, self._embedder.dim)
        raise ConfigError(
            f"unknown vector.backend {config.vector.backend!r} (valid: auto, lance, sqlite)"
        )

    async def _build_llm_router(self, config: MemspineConfig) -> LLMRouter:
        providers: dict[str, LLMService] = {}
        openai_roles = {
            role: role_config
            for role, role_config in config.llm.roles.items()
            if role_config.provider == "openai_compat"
        }
        if openai_roles:
            timeout = max(role.timeout_seconds for role in openai_roles.values())
            self._http = HTTPClient(timeout_seconds=timeout)
            await self._http.connect()
        for role, role_config in config.llm.roles.items():
            if role_config.provider == "openai_compat":
                assert self._http is not None
                providers[role] = OpenAICompatLLM(
                    self._http,
                    base_url=role_config.base_url,
                    model=role_config.model,
                    api_key=role_config.api_key,
                )
            elif role_config.provider == "llama_cpp":
                from memspine.services.llm.llama_cpp import LlamaCppLLM

                providers[role] = LlamaCppLLM(model_path=role_config.model)
            else:
                raise ConfigError(
                    f"unknown llm provider {role_config.provider!r} for role {role!r} "
                    "(valid in P1: openai_compat, llama_cpp; bedrock lands with [aws])"
                )
        return LLMRouter(providers)
