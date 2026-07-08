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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self, TypeVar, cast

from memspine.clients.http import HTTPClient
from memspine.clients.kuzu import KuzuClient
from memspine.clients.lancedb import LanceDBClient
from memspine.clients.sqlite import SQLiteClient
from memspine.config import constants
from memspine.config.loader import ResolvedConfig, load_config
from memspine.config.schema import MemspineConfig
from memspine.core.audit import TaintReport, trace_taint
from memspine.core.erasure import payload_retains_content
from memspine.core.events import EventKind, EventLogMode, MemoryEvent, fingerprint_payload
from memspine.core.firewall import Firewall, FirewallVerdict
from memspine.core.namespace import grant_allows, validate_namespace
from memspine.core.policies.assembly import AssembledContext, AssemblyPolicy
from memspine.core.policies.compression import CompressionPolicy
from memspine.core.policies.conflict import ConflictPolicy
from memspine.core.policies.dedup import DedupPolicy
from memspine.core.policies.retention import RetentionPolicy
from memspine.core.policies.scoring import ScoringPolicy
from memspine.core.policies.trust import TrustPolicy
from memspine.core.projector import Projector
from memspine.core.records import (
    ArchivedVersion,
    MemoryRecord,
    PiiTier,
    RecordStatus,
    SourceInfo,
)
from memspine.core.registry import SERVICE_EXTRAS, dependency_closure, missing_services
from memspine.core.replay import catch_up
from memspine.core.replay import rebuild as replay_rebuild
from memspine.exceptions import (
    ConfigError,
    ConflictError,
    MemspineError,
    MissingServiceError,
    StorageError,
)
from memspine.memories.associative.evolution import propose_links
from memspine.memories.associative.projector import GraphProjector
from memspine.memories.associative.store import AssociativeMemory
from memspine.memories.episodic.sessions import Session
from memspine.memories.episodic.store import EpisodicMemory
from memspine.memories.procedural.prompt_registry import prompt_version_records
from memspine.memories.procedural.skills import (
    ProceduralMemory,
    make_skill_record,
    stage_status,
)
from memspine.memories.prospective.watches import ProspectiveMemory, make_watch_record
from memspine.memories.reflective.reflections import ReflectiveMemory
from memspine.memories.resource.store import ResourceMemory
from memspine.memories.semantic.entities import EntityExtractor, LLMEntityExtractor
from memspine.memories.semantic.store import SemanticMemory
from memspine.memories.shared.grants import Grant, SharedMemory
from memspine.memories.shared.subscriptions import make_subscription_record
from memspine.memories.working.manager import DEFAULT_PAGE_SIZE, WorkingMemory
from memspine.memories.working.persona import make_persona_record
from memspine.observability.logging import (
    EVENT_FORGET,
    EVENT_LINK,
    EVENT_REBUILD,
    EVENT_RETRIEVE,
    EVENT_WRITE,
    get_logger,
)
from memspine.prompts.registry import PromptRegistry
from memspine.services.cache.base import MemoryKV
from memspine.services.cache.semantic import CachedEmbedding, CachedExtractor
from memspine.services.embedding.base import EmbeddingService
from memspine.services.graph.base import GraphStore
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.lexical.base import LexicalStore, rrf_fuse
from memspine.services.lexical.projector import LexicalProjector
from memspine.services.lexical.sqlite_fts5 import SQLiteFTS5Lexical
from memspine.services.llm.base import LLMRouter, LLMService
from memspine.services.llm.local import OpenAICompatLLM
from memspine.services.rerank.base import Reranker, concat_background
from memspine.services.secrets.env import EnvSecrets
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.services.vector.base import VectorStore
from memspine.services.vector.projector import VectorProjector
from memspine.services.vector.sqlite_store import SQLiteVectorStore
from memspine.workers.inline import InlineRunner
from memspine.workers.pipelines import PIPELINES, PipelineContext, Summarize
from memspine.workers.runner import TaskRunner
from memspine.workers.schedule import run_sleep_cycle

__all__ = ["Engine"]

_log = get_logger(__name__)

_T = TypeVar("_T")

#: Services the Phase-0 engine always constructs (core install, D-03).
_CORE_SERVICES = frozenset({"storage", "secrets"})

#: Roles whose writes may corroborate a quarantined record out of quarantine
#: (E1). Excludes assistant/tool — the indirect-injection authorship surface.
_CORROBORATION_ROLES = frozenset({"operator", "system", "user"})


def _cosine(u: list[float], v: list[float]) -> float:
    """Dot product over the unit-normalized vectors every embedder emits."""
    if len(u) != len(v):
        # A dimension mismatch means the embedder changed under us — a silent
        # 0.0 would read as "plan cache has no coverage" forever (D-10).
        raise MemspineError(
            f"embedding dimension mismatch ({len(u)} vs {len(v)}) — was the "
            "embedder model changed without a rebuild?"
        )
    return sum(a * b for a, b in zip(u, v, strict=True))


def _as_options_dict(raw: Any) -> dict[str, object] | None:
    """Policy sub-blocks arrive as plain config dicts; anything else is a typo
    the policy's own extra='forbid' validation will surface."""
    return dict(raw) if isinstance(raw, dict) else None


def _static_prefilter(
    query: str, candidates: list[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """E8 optional first stage (opt-in): a cheap lexical-overlap gate over the
    candidate set. Applied *after* the vector leg (a true pre-vector
    static-embedding prefilter is the E4 model2vec track); when nothing
    overlaps it keeps the original set — a precision stage must never turn a
    working retrieval into an empty one."""
    query_tokens = set(query.lower().split())
    kept = [
        (record, relevance)
        for record, relevance in candidates
        if query_tokens & set(record.content.lower().split())
    ]
    return kept or candidates


def _minmax_normalize(scores: list[float]) -> list[float]:
    """Cross-encoder logits → [0, 1] relevance (rank-preserving) so reranked
    scores compose with the M1 composite exactly like cosine similarities."""
    lo, hi = min(scores), max(scores)
    if hi - lo <= 1e-12:
        # SF-2/ADR-018: >1 candidate collapsing to a single score is the
        # signature of a broken/misconfigured cross-encoder. Stay neutral
        # (behavior unchanged) but surface it once so it is not silent.
        if len(scores) > 1:
            _log.warning("rerank.degenerate_scores", count=len(scores), value=lo)
        return [0.5] * len(scores)
    return [(score - lo) / (hi - lo) for score in scores]


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
        self._lance: LanceDBClient | None = None
        self._kuzu: KuzuClient | None = None
        self._storage: SQLiteStorage | None = None
        self._projectors: list[Projector] = []
        self._embedder: EmbeddingService | None = None
        self._vector: VectorStore | None = None
        self._lexical: LexicalStore | None = None
        self._graph: GraphStore | None = None
        self._llm: LLMRouter | None = None
        self._working: WorkingMemory | None = None
        self._semantic: SemanticMemory | None = None
        self._episodic: EpisodicMemory | None = None
        self._resource: ResourceMemory | None = None
        self._procedural: ProceduralMemory | None = None
        self._reflective: ReflectiveMemory | None = None
        self._associative: AssociativeMemory | None = None
        self._prospective: ProspectiveMemory | None = None
        self._shared: SharedMemory | None = None
        self._prompts: PromptRegistry | None = None
        self._scoring: ScoringPolicy | None = None
        self._assembly: AssemblyPolicy | None = None
        self._assembly_compression: CompressionPolicy | None = None
        self._reranker: Reranker | None = None
        self._rerank_unavailable = False
        # SF-1/ADR-018: latch so the "invalidation watches never fire under
        # event_log.mode=ephemeral" warning is logged at most once per engine.
        self._ephemeral_watch_warned = False
        self._inflate: CompressionPolicy = CompressionPolicy.bind()
        self._firewall: Firewall = Firewall()
        self._summarize: Summarize | None = None
        self._runner: TaskRunner | None = None
        self._started = False
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._sync_loop: asyncio.AbstractEventLoop | None = None
        self._sync_thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> Self:
        """Start the engine; on any mid-start failure every already-opened
        client/service is torn down before the error propagates (no leaks)."""
        if self._started:
            return self
        try:
            return await self._start_inner()
        except BaseException:
            await self._teardown()
            raise

    async def _start_inner(self) -> Self:
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
        self._vector = await self._build_vector_store(config)
        # Lexical BM25 leg (D-25): built ONLY when hybrid retrieval is on, so
        # profile="simple" stays truly inert — no lexical index, no projector,
        # no write-path cost, and describe()["projectors"] is unchanged. When
        # first switched on, catch-up/rebuild backfills the index from the log.
        if config.read.hybrid:
            self._lexical = SQLiteFTS5Lexical(self._client)
        # P6 graph store (D-26): constructed only when associative memory
        # projects it — profile="simple" never constructs one. An explicit
        # ``graph:`` block without associative would be a dead handle (no
        # projector ever writes it), so it is a config error, not a store.
        if "associative" in self._enabled:
            self._graph = await self._build_graph_store(config)
        elif "graph" in config.model_fields_set:
            raise ConfigError(
                "graph configured but memories.associative not enabled — "
                "enable it or remove the graph block"
            )
        self._llm = await self._build_llm_router(config)
        self._scoring = ScoringPolicy.bind(config.read.scoring)
        self._assembly = AssemblyPolicy.bind(config.read.assembly)
        # E5 assembly-stage compression binding (D-51): master switch defaults
        # off inside the options, so profile="simple" behavior never changes.
        self._assembly_compression = CompressionPolicy.bind(
            _as_options_dict(config.read.compression)
        )
        # E8 rerank (D-51): validate the mode now; the model itself loads
        # lazily on first search (a config typo must fail at start, a heavy
        # ONNX download must not).
        if config.read.rerank not in ("off", "fastembed", "flashrank"):
            raise ConfigError(
                f"unknown read.rerank {config.read.rerank!r} (valid: off, fastembed, flashrank)"
            )
        self._working = WorkingMemory(
            append_event=self._append_and_project,
            page_size=int(
                self._memory_policy(config, "working").get("page_size", DEFAULT_PAGE_SIZE)
            ),
        )
        # Prompt pack resolution (§4 step 2, D-43): defaults + config overrides.
        self._prompts = PromptRegistry(overrides=config.prompts.overrides)
        if "semantic" in self._enabled:
            semantic_policies = self._memory_policy(config, "semantic")
            self._semantic = SemanticMemory(
                storage=self._storage,
                embedder=self._embedder,
                append_event=self._append_and_project,
                conflict=ConflictPolicy.bind(_as_options_dict(semantic_policies.get("conflict"))),
                dedup=DedupPolicy.bind(_as_options_dict(semantic_policies.get("dedup"))),
                extractor=self._build_extractor(config),
            )
        # Memory Firewall (E1/M17): trust matrix binds from the semantic
        # policy block (D-14 channel); the gate itself covers every type.
        self._firewall = Firewall(
            TrustPolicy.bind(_as_options_dict(self._memory_policy(config, "semantic").get("trust")))
        )
        if "episodic" in self._enabled:
            self._episodic = EpisodicMemory(self._storage)
        if "resource" in self._enabled:
            self._resource = ResourceMemory(
                self._append_and_project,
                storage=self._storage,
                assess=self._gated_assess,
            )
        if "procedural" in self._enabled:
            self._procedural = ProceduralMemory(self._storage, self._append_and_project)
        if "reflective" in self._enabled:
            self._reflective = ReflectiveMemory(
                self._storage,
                self._append_and_project,
                assess=self._gated_assess,
            )
        if "associative" in self._enabled:
            # The graph store was constructed above (the associative gate).
            assert self._graph is not None
            self._associative = AssociativeMemory(
                self._storage, self._graph, self._append_and_project
            )
        if "prospective" in self._enabled:
            self._prospective = ProspectiveMemory(self._storage, self._append_and_project)
        if "shared" in self._enabled:
            self._shared = SharedMemory(self._storage, self._append_and_project)
        self._summarize = self._build_summarize()
        self._projectors = [
            RecordProjector(self._storage),
            VectorProjector(self._vector, self._embedder),
        ]
        if self._lexical is not None:
            # Registered only when hybrid is on, so rebuild() replays it and the
            # index backfills from seq 0 the first time hybrid is enabled.
            self._projectors.append(LexicalProjector(self._lexical))
        if self._associative is not None:
            # Registered only when associative is enabled, so rebuild() replays
            # it and profile="simple" never projects a graph (D0.1/ADR-015).
            assert self._graph is not None
            self._projectors.append(GraphProjector(self._graph))

        # Background runner seam (D-16): inline default, dbos durable [dbos].
        self._runner = self._build_runner(config)
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
        await self._teardown()

    async def _teardown(self) -> None:
        if self._runner is not None:
            await self._runner.close()
        if self._storage is not None:
            await self._storage.stop()
        if self._lexical is not None:
            await self._lexical.close()  # shares the SQLite client; a no-op there
        if self._graph is not None:
            await self._graph.close()  # store-held handles; connections close below
        for client in (self._client, self._http, self._lance, self._kuzu):
            if client is not None:
                await client.close()
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
        entity: str | None = None,
        attribute: str | None = None,
    ) -> MemoryRecord:
        """Append a WRITE event through the single door; projection materializes it.

        ``entity``/``attribute`` key a semantic fact directly (M13.3) so the M4
        conflict ladder engages without requiring an extractor — callers that
        know the fact key should always pass it.
        """
        storage = self._require_started()
        ns = validate_namespace(namespace)
        # SEC-H1/ADR-018: the "shared" type is engine-internal bookkeeping
        # (grants + subscriptions carry authorization state). A public write of
        # it would forge a live grant — parse_grant only checks shape, so a
        # crafted content='{"grant":...}' would bypass grant()'s scope/self-grant/
        # supersession guards. Grants come ONLY from grant()/subscribe(), which
        # build the record and go through _write_locked directly.
        if memory_type == "shared":
            raise ConflictError("memory_type 'shared' is engine-internal — use grant()/subscribe()")
        record = MemoryRecord(
            namespace=ns,
            memory_type=memory_type,
            content=content,
            source=source or SourceInfo(role=actor),
            pii_tier=pii_tier,
            entity=entity,
            attribute=attribute,
        )
        # One writer per namespace: the firewall's context reads, the write,
        # and corroboration form one unit racing writers must not interleave.
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await self._write_locked(storage, ns, record, memory_type, actor)

    async def _write_locked(
        self,
        storage: SQLiteStorage,
        ns: str,
        record: MemoryRecord,
        memory_type: str,
        actor: str,
    ) -> MemoryRecord:
        # Memory Firewall gate (E1/M17): every write of every type passes the
        # deterministic trust/anomaly/instruction assessment BEFORE the door.
        verdict = await self._assess_write(record)
        record = verdict.apply(record)
        if verdict.quarantine:
            # Quarantined content is stored inert: no dedup merging, no
            # conflict-ladder participation, no retrieval surface — but the
            # write IS recorded (audit + later corroboration need it).
            await self._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace=ns,
                    actor=actor,
                    payload={
                        "record": record.model_dump(mode="json"),
                        "firewall": {"reasons": verdict.reasons},
                    },
                )
            )
            _log.warning(
                "memory.quarantined",
                namespace=ns,
                record_id=record.record_id,
                reasons=verdict.reasons,
            )
            return record

        # Semantic writes run the full M5/M4 pipeline (dedup → entities →
        # conflict); every other type is a plain WRITE through the door.
        if memory_type == "semantic" and self._semantic is not None:
            result = await self._semantic.write(record)
            _log.info(
                EVENT_WRITE,
                namespace=ns,
                record_id=result.record.record_id,
                action=result.action,
            )
            await self._corroborate(ns, result.record)
            await self._evolve_links(ns, result.record)
            return result.record

        event = MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor=actor,
            payload={"record": record.model_dump(mode="json")},
        )
        await self._append_and_project(event)
        _log.info(EVENT_WRITE, namespace=ns, record_id=record.record_id)
        await self._corroborate(ns, record)
        await self._evolve_links(ns, record)

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
        records = self._inflate_all(await storage.list_records(ns, memory_type), ns)
        _log.info(EVENT_RETRIEVE, namespace=ns, memory_type=memory_type, count=len(records))
        return records

    async def search(
        self,
        query: str,
        namespace: str = "default",
        top_k: int = constants.SEARCH_TOP_K,
    ) -> list[tuple[MemoryRecord, float]]:
        """Semantic retrieval (P1 + E8 opt-in stages, D-51):
        ``[static_prefilter?] → vector/hybrid → [rerank?] → score`` (MMR and
        assembly follow in :meth:`assemble`).

        The retrieval leg is **vector-only by default**; with ``read.hybrid: true``
        (D-25) a lexical BM25 leg (``services/lexical``) is fused into the
        candidate ranking via reciprocal-rank fusion (``rrf_fuse``), so a record
        that only lexical search would surface can enter the results. Hybrid is
        opt-in for backward-compat (default-on is the intended v0.2 flip); off,
        results are bit-identical to the vector-only pipeline. The E1
        status/quarantine gate below runs on the FUSED candidates, so held
        content never surfaces through the lexical leg either.
        ``static_prefilter`` is a cheap lexical-overlap gate applied POST-fusion,
        not a true pre-vector prefilter.

        Returns ``(record, score)`` pairs sorted by the M1 composite score
        (recency/relevance/importance + utility), not raw cosine — a stale
        near-duplicate loses to a fresher, proven-useful memory. Each search
        appends one RETRIEVE event so access stats (reinforcement, M1) update
        through the write door like every other mutation. Both E8 stages are
        off by default: results are bit-identical to the plain pipeline.
        """
        storage = self._require_started()
        if self._embedder is None or self._vector is None or self._scoring is None:
            raise MemspineError("retrieval services not constructed — engine not started?")
        if top_k < 1:
            # SQLite LIMIT treats -1 as unbounded while Python's ``[:top_k]`` slice
            # would diverge; reject rather than let the two layers silently disagree
            # (REST already guards ``ge=1``).
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        ns = validate_namespace(namespace)
        [query_vector] = await self._embedder.embed([query])
        use_hybrid = self._config().read.hybrid and self._lexical is not None
        # Hybrid recall (E8/D-25): fetch a wider candidate window per leg so a
        # record ranked just outside a single leg's top_k, but strong when the two
        # legs combine, can still enter the fused top_k.
        fetch_k = top_k * constants.LEXICAL_FETCH_MULTIPLIER if use_hybrid else top_k
        vector_hits = await self._vector.query(
            ns, query_vector, embedder_id=self._embedder.embedder_id, top_k=fetch_k
        )
        # Hybrid (D-25): fuse the lexical BM25 leg via RRF. Off (default),
        # ``ranked`` is exactly the vector hits in cosine order — bit-identical.
        if use_hybrid:
            assert self._lexical is not None  # narrowed by use_hybrid
            try:
                lexical_hits = await self._lexical.search(ns, query, top_k=fetch_k)
            except Exception as exc:
                # Defense in depth: a broken lexical leg degrades to vector-only
                # (fusing an empty leg preserves the vector ordering), it never
                # takes down the whole search().
                _log.warning("lexical.search_failed", namespace=ns, error=str(exc))
                lexical_hits = []
            fused = rrf_fuse(vector_hits, lexical_hits)[:top_k]
            # F1: raw RRF scores are ~1/(k+1) (≈0.016), but the M1 composite expects
            # relevance in [0, 1]. Normalize by the theoretical max (a record ranked
            # #1 in BOTH legs scores 2/(k+1)) so the fused relevance composes with
            # recency/importance exactly like a cosine similarity would — otherwise
            # relevance collapses and recency/importance dominate under hybrid.
            rrf_max = 2.0 / (constants.RRF_K + 1)
            ranked: list[tuple[str, float]] = [(rid, score / rrf_max) for rid, score in fused]
        else:
            ranked = [(hit.record_id, hit.score) for hit in vector_hits]
        candidates: list[tuple[MemoryRecord, float]] = []
        for record_id, relevance in ranked:
            record = await storage.get_record(record_id)
            if record is None or record.status is not RecordStatus.ACTIVATED:
                # Only live facts reach a context window: DELETED/QUARANTINED are
                # excluded (E1), and ARCHIVED/superseded history never surfaces as
                # current truth — a promoted-then-superseded record cannot re-enter.
                continue
            if record.quarantined:
                continue  # defense in depth: quarantined never reaches assembly
            try:
                record = self._inflate.inflate(record)  # cold-tier content restored (M6)
            except StorageError:
                # One corrupt cold-tier row must not take down the whole query.
                _log.warning("memory.inflate_failed", namespace=ns, record_id=record.record_id)
                continue
            candidates.append((record, relevance))
        # E8 stage: static prefilter (opt-in, default off).
        if candidates and self._config().read.static_prefilter:
            candidates = _static_prefilter(query, candidates)
        # E8 stage: cross-encoder rerank (opt-in, default off). The reranked
        # relevance replaces the vector similarity; the E1 gates above already
        # ran, so a reranker can only reorder live content, never resurface
        # held content. Failures degrade loudly to the vector ordering.
        reranker = self._rerank_provider()
        if reranker is not None and candidates:
            documents = [concat_background(record) for record, _ in candidates]
            try:
                raw_scores = await reranker.rerank(query, documents)
                relevances = _minmax_normalize(raw_scores)
                candidates = [
                    (record, relevance)
                    for (record, _), relevance in zip(candidates, relevances, strict=True)
                ]
            except Exception as exc:
                _log.warning(
                    "rerank.failed", namespace=ns, reranker=reranker.reranker_id, error=str(exc)
                )
        scored = [
            (record, self._scoring.composite_score(record, relevance=relevance))
            for record, relevance in candidates
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        if scored:
            # Reinforcement stats via the log (M1): last_accessed_at + access_count.
            await self._append_and_project(
                MemoryEvent(
                    kind=EventKind.RETRIEVE,
                    namespace=ns,
                    actor="system",
                    payload={"record_ids": [record.record_id for record, _ in scored]},
                )
            )
        _log.info(EVENT_RETRIEVE, namespace=ns, query=True, count=len(scored))
        return scored

    async def assemble(
        self,
        query: str,
        namespace: str = "default",
        budget_tokens: int = constants.ASSEMBLE_BUDGET_TOKENS,
        top_k: int = constants.ASSEMBLE_TOP_K,
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
        # E1: instruction-shaped content enters a context window WRAPPED — the
        # flag was stored inert at write time precisely so assembly could do
        # this; unwrapped, a flagged-but-unquarantined record (e.g. a benign
        # user imperative) would read as instructions to the model.
        scored = [
            (
                record.model_copy(
                    update={
                        "content": constants.INSTRUCTION_FLAG_WRAP.format(content=record.content)
                    }
                )
                if record.instruction_flag
                else record,
                score,
            )
            for record, score in scored
        ]
        # E5 (D-51): the compression policy's own master switch decides whether
        # the fit stage runs; with the default options this is a no-op.
        return self._assembly.assemble(
            scored, budget_tokens=budget_tokens, compression=self._assembly_compression
        )

    async def set_persona(self, namespace: str, text: str) -> MemoryRecord:
        """Pin the persona block (M13.1): first token of the E2 stable prefix.

        Updating supersedes in place: the persona keeps ONE stable record_id
        per namespace (version bumped, prior text archived to history) so the
        stable prefix stays stable instead of accumulating contradictory
        personas that paging can never evict.
        """
        storage = self._require_started()
        ns = validate_namespace(namespace)
        existing = [
            record
            for record in await storage.list_records(ns, "working")
            if record.source.channel == "persona" and record.status is not RecordStatus.DELETED
        ]
        if existing:
            existing.sort(key=lambda record: record.recorded_at)
            prior = existing[0]
            record = prior.model_copy(
                update={
                    "content": text,
                    "content_fingerprint": fingerprint_payload({"content": text}),
                    "version": prior.version + 1,
                    "history": [
                        *prior.history,
                        ArchivedVersion(
                            version=prior.version,
                            content=prior.content,
                            archived_at=datetime.now(UTC),
                            reason="persona_update",
                        ),
                    ],
                }
            )
        else:
            record = make_persona_record(ns, text)
        event = MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor="system",
            payload={"record": record.model_dump(mode="json")},
        )
        await self._append_and_project(event)
        _log.info(EVENT_WRITE, namespace=ns, record_id=record.record_id, persona=True)
        return record

    async def forget(self, record_id: str, namespace: str = "default", hard: bool = False) -> None:
        """Forget one memory (M7).

        Soft (default): FORGET event → status=DELETED in the read model,
        vector row removed; the log keeps the history.

        Hard (``hard=True``, the P4 cascade): the row leaves the read model
        entirely AND every log payload carrying its content is redacted —
        GDPR-erasure semantics in an append-only design. Legal holds block it.
        """
        storage = self._require_started()
        ns = validate_namespace(namespace)
        # Same per-namespace lock as write(): a hard delete's read-hold-check-
        # redact sequence must not interleave with a concurrent write or its
        # corroboration read-modify-write on the same record.
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            await self._forget_locked(storage, ns, record_id, hard)

    async def _forget_locked(
        self, storage: SQLiteStorage, ns: str, record_id: str, hard: bool
    ) -> None:
        record = await storage.get_record(record_id)
        # SEC-C2/ADR-018: forget is scoped to the caller's namespace. A grantee
        # who learned a foreign record_id via shared_search must not be able to
        # delete the grantor's data by passing its own namespace.
        #
        # - A record that EXISTS in another namespace ALWAYS raises the ADR-014
        #   anti-oracle error (same shape as _require_watch) and is NOT touched
        #   — this closes the IDOR for both soft and hard paths.
        # - A record ABSENT from the read model: the SOFT path raises the SAME
        #   anti-oracle error (missing and foreign are indistinguishable, so a
        #   leaked id is no existence oracle). The HARD path instead proceeds as
        #   an idempotent no-op: a hard delete removes the row during projection,
        #   but its LOG redaction may still need a retry after a mid-operation
        #   failure (M7 durability) — a second hard forget of the now-absent row
        #   must complete the erasure, never raise.
        if record is None:
            if not hard:
                raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
        elif record.namespace != ns:
            raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
        if hard and record is not None:
            retention = RetentionPolicy.bind(
                _as_options_dict(
                    self._memory_policy(self._config(), record.memory_type).get("retention")
                )
            )
            if retention.on_legal_hold(record):
                raise MemspineError(
                    f"record {record_id} is under legal hold — hard delete refused (M7)"
                )
        await self._append_and_project(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace=ns,
                actor="user",
                payload={"record_id": record_id, "hard": hard},
            )
        )
        if hard:
            redacted = await storage.redact_event_payloads(record_id)
            # D-18: the hard-delete cascade escalates to alert severity.
            _log.error(
                EVENT_FORGET, namespace=ns, record_id=record_id, hard=True, redacted=len(redacted)
            )
        # M7 delete hooks: every enabled memory type drops derived state that
        # references the record (e.g. the semantic LSH cache stops probing it).
        for memory in (
            self._working,
            self._semantic,
            self._episodic,
            self._resource,
            self._procedural,
            self._reflective,
            self._associative,
            self._prospective,
            self._shared,
        ):
            if memory is not None:
                await memory.on_forget(ns, record_id)
        _log.info(EVENT_FORGET, namespace=ns, record_id=record_id)

    async def verify_forget(self, record_id: str, namespace: str = "default") -> dict[str, object]:
        """M7 ``forget --verify``: prove erasure across every store we own.

        Uses the SAME payload walker as the redactor (``payload_retains_content``
        ↔ ``redact_record``) so the proof cannot share a blind spot with the
        erasure. An unverifiable vector backend and an ephemeral (unpersisted)
        log are reported as *unproven*, never silently as clean.

        SEC-C2/ADR-018: scoped to ``namespace``. A record that still exists in
        another namespace raises the anti-oracle error — a caller must not probe
        erasure state of a namespace it does not own. (Post-hard-delete the row
        is absent, which is the normal verify path and proceeds.)
        """
        storage = self._require_started()
        existing = await storage.get_record(record_id)
        if existing is not None and existing.namespace != namespace:
            raise ConflictError(f"no such record {record_id!r} in namespace {namespace!r}")
        record_absent = existing is None
        vector_absent: bool | None = None
        exists = getattr(self._vector, "exists", None)
        if callable(exists):
            vector_absent = not await exists(record_id)
        # The lexical index (memory_fts) holds raw content when hybrid is on, so
        # erasure is not proven until it too is inspected. None => no lexical store
        # is owned (hybrid off) — nothing to erase, so it cannot block ``clean``.
        lexical_absent: bool | None = None
        if self._lexical is not None:
            lexical_absent = not await self._lexical.exists(record_id)
        log_verifiable = storage.can_rebuild  # ephemeral persists nothing to prove
        log_clean = True
        after = 0
        while log_verifiable:
            batch = await storage.read_events(after_seq=after)
            if not batch:
                break
            for event in batch:
                if payload_retains_content(event.payload, record_id):
                    log_clean = False
            assert batch[-1].seq is not None
            after = batch[-1].seq
        clean = (
            record_absent
            and log_verifiable
            and log_clean
            and vector_absent is True
            and lexical_absent is not False  # True (absent) or None (no store) both pass
        )
        return {
            "record_id": record_id,
            "record_absent": record_absent,
            "vector_absent": vector_absent,  # None => backend cannot prove it
            "lexical_absent": lexical_absent,  # None => no lexical store owned
            "log_verifiable": log_verifiable,
            "log_redacted": log_clean,
            "clean": clean,
        }

    async def audit_taint(self, record_id: str, namespace: str = "default") -> TaintReport:
        """E1 blast-radius audit: origin + every derivation, from the log.

        SEC-C3/ADR-018: scoped to ``namespace``. The seed record must belong to
        the caller's namespace; a foreign or unknown seed raises the ADR-014
        anti-oracle error. The taint WALK itself is not weakened — derivation
        edges are namespace-local post-P5/P6, so scoping the seed is sufficient.
        """
        storage = self._require_started()
        ns = validate_namespace(namespace)
        report = await trace_taint(storage, record_id)
        record = await storage.get_record(record_id)
        seed_ns = record.namespace if record is not None else report.origin_namespace
        if seed_ns != ns:
            raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
        return report

    async def _assess_write(self, record: MemoryRecord) -> FirewallVerdict:
        """Gather the firewall's namespace context: nearest-neighbour
        similarities (embedding outlier) + recent contents (MINJA prefixes).

        Quarantined rows are EXCLUDED from both signals: a cluster of similar
        poison writes must not dampen each other's outlier score or supply the
        MINJA bridge prefixes (they are held content, not namespace truth).
        """
        storage = self._require_started()
        neighbour_sims: list[float] | None = None
        if self._embedder is not None and self._vector is not None:
            [vector] = await self._embedder.embed([record.content])
            hits = await self._vector.query(
                record.namespace,
                vector,
                embedder_id=self._embedder.embedder_id,
                top_k=constants.ANOMALY_MIN_NEIGHBOURS,
            )
            neighbour_sims = []
            for hit in hits:
                neighbour = await storage.get_record(hit.record_id)
                if neighbour is not None and neighbour.quarantined:
                    continue
                neighbour_sims.append(hit.score)
        recent = [
            existing
            for existing in await storage.list_records(record.namespace)
            if not existing.quarantined
        ]
        recent.sort(key=lambda existing: existing.recorded_at)
        recent_contents = [existing.content for existing in recent[-50:]]
        return self._firewall.assess(
            record, neighbour_similarities=neighbour_sims, recent_contents=recent_contents
        )

    async def _gated_assess(self, record: MemoryRecord) -> MemoryRecord:
        """The full firewall gate as an injectable seam (E1): resource ingest
        and reflections get the SAME anomaly context as Engine.write — a
        context-free assess would silently disable the embedding-outlier and
        MINJA defenses on exactly the RAG-poisoning surface."""
        verdict = await self._assess_write(record)
        if verdict.quarantine:
            # Same loud signal as _write_locked — a quarantined ingest chunk or
            # reflection must never look like a normal write in the logs.
            _log.warning(
                "memory.quarantined",
                namespace=record.namespace,
                record_id=record.record_id,
                reasons=verdict.reasons,
            )
        return verdict.apply(record)

    async def _corroborate(self, namespace: str, incoming: MemoryRecord) -> None:
        """E1 promotion path: a trusted write that independently asserts what a
        quarantined record claims counts as corroboration; enough of them
        activate the record (through the door, as a delta).

        Independence is the security property (E1): the corroborator must be a
        *different, trusted* source than the quarantined write — a poison's own
        author (or any low-trust/external channel) can never vote itself out of
        quarantine. Because external channels are capped below the promotion
        floor, an attacker confined to retrieved/web/tool input cannot
        corroborate at all.
        """
        # Only genuinely privileged roles corroborate: operator/system/user.
        # assistant/tool (LLM- or tool-authored, the indirect-injection surface)
        # are excluded even on internal channels — an injection-influenced
        # assistant must not vote poison out of quarantine (empirically found).
        if (
            incoming.trust < constants.TRUST_DEFAULT
            or incoming.quarantined
            or incoming.source.role not in _CORROBORATION_ROLES
        ):
            return
        storage = self._require_started()
        for held in await storage.list_records(namespace):
            if not held.quarantined or held.status is not RecordStatus.QUARANTINED:
                continue
            # Independence: a record cannot corroborate itself, and neither can
            # a write from the identical source signature (same role+channel+
            # message) — that is a replay, not an independent second opinion.
            if incoming.record_id == held.record_id:
                continue
            if (
                incoming.source.role == held.source.role
                and incoming.source.channel == held.source.channel
                and incoming.source.message_id == held.source.message_id
            ):
                continue
            same_content = held.content_fingerprint == incoming.content_fingerprint
            # (entity, attribute) keys are only comparable within one memory
            # type — a semantic fact keyed ("release", "skill") must never
            # corroborate a quarantined *procedural* skill of the same name.
            same_fact = (
                held.memory_type == incoming.memory_type
                and held.entity is not None
                and held.entity == incoming.entity
                and held.attribute == incoming.attribute
            )
            if not (same_content or same_fact):
                continue
            count = held.corroborations + 1
            change: dict[str, object] = {"corroborations": count}
            promoted = self._firewall.policy.may_promote(
                held.model_copy(update={"corroborations": count})
            )
            if promoted:
                change["quarantined"] = False
                if held.memory_type == "procedural" and held.skill_stage is not None:
                    # M13.4: corroboration only lifts the quarantine — it must
                    # never skip the ladder. The record resumes the status its
                    # stage implies (RESOLVING pre-active), and still has to be
                    # promoted through verified + the dry-run gate to surface.
                    change["status"] = stage_status(held.skill_stage).value
                elif held.memory_type == "semantic":
                    # If the corroborators themselves established an active fact
                    # on the same key, the promoted record joins the history as
                    # its corroborated predecessor — never a second active fact.
                    incumbent = None
                    if held.entity is not None and held.attribute is not None:
                        incumbent = await storage.find_active_fact(
                            namespace, held.entity, held.attribute
                        )
                    if incumbent is not None and incumbent.record_id != held.record_id:
                        change["status"] = RecordStatus.ARCHIVED.value
                        change["evolve_to"] = incumbent.record_id
                        # Clamp so a held record newer than the incumbent never
                        # gets an inverted (valid_to < valid_from) interval.
                        close_at = max(held.valid_from, incumbent.valid_from)
                        change["valid_to"] = close_at.isoformat()
                    else:
                        change["status"] = RecordStatus.ACTIVATED.value
                else:
                    # Non-fact types (episodic, prospective watches, …): the
                    # single-active-fact invariant is semantic-only (ADR-016) —
                    # a semantic incumbent must never archive e.g. a watch that
                    # merely reuses the key columns as its watched target.
                    change["status"] = RecordStatus.ACTIVATED.value
            await self._append_and_project(
                MemoryEvent(
                    kind=EventKind.DECAY_TRANSITION,
                    namespace=namespace,
                    actor="system",
                    payload={
                        "record_id": held.record_id,
                        "set": change,
                        "transition": "quarantined->activated" if promoted else "corroborated",
                        "reason": "quarantine_promoted" if promoted else "corroborated",
                    },
                )
            )
            _log.info(
                "memory.quarantine_promoted" if promoted else "memory.corroborated",
                namespace=namespace,
                record_id=held.record_id,
                corroborations=count,
            )

    def _config(self) -> MemspineConfig:
        assert self._resolved is not None
        return self._resolved.config

    def _rerank_provider(self) -> Reranker | None:
        """The E8 reranker for the configured mode (D-51), constructed lazily
        on first use (cross-encoder weights must not load at engine start).
        An unavailable adapter is skip-logged ONCE and the stage disables
        itself — retrieval quality degrades, retrieval never fails."""
        mode = self._config().read.rerank
        if mode == "off" or self._rerank_unavailable:
            return None if mode == "off" else self._reranker
        if self._reranker is not None:
            return self._reranker
        try:
            if mode == "fastembed":
                from memspine.services.rerank.fastembed_rerank import FastembedReranker

                self._reranker = FastembedReranker()
            elif mode == "flashrank":
                from memspine.services.rerank.flashrank_rerank import FlashrankReranker

                self._reranker = FlashrankReranker()
        except Exception as exc:
            # COR-3/ADR-018: not just ImportError/MissingServiceError — a
            # cross-encoder constructor can raise OSError (model download),
            # ValueError, RuntimeError (weight load), etc. ANY construction
            # failure self-disables the stage (matching the per-call .rerank()
            # guard); retrieval degrades to vector order, never crashes.
            self._rerank_unavailable = True
            _log.info(
                "rerank.unavailable",
                provider=mode,
                detail=f"E8 rerank stage skipped — {exc}",
            )
        return self._reranker

    async def timeline(
        self,
        namespace: str = "default",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MemoryRecord]:
        """Chronological episodic timeline (M13.2), optionally windowed."""
        self._require_started()
        if self._episodic is None:
            raise MemspineError("episodic memory not enabled — set memories.episodic.enabled: true")
        ns = validate_namespace(namespace)
        return self._inflate_all(await self._episodic.timeline(ns, start, end), ns)

    async def sessions(
        self, namespace: str = "default", gap_minutes: int = constants.SESSION_GAP_MINUTES
    ) -> list[Session]:
        """Derived session boundaries over the episodic timeline (M13.2)."""
        self._require_started()
        if self._episodic is None:
            raise MemspineError("episodic memory not enabled — set memories.episodic.enabled: true")
        return await self._episodic.sessions(validate_namespace(namespace), gap_minutes)

    async def ingest(
        self,
        path: str | Path,
        namespace: str = "default",
        pii_tier: PiiTier = PiiTier.NONE,
    ) -> list[MemoryRecord]:
        """Ingest a document (D-29): extract → chunk → resource records."""
        self._require_started()
        if self._resource is None:
            raise MemspineError("resource memory not enabled — set memories.resource.enabled: true")
        ns = validate_namespace(namespace)
        # Same one-writer-per-namespace unit as write()/forget(): ingest's
        # firewall-context reads and chunk writes must not interleave with a
        # concurrent hard delete's read-hold-check-redact sequence.
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await self._resource.ingest(ns, path, pii_tier=pii_tier)

    # ── procedural + reflective verbs (P5: M13.4 / M13.7 / E6) ───────────────

    async def add_skill(
        self,
        content: str,
        name: str,
        namespace: str = "default",
        actor: str = "user",
        source: SourceInfo | None = None,
    ) -> MemoryRecord:
        """Store a skill at ``draft`` (M13.4). It must climb the ladder —
        staged → verified → dry-run-gated active — before it is ever offered."""
        return await self._write_procedural(
            make_skill_record(
                validate_namespace(namespace),
                name,
                content,
                kind="skill",
                source=source or SourceInfo(role=actor),
            ),
            actor,
        )

    async def record_plan(
        self,
        task: str,
        content: str,
        namespace: str = "default",
        actor: str = "assistant",
        source: SourceInfo | None = None,
    ) -> MemoryRecord:
        """E6: capture a validated multi-step plan after a task SUCCEEDED.

        Plans enter at ``staged`` (success was the first validation) and are
        held out of every retrieval surface — like E1 quarantine — until they
        are promoted through ``verified`` and the dry-run gate into ``active``.
        """
        return await self._write_procedural(
            make_skill_record(
                validate_namespace(namespace),
                task,
                content,
                kind="plan",
                source=source or SourceInfo(role=actor),
            ),
            actor,
        )

    async def _write_procedural(self, record: MemoryRecord, actor: str) -> MemoryRecord:
        storage = self._require_started()
        if self._procedural is None:
            raise MemspineError(
                "procedural memory not enabled — set memories.procedural.enabled: true"
            )
        ns = record.namespace
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await self._write_locked(storage, ns, record, "procedural", actor)

    async def promote_skill(
        self,
        record_id: str,
        namespace: str = "default",
        dry_run_passed: bool = False,
    ) -> MemoryRecord:
        """One legal step up draft→staged→verified→active; verified→active
        requires ``dry_run_passed=True`` (M13.4)."""
        self._require_started()
        if self._procedural is None:
            raise MemspineError(
                "procedural memory not enabled — set memories.procedural.enabled: true"
            )
        ns = validate_namespace(namespace)
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await self._procedural.promote(
                record_id, namespace=ns, dry_run_passed=dry_run_passed
            )

    async def deprecate_skill(self, record_id: str, namespace: str = "default") -> MemoryRecord:
        """Retire a skill/plan (terminal, M13.4)."""
        self._require_started()
        if self._procedural is None:
            raise MemspineError(
                "procedural memory not enabled — set memories.procedural.enabled: true"
            )
        ns = validate_namespace(namespace)
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await self._procedural.deprecate(record_id, namespace=ns)

    async def skills(
        self,
        namespace: str = "default",
        kind: str | None = None,
        usable_only: bool = True,
    ) -> list[MemoryRecord]:
        """Procedural records; by default only the usable (ACTIVE) set (M13.4)."""
        self._require_started()
        if self._procedural is None:
            raise MemspineError(
                "procedural memory not enabled — set memories.procedural.enabled: true"
            )
        ns = validate_namespace(namespace)
        return self._inflate_all(
            await self._procedural.list(ns, kind=kind, usable_only=usable_only), ns
        )

    async def recall_plan(
        self,
        task: str,
        namespace: str = "default",
        min_similarity: float = constants.PLAN_RECALL_MIN_SIMILARITY,
    ) -> MemoryRecord | None:
        """E6 plan cache lookup: the usable plan whose *task* embedding is most
        similar to ``task`` — or None when nothing clears the floor.

        Similarity is computed over the stored plans' task strings (their
        ``entity``), not plan bodies: two different tasks can share plan steps
        without being interchangeable.
        """
        self._require_started()
        if self._procedural is None:
            raise MemspineError(
                "procedural memory not enabled — set memories.procedural.enabled: true"
            )
        if self._embedder is None:
            raise MemspineError("retrieval services not constructed — engine not started?")
        ns = validate_namespace(namespace)
        plans = [
            plan
            for plan in await self._procedural.list(ns, kind="plan", usable_only=True)
            if plan.entity is not None
        ]
        if not plans:
            # Misses must be visible: "no plans recorded" vs "none cleared the
            # floor" is the first question when plan reuse isn't happening.
            _log.info(EVENT_RETRIEVE, namespace=ns, plan=True, hit=False, candidates=0)
            return None
        vectors = await self._embedder.embed([task, *(str(plan.entity) for plan in plans)])
        task_vec, plan_vecs = vectors[0], vectors[1:]
        best: tuple[MemoryRecord, float] | None = None
        for plan, plan_vec in zip(plans, plan_vecs, strict=True):
            score = _cosine(task_vec, plan_vec)
            if best is None or score > best[1]:
                best = (plan, score)
        assert best is not None
        if best[1] < min_similarity:
            _log.info(
                EVENT_RETRIEVE,
                namespace=ns,
                plan=True,
                hit=False,
                candidates=len(plans),
                best_similarity=round(best[1], 4),
            )
            return None
        record = self._inflate.inflate(best[0])
        # Reinforcement (M1): a reused plan's utility signal rides the log.
        await self._append_and_project(
            MemoryEvent(
                kind=EventKind.RETRIEVE,
                namespace=ns,
                actor="system",
                payload={"record_ids": [record.record_id]},
            )
        )
        _log.info(EVENT_RETRIEVE, namespace=ns, plan=True, similarity=round(best[1], 4))
        return record

    async def reflect(
        self,
        content: str,
        source_record_ids: list[str],
        namespace: str = "default",
        actor: str = "assistant",
        source: SourceInfo | None = None,
    ) -> MemoryRecord:
        """Write a reflection derived from existing records (M13.7).

        Depth is computed from the fetched parents and hard-capped at 2;
        quarantined/deleted parents and parents outside ``namespace`` are
        refused (no laundering, no tenant crossing — E1). Reflection content
        is caller-authored, so it carries the caller's role (never a blanket
        "system") and its trust is capped at the least-trusted parent.
        """
        self._require_started()
        if self._reflective is None:
            raise MemspineError(
                "reflective memory not enabled — set memories.reflective.enabled: true"
            )
        ns = validate_namespace(namespace)
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await self._reflective.reflect(
                ns,
                content,
                source_record_ids,
                source=source or SourceInfo(role=actor, channel="reflection"),
            )

    # ── associative verbs (P6: M13.6 / D-40 / ADR-015) ───────────────────────

    async def associate(
        self,
        src_id: str,
        dst_id: str,
        namespace: str = "default",
        rel: str = "related",
        weight: float = 1.0,
    ) -> None:
        """Link two records (M13.6): a budget-checked LINK event through the
        door (ADR-015); the graph projection materializes the edge. Both
        records must live in ``namespace`` — cross-namespace links refused."""
        self._require_started()
        if self._associative is None:
            raise MemspineError(
                "associative memory not enabled — set memories.associative.enabled: true"
            )
        ns = validate_namespace(namespace)
        # Same one-writer-per-namespace unit as write(): the budget read and
        # the LINK append must not interleave with a concurrent linker.
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            await self._associative.link(ns, src_id, dst_id, rel=rel, weight=weight)

    async def related(
        self, record_id: str, namespace: str = "default", k: int = 10
    ) -> list[MemoryRecord]:
        """Records associated with ``record_id`` via personalized PageRank over
        the link graph (D-40). Same E1 gate as ``search``: only ACTIVATED,
        never quarantined, never cross-namespace."""
        self._require_started()
        if self._associative is None:
            raise MemspineError(
                "associative memory not enabled — set memories.associative.enabled: true"
            )
        ns = validate_namespace(namespace)
        return self._inflate_all(await self._associative.related(ns, record_id, k=k), ns)

    # ── prospective + shared verbs (P7: M13.8 / R2 / ADR-016) ────────────────

    async def watch(
        self,
        content: str,
        namespace: str = "default",
        due_at: datetime | None = None,
        entity: str | None = None,
        attribute: str | None = None,
        actor: str = "user",
        source: SourceInfo | None = None,
    ) -> MemoryRecord:
        """Store a prospective watch (M13.8): ``content`` is what to do or
        remember when it fires — at ``due_at`` (bi-temporal reuse: the due
        time rides ``valid_from``) OR when the watched ``entity``/``attribute``
        fact key is invalidated by the M4 conflict ladder.

        A watch is a write like any other: it enters through the firewall
        gate (E1) — instruction-shaped watch content is quarantined and can
        never fire.
        """
        storage = self._require_started()
        self._require_prospective()  # enablement check — the write rides the normal door
        ns = validate_namespace(namespace)
        record = make_watch_record(
            ns,
            content,
            due_at=due_at,
            entity=entity,
            attribute=attribute,
            source=source or SourceInfo(role=actor),
        )
        # SF-1/ADR-018: an invalidation (target) watch reads M4 CONFLICT events
        # to fire; in ephemeral mode nothing is persisted to read, so it can
        # never fire. This is documented in ADR-016 but was runtime-silent —
        # warn once (the engine knows the mode; the pure builder does not).
        if (
            due_at is None
            and entity is not None
            and not self._ephemeral_watch_warned
            and self._config().event_log.mode is EventLogMode.EPHEMERAL
        ):
            self._ephemeral_watch_warned = True
            _log.warning(
                "prospective.ephemeral_invalidation_never_fires",
                namespace=ns,
                detail="event_log.mode=ephemeral persists no CONFLICT events — "
                "invalidation (target) watches can never fire (ADR-016); "
                "due-time watches are unaffected",
            )
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await self._write_locked(storage, ns, record, "prospective", actor)

    async def due(
        self, namespace: str = "default", now: datetime | None = None
    ) -> list[MemoryRecord]:
        """Fired-but-unacknowledged watches (M13.8): due time reached or the
        watched fact key invalidated. Read-only and pull-based (ADR-016) —
        a fired watch stays here until ``acknowledge_watch`` archives it.
        ``now`` defaults to the current UTC instant; pass it explicitly to
        own time (tests always should)."""
        self._require_started()
        prospective = self._require_prospective()
        ns = validate_namespace(namespace)
        if now is None:
            now = datetime.now(UTC)
        elif now.tzinfo is None:
            # COR-1/ADR-018: watch due times are tz-aware (make_watch_record
            # rejects naive). Comparing an aware valid_from to a naive `now`
            # raises deep in the trigger — reject it loudly at the boundary.
            raise ConflictError("now must be timezone-aware (naive datetimes are ambiguous)")
        fired = await prospective.pending(ns, now)
        return self._inflate_all(fired, ns)

    async def acknowledge_watch(self, record_id: str, namespace: str = "default") -> MemoryRecord:
        """Acknowledge a fired watch: archived via a delta event
        (``reason="watch_fired"``), idempotent (M13.8)."""
        self._require_started()
        prospective = self._require_prospective()
        ns = validate_namespace(namespace)
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await prospective.acknowledge(record_id, namespace=ns)

    async def grant(
        self,
        to_namespace: str,
        namespace: str = "default",
        memory_types: list[str] | None = None,
        actor: str = "user",
    ) -> MemoryRecord:
        """Grant ``to_namespace`` read access to ``namespace``'s records (R2),
        optionally scoped to ``memory_types``. The grant record rides the log
        (D0.1); enforcement lives in ``core.namespace.grant_allows``. Grant
        content is engine-built canonical JSON — the stated firewall
        exemption shared with prompt sync (ADR-014 §2/ADR-016)."""
        self._require_started()
        shared = self._require_shared()
        grantor = validate_namespace(namespace)
        grantee = validate_namespace(to_namespace)
        # Write lock on the GRANTOR namespace: the read-diff-append unit must
        # not interleave with a concurrent grant/revoke or forget cascade.
        async with self._write_locks.setdefault(grantor, asyncio.Lock()):
            return await shared.grant(
                grantor,
                grantee,
                memory_types=memory_types,
                source=SourceInfo(role=actor, channel="grant"),
            )

    async def revoke(self, to_namespace: str, namespace: str = "default") -> MemoryRecord:
        """Revoke ``to_namespace``'s read access to ``namespace`` (R2): the
        grant record is archived via a delta event; raises when no grant is
        live (a typo'd grantee must not read as success)."""
        self._require_started()
        shared = self._require_shared()
        grantor = validate_namespace(namespace)
        grantee = validate_namespace(to_namespace)
        async with self._write_locks.setdefault(grantor, asyncio.Lock()):
            return await shared.revoke(grantor, grantee)

    async def shared_search(
        self,
        query: str,
        namespace: str = "default",
        top_k: int = constants.SEARCH_TOP_K,
    ) -> list[tuple[MemoryRecord, float]]:
        """Own-namespace search plus granted foreign results (R2/E1).

        Foreign records are LIVE VIEWS into the grantor namespace — never
        copied (no second source of truth) — and are clearly marked by their
        differing ``record.namespace``. Their trust is capped at
        ``TRUST_RETRIEVED_CAP``: content crossing a grant is foreign, and its
        home-namespace trust must never ride along (E1). Quarantined or
        non-ACTIVATED records never cross, ``shared`` bookkeeping records
        never cross, and foreign reads append NO events — a reader must not
        mutate grantor state (no reinforcement across the boundary).
        """
        storage = self._require_started()
        shared = self._require_shared()
        if self._embedder is None or self._vector is None or self._scoring is None:
            raise MemspineError("retrieval services not constructed — engine not started?")
        ns = validate_namespace(namespace)
        results = await self.search(query, namespace=ns, top_k=top_k)
        grants = await shared.grants_to(ns)
        if not grants:
            return results
        [query_vector] = await self._embedder.embed([query])
        for grantor in sorted(grants):
            # SF-7/ADR-018: one grantor's broken vector index must not sink the
            # reader's own results or the other grantors' — contain per grantor.
            try:
                hits = await self._vector.query(
                    grantor, query_vector, embedder_id=self._embedder.embedder_id, top_k=top_k
                )
                for hit in hits:
                    record = await storage.get_record(hit.record_id)
                    if (
                        record is None
                        or record.namespace != grantor
                        or record.status is not RecordStatus.ACTIVATED
                        or record.quarantined
                    ):
                        continue  # same E1 gate as search — held content never crosses
                    # The ONE enforcement point (ADR-016): scope + bookkeeping gate.
                    if not grant_allows(ns, record.namespace, record.memory_type, grants):
                        continue
                    try:
                        record = self._inflate.inflate(record)
                    except StorageError:
                        _log.warning(
                            "memory.inflate_failed", namespace=grantor, record_id=hit.record_id
                        )
                        continue
                    # E1: foreign content is retrieved content — trust-capped,
                    # never its home-namespace trust.
                    record = record.model_copy(
                        update={"trust": min(record.trust, constants.TRUST_RETRIEVED_CAP)}
                    )
                    results.append(
                        (record, self._scoring.composite_score(record, relevance=hit.score))
                    )
            except Exception as exc:
                _log.warning(
                    "shared_search.grantor_failed",
                    namespace=ns,
                    grantor=grantor,
                    error=str(exc),
                    exc_info=True,
                )
        results.sort(key=lambda pair: pair[1], reverse=True)
        # COR-2/ADR-018: the per-grantor loop appended up to top_k EACH — a final
        # truncation keeps the contract that shared_search returns at most top_k.
        results = results[:top_k]
        _log.info(
            EVENT_RETRIEVE, namespace=ns, shared=True, grantors=sorted(grants), count=len(results)
        )
        return results

    async def subscribe(
        self,
        query: str,
        namespace: str = "default",
        actor: str = "user",
        source: SourceInfo | None = None,
    ) -> MemoryRecord:
        """Store a standing query (ADR-016, v0.1 minimal). Caller free text —
        it enters through the firewall gate like any write. Pull-based: feed
        ``record.content`` to ``shared_search`` yourself; push delivery is
        deferred to the taskiq build."""
        storage = self._require_started()
        self._require_shared()
        ns = validate_namespace(namespace)
        record = make_subscription_record(ns, query, source=source or SourceInfo(role=actor))
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            return await self._write_locked(storage, ns, record, "shared", actor)

    async def subscriptions(self, namespace: str = "default") -> list[MemoryRecord]:
        """Live standing-query records in ``namespace`` (ADR-016)."""
        self._require_started()
        shared = self._require_shared()
        ns = validate_namespace(namespace)
        return self._inflate_all(await shared.subscriptions(ns), ns)

    async def grants_from(self, namespace: str = "default") -> list[Grant]:
        """The live grants ``namespace`` has issued (operator listing surface,
        R2/ADR-016). Scoped to the grantor — a caller lists only its OWN
        grants, never another namespace's."""
        self._require_started()
        shared = self._require_shared()
        grantor = validate_namespace(namespace)
        return await shared.grants_from(grantor)

    def _require_prospective(self) -> ProspectiveMemory:
        if self._prospective is None:
            raise MemspineError(
                "prospective memory not enabled — set memories.prospective.enabled: true"
            )
        return self._prospective

    def _require_shared(self) -> SharedMemory:
        if self._shared is None:
            raise MemspineError("shared memory not enabled — set memories.shared.enabled: true")
        return self._shared

    async def _evolve_links(self, ns: str, record: MemoryRecord) -> None:
        """Bounded A-MEM hook (D-42/ADR-015): after a non-quarantined semantic/
        episodic write, propose links to the vector neighbourhood. Best-effort:
        a failure is logged loudly (never raised) — an auto-link must not fail
        the write it decorates."""
        if (
            self._associative is None
            or record.quarantined
            or record.memory_type not in ("semantic", "episodic")
        ):
            return
        assert self._graph is not None and self._storage is not None
        if self._embedder is None or self._vector is None:
            return
        try:
            [vector] = await self._embedder.embed([record.content])
            hits = await self._vector.query(
                ns, vector, embedder_id=self._embedder.embedder_id, top_k=constants.SEARCH_TOP_K
            )
            created = await propose_links(
                namespace=ns,
                record=record,
                hits=hits,
                storage=self._storage,
                graph=self._graph,
                append_event=self._append_and_project,
            )
            if created:
                _log.info(
                    EVENT_LINK,
                    namespace=ns,
                    record_id=record.record_id,
                    links=created,
                    reason="evolution",
                )
        except Exception as exc:
            _log.warning(
                "associative.evolution_failed",
                namespace=ns,
                record_id=record.record_id,
                error=str(exc),
                error_kind=exc.__class__.__name__,
                exc_info=True,
            )

    async def sync_prompt_versions(self, namespace: str = "default") -> list[MemoryRecord]:
        """D-43 §4: record each resolved prompt version as a procedural record
        (reference only — the definition lives in prompts/). Idempotent."""
        self._require_started()
        if self._procedural is None or self._prompts is None:
            raise MemspineError(
                "prompt-version sync needs procedural memory enabled and prompts loaded"
            )
        ns = validate_namespace(namespace)
        # Same one-writer-per-namespace unit as every other write verb: the
        # read-diff-append sequence below must not interleave with itself
        # (double-sync duplicating versions) or with corroboration's
        # read-modify-write. Firewall exemption: content is deterministic,
        # system-generated reference strings — see ADR-014.
        async with self._write_locks.setdefault(ns, asyncio.Lock()):
            existing = await self._procedural.list(ns, kind="prompt")
            fresh = prompt_version_records(ns, self._prompts, existing)
            for record in fresh:
                await self._append_and_project(
                    MemoryEvent(
                        kind=EventKind.WRITE,
                        namespace=ns,
                        actor="system",
                        payload={"record": record.model_dump(mode="json")},
                    )
                )
        if fresh:
            _log.info(EVENT_WRITE, namespace=ns, prompt_versions=len(fresh))
        return fresh

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
        stats = await run_sleep_cycle(self._runner, self._pipeline_ctx())
        degraded = {
            name: stage
            for name, stage in stats.items()
            if stage.get("status") in ("error", "partial")
        }
        if degraded:
            # Callers rarely inspect the stats dict — degradation must be loud.
            _log.warning("memory.sleep_degraded", failures=degraded)
        return stats

    def _inflate_all(self, records: list[MemoryRecord], namespace: str) -> list[MemoryRecord]:
        """Inflate cold-tier content, skipping (loudly) any corrupt row rather
        than failing the entire read (blast-radius containment).

        Quarantined rows are NOT filtered here by design: ``retrieve()`` is the
        operator listing/audit surface, so held content stays inspectable.
        Model-facing paths (``search``/``assemble``/timeline/sessions) apply
        the E1 quarantine gate themselves — never feed ``retrieve()`` output
        to a context window."""
        inflated: list[MemoryRecord] = []
        for record in records:
            if record.status is RecordStatus.DELETED:
                continue
            try:
                inflated.append(self._inflate.inflate(record))
            except StorageError:
                _log.warning(
                    "memory.inflate_failed", namespace=namespace, record_id=record.record_id
                )
        return inflated

    async def rebuild(self) -> dict[str, int]:
        """Rebuild every projector from seq 0 (D0.1). Raises off-window (D-45)."""
        storage = self._require_started()
        counts = {
            projector.name: await replay_rebuild(storage, projector)
            for projector in self._projectors
        }
        if self._semantic is not None:
            self._semantic.invalidate_index()  # LSH state rebuilt from fresh rows
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
            "graph": type(self._graph).__name__ if self._graph else None,
            "llm_roles": self._llm.roles if self._llm else [],
            "prompts": (
                {p.id: p.prompt_version for p in self._prompts.list()} if self._prompts else {}
            ),
            "semantic_pipeline": self._semantic is not None,
            "firewall": "deterministic (trust-matrix + instruction-flag + anomaly)",
            "episodic": self._episodic is not None,
            "resource_ingest": self._resource is not None,
            "procedural": self._procedural is not None,
            "reflective": self._reflective is not None,
            "associative": self._associative is not None,
            "prospective": self._prospective is not None,
            "shared": self._shared is not None,
            "consolidation_summarizer": "llm" if self._summarize is not None else "extractive",
            "projectors": [projector.name for projector in self._projectors],
            "runner": config.workers.runner,
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

    def _namespace_lock(self, namespace: str) -> asyncio.Lock:
        """The same per-namespace lock every write verb holds — handed to
        pipelines so their read-then-write units serialize with forget (M5)."""
        return self._write_locks.setdefault(namespace, asyncio.Lock())

    def _pipeline_ctx(self) -> PipelineContext:
        assert self._storage is not None and self._resolved is not None
        return PipelineContext(
            storage=self._storage,
            config=self._resolved.config,
            append_event=self._append_and_project,
            summarize=self._summarize,
            # Only when associative projects it (ADR-015): an explicit-config
            # graph store without the projector would reorganize a stale graph.
            graph=self._graph if self._associative is not None else None,
            lock=self._namespace_lock,
        )

    def _build_runner(self, config: MemspineConfig) -> TaskRunner:
        if config.workers.runner == "inline":
            return InlineRunner()
        if config.workers.runner == "dbos":
            from memspine.workers.dbos_runner import DBOSRunner

            return DBOSRunner()
        if config.workers.runner == "taskiq":
            from memspine.workers.taskiq_runner import TaskiqRunner

            return TaskiqRunner(url=config.workers.broker_url)
        raise ConfigError(
            f"unknown workers.runner {config.workers.runner!r} (valid: inline, dbos, taskiq)"
        )

    def _build_summarize(self) -> Summarize | None:
        """The consolidation summarizer (M2): only when a ``summarize`` LLM role
        is bound; pipelines fall back to the deterministic extractive path."""
        if self._llm is None or self._prompts is None or "summarize" not in self._llm.roles:
            return None
        llm = self._llm.for_role("summarize")
        prompt = self._prompts.for_role("summarize")

        async def summarize(content: str) -> str:
            return await llm.chat(prompt.render({"content": content}))

        return summarize

    @staticmethod
    def _memory_policy(config: MemspineConfig, memory_type: str) -> dict[str, Any]:
        mem = config.memories.get(memory_type)
        return dict(mem.policies) if mem is not None else {}

    def _build_extractor(self, config: MemspineConfig) -> EntityExtractor | None:
        """Entity extraction provider (D-28): off (default) | llm | gliner."""
        mode = str(self._memory_policy(config, "semantic").get("entity_extraction", "off"))
        if mode == "off":
            return None
        if mode == "llm":
            assert self._prompts is not None and self._llm is not None
            # E3 extraction cache: keyed by (prompt version x content hash), so
            # a prompt upgrade cleanly invalidates (N7).
            return CachedExtractor(
                LLMEntityExtractor(
                    self._llm.for_role("extract"), self._prompts.for_role("extract")
                ),
                MemoryKV(),
            )
        if mode == "gliner":
            from memspine.memories.semantic.entities import GlinerEntityExtractor

            return GlinerEntityExtractor()
        raise ConfigError(
            f"unknown memories.semantic.policies.entity_extraction {mode!r} "
            "(valid: off, llm, gliner)"
        )

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

    async def _build_vector_store(self, config: MemspineConfig) -> VectorStore:
        assert self._client is not None and self._embedder is not None
        backend = config.vector.backend
        if backend == "auto":
            try:
                import lancedb  # noqa: F401

                backend = "lance"
            except ImportError:
                backend = "sqlite"
        # A projection must never outlive its log (D0.1): with a throwaway
        # ':memory:' log, a durable on-disk Lance table would accumulate ghost
        # rows across runs — force the same-lifetime SQLite store instead.
        if backend == "lance" and config.storage.path == ":memory:":
            _log.warning(
                "vector.lance_downgraded_to_sqlite",
                detail="storage.path=':memory:' — a durable Lance projection would "
                "outlive the in-memory event log (ghost rows on restart)",
            )
            backend = "sqlite"
        if backend == "sqlite":
            return SQLiteVectorStore(self._client)
        if backend == "lance":
            from memspine.services.vector.lancedb_store import LanceDBVectorStore

            self._lance = LanceDBClient(f"{config.storage.path}.lance")
            await self._lance.connect()
            return LanceDBVectorStore(self._lance, self._embedder)
        raise ConfigError(
            f"unknown vector.backend {config.vector.backend!r} (valid: auto, lance, sqlite)"
        )

    async def _build_graph_store(self, config: MemspineConfig) -> GraphStore:
        """Graph provider selection (D-26). ``sqlite_adjacency`` (default) rides
        the existing SQLite client; ``kuzu`` opens its own embedded database via
        a dedicated client (D-24) — in-memory when the event log is in-memory,
        so the projection can never outlive its log (D0.1)."""
        provider = config.graph.provider
        if provider == "sqlite_adjacency":
            assert self._client is not None
            return SQLiteAdjacencyGraph(self._client)
        if provider == "kuzu":
            from memspine.services.graph.kuzu import KuzuGraphStore

            path = ":memory:" if self._client_is_memory(config) else f"{config.storage.path}.kuzu"
            self._kuzu = KuzuClient(path)
            await self._kuzu.connect()
            return KuzuGraphStore(self._kuzu)
        if provider == "ladybug":
            from memspine.services.graph.ladybug import LadybugGraphStore

            # Stub: the constructor always raises the D-10 error naming
            # [graph]; the cast documents that no value ever escapes here.
            return cast("GraphStore", LadybugGraphStore())
        if provider == "neo4j":
            from memspine.services.graph.neo4j import Neo4jGraphStore

            return cast("GraphStore", Neo4jGraphStore())  # stub — always raises
        raise ConfigError(
            f"unknown graph.provider {provider!r} (valid: sqlite_adjacency, kuzu, ladybug, neo4j)"
        )

    @staticmethod
    def _client_is_memory(config: MemspineConfig) -> bool:
        return config.storage.path == ":memory:"

    async def _build_llm_router(self, config: MemspineConfig) -> LLMRouter:
        providers: dict[str, LLMService] = {}
        has_openai_role = any(
            role_config.provider == "openai_compat" for role_config in config.llm.roles.values()
        )
        if has_openai_role:
            # One shared pool; each provider enforces its own per-role timeout
            # per request (a role's 5s fail-fast must not inherit chat's 300s).
            self._http = HTTPClient()
            await self._http.connect()
        for role, role_config in config.llm.roles.items():
            if role_config.provider == "openai_compat":
                assert self._http is not None
                providers[role] = OpenAICompatLLM(
                    self._http,
                    base_url=role_config.base_url,
                    model=role_config.model,
                    api_key=role_config.api_key,
                    timeout_seconds=role_config.timeout_seconds,
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
