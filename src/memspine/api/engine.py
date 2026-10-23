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
import contextlib
import dataclasses
import functools
import os
import threading
import uuid
from collections import Counter
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar

import orjson
from dishka import AsyncContainer
from sqlalchemy import Engine as SyncSqlEngine

from memspine.api import admin, handle, ops_advisory, ops_memory, ops_trust
from memspine.api.handle import Memory
from memspine.compose.answer import final_answer
from memspine.compose.compression import CompressionPolicy
from memspine.compose.pack import (
    AssembledContext,
    AssemblyPolicy,
)
from memspine.compose.pipeline import answer as compose_answer
from memspine.compose.pipeline import pack as compose_pack
from memspine.compose.pipeline import render as compose_render
from memspine.compose.pipeline import sections as compose_sections
from memspine.compose.pipeline import windows as compose_windows
from memspine.compose.pipeline.state import AnswerVerification
from memspine.config import constants
from memspine.config.loader import ResolvedConfig
from memspine.config.schema import FirewallConfig, MemspineConfig
from memspine.core.correction import Correction
from memspine.core.fact_chain import (
    fact_projection_on,
)
from memspine.core.integrity import IntegrityPolicy
from memspine.core.owner_check import (
    InjectionLog,
)
from memspine.core.policies.conflict import ConflictPolicy
from memspine.core.policies.dedup import DedupPolicy
from memspine.core.policies.retention import RetentionPolicy
from memspine.core.policies.scoring import ScoringPolicy
from memspine.core.policies.trust import TrustPolicy
from memspine.core.read_filters import (
    DateBound,
)
from memspine.core.relevance_probes import Calibration
from memspine.core.vetting import VetFlag
from memspine.derive import write as derive_write
from memspine.derive.feedback import LatencyStats
from memspine.derive.pipeline import builders as derive_builders
from memspine.derive.pipeline import graph as derive_graph
from memspine.derive.pipeline import integrate as derive_integrate
from memspine.derive.pipeline import scheduler as derive_scheduler
from memspine.derive.pipeline.stages.context import (
    CalibrateEpisode,
    FindEntities,
    PipelineContext,
    PredictEpisode,
    SessionClosed,
    Summarize,
    SummarizeEntities,
    SummarizeIncremental,
)
from memspine.derive.pipeline.stages.table import PIPELINES
from memspine.derive.scheduler import DerivationScheduler
from memspine.economy import ledger as _economy_ledger
from memspine.economy.slo import timed_read
from memspine.exceptions import (
    ConfigError,
    MemspineError,
    MissingServiceError,
    StorageError,
)
from memspine.ingest import asset_describer
from memspine.ingest import assets as ingest_assets
from memspine.ingest import gateway as ingest_gateway
from memspine.ingest import windows as ingest_windows
from memspine.ingest import write as ingest_write
from memspine.ingest.message_helpers import (
    _RECALL_MARKERS,  # noqa: F401  (tests import it from here)
)
from memspine.ingest.write import (
    WriteOutcome,
    _NeighbourBatch,
)
from memspine.memories.associative.entities import (
    EntityPolicy,
)
from memspine.memories.associative.projector import GraphProjector
from memspine.memories.associative.resolution import ResolveBatch
from memspine.memories.associative.store import AssociativeMemory
from memspine.memories.episodic.lifecycle import passive_after
from memspine.memories.episodic.sessions import Session
from memspine.memories.episodic.store import EpisodicMemory
from memspine.memories.procedural.skills import (
    ProceduralMemory,
)
from memspine.memories.procedural.task_state import (
    TaskState,
)
from memspine.memories.procedural.trajectory import (
    TrajectoryStep,
)
from memspine.memories.prospective.watches import ProspectiveMemory
from memspine.memories.reflective.reflections import ReflectiveMemory
from memspine.memories.resource.store import ResourceMemory
from memspine.memories.semantic.store import SemanticMemory
from memspine.memories.semantic.write_pipeline import (
    ExtractEdges,
)
from memspine.memories.shared.grants import Grant, SharedMemory
from memspine.memories.working.manager import DEFAULT_PAGE_SIZE, WorkingMemory
from memspine.memory.pipeline import procedural as memory_procedural
from memspine.memory.pipeline import prospective as memory_prospective
from memspine.memory.pipeline import shared as memory_shared
from memspine.memory.pipeline import working as memory_working
from memspine.memory.procedural import Suggestion
from memspine.memory.prospective import Reminder
from memspine.models.encoders.query import QueryEncoder
from memspine.models.encoders.query.perspective import (
    PerspectiveOptions,
    QuestionPerspective,
)
from memspine.models.encoders.query.temporal_query import (
    LegHit,
    NamedLeg,
)
from memspine.observability import bindings as _bindings
from memspine.observability import inspect as inspect_obs
from memspine.observability import tap
from memspine.observability.logging import (
    EVENT_REBUILD,
    get_logger,
)
from memspine.observability.timers import StepTimers, timed
from memspine.ops import ledger as _ops
from memspine.ops.otel import OtelExporter
from memspine.prompts.models import (
    ReadPlan,
)
from memspine.prompts.registry import PromptRegistry
from memspine.providers.changes import ChangeCategory, ChangeStrategy, RevisionGuard, SqlChangeStore
from memspine.providers.connectors import legacy_sqlite_file
from memspine.providers.contracts import SqlAccess
from memspine.providers.control import EngineProviderChanges, ProviderChangeRuntime
from memspine.providers.decider import build_read_decider, read_decider_slot
from memspine.providers.engine_changes import build_engine_change_support
from memspine.providers.health import ProviderHealthReport, ProviderStatusService, ReadinessReport
from memspine.providers.identity import resolve_deployment
from memspine.providers.lifecycle import (
    ProviderLifecycleManager,
    ProviderResourceCleanupError,
)
from memspine.providers.projection_bindings import (
    projection_target_bindings,
    retire_graph_mirror_state,
)
from memspine.providers.recovery import CleanupQuiescenceGuard
from memspine.providers.rerank import (
    RerankSettings,
    build_reranker_result,
    validate_reranker_settings,
)
from memspine.providers.shared import (
    Lease,
    ResourceRegistry,
    path_identity,
    resource_key,
    shared_registry,
)
from memspine.providers.switching import verified_active_projection_routes
from memspine.read.contract import (
    QueryContract,
)
from memspine.read.freshness import SourceSeqTracker
from memspine.read.pipeline import deep as read_deep
from memspine.read.pipeline import freshness as read_freshness
from memspine.read.pipeline import fusion as read_fusion
from memspine.read.pipeline import gate as read_gate
from memspine.read.pipeline import legs as read_legs
from memspine.read.pipeline import lift as read_lift
from memspine.read.pipeline import request as read_request
from memspine.read.pipeline import rerank as read_rerank
from memspine.read.pipeline import router as read_router
from memspine.read.pipeline import scope_guard
from memspine.read.pipeline import timeview as read_timeview
from memspine.read.pipeline.state import (
    _READ_SNAPSHOT,
    ReadResult,
)
from memspine.read.pipeline.timeview import (
    _passive_scoped,
    _snapshot_listings,
    _snapshot_scoped,
)
from memspine.runtime import scheduler as _scheduler_rt
from memspine.runtime.container import (
    Capabilities,
    DecisionState,
    EngineSpec,
    SemanticExtractor,
    SemanticWritePipeline,
    Wiring,
    WiringHooks,
    client_is_memory,
    derived_base,
    gliner_consumers,
    lexical_projector_name,
    make_container,
    memory_policy,
    preflight_projection_bindings,
    prewarm_provider_imports,
)
from memspine.runtime.warmup import WarmState, WarmTarget, WarmupController, find_warm_hook
from memspine.services.cache.base import KVCache
from memspine.services.cache.semantic import CachedExtractor
from memspine.services.concurrency import Priority, prioritised
from memspine.services.decider.decider import Decider
from memspine.services.embedding.base import EmbeddingService
from memspine.services.llm.base import LLMRouter, LLMService
from memspine.services.rerank.base import Reranker

if TYPE_CHECKING:
    from memspine.config.schema.model import GlinerConsumer
from memspine.schemas.durable import AssertionEnvelope, OwnerScope
from memspine.schemas.durable.erasure import RetentionBoundary
from memspine.semantics.domains import topic_hook
from memspine.services.documents.base import DocumentStore
from memspine.services.graph.base import GraphStore
from memspine.services.graph.qualifiers import QualifierFilter
from memspine.services.lexical.base import LexicalHit, LexicalStore
from memspine.services.lexical.projector import LexicalProjector
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.records_port import RecordsStore
from memspine.services.vector.base import VectorHit, VectorStore
from memspine.services.vector.projector import VectorProjector
from memspine.store import side_tables as _side_tables
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.projections.projector import Projector
from memspine.store.records import (
    MemoryRecord,
    PiiTier,
    RecordStatus,
    SourceInfo,
)
from memspine.store.replay import catch_up
from memspine.store.replay import rebuild as replay_rebuild
from memspine.store.scope import validate_namespace
from memspine.trust.audit import IntegrityReport, TaintReport
from memspine.trust.erasure import redact_record
from memspine.trust.firewall import Firewall, FirewallSignals, FirewallVerdict, QueryHistory
from memspine.trust.pipeline import audit as trust_audit
from memspine.trust.pipeline import audit_v2 as trust_audit_v2
from memspine.trust.pipeline import consent as trust_consent
from memspine.trust.pipeline import erasure as trust_erasure
from memspine.trust.pipeline import erasure_v2 as trust_erasure_v2
from memspine.trust.pipeline import firewall as trust_firewall
from memspine.trust.pipeline import horizon as trust_horizon
from memspine.trust.pipeline import privacy as trust_privacy
from memspine.trust.pipeline import quarantine as trust_quarantine
from memspine.trust.pipeline import retention_v2 as trust_retention_v2
from memspine.trust.pipeline.state import (
    AuthorizeDecision,
    _as_options_dict,
)
from memspine.trust.privacy import (
    AuditChainReport,
    current_principal,
    export_line,
    export_record,
    payload_mentions,
    read_scope,
)
from memspine.trust.retention import ChainReport
from memspine.workers.list_cards import (
    LabelClasses,
    ListCard,
)
from memspine.workers.runner import TaskRunner
from memspine.workers.schedule import run_sleep_cycle
from memspine.workers.scheduler import SleepScheduler
from memspine.workers.windows import WindowPipeline

__all__ = ["Engine"]


_log = get_logger(__name__)


class _EngineTeardownError(RuntimeError):
    """Sanitized summary of cleanup steps that did not complete."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


#: R02 / I75 v2: legs that vote from the same evidence form one *source family*. A leg that
#: replays or re-filters another leg's order (the perspective leg replays the vector order by
#: subject) is not an independent vote; keyword legs and temporal legs are separate evidence.
#: The leg -> family table is the typed ``read_legs`` registry (memspine.read.registry).


_T = TypeVar("_T")

#: Services the Phase-0 engine always constructs (core install, D-03).
_CORE_SERVICES = frozenset({"storage", "secrets"})


def _build_cost_ledger(
    config: MemspineConfig, sql_engine: SyncSqlEngine | None = None
) -> _economy_ledger.WriteCostLedger:
    """W2-12: the cost ledger, with the optional budget (which needs the durable outbox, since
    paused work is kept there, marked underived)."""
    spec = config.economy.budget
    # FU-SLEEP (economy.ledger_persist); None = in-process
    store = _build_ledger_store(config, sql_engine)
    if spec is None:
        return _economy_ledger.WriteCostLedger(store=store)
    if not config.workers.outbox:
        raise ConfigError(
            "economy.budget pauses derivation by parking the work in the outbox: "
            "set workers.outbox: true (or remove economy.budget)"
        )
    return _economy_ledger.WriteCostLedger(
        budget=_economy_ledger.Budget(
            max_calls=spec.max_calls, max_tokens=spec.max_tokens, period_s=spec.period_s
        ),
        store=store,
    )


def _build_ledger_store(
    config: MemspineConfig, sql_engine: SyncSqlEngine | None
) -> _economy_ledger.LedgerStore | None:
    """FU-SLEEP (``economy.ledger_persist``): the ledger rows, in the ``cost_entry`` table of the
    storage database (RM-111 WP5: encrypted with it on SQLite, shared on Postgres; ``sql_engine``
    is its sync engine). None when the flag is off, or when there is no durable database to keep
    them in (``:memory:`` storage: logged)."""
    if not config.economy.ledger_persist:
        return None
    if sql_engine is None:
        _log.warning(
            "ledger_persist.in_memory",
            detail="no durable storage database: the cost ledger is not persisted",
        )
        return None
    return _economy_ledger.LedgerStore(sql_engine)


def _seam_target(config: MemspineConfig, engine_token: str) -> dict[str, str]:
    """RM-75: the storage identity a per-store seam (cost / ops ledger) is shared under. An
    in-memory store is unique per Engine, so its seams are too."""
    base = derived_base(config)
    return {"engine": engine_token} if base == ":memory:" else {"base": base}


class Engine:
    """Async-first facade; thin sync wrappers on the public verbs (D-01)."""

    def __init__(
        self,
        template: str | None = None,
        user_config: str | Path | dict[str, Any] | None = None,
        dotenv_path: str | Path | None = ".env",
        provider_change_runtime: ProviderChangeRuntime | None = None,
        **overrides: Any,
    ) -> None:
        self._template = template
        self._user_config = user_config
        self._dotenv_path = dotenv_path
        self._overrides = overrides
        # Deliberately programmatic and non-serializable: cleanup strategies and
        # revision guards are trusted application dependencies, not YAML config.
        self._provider_change_runtime = provider_change_runtime
        self._provider_changes_controller: EngineProviderChanges | None = None
        self._resolved: ResolvedConfig | None = None
        self._enabled: set[str] = set()
        self._auto_enabled: list[str] = []
        self._sql: SqlAccess | None = None
        self._provider_resources: ProviderLifecycleManager | None = None
        self._gliner_pool: Any | None = None
        #: RM-106: the opt-in GLiNER topic tagger (``model.extraction.topic``), set at
        #: start; None when the slot is absent.
        self._topic_tagger: Any | None = None
        self._decision: Any | None = None
        self._provider_results: dict[str, Any] = {}
        self._provider_owner_id: str | None = None
        self._reranker_build_lock = asyncio.Lock()
        #: Keyed by the resolved :class:`RerankSettings` (one build per distinct chain entry).
        self._reranker_build_results: dict[RerankSettings, Any | None] = {}
        self._reranker_build_tasks: dict[RerankSettings, asyncio.Task[Any | None]] = {}
        self._provider_tearing_down = False
        self._teardown_task: asyncio.Task[None] | None = None
        # Inside a projection batch (ingest_messages): the last seq each
        # projector applied, checkpointed when the batch flushes. None outside.
        self._batch_offsets: dict[str, int] | None = None
        # Phase 2: one shared KV cache + the optional clients backing it.
        self._cache: KVCache | None = None
        #: #43: the extraction cache, kept so erasure can purge an erased text's entry.
        self._cached_extractor: CachedExtractor | None = None
        self._storage: RecordsStore | None = None
        self._projectors: list[Projector] = []
        self._embedder: EmbeddingService | None = None
        self._vector: VectorStore | None = None
        self._lexical: LexicalStore | None = None
        self._graph: GraphStore | None = None
        #: ADR-081 ``storage.documents`` (None when the slot is not configured).
        self._documents: DocumentStore | None = None
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
        #: W1: the adaptive-window write (``write.window_extract``), None when off.
        self._windows: WindowPipeline | None = None
        self._scoring: ScoringPolicy | None = None
        self._assembly: AssemblyPolicy | None = None
        self._assembly_compression: CompressionPolicy | None = None
        self._reranker: Reranker | None = None
        self._rerank_unavailable = False
        self._rerank_calls = 0  # E8 attempts / failures, read by rerank_stats()
        self._rerank_failures = 0
        self._rerank_gated = 0  # N41: reranks whose order was not used (low confidence)
        #: N60: sentence vectors per (record id, content) for the MaxSim leg.
        self._sentence_vectors: dict[tuple[str, str], list[list[float]]] = {}
        #: read.lexical_strip_names: speaker names per namespace, keyed by record count
        self._speaker_names: dict[str, tuple[int, frozenset[str]]] = {}
        #: I39 perspective layer: per namespace, the participants seen and the last speaker
        self._persp_state: dict[str, dict[str, Any]] = {}
        #: I64: record ids injected in the last replies, per (namespace, session)
        self._injections = InjectionLog()
        #: I60: word set of each namespace's records, keyed by record count
        self._entity_vocab: dict[str, tuple[int, frozenset[str]]] = {}
        #: E04 / E03: the asset adapter and the external-evidence broker, built on first use
        self._assets: Any = None
        #: RM-113 item 18: the asset registry (records database tables), built on first use
        self._asset_rows: Any = None
        self._external: Any = None
        self._external_provider: Any = None
        self._http: Any = None  # RM-113: shared HTTP connector (see _http_connector)
        #: read.session_leg: unit vector per (record id, content)
        self._session_vectors: dict[tuple[str, str], list[float]] = {}
        # E4 (ADR-020): whether the vector leg runs the two-stage quantized
        # rescore (manifest-driven + vector.quantization override). Off => the
        # exact query() path, byte-identical to the pre-E4 pipeline.
        self._rescore_active = False
        # E4 model2vec static-embedding prefilter (opt-in, default off). The
        # embedder loads lazily; a missing [static] extra self-disables the
        # stage once (skip-log), never crashes retrieval.
        self._static_embedder: EmbeddingService | None = None
        self._static_prefilter_on = False
        self._static_unavailable = False
        # SF-1/ADR-018: latch so the "invalidation watches never fire under
        # event_log.mode=ephemeral" warning is logged at most once per engine.
        self._ephemeral_watch_warned = False
        self._naive_warned = False  # F5: the naive-datetime warning is logged once
        self._session_ticks: dict[tuple[str, str, datetime], int] = {}  # F5: session_sequence
        self._inflate: CompressionPolicy = CompressionPolicy.bind()
        self._firewall: Firewall = Firewall()
        self._query_history = QueryHistory()
        self._summarize: Summarize | None = None
        #: #61: the read-time query encoder (``read.query_encoder``), built on first use.
        self._query_encoder: QueryEncoder | None = None
        self._extract_edges: ExtractEdges | None = None
        self._extract_session_edges: ExtractEdges | None = None
        self._runner: TaskRunner | None = None
        self._sched_v2: _scheduler_rt.Scheduler | None = None  # RT-02
        self._cost_ledger: _economy_ledger.WriteCostLedger | None = None  # W2-12
        self._ops_ledger: _ops.OpsLedger | None = None  # T5-06 (observability.ops_ledger)
        #: RM-75 (ADR-079 decision 5): this Engine's gate, ledgers and tap, bound on every public
        #: call (and inherited by the tasks it spawns); shared leases on the keyed instances.
        self._binding = _bindings.EngineBinding()
        self._seam_token = uuid.uuid4().hex
        self._sched_lease: Lease[_scheduler_rt.Scheduler] | None = None
        self._cost_lease: Lease[_economy_ledger.WriteCostLedger] | None = None
        self._ops_lease: Lease[_ops.OpsLedger] | None = None
        self._tap_lease: Lease[tap.TapState] | None = None
        self._otel: OtelExporter | None = None  # T5-06 (observability.otel)
        self._scheduler: SleepScheduler | None = None  # D1: autonomous sleep loop
        #: O4-01a: namespace jobs, leases, watermarks and triggers (``offline.sleep_v2``).
        self._derive_sched: DerivationScheduler | None = None
        #: O4-01b: latest raw-turn seq per namespace (``offline.budget_v2``, ``read.freshness_v2``).
        self._freshness_tracker: SourceSeqTracker | None = None
        self._started = False
        self._write_locks: dict[str, asyncio.Lock] = {}
        #: #63: open neighbour batches per namespace (they observe every write).
        self._neighbour_batches: dict[str, list[_NeighbourBatch]] = {}
        self._last_write_action: str = "added"  # G8: read by write_ex()
        self._write_timers: StepTimers | None = (
            None  # I73: set only under observability.write_timers
        )
        self._timed_attrs: list[
            str
        ] = []  # engine attributes wrapped by the timers (restored at stop)
        #: B0 read ledger: (namespace, session) -> {record_id: view trust at read}
        self._read_ledger: dict[tuple[str, str], dict[str, float]] = {}
        #: B0 ``turn`` mode: ledgers a write has used since their last read. The
        #: next read on such a key starts a fresh turn (R4-1).
        self._ledger_written: set[tuple[str, str]] = set()
        #: W2-03 (write.role_policy_v2): the session's last read for the echo guard, and its counters
        self._last_read: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
        self._role_metrics: Counter[str] = Counter()
        self._sync_loop: asyncio.AbstractEventLoop | None = None
        self._sync_thread: threading.Thread | None = None
        #: #49: the audit hash chain head (None = not loaded from the log yet).
        self._audit_head: str | None = None
        self._audit_lock = asyncio.Lock()
        #: #48: the retention clock (tests swap in a fake one).
        self._clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        #: #53: ``episodic.policies.sessions.passive_after`` (None: lifecycle off) and,
        #: per namespace, the PASSIVE session ids (loaded lazily, dropped after a sleep).
        self._sessions_horizon: timedelta | None = None
        self._passive_sessions: dict[str, set[str]] = {}
        #: F-08: the DI container of the current start (None before start and after stop).
        self._container: AsyncContainer | None = None
        #: RM-78: the registry this Engine shares resources through (set at start).
        self._resource_registry: ResourceRegistry | None = None
        #: RM-77: the durable store this Engine shares with others (None: ``:memory:``)
        self._store_identity: str | None = None
        self._wiring: Wiring | None = None
        #: RM-74: this Engine's warm-up (None before start and after stop).
        self._warmup: WarmupController | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def scope(
        self,
        scope: OwnerScope | str,
        *,
        principal: str | None = None,
        purpose: str | None = None,
        viewer: str | None = None,
        multi_party: bool = False,
    ) -> Memory:
        """A scoped handle (G-16 D): tenant/owner, principal, purpose and viewer are fixed once.

        A multi-party scope without a viewer bound to the principal raises
        :class:`~memspine.read.tiers.ReadRequestError`."""
        return Memory(
            self,
            scope,
            principal=principal,
            purpose=purpose,
            viewer=viewer,
            multi_party=multi_party,
        )

    def bound(self) -> contextlib.AbstractContextManager[None]:
        """RM-75: run a block as this Engine, so code that is not an Engine call (a raw thread,
        a direct pipeline call, a host callback) reads this Engine's model gate, ledgers, tap
        and inspect switches. Engine calls bind themselves; with one started Engine in the
        process unbound code already resolves to it."""
        return _bindings.bound(self._binding)

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> Self:
        """Start the engine; on any mid-start failure every already-opened
        client/service is torn down before the error propagates (no leaks)."""
        if self._started:
            return self
        if self._teardown_task is not None and self._teardown_task.done():
            try:
                self._teardown_task.result()
            except BaseException:
                # A failed close report remains authoritative; do not reuse an
                # Engine whose dependencies may still be open or partly closed.
                await self._teardown()
            self._teardown_task = None
        self._provider_tearing_down = False
        try:
            return await self._start_inner()
        except BaseException as startup_error:
            try:
                await self._teardown()
            except BaseException as cleanup_error:
                startup_error.add_note(
                    "Startup cleanup was incomplete: " + self._cleanup_error_detail(cleanup_error)
                )
            raise

    async def _start_inner(self) -> Self:
        _bindings.register(self._binding, self)  # RM-75: a live Engine (see observability.bindings)
        # 1. secrets, then config (D-22 two-phase): the container's bootstrap providers.
        # The hooks are the engine callbacks the builders need (read now, as the memories
        # always captured them); everything else is resolved lazily, in the order below.
        container = self._container = make_container(
            EngineSpec(
                template=self._template,
                user_config=self._user_config,
                dotenv_path=self._dotenv_path,
                overrides=self._overrides,
            ),
            hooks=WiringHooks(
                pipeline_ctx=self._pipeline_ctx,
                withheld_texts=self._withheld_texts,
                edge_extract_callable=self._edge_extract_callable,
            ),
        )
        self._resolved = await container.get(ResolvedConfig)
        config = self._resolved.config
        try:  # RM-77: read-cache generations count every Engine of the process on this store
            store_base: str | None = derived_base(config)
        except ConfigError:  # no projection placement: the build reports it below
            store_base = None
        self._store_identity = (
            None if store_base in (None, ":memory:") else path_identity(str(store_base))
        )
        prewarm_provider_imports(config)  # PA-046: overlap slow provider imports with startup
        cleanup_settings = config.runtime.service_switches.projections
        if cleanup_settings.cleanup_interval_s is not None:
            runtime = self._provider_change_runtime
            if runtime is None or not any(
                category in {"graph", "vector", "lexical"}
                for category, _strategy_name in runtime.strategies
            ):
                raise ConfigError(
                    "runtime.service_switches.projections.cleanup_interval_s requires "
                    "provider_change_runtime with an injected projection strategy"
                )
        if config.observability.otel.enabled:  # T5-06: MissingServiceError names memspine[otel]
            self._otel = OtelExporter()

        # 2. validate combination (C1b) + required services (D-10); MOD-02: enable the
        # pinned plug-ins (config ``plugins:``; empty = nothing happens).
        capabilities = await container.get(Capabilities)
        self._enabled, self._auto_enabled = capabilities.enabled, capabilities.auto_enabled
        wiring = await container.get(Wiring)

        # 3./4. construct services + open the write door.
        storage: RecordsStore = await container.get(RecordsStore)
        self._storage = storage
        self._sql, self._provider_resources = wiring.sql, wiring.resources
        self._gliner_pool = wiring.gliner_pool
        self._topic_tagger = self._build_topic_tagger(config)
        self._provider_results = wiring.provider_results
        self._resource_registry = wiring.registry  # RM-78: process or private (scope)
        self._wiring = wiring
        _snapshot_listings(self._storage)  # I2
        await storage.start()
        # RM-112: the identity lives in the records store (a table, or a MongoDB document).
        wiring.owner_id = await resolve_deployment(
            storage, config.deployment.id, config.runtime.environment
        )
        self._provider_owner_id = wiring.owner_id
        await self._import_side_files(store_base)
        if self._sql is not None:  # verified switches keep their manifests in SQL tables
            async with self._sql.engine.connect() as connection:
                wiring.projection_routes = await verified_active_projection_routes(
                    connection, wiring.owner_id
                )
        await preflight_projection_bindings(config, wiring, self._enabled)
        if config.event_log.mode is EventLogMode.EPHEMERAL:
            _log.warning(
                "event_log.ephemeral_mode_active",
                detail="events are not persisted: rebuild and audit taint unavailable (D-45)",
            )

        # P1 services: embedding (E3-cached), vector store, LLM router, policies.
        # One shared cache (Phase 2) fronts both the embedding and extraction
        # caches; the emb:/ext: producer key prefixes keep them isolated in it.
        self._cache = await container.get(KVCache)
        # RT-02 (flags runtime.scheduler_v2 / runtime.model_concurrency): the scheduler of these
        # settings, shared with Engines that have the same (RM-75); None when both are off, and
        # then nothing below changes.
        seams = self._binding
        self._sched_lease = self._own_lease(
            "seam.model_scheduler",
            await _scheduler_rt.acquire(config.runtime, self._resource_registry),
        )
        if self._sched_lease is not None:
            sched = self._sched_v2 = self._sched_lease.value
            seams.scheduler, seams.gate = sched, sched.gate
            seams.slo_ledger = sched.slo_ledger
        if config.economy.ledger_v2:  # W2-12 (W-26, P-22 C): the write-side cost ledger
            cost_lease = self._cost_lease = await self._acquire_cost_ledger(config)
            self._own_lease("seam.cost_ledger", cost_lease)
            self._cost_ledger = seams.cost_ledger = cost_lease.value
        self._embedder = await container.get(EmbeddingService)
        if self._sched_v2 is not None:
            self._embedder = self._sched_v2.wrap_embedder(self._embedder)
        self._vector = await container.get(VectorStore)
        self._rescore_active = wiring.rescore_active
        # E4 model2vec prefilter (ADR-020): only the intent is recorded here; the
        # static embedder loads lazily on first search so a heavy model download
        # never blocks start and a missing [static] extra self-disables (skip-log).
        self._static_prefilter_on = config.read.static_embedding_prefilter
        # Lexical BM25 leg (D-25): built when hybrid retrieval is on — the v0.2
        # A3 default (ADR-019). With ``read.hybrid: false`` no lexical index,
        # projector, or write-path cost exists and describe()["projectors"] is
        # unchanged from vector-only. Toggling on later: catch-up/rebuild
        # backfills the index from the persisted log at first construction.
        if config.read.hybrid:
            self._lexical = await container.get(LexicalStore)
        # P6 graph store (D-26): constructed only when associative memory
        # projects it — profile="simple" never constructs one. An explicit
        # ``graph:`` block without associative would be a dead handle (no
        # projector ever writes it), so it is a config error, not a store.
        if "associative" in self._enabled:
            self._graph = await container.get(GraphStore)
        elif "graph" in config.model_fields_set or (
            "graph" in config.storage.model_fields_set
            and (config.storage.graph.provider != "auto" or config.storage.graph.settings)
        ):
            # ADR-081: an explicit (non-auto) storage.graph slot is a graph block too.
            raise ConfigError(
                "graph configured but memories.associative not enabled — "
                "enable it or remove the graph block (graph or storage.graph)"
            )
        if config.storage.documents is not None:  # ADR-081: the optional documents slot
            self._documents = await container.get(DocumentStore)
        self._llm = await container.get(LLMRouter)
        self._decision_off = (await container.get(DecisionState)).off  # H24
        self._scoring = ScoringPolicy.bind(config.read.scoring)
        self._assembly = AssemblyPolicy.bind(config.read.assembly)
        # E5 assembly-stage compression binding (D-51): master switch defaults
        # off inside the options, so profile="simple" behavior never changes.
        self._assembly_compression = CompressionPolicy.bind(
            _as_options_dict(config.read.compression)
        )
        # E8 rerank (D-51, ADR-082): validate every ``model.rerank.chain`` entry now; the
        # models load lazily on first search (a config typo must fail at start, a heavy
        # ONNX download must not).
        for index, stage in enumerate(config.model.rerank.chain):
            if stage.provider == "off":
                raise ConfigError(
                    f"model.rerank.chain[{index}]: 'off' is not a reranker provider; "
                    "use model.rerank.chain: [] to turn reranking off"
                )
            validate_reranker_settings(
                stage.provider,
                stage.settings,
                explicit_deployment_id=config.deployment.id is not None,
                path=f"model.rerank.chain[{index}]",
            )
        self._working = WorkingMemory(
            append_event=self._append_and_project,
            page_size=int(
                self._memory_policy(config, "working").get("page_size", DEFAULT_PAGE_SIZE)
            ),
        )
        # Prompt pack resolution (§4 step 2, D-43): defaults + config overrides.
        self._prompts = await container.get(PromptRegistry)
        if "semantic" in self._enabled:
            semantic_policies = self._memory_policy(config, "semantic")
            self._semantic = SemanticMemory(
                storage=self._storage,
                embedder=self._embedder,  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
                append_event=self._append_and_project,
                conflict=ConflictPolicy.bind(
                    {
                        **(_as_options_dict(semantic_policies.get("conflict")) or {}),
                        **({"integration_v2": True} if config.write.integration_v2 else {}),
                    }
                ),
                dedup=DedupPolicy.bind(_as_options_dict(semantic_policies.get("dedup"))),
                extractor=(await container.get(SemanticExtractor)).value,
                write_pipeline=(await container.get(SemanticWritePipeline)).value,
                merge_reinforcement_gate=(
                    config.integrity.enabled and config.integrity.merge_reinforcement_gate
                ),
                screen_derived=self._screen_derived,
            )
        # Memory Firewall (E1/M17): trust matrix binds from the semantic
        # policy block (D-14 channel); the gate itself covers every type.
        self._firewall = Firewall(
            TrustPolicy.bind(
                _as_options_dict(self._memory_policy(config, "semantic").get("trust"))
            ),
            FirewallSignals(**config.firewall.signals.model_dump()),
        )
        if "episodic" in self._enabled:
            self._episodic = EpisodicMemory(self._storage)
        # #53: validated at start, so a bad duration fails here, not at the first sleep.
        self._sessions_horizon = passive_after(self._memory_policy(config, "episodic"))
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
                self._storage,
                self._graph,
                self._append_and_project,
                policies=self._memory_policy(config, "associative"),
                vector=self._vector,  # E1 rrf strategy: graph-rank ⊕ vector-rank
                embedder=self._embedder,
            )
        if "prospective" in self._enabled:
            self._prospective = ProspectiveMemory(self._storage, self._append_and_project)
        if "shared" in self._enabled:
            self._shared = SharedMemory(self._storage, self._append_and_project)
        self._summarize = self._build_summarize()
        self._query_encoder = None  # #61: rebuilt (and its cue index reloaded) per start
        self._extract_edges = self._build_edge_extractor(config)
        self._extract_session_edges = self._build_session_edge_extractor(config)
        self._windows = self._build_windows(config)
        try:  # E01: a bad ``fact_projection`` fails at start, not mid-sweep
            fact_projection_on(self._memory_policy(config, "semantic"))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        self._cached_extractor = wiring.cached_extractor  # #43
        self._projectors = [
            RecordProjector(self._storage),
            VectorProjector(self._vector, self._embedder),  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
        ]
        if self._lexical is not None:
            # Registered only when hybrid is on, so rebuild() replays it and the
            # index backfills from seq 0 the first time hybrid is enabled.
            self._projectors.append(
                LexicalProjector(self._lexical, name=lexical_projector_name(config))
            )
        if self._associative is not None:
            # Registered only when associative is enabled, so rebuild() replays
            # it and profile="simple" never projects a graph (D0.1/ADR-015).
            assert self._graph is not None
            entities = EntityPolicy.from_policy(
                self._memory_policy(config, "associative").get("entity_nodes")
            )
            self._projectors.append(
                GraphProjector(
                    self._graph,
                    entities,
                    embed=self._embedder.embed
                    if config.graph.entity_embeddings and self._embedder is not None
                    else None,
                )
            )

        # A durable high-water mark is valid only for the provider target that
        # produced it. Fail closed before catch-up can serve data under a changed
        # graph/vector/lexical target.
        assert self._provider_owner_id is not None
        if self._sql is not None:
            # ADR-081 amendment 2026-10-23: the graph mirror is gone. A store that once ran it
            # keeps no offset, binding or queued outbox work for it (idempotent, never fails).
            # A records store without SQL (MongoDB, RM-112) never ran the mirror.
            await retire_graph_mirror_state(self._storage, self._sql.engine)
        binding_store = self._storage.projection_bindings()
        for binding in projection_target_bindings(
            config,
            self._provider_results,
            list(self._projectors),
            self._provider_owner_id,
            self._embedder,
            wiring.projection_routes,
        ):
            await binding_store.ensure(binding)

        # Register pipelines before starting remote runners so workflow recovery
        # cannot observe an incomplete registry. Any start failure is covered by
        # Engine.start's startup teardown, including runner.close().
        runner = self._runner = await container.get(TaskRunner)
        assert runner is not None
        for name, pipeline in PIPELINES.items():
            runner.register(name, pipeline)
        await runner.start()

        # 5.(minimal)/6. catch-up from high-water marks; rolling mode prunes on
        # boot via the pipeline (continuous scheduling joins the P3 sleep cycle).
        await catch_up(self._storage, list(self._projectors))
        if config.event_log.mode is EventLogMode.ROLLING:
            stats = await self._runner.run("event_log_prune", self._pipeline_ctx())  # type: ignore[union-attr]  # CI-GREEN: pre-existing typing debt
            if stats.get("pruned"):
                _log.info("event_log.pruned", **stats)
        # D1: autonomous maintenance loop (opt-in). Runs the full sleep cycle on
        # a fixed interval so learning dynamics advance without an external cron.
        interval = config.workers.sleep_interval_seconds
        if interval is not None and interval > 0:
            if client_is_memory(config):
                # An in-memory SQLite DB shares one connection and can't use WAL,
                # so a background sleep cycle collides with foreground writes
                # ("table is locked"); and an ephemeral DB has nothing durable to
                # maintain. Skip the loop rather than emit lock-contention noise.
                _log.warning(
                    "scheduler.disabled_on_memory_db",
                    detail="workers.sleep_interval_seconds ignored for storage.path=':memory:' "
                    "(no WAL, nothing to persist) — use a file-backed DB",
                )
            else:

                async def _tick() -> object:
                    assert self._runner is not None  # set above; loop only runs while started
                    stats = await run_sleep_cycle(self._runner, self._pipeline_ctx())
                    self._passive_sessions.clear()  # #53
                    return stats

                self._scheduler = SleepScheduler(interval, _tick)
                self._scheduler.start()
        if config.offline.budget_v2 or config.read.freshness_v2:  # O4-01b: source seq, covers_seq
            self._freshness_tracker = SourceSeqTracker()
            if not client_is_memory(config):
                await read_freshness.bootstrap(self, self._freshness_tracker)
        if config.offline.sleep_v2:  # O4-01a: non-blocking sleep, triggers, leases, watermarks
            self._derive_sched = derive_scheduler.build_sleep_v2(self, config)
            if not config.offline.sleep_manual_only:
                await self._derive_sched.on_open()
        self._install_write_timers(config)
        if config.observability.ops_ledger:  # T5-06: per-stage ops ledger, off by default
            ops_lease = self._ops_lease = await self._registry().acquire(
                resource_key(
                    "observability.ops_ledger",
                    _seam_target(config, self._seam_token),
                    {"ops_ledger": True},
                    scope="thread",
                ),
                self._new_ops_ledger,
            )
            self._own_lease("seam.ops_ledger", ops_lease)
            self._ops_ledger = seams.ops_ledger = ops_lease.value
        obs = config.observability
        # TI-1: off unless asked; RM-75: this Engine's switches, never the process default
        seams.inspect = inspect_obs.switches(obs.inspect, obs.inspect_content)
        if obs.tap.enabled or obs.otel.enabled or obs.inspect:
            # WT-01: debugging wiretap, off by default. T5-06: the OTel exporter is a second sink
            # on the tap; without a tap config it runs on a sink-less, content-free tap.
            tap_cfg = obs.tap
            if not tap_cfg.enabled:
                # TI-1: inspect needs a sink of its own (the configured jsonl); OTel alone none
                sink = tap_cfg.sink if obs.inspect else "none"
                tap_cfg = tap_cfg.model_copy(update={"enabled": True, "sink": sink})
            if obs.inspect and obs.inspect_content:  # TI-1: text payloads need the content opt-in
                tap_cfg = tap_cfg.model_copy(
                    update={
                        "content": True,
                        "max_content_chars": max(tap_cfg.max_content_chars, 20000),
                    }
                )
            self._tap_lease = await self._acquire_tap(tap_cfg)
            seams.tap = self._tap_lease.value
            if self._otel is not None:
                seams.tap_sinks.append(self._otel)  # this Engine's exporter only
        if config.privacy.erasure_v2:  # T5-04: erased / hidden ids, queue, purge table
            await trust_erasure_v2.load(self)
        self._started = True
        # Finish any accepted intake the last run did not apply.
        from memspine.ingest import gateway as intake_gateway

        if self._sql is not None:  # RM-112: the intake book and message index are SQL tables
            await intake_gateway.recover(self)
            # Rebuild the lightweight source-identity projection for stores imported
            # before it existed. This runs at startup, never on each message call.
            from memspine.ingest.message_store import backfill_from_storage

            await backfill_from_storage(self._require_started())
        if cleanup_settings.cleanup_interval_s is not None:
            runtime = self._provider_change_runtime
            assert runtime is not None
            controller = self._build_provider_changes_controller(runtime)
            self._provider_changes_controller = controller
            await controller.start_cleanup_worker()
        await self._start_warmup(config)
        return self

    # -- RM-74: warm-up after start (ADR-079 decision 3) ----------------------------------

    def _registry(self) -> ResourceRegistry:
        """The resource registry of this start (``runtime.shared_resources.scope``)."""
        return self._resource_registry or shared_registry()

    def _own_lease[L: Lease[Any]](self, resource_id: str, lease: L | None) -> L | None:
        """RM-75: hand a shared seam lease to this Engine's ``ProviderLifecycleManager``; its
        ordered close (the ``provider resources`` teardown step, after every worker that could
        use the seam has stopped) releases it, and the last holder in the process closes it."""
        if lease is None:
            return None
        resources = self._provider_resources
        if resources is None:  # pragma: no cover - start always wires the manager first
            raise MemspineError("no lifecycle manager to own a shared lease")
        resources.own(resource_id, lease.aclose)
        return lease

    async def _start_warmup(self, config: MemspineConfig) -> None:
        shared = config.runtime.shared_resources
        controller = self._warmup = WarmupController(
            self._warm_targets(config),
            mode=shared.warmup,
            categories=shared.warmup_categories(),
            timeout_s=shared.warmup_timeout_s,
            concurrency=shared.warmup_concurrency,
        )
        if shared.warmup == "blocking":
            await controller.run_blocking()
        elif shared.warmup == "background":
            controller.start_background()

    def _warm_targets(self, config: MemspineConfig) -> list[WarmTarget]:
        """One target per configured resource that has a warm hook; nothing optional is
        imported here (each hook builds or loads only when the warm-up runs it)."""
        targets: list[WarmTarget] = []
        embed_hook = find_warm_hook(self._embedder)
        if embed_hook is not None:
            targets.append(WarmTarget("embedding", "embedding", embed_hook))
        for index, stage in enumerate(config.model.rerank.chain):
            name = f"rerank:{stage.provider}"
            if any(t.name == name for t in targets):
                name = f"{name}#{index}"
            targets.append(WarmTarget(name, "rerank", self._chain_warm_hook(index)))
        if self._gliner_pool is not None:
            # Topic / sensitivity are offline only (RM-99 / RM-106) and are not warmed; the
            # consumers share one pool load (ADR-082 item 4), so any warmed one warms them all.
            for consumer in gliner_consumers(config):
                if consumer in ("topic", "sensitivity"):
                    continue
                category = "decider" if consumer == "route" else "extraction"
                targets.append(
                    WarmTarget(
                        f"{category}:{consumer}",
                        category,
                        self._gliner_warm_hook(config, consumer),  # type: ignore[arg-type]
                    )
                )
        lexical_hook = find_warm_hook(self._lexical)
        if lexical_hook is not None:
            targets.append(WarmTarget("lexical", "lexical", lexical_hook))
        llm_services = getattr(self._llm, "services", None)
        if callable(llm_services):
            for index, service in enumerate(llm_services()):
                hook = find_warm_hook(service)
                if hook is not None:
                    name = getattr(service, "provider_id", f"llm-{index}")
                    targets.append(WarmTarget(f"llm:{name}", "llm", hook))
        read_slot = read_decider_slot(config)
        read_tasks: tuple[str, ...] = () if read_slot is None else tuple(read_slot.tasks)
        if getattr(self, "_decider_override", None) is None and any(
            self._decider_active(task) for task in (*read_tasks, "sensitivity_scope")
        ):

            async def warm_decider() -> None:
                hook = find_warm_hook(self._decider_adapter())
                if hook is not None:
                    await hook()

            targets.append(WarmTarget("decider", "decider", warm_decider))
        unique: dict[str, WarmTarget] = {}
        for target in targets:
            unique.setdefault(target.name, target)
        return list(unique.values())

    def _chain_warm_hook(self, index: int) -> Callable[[], Awaitable[None]]:
        """Warm ``model.rerank.chain[index]`` (entry 0 is the engine's own reranker)."""

        async def warm() -> None:
            from memspine.read.pipeline.rerank import _chain_provider

            rerank_cfg = self._config().model.rerank
            if index == 0:
                reranker = await self._rerank_provider()
            else:
                reranker = await _chain_provider(self, rerank_cfg.chain[index], rerank_cfg, None)
            hook = find_warm_hook(reranker)
            if hook is not None:
                await hook()

        return warm

    def _gliner_warm_hook(
        self, config: MemspineConfig, consumer: GlinerConsumer
    ) -> Callable[[], Awaitable[None]]:
        async def warm() -> None:
            from memspine.config.schema.model import gliner_settings

            pool = self._gliner_pool
            if pool is None:
                return
            lease = await pool.acquire(gliner_settings(config, consumer))
            await lease.aclose()  # the pool keeps the loaded weights resident

        return warm

    def warm_state(self) -> dict[str, WarmState]:
        """RM-74: warm state per resource (``warm`` | ``warming`` | ``failed`` with the error
        type | ``not_requested``); empty before start."""
        return {} if self._warmup is None else self._warmup.states()

    async def wait_until_warm(self, timeout_s: float | None = None) -> dict[str, WarmState]:
        """RM-74: wait for a background warm-up (returns at once in the other modes)."""
        if self._warmup is None:
            return {}
        return dict(await self._warmup.wait(timeout_s))

    async def _import_side_files(self, store_base: str | None) -> None:
        """RM-111 WP5 (ADR-081): a ``<base>.sleep``, ``<base>.cost`` or
        ``<base>.deadletters.sqlite`` left by an older version is imported once into the storage
        database (idempotent) and renamed to ``*.imported``. Nothing to do for ``:memory:``."""
        sql, resources = self._sql, self._provider_resources
        if store_base in (None, ":memory:") or sql is None or resources is None:
            return
        if sql.is_memory or "sync_sql" not in sql.capabilities:
            return
        engine = sql.sync_lease(resources, "side_file_import").engine
        await asyncio.to_thread(
            functools.partial(
                _side_tables.import_side_files,
                engine,
                str(store_base),
                open_file=legacy_sqlite_file,  # RM-113: the store never opens a connection
            )
        )

    async def _acquire_cost_ledger(
        self, config: MemspineConfig
    ) -> Lease[_economy_ledger.WriteCostLedger]:
        """RM-75: the cost ledger for this economy config and store, shared by Engines that agree."""

        sql_engine = None
        sql, resources = self._sql, self._provider_resources
        if (
            config.economy.ledger_persist
            and sql is not None
            and resources is not None
            and not sql.is_memory
            and "sync_sql" in sql.capabilities
        ):
            sql_engine = sql.sync_lease(resources, "cost_ledger").engine

        async def build() -> _economy_ledger.WriteCostLedger:
            return _build_cost_ledger(config, sql_engine)

        async def close(ledger: _economy_ledger.WriteCostLedger) -> None:
            ledger.close()

        key = resource_key(
            "economy.cost_ledger",
            _seam_target(config, self._seam_token),
            {"economy": config.economy.model_dump(mode="json"), "outbox": config.workers.outbox},
            scope="thread",
        )
        return await self._registry().acquire(key, build, close=close)

    @staticmethod
    async def _new_ops_ledger() -> _ops.OpsLedger:
        return _ops.OpsLedger()

    async def _acquire_tap(self, tap_cfg: Any) -> Lease[tap.TapState]:
        """RM-75: the tap state for these effective tap settings, shared by Engines that agree."""

        async def build() -> tap.TapState:
            return tap.open_state(tap_cfg)

        async def close(state: tap.TapState) -> None:
            tap.close_state(state)

        key = resource_key("observability.tap", {}, tap_cfg.model_dump(mode="json"), scope="thread")
        return await self._registry().acquire(key, build, close=close)

    def _install_write_timers(self, config: MemspineConfig) -> None:
        """I73: wrap the write-door steps with monotonic timers, only when
        ``observability.write_timers`` is on (otherwise nothing is touched)."""
        if not config.observability.write_timers:
            return
        timers = self._write_timers = StepTimers()
        self._timed_attrs = []

        def wrap(owner: object, attr: str, step: str, *, engine: bool = False) -> None:
            fn = getattr(owner, attr, None)
            if fn is None:
                return
            setattr(owner, attr, timed(timers, step, fn))
            if engine:
                self._timed_attrs.append(attr)

        wrap(self, "write", "write_total", engine=True)
        wrap(self, "_screen_write", "firewall", engine=True)
        wrap(self, "_redact_fields", "redaction", engine=True)
        wrap(self, "_assess_write", "firewall_assess", engine=True)
        wrap(self, "_govern_write", "sensitivity_tags", engine=True)
        wrap(self, "_grade_sensitivity", "llm_sensitivity", engine=True)
        wrap(self, "_annotate_perspective", "perspective_tags", engine=True)
        wrap(self, "_append_and_project", "append_and_project", engine=True)
        wrap(self._embedder, "embed", "embed")
        for projector in self._projectors:
            wrap(projector, "apply", f"project:{projector.name}")
        semantic = self._semantic
        if semantic is not None:
            wrap(semantic, "_write_primary", "semantic_write")
            wrap(semantic, "_confirm_duplicate", "dedup")
            wrap(semantic, "_resolve_conflict", "conflict_ladder")
            wrap(getattr(semantic, "_extractor", None), "extract", "llm_extract")
            wrap(getattr(semantic, "_write_pipeline", None), "run", "llm_edges")

    def write_timers(self, *, reset: bool = False) -> dict[str, dict[str, float | int]]:
        """I73: per-step write-path timings since ``start()`` or the last reset.

        ``{step: {count, total_ms, mean_ms, p50_ms, p95_ms}}``; steps are inclusive and
        may nest (see :mod:`memspine.observability.timers`). Empty unless
        ``observability.write_timers`` is on."""
        return self._write_timers.snapshot(reset=reset) if self._write_timers else {}

    async def stop(self) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        task = self._teardown_task
        retryable_failure = False
        if task is not None and task.done():
            try:
                task.result()
            except _EngineTeardownError as exc:
                retryable_failure = exc.retryable
            except BaseException:
                pass
        if task is None or retryable_failure:
            task = asyncio.create_task(self._teardown_inner(), name="memspine-engine-teardown")
            self._teardown_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is None or current.cancelling() == 0:
                raise
            # Stop is cancellation-safe: finish ordered cleanup before returning
            # cancellation to the caller.
            try:
                await asyncio.shield(task)
            except BaseException as cleanup_error:
                _log.error(
                    "engine.teardown_incomplete",
                    failures=[self._cleanup_error_detail(cleanup_error)],
                )
            raise

    async def _teardown_inner(self) -> None:
        self._provider_tearing_down = True
        if self._warmup is not None:
            # RM-74: a pending background warm-up stops first (before its resources close).
            await self._warmup.cancel()
        try:
            await self._drain_reranker_build()
        except Exception as exc:
            # A lazy builder that ignores cancellation can still be using its
            # provider and SQL dependencies, so leave those resources open.
            raise _EngineTeardownError(
                "engine teardown blocked before dependency cleanup: reranker build drain "
                f"failed ({type(exc).__name__})",
                retryable=True,
            ) from None

        failures: list[str] = []

        async def attempt(label: str, action: Callable[[], Awaitable[Any] | None]) -> None:
            try:
                result = action()
                if result is not None:
                    await result
            except asyncio.CancelledError as exc:
                failures.append(f"{label} ({type(exc).__name__})")
            except Exception as exc:
                failures.append(f"{label} ({self._cleanup_error_detail(exc)})")

        async def stop_provider_cleanup_worker() -> None:
            if self._provider_changes_controller is not None:
                controller, self._provider_changes_controller = (
                    self._provider_changes_controller,
                    None,
                )
                await controller.stop_cleanup_worker()

        # The cleanup worker uses the same SQL and target strategies as serving;
        # drain it before any scheduler, client, or SQL dependency is stopped.
        await attempt("provider cleanup worker", stop_provider_cleanup_worker)

        seams = self._binding
        # RM-75: the ops / cost ledger and model scheduler leases belong to the lifecycle
        # manager (``_own_lease``), which releases them in its ordered close below; here this
        # Engine only stops using them.
        self._ops_ledger = seams.ops_ledger = self._ops_lease = None
        self._otel = None
        self._cost_ledger = seams.cost_ledger = self._cost_lease = None

        async def detach_model_scheduler() -> None:
            seams.scheduler = seams.gate = seams.slo_ledger = None
            sched, self._sched_v2 = self._sched_v2, None
            self._sched_lease = None
            if sched is not None:
                await sched.close_embedder(self._embedder)  # this Engine's executors only

        await attempt("model scheduler", detach_model_scheduler)

        async def stop_scheduler() -> None:
            if self._scheduler is not None:
                scheduler, self._scheduler = self._scheduler, None
                await scheduler.stop()

        await attempt("sleep scheduler", stop_scheduler)

        async def stop_derive_scheduler() -> None:
            if self._derive_sched is not None:
                derive_sched, self._derive_sched = self._derive_sched, None
                await derive_sched.stop()

        await attempt("derive scheduler", stop_derive_scheduler)
        await attempt("asset describer", lambda: asset_describer.shutdown(self))

        async def close_http() -> None:
            if self._http is not None:
                await self._http.close()  # the pool reopens on the next request

        await attempt("HTTP connector", close_http)

        async def stop_windows() -> None:
            if self._windows is not None:
                windows, self._windows = self._windows, None
                await windows.stop()

        await attempt("window workers", stop_windows)

        async def close_runner() -> None:
            if self._runner is not None:
                runner, self._runner = self._runner, None
                await runner.close()

        await attempt("task runner", close_runner)

        async def stop_storage() -> None:
            if self._storage is not None:
                storage, self._storage = self._storage, None
                await storage.stop()

        await attempt("SQL storage", stop_storage)

        async def close_container() -> None:
            if self._container is not None:
                # Finalisers close owned provider resources in dependency order.
                container, self._container = self._container, None
                try:
                    await container.close()
                except Exception:
                    resources = self._provider_resources
                    if resources is not None:
                        report = await resources.close_all()
                        if report.failures:
                            raise ProviderResourceCleanupError(report) from None
                    raise

        await attempt("decider weights", self._release_deciders)
        await attempt("provider resources", close_container)
        for attr in self._timed_attrs:  # I73: drop the instance wrappers, restore the methods
            self.__dict__.pop(attr, None)
        self._timed_attrs = []
        self._write_timers = None

        async def release_tap() -> None:
            sinks, seams.tap_sinks, seams.tap = seams.tap_sinks, [], None
            for sink in sinks:
                with contextlib.suppress(Exception):
                    sink.close()
            if self._tap_lease is not None:
                lease, self._tap_lease = self._tap_lease, None
                await lease.aclose()

        await attempt("debug tap", release_tap)
        seams.clear()
        _bindings.unregister(seams)

        async def release_registry() -> None:
            # RM-78: withdraw this Engine's retention policy (a private registry also closes
            # what it still retains); idempotent with the container's own release.
            wiring, self._wiring = self._wiring, None
            if wiring is not None:
                await wiring.release_registry()

        await attempt("resource registry", release_registry)
        self._resource_registry = None
        self._started = False
        self._gliner_pool = None
        self._topic_tagger = None
        self._decision = None
        self._window_prepass = None
        if failures:
            _log.error("engine.teardown_incomplete", failures=failures)
            raise _EngineTeardownError("engine teardown incomplete: " + "; ".join(failures))

    @staticmethod
    def _cleanup_error_detail(error: BaseException) -> str:
        if isinstance(error, ProviderResourceCleanupError):
            return str(error)
        if isinstance(error, _EngineTeardownError):
            return str(error)
        if isinstance(error, BaseExceptionGroup):
            details = [Engine._cleanup_error_detail(item) for item in error.exceptions]
            return "; ".join(dict.fromkeys(details))
        return type(error).__name__

    async def _drain_reranker_build(self) -> None:
        """Cancel and drain lazy provider construction before closing its dependencies."""
        async with self._reranker_build_lock:
            tasks = list(self._reranker_build_tasks.values())
            for task in tasks:
                if not task.done():
                    task.cancel()
        pending_tasks = [task for task in tasks if not task.done()]
        if not pending_tasks:
            self._reranker_build_tasks.clear()
            for task in tasks:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _log.warning("rerank.provider_build_stopped", error=type(exc).__name__)
            return
        timeout_s = 5.0
        if self._resolved is not None:
            timeout_s = self._resolved.config.runtime.provider_services.health_timeout_s
        done, pending = await asyncio.wait(pending_tasks, timeout=timeout_s)
        if pending:
            # Keep dependencies open rather than racing a builder that ignored cancellation.
            raise TimeoutError("reranker builder did not stop before provider teardown")
        self._reranker_build_tasks.clear()
        done.update(task for task in tasks if task.done())
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _log.warning("rerank.provider_build_stopped", error=type(exc).__name__)

    # ── public verbs (P0: write / retrieve / rebuild / describe) ─────────────

    @tap.traced("write")
    @_ops.staged("write")  # T5-06: per-stage ops ledger (a pass-through when it is off)
    @prioritised(Priority.WRITE)  # RT-02: foreground write class (a no-op without the gate)
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
        group_id: str | None = None,
        tags: list[str] | None = None,
        derived_from: Sequence[str] | None = None,
        valid_from: datetime | None = None,
        session_id: str | None = None,
        ledger_id: str | None = None,
        extraction_hint: str | None = None,
        parent_weights: Mapping[str, float] | None = None,
        purposes: Sequence[str] | None = None,
        reply_to: str | None = None,
        participants: Sequence[str] | None = None,
        visibility: str | None = None,
    ) -> MemoryRecord:
        """Append a WRITE event through the single door; projection materializes it.

        ``entity``/``attribute`` key a semantic fact directly (M13.3) so the M4
        conflict ladder engages without requiring an extractor — callers that
        know the fact key should always pass it. ``group_id``/``tags`` (D2) are a
        sub-scoping facet within the namespace (a conversation, a document, a
        project) — a retrieval filter, never an isolation boundary.

        ``derived_from`` names the records the writer had in context when it
        produced ``content`` (own or granted). They are always recorded as
        ``source.parents``; with ``integrity.enabled`` they also cap the new
        record's trust at the least-trusted parent's view trust (MTI-D), which
        is what stops a paraphrase from laundering foreign content.

        ``valid_from`` sets the record's EVENT time (when the content was true or
        said), distinct from ``recorded_at`` (when the engine learned it). Default
        is "now", which keeps every existing caller unchanged.

        ``purposes`` (#50) are the read purposes the record may serve, stored as its
        ``consent_tags`` (``*`` = any); enforced only under ``consent.enforce``.

        ``reply_to`` (G27, ADR-061) names the record this one answers, in the same
        namespace (a missing, foreign or erased one is refused like a missing
        record). It is stored as a ``reply_to:<id>`` tag, so ``read.reply_links``
        can show the answered message beside a replayed reply; with associative
        memory on, a ``reply_to`` LINK (reply -> answered) is written too, unless
        either end is held or at its link budget (logged, the write stands).

        ``participants`` / ``visibility`` (I53): the speakers present (the first is the
        speaker of this record) and who may see it (``private`` | ``participants`` |
        ``owner_shared``), stored as ``participant:<name>`` / ``visibility:<v>`` tags. They
        matter only to a read with ``viewer=``; the namespace stays the isolation boundary.
        """
        return await ingest_write.write(
            self,
            content,
            namespace,
            memory_type,
            source,
            pii_tier,
            actor,
            entity,
            attribute,
            group_id,
            tags,
            derived_from,
            valid_from,
            session_id,
            ledger_id,
            extraction_hint,
            parent_weights,
            purposes,
            reply_to,
            participants,
            visibility,
        )

    async def _link_reply(self, ns: str, written: MemoryRecord, reply_to: str) -> None:
        return await ingest_write._link_reply(self, ns, written, reply_to)

    async def _reopen_session(self, storage: RecordsStore, ns: str, session: str | None) -> None:
        return await ingest_windows._reopen_session(self, storage, ns, session)

    async def write_ex(self, content: str, **kwargs: Any) -> WriteOutcome:
        """``write`` that also reports what happened (G8).

        ``action`` is ``added`` | ``merged`` | ``updated`` | ``rejected`` |
        ``quarantined``. On ``merged`` the returned record is the KEPT record
        (its id predates this call) — a caller tracking lineage must alias its
        occurrence to that id instead of assuming a fresh record.
        ``occurrence_id`` is fresh per call, so two identical deposits remain
        distinguishable in the caller's own bookkeeping.
        """
        return await ingest_write.write_ex(self, content, **kwargs)

    @staticmethod
    def _ledger_key(ns: str, session_id: str | None) -> tuple[str, str]:
        return read_router._ledger_key(ns, session_id)

    def _record_reads(
        self,
        ns: str,
        session_id: str | None,
        results: Sequence[tuple[MemoryRecord, float]],
    ) -> None:
        return read_router._record_reads(self, ns, session_id, results)

    def _consume_reads(self, ns: str, session_id: str | None) -> dict[str, float]:
        return read_router._consume_reads(self, ns, session_id)

    async def add_cues(
        self,
        record_id: str,
        cues: Sequence[str],
        namespace: str = "default",
        source: SourceInfo | None = None,
        actor: str = "user",
        extra_tags: Sequence[str] = (),
        *,
        require_live: bool = False,
    ) -> list[MemoryRecord]:
        """C8': attach anticipatory cues (likely future questions) to a record.

        A cue is a retrieval KEY, never content: with ``read.anticipatory_cues``
        a search hit on a cue resolves to its target record, and only when the
        cue's trust reaches ``read.cue_min_trust``. Each cue goes through the
        write door (firewall screening, quarantine) and its trust is capped at
        the target's, so a cue can never make content more trusted than it is,
        and a low-trust source cannot plant cues that redirect retrieval.

        ``require_live`` (I72, the sleep-cycle deposit): a target that is gone or no
        longer live (forgotten, archived, quarantined) when the cue is about to be
        written skips the cue (audited) instead of raising.
        """
        return await derive_integrate.add_cues(
            self, record_id, cues, namespace, source, actor, extra_tags, require_live=require_live
        )

    async def principal_reputation(self, principal: str) -> float:
        """B7: trust multiplier in (0, 1] for ``principal``, from the event log.

        ``bad`` counts the principal's records that are currently quarantined,
        plus those that were the seed of a taint rollback (descendants archived
        by the same rollback are not blamed on their authors). With a uniform
        Beta prior, the mean is ``(1 + good) / (2 + good + bad)``, and the
        multiplier is ``min(1, 2 * mean)``: 1.0 for a new or clean principal.
        """
        return await trust_firewall.principal_reputation(self, principal)

    async def retract(
        self,
        entity: str,
        attribute: str,
        namespace: str = "default",
        reason: str = "",
        source: SourceInfo | None = None,
        valid_from: datetime | None = None,
    ) -> MemoryRecord:
        """C4'/FORK-A6: end a fact with no successor (the user retracts it).

        Writes a ``retract``-tagged semantic record on the key; the conflict
        ladder's deterministic retraction rung turns it into INVALIDATE: the
        current fact is archived with ``valid_to`` set, and the retraction is
        kept as a closed record, so history shows when and why it ended. The
        trust gate still applies: a markedly less trusted source cannot retract.
        """
        return await trust_firewall.retract(
            self, entity, attribute, namespace, reason, source, valid_from
        )

    async def send(
        self,
        content: str,
        from_namespace: str,
        to_namespace: str,
        derived_from: Sequence[str] | None = None,
        session_id: str | None = None,
        ledger_id: str | None = None,
        actor: str = "assistant",
        principal: str | None = None,
    ) -> MemoryRecord:
        """B5: an agent-to-agent message, mediated by memory.

        The message becomes an episodic record in the RECEIVER's namespace
        (``channel="message"``) whose lineage is the sender's declared parents
        plus the sender's recorded reads (B0), and whose trust is capped at
        ``view_trust(min(sender's base trust, parents' views), sender -> receiver)``:
        a message crosses the same attenuated edge a grant read would. The
        receiver must already hold a grant on the sender (the edge must exist),
        so messaging cannot create reachability the grant graph does not have.
        Paper A's complete-mediation assumption (A1) then covers messages too.
        """
        return await trust_firewall.send(
            self,
            content,
            from_namespace,
            to_namespace,
            derived_from,
            session_id,
            ledger_id,
            actor,
            principal,
        )

    async def approve(
        self,
        subject: str,
        field: str,
        value: str,
        namespace: str = "default",
        *,
        valid_until: datetime | None = None,
        actor: str = "user",
    ) -> MemoryRecord:
        """G29 (plan v3.2): record an explicit approval for one change (``subject``'s
        ``field`` set to ``value``), optionally until ``valid_until``. Only operator or
        user approvals count in :meth:`authorize`; a newer approval on the same key
        supersedes the older one through the conflict ladder."""
        return await trust_firewall.approve(
            self, subject, field, value, namespace, valid_until=valid_until, actor=actor
        )

    async def _approved(self, ns: str, subject: str, field: str, value: str) -> bool:
        """G29: a live, exactly matching approval exists for (subject, field, value)."""
        return await trust_firewall._approved(self, ns, subject, field, value)

    async def authorize(
        self,
        evidence_ids: Sequence[str],
        namespace: str = "default",
        threshold: float = 0.5,
        *,
        approval: tuple[str, str, str] | None = None,
    ) -> AuthorizeDecision:
        """B6: may an action justified by ``evidence_ids`` proceed for ``namespace``?

        Allowed iff every evidence record is readable by ``namespace`` and its
        CURRENT effective view trust (its trust re-checked against live parents,
        B4', then attenuated across the grant edge) is at least ``threshold``.
        Unknown, unreadable or quarantined evidence denies (fail closed). The
        weakest link is returned so the caller can explain or escalate.

        G29 (plan v3.2, MEM-INV): ``approval=(subject, field, value)`` also requires an
        explicit approval (:meth:`approve`) for exactly that change: a live record
        tagged ``approval`` on key (subject, field) whose value matches exactly, written
        by an operator or the user, and valid now. Deny by default; a remembered
        instruction or a similar value is never an approval.
        """
        return await trust_firewall.authorize(
            self, evidence_ids, namespace, threshold, approval=approval
        )

    def end_session(self, namespace: str = "default", session_id: str | None = None) -> None:
        """B0: forget a session's read ledger (``session_id=None``: the namespace's
        anonymous ledger, which no read or write ever clears on its own)."""
        return ingest_windows.end_session(self, namespace, session_id)

    async def effective_trust(self, record_id: str) -> float:
        """B4': a record's trust re-checked against its CURRENT parents.

        ``min(stored trust, min over parents of their effective view trust)``,
        recursively; a parent that is now missing, quarantined, archived or
        deleted contributes 0 (fail closed). Parents recorded before integrity
        was enabled are ignored if unknown to storage only when integrity is off.
        Memoised per call; cycles are impossible (parents predate children).

        N6: under ``integrity.live_reevaluation`` the grant edge is re-checked
        too. A parent the record's writer could only read through a grant that
        is now revoked (or no longer covers the parent's memory type) counts as
        0, and so does a message whose sender grant is gone. Because the walk
        takes the min over every ancestor, revocation drains the whole derived
        chain. With re-evaluation off, grants are not consulted.
        """
        return await ops_trust.effective_trust(self, record_id)

    async def _grants_cached(
        self, reader: str, cache: dict[str, Mapping[str, frozenset[str] | None]]
    ) -> Mapping[str, frozenset[str] | None]:
        """``reader``'s live grants, read once per caller-held ``cache``."""
        return await memory_shared._grants_cached(self, reader, cache)

    async def _probe_legs(
        self,
        ns: str,
        text: str,
        vector: list[float],
        fetch_k: int,
        lexical: bool,
    ) -> list[list[LegHit]]:
        return await read_legs._probe_legs(self, ns, text, vector, fetch_k, lexical)

    def _encoder(self) -> QueryEncoder:
        return read_legs._encoder(self)

    async def _encoder_legs(self, ns: str, query: str) -> list[list[LegHit]]:
        return await read_legs._encoder_legs(self, ns, query)

    async def _metadata_legs(
        self, ns: str, query: str, fetch_k: int, *, temporal: Sequence[LegHit] = ()
    ) -> list[list[LegHit]]:
        return await read_legs._metadata_legs(self, ns, query, fetch_k, temporal=temporal)

    async def _person_time_leg(self, ns: str, plan: ReadPlan) -> list[LegHit]:
        return await read_legs._person_time_leg(self, ns, plan)

    def _graph_admit(self, ns: str, *, history: bool = False) -> Callable[[MemoryRecord], bool]:
        return read_legs._graph_admit(self, ns, history=history)

    async def _graph_seeds(
        self,
        ns: str,
        query: str,
        fallback_ids: Sequence[str] = (),
    ) -> list[str]:
        return await read_legs._graph_seeds(self, ns, query, fallback_ids)

    async def _graph_walk(
        self,
        ns: str,
        query: str,
        fallback_ids: Sequence[str] = (),
        *,
        history: bool = False,
    ) -> list[tuple[MemoryRecord, int]]:
        return await read_legs._graph_walk(self, ns, query, fallback_ids, history=history)

    async def _graph_leg(
        self,
        ns: str,
        query: str,
        vector_hits: Sequence[VectorHit],
        lexical_hits: Sequence[LexicalHit],
        extra_legs: list[list[LegHit]],
        use_hybrid: bool,
    ) -> list[LegHit]:
        return await read_legs._graph_leg(
            self,
            ns,
            query,
            vector_hits,
            lexical_hits,
            extra_legs,
            use_hybrid,
        )

    async def _graph_node_legs(
        self,
        ns: str,
        query: str,
        query_vector: list[float],
        fetch_k: int,
    ) -> list[list[LegHit]]:
        return await read_legs._graph_node_legs(self, ns, query, query_vector, fetch_k)

    async def _mmr_order(
        self,
        query: str,
        scored: list[tuple[MemoryRecord, float]],
        lam: float,
    ) -> list[tuple[MemoryRecord, float]]:
        return await read_fusion._mmr_order(self, query, scored, lam)

    async def _maxsim_leg(
        self,
        ns: str,
        query_vector: list[float],
        vector_hits: Sequence[Any],
        lexical_hits: Sequence[Any],
    ) -> list[list[LegHit]]:
        return await read_legs._maxsim_leg(self, ns, query_vector, vector_hits, lexical_hits)

    def _perspective_options(self) -> PerspectiveOptions:
        return read_lift._perspective_options(self)

    async def _persp_state_for(self, ns: str) -> dict[str, Any]:
        return await read_lift._persp_state_for(self, ns)

    async def _annotate_perspective(
        self,
        ns: str,
        content: str,
        role: str,
        tags: list[str] | None,
        opts: PerspectiveOptions,
        when: datetime | None = None,
    ) -> list[str]:
        return await read_lift._annotate_perspective(self, ns, content, role, tags, opts, when)

    async def _question_perspective(self, ns: str, query: str) -> QuestionPerspective:
        return await read_lift._question_perspective(self, ns, query)

    def _penalise_reinjected(
        self,
        candidates: list[tuple[MemoryRecord, float]],
        ns: str,
        session_id: str,
    ) -> list[tuple[MemoryRecord, float]]:
        return read_fusion._penalise_reinjected(self, candidates, ns, session_id)

    async def _entity_vocabulary(self, ns: str) -> frozenset[str]:
        return await read_lift._entity_vocabulary(self, ns)

    # ── E04 image assets and E03 public knowledge (both opt-in, default off) ─────────

    def set_asset_adapter(self, adapter: Any) -> None:
        """E04: use ``adapter`` (an ``AssetAdapter``: tests, or a shared instance)."""
        self._assets = adapter

    def set_external_provider(self, provider: Any) -> None:
        """E03: use ``provider`` (an ``ExternalProvider``) instead of ``read.external_provider``."""
        self._external_provider = provider
        self._external = None

    def _asset_adapter(self) -> Any:
        return ingest_assets._asset_adapter(self)

    def asset_stats(self) -> dict[str, int]:
        """E04 cost counters of this engine (fetches, vision calls, failures, cache hits)."""
        return admin.asset_stats(self)

    def external_stats(self) -> dict[str, int]:
        """E03 counters: network calls made, cache hits and misses, refusals, errors."""
        return admin.external_stats(self)

    def _attachment_refs(
        self, turn: Mapping[str, Any], index: int, session_id: str | None
    ) -> list[tuple[Any, str]]:
        return ingest_assets._attachment_refs(self, turn, index, session_id)

    async def _register_assets(
        self,
        refs: list[tuple[Any, str]],
        turn: Mapping[str, Any],
        index: int,
        session_id: str | None,
        namespace: str,
        record_id: str,
    ) -> None:
        await ingest_assets._register_assets(
            self, refs, turn, index, session_id, namespace, record_id
        )

    def _asset_registry(self) -> Any:
        return ingest_assets._asset_registry(self)

    async def _erase_assets(self, namespace: str, record_ids: Sequence[str] | None) -> int:
        return await ingest_assets._erase_assets(self, namespace, record_ids)

    async def import_asset_dir(
        self, directory: str | os.PathLike[str], *, namespace: str | None = None
    ) -> Any:
        """RM-113 item 18: import a pre-item-18 asset directory (the old ``ingest.asset_dir``:
        ``registry.sqlite`` plus ``blobs/``) once. Rows go to the asset registry in the records
        database, bytes to ``storage.documents`` (left in place without that slot); the files
        are renamed ``*.imported``. Idempotent (a content digest is recorded). A row takes the
        namespace of the record that carried it, else ``namespace``; without either it is
        skipped. Returns an ``AssetImportResult`` (None when the directory has no registry)."""
        self._require_started()
        return await ingest_assets._import_asset_dir(self, os.fspath(directory), namespace)

    async def _asset_overlay(
        self,
        query: str,
        ns: str,
        context: AssembledContext,
    ) -> AssembledContext:
        return await read_legs._asset_overlay(self, query, ns, context)

    def _http_connector(self) -> Any:
        """RM-113: the Engine's one shared HTTP connector (asset fetch, vision, external
        search). Built on first use; its pool opens lazily on the first request and is closed
        at stop (a restart reopens it)."""
        if self._http is None:
            from memspine.providers.connectors import http_connector

            self._http = http_connector(self._config().runtime.shared_resources.http)
        return self._http

    def _external_broker(self) -> Any:
        if self._external is None:
            from memspine.services.external import (
                EvidenceCache,
                ExternalBroker,
                HttpSearchProvider,
                NoopProvider,
                ProviderUnavailableError,
                WikipediaProvider,
            )

            read = self._config().read
            provider = self._external_provider
            if provider is None:
                if read.external_provider == "http":
                    try:
                        provider = HttpSearchProvider.from_env(self._http_connector())
                    except ProviderUnavailableError:
                        provider = NoopProvider()  # reported per call as provider_unavailable
                elif read.external_provider == "wikipedia":
                    provider = WikipediaProvider(http=self._http_connector())
                else:
                    provider = NoopProvider()
            self._external = ExternalBroker(
                provider,
                EvidenceCache(read.external_cache_dir),
                max_calls=read.external_max_calls,
                max_results=read.external_max_results,
            )
        return self._external

    async def _external_overlay(
        self,
        query: str,
        ns: str,
        context: AssembledContext,
    ) -> AssembledContext:
        return await read_legs._external_overlay(self, query, ns, context)

    async def _read_overlay(
        self,
        query: str,
        ns: str,
        context: AssembledContext,
        session_id: str | None,
        *,
        shared: bool = False,
    ) -> AssembledContext:
        return await read_legs._read_overlay(self, query, ns, context, session_id, shared=shared)

    async def _perspective_leg(
        self, ns: str, qp: QuestionPerspective, vector_hits: Sequence[Any]
    ) -> list[list[LegHit]]:
        return await read_lift._perspective_leg(self, ns, qp, vector_hits)

    def _apply_perspective(
        self,
        candidates: list[tuple[MemoryRecord, float]],
        qp: QuestionPerspective,
    ) -> list[tuple[MemoryRecord, float]]:
        return read_lift._apply_perspective(self, candidates, qp)

    async def _strip_speaker_names(self, ns: str, query: str) -> str:
        return await read_legs._strip_speaker_names(self, ns, query)

    async def _session_leg(
        self,
        ns: str,
        query_vector: list[float],
        fetch_k: int,
    ) -> list[list[LegHit]]:
        return await read_legs._session_leg(self, ns, query_vector, fetch_k)

    async def _speaker_vote_leg(
        self,
        ns: str,
        query: str,
        query_vector: list[float],
        vector_hits: Sequence[Any],
        fetch_k: int,
    ) -> list[list[LegHit]]:
        return await read_legs._speaker_vote_leg(
            self, ns, query, query_vector, vector_hits, fetch_k
        )

    @staticmethod
    def _bridge_gate(query: str, read: Any) -> bool:
        return read_legs._bridge_gate(query, read)

    async def _bridge_leg(
        self,
        query: str,
        ns: str,
        scored: Sequence[tuple[MemoryRecord, float]],
        *,
        session_id: str | None,
        keep_k: int,
        hide: Callable[[MemoryRecord], bool] | None,
        probes: Sequence[str],
    ) -> NamedLeg | None:
        return await read_legs._bridge_leg(
            self,
            query,
            ns,
            scored,
            session_id=session_id,
            keep_k=keep_k,
            hide=hide,
            probes=probes,
        )

    async def _anchor_legs(
        self,
        ns: str,
        vector_hits: Sequence[Any],
        lexical_hits: Sequence[Any],
        fetch_k: int,
    ) -> list[list[LegHit]]:
        return await read_legs._anchor_legs(self, ns, vector_hits, lexical_hits, fetch_k)

    def _leg_weights_for(self, query: str) -> dict[str, float]:
        return read_legs._leg_weights_for(self, query)

    async def _with_session_neighbours(
        self,
        ns: str,
        candidates: list[tuple[MemoryRecord, float]],
        documents: list[str],
        width: int,
    ) -> list[str]:
        return await read_legs._with_session_neighbours(self, ns, candidates, documents, width)

    async def _graph_rerank(
        self,
        ns: str,
        query: str,
        candidates: list[tuple[MemoryRecord, float]],
    ) -> list[tuple[MemoryRecord, float]]:
        return await read_legs._graph_rerank(self, ns, query, candidates)

    async def _causal_walk(
        self,
        ns: str,
        candidates: list[tuple[MemoryRecord, float]],
        group_id: str | None,
        tags: list[str] | None,
        memory_type: str | None,
    ) -> list[tuple[MemoryRecord, float]]:
        return await read_legs._causal_walk(self, ns, candidates, group_id, tags, memory_type)

    async def _community_gate(
        self,
        ns: str,
        query: str,
        vector_hits: Sequence[VectorHit],
        lexical_hits: Sequence[LexicalHit],
        extra_legs: list[list[LegHit]],
        use_hybrid: bool,
    ) -> list[str]:
        return await read_legs._community_gate(
            self,
            ns,
            query,
            vector_hits,
            lexical_hits,
            extra_legs,
            use_hybrid,
        )

    async def _graph_facts_section(
        self, ns: str, query: str, allowance: int
    ) -> MemoryRecord | None:
        """GP-5 (``read.cards_include_edges``): the graph facts block, or None.

        The edge facts (records tagged ``rel:``) the graph walk reaches from the
        entities the query names, live and superseded alike, one line each:
        ``[2023-05-01 → present] Melanie read "X" (sources: 2)``, a superseded one
        with its end date. ``sources`` counts the live episodes stating the fact
        (its parents plus the ``edge_source:`` provenance of duplicates, GR-9). A
        fact's text goes through the per-record wrappers (marker escaping, the
        instruction and untrusted-note wrappers); a fact repeated verbatim is shown
        once. Lines are kept nearest first while the block fits ``allowance``, then
        shown oldest first. The block's parents are the facts it shows, so the read
        below leaves them out (no record twice)."""
        return await compose_sections._graph_facts_section(self, ns, query, allowance)

    @staticmethod
    def _by_time(
        lines: Mapping[str, tuple[MemoryRecord, str]],
    ) -> list[tuple[MemoryRecord, str]]:
        return read_timeview._by_time(lines)

    async def _admitted_edge_sources(self, ns: str, fact: MemoryRecord) -> int:
        return await read_legs._admitted_edge_sources(self, ns, fact)

    async def _edge_sources(self, storage: RecordsStore, fact: MemoryRecord) -> int:
        return await read_legs._edge_sources(self, storage, fact)

    async def _current_state_view(
        self, ns: str, scored: list[tuple[MemoryRecord, float]]
    ) -> list[tuple[MemoryRecord, float]]:
        """C4': annotate keyed facts with CURRENT / HISTORY from the bi-temporal chain.

        Only an open fact (``valid_to is None``) is labelled CURRENT; a closed one that
        is still activated (a backfill, a contested statement) renders as history. A
        HISTORY entry is a fact that was superseded or ended: it has a ``valid_to``, is
        not quarantined, was not archived by a taint rollback or repair, and passes the
        admission threshold. Each entry is inflated, dated against its own event time
        and wrapped like any other context record.
        """
        return await compose_sections._current_state_view(self, ns, scored)

    def _lead_clean(self, record: MemoryRecord) -> bool:
        """H22: an entry may enter the lead section only unwrapped: not instruction
        flagged and not below the untrusted-note threshold (those records still
        reach the context through retrieval, wrapped)."""
        return compose_sections._lead_clean(self, record)

    def _lead_record(
        self,
        ns: str,
        content: str,
        parts: list[MemoryRecord],
        extra_tags: Sequence[str] = (),
    ) -> MemoryRecord:
        """A synthetic, never-stored context record for one lead block.

        Its trust is the least of its entries' (a block is never more trusted than
        what it shows) and its parents are the entries, so the provenance of every
        line stays visible to the caller. Its purposes are the intersection of the
        entries' and its PII tier their highest (#50), so the purpose gate and the
        remote-LLM gate judge the block as strictly as its strictest entry.
        """
        return compose_sections._lead_record(self, ns, content, parts, extra_tags)

    async def _standing_block(self, ns: str) -> tuple[MemoryRecord, list[MemoryRecord]] | None:
        """H22: the user's stated preferences and standing requests, newest wins a slot.

        Candidates are user-role episodic turns and semantic facts that match the
        standing cue rules, at or above ``read.standing_min_trust``, and pass the
        context gates (status, quarantine, admission, live re-evaluation). An
        external document or tool output never qualifies, whatever it says. The
        E1 instruction flag does not exclude a user's own request (it is the point
        of the block), but a quarantined one never shows.
        """
        return await compose_sections._standing_block(self, ns)

    async def _timeline_blocks(
        self, ns: str, scored: list[tuple[MemoryRecord, float]]
    ) -> list[MemoryRecord]:
        """H22: one dated timeline per topic entity of the retrieved keyed facts.

        Entities come from the best-scored keyed semantic candidates. Each timeline
        lists every live fact on the entity (any attribute) and its superseded
        history, oldest first, keeping the newest ``read.timeline_items``. An entry
        passes the same gates as a context record (live view for current facts, the
        C4' history view for superseded ones; taint rollbacks and retractions never
        show) and must be clean (:meth:`_lead_clean`). An entity with fewer than two
        entries gets no timeline: retrieval already shows a single fact.
        """
        return await compose_sections._timeline_blocks(self, ns, scored)

    async def _lead_section(
        self, ns: str, scored: list[tuple[MemoryRecord, float]], budget_tokens: int
    ) -> tuple[list[MemoryRecord], list[MemoryRecord]]:
        """H22: (standing preferences, timelines) within the lead sub-budget.

        Standing preferences are filled first, then timelines in entity order; a
        block that does not fit is skipped. Each block passes the untrusted-note
        wrapper (its trust is its weakest entry's), like any context record.
        """
        return await compose_sections._lead_section(self, ns, scored, budget_tokens)

    async def _history_view(self, record: MemoryRecord) -> MemoryRecord | None:
        """C4': one HISTORY entry as the model sees it, or None when it may not be shown.

        A record archived by a taint rollback or repair is never shown: the
        durable :data:`_TAINT_ARCHIVED_TAG` covers logs that no longer hold the
        event (:meth:`_taint_archived_ids` covers records archived before the tag).

        Admission uses the stored trust, capped under live re-evaluation by the
        current view of each parent (an archived record's own effective trust is 0 by
        definition, so it cannot be used). The entry is inflated, dated against its
        own event time and wrapped (instruction flag, untrusted note).
        """
        return await compose_sections._history_view(self, record)

    async def _taint_archived_ids(self) -> set[str]:
        """Records whose latest status change was a taint rollback or repair (from the log).

        Scanned incrementally: the last sequence number seen is kept per storage
        instance, so repeated reads only read new events.
        """
        return await trust_quarantine._taint_archived_ids(self)

    def _integrity(self) -> IntegrityPolicy:
        return trust_firewall._integrity(self)

    async def _parent_trust_cap(self, ns: str, parents: Sequence[str]) -> list[float] | None:
        """View trusts of ``parents`` for a writer in ``ns`` (MTI-D input).

        ``None`` when integrity is off or there are no parents — the write is
        then exactly the pre-MTI write. A parent the writer cannot read (missing,
        not live, or outside every grant) counts as 0.0: a caller must not be
        able to raise trust by naming records it never saw.
        """
        return await trust_firewall._parent_trust_cap(self, ns, parents)

    @tap.traced("write")
    @_ops.staged("write")  # T5-06: per-stage ops ledger (a pass-through when it is off)
    @prioritised(Priority.WRITE)  # RT-02: foreground write class (a no-op without the gate)
    async def ingest_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        namespace: str = "default",
        actor: str = "user",
        session_id: str | None = None,
        channel: str = "messages",
        group_id: str | None = None,
        tags: list[str] | None = None,
        valid_from: datetime | None = None,
        conversation_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Accept source messages through the canonical durable ingestion path.

        Preserve supplied ``message_id``, ``session_id``, ``conversation_id`` and
        ``created_at``; generate missing identities and creation times. One missing
        session is generated per call. Each root without an explicit conversation
        starts its own conversation; replies inherit their parent's conversation.
        ``reply_to`` names a source message ID in this namespace, stored already
        or earlier in the batch, with matching session/conversation ownership.

        Nested ``sender`` records identity independently of conversational ``role``.
        Returned records expose resolved source metadata in ``record.source.message``.
        Identical supplied-ID retries return existing results; changed data raises
        until an explicit revision API is provided. Excluded turns produce no record.

        ``created_at`` accepts ISO text or datetime. ``timestamp`` and batch
        ``valid_from`` are accepted time inputs; absent source time uses the engine
        clock and retains its generated provenance. Write policies, firewall and
        trust still apply; optional extraction respects conversation boundaries.
        Acceptance is durable before application, so interrupted writes can resume
        with their original identities. Projection/embedding batching remains an
        internal optimization, not a separate ingestion mode.
        """
        from memspine.ingest.message_ingestion import ingest_messages as ingest_batch

        return await ingest_batch(
            self,
            messages,
            namespace,
            actor,
            session_id,
            channel,
            group_id,
            tags,
            valid_from,
            conversation_id=conversation_id,
        )

    async def _prefetch_neighbours(
        self, ns: str, contents: Sequence[str]
    ) -> _NeighbourBatch | None:
        return await ingest_write._prefetch_neighbours(self, ns, contents)

    async def _write_turns(
        self,
        messages: Sequence[Mapping[str, str]],
        namespace: str,
        actor: str,
        session_id: str | None,
        channel: str,
        group_id: str | None,
        tags: list[str] | None,
        valid_from: datetime | None,
    ) -> list[MemoryRecord]:
        return await ingest_write._write_turns(
            self, messages, namespace, actor, session_id, channel, group_id, tags, valid_from
        )

    def _localize(self, value: datetime) -> datetime:
        return ingest_write._localize(self, value)

    def _event_time(self, value: object) -> datetime | None:
        return ingest_write._event_time(self, value)

    def _sequenced(self, namespace: str, session_id: str, stamp: datetime) -> datetime:
        return ingest_write._sequenced(self, namespace, session_id, stamp)

    def _depositable_contents(self, messages: Sequence[Mapping[str, str]]) -> list[str]:
        return ingest_write._depositable_contents(self, messages)

    async def _prewarm_embeddings(self, texts: Sequence[str]) -> None:
        return await ingest_write._prewarm_embeddings(self, texts)

    @tap.traced("write")
    @_ops.staged("write")  # T5-06: per-stage ops ledger (a pass-through when it is off)
    @prioritised(Priority.WRITE)  # RT-02: foreground write class (a no-op without the gate)
    async def write_episode(
        self,
        messages: Sequence[Mapping[str, str]],
        namespace: str = "default",
        actor: str = "user",
        channel: str = "messages",
        tags: list[str] | None = None,
    ) -> list[MemoryRecord]:
        """C4: ingest a transcript as ONE conversation. Like
        :meth:`ingest_messages`, but stamps every turn with a shared,
        content-derived session id (``message_id``) AND the same ``group_id``
        (D2), so the turns are retrievable and consolidatable as a single
        episode — the M13.2 session detector then treats them as one session at
        consolidation time."""
        return await ingest_write.write_episode(self, messages, namespace, actor, channel, tags)

    async def _write_locked(
        self,
        storage: RecordsStore,
        ns: str,
        record: MemoryRecord,
        memory_type: str,
        actor: str,
        trust_cap: list[float] | None = None,
    ) -> MemoryRecord:
        return await ingest_write._write_locked(
            self, storage, ns, record, memory_type, actor, trust_cap
        )

    async def _write_locked_ex(
        self,
        storage: RecordsStore,
        ns: str,
        record: MemoryRecord,
        memory_type: str,
        actor: str,
        trust_cap: list[float] | None = None,
    ) -> tuple[MemoryRecord, str]:
        return await ingest_write._write_locked_ex(
            self, storage, ns, record, memory_type, actor, trust_cap
        )

    async def _screen_derived(
        self, record: MemoryRecord, trust_cap: list[float]
    ) -> tuple[MemoryRecord, list[str]]:
        return await ingest_write._screen_derived(self, record, trust_cap)

    async def _screen_write(
        self, record: MemoryRecord, trust_cap: list[float] | None
    ) -> tuple[MemoryRecord, FirewallVerdict]:
        return await ingest_write._screen_write(self, record, trust_cap)

    def _redact_fields(self, record: MemoryRecord, fw: FirewallConfig) -> MemoryRecord:
        return ingest_write._redact_fields(self, record, fw)

    # -- I48 / I52 / I53: governance labels (tags only; the text is never copied out) --

    async def _grade_sensitivity(self, content: str, mode: str) -> tuple[str, tuple[str, ...]]:
        return await ingest_write._grade_sensitivity(self, content, mode)

    async def _govern_write(self, record: MemoryRecord) -> MemoryRecord:
        return await ingest_write._govern_write(self, record)

    @staticmethod
    def _mark_inferred(
        record: MemoryRecord, parents: Sequence[MemoryRecord], wc: Any
    ) -> list[str] | None:
        return ingest_write._mark_inferred(record, parents, wc)

    async def review_inferred(
        self, namespace: str = "default", *, include_supported: bool = False
    ) -> list[MemoryRecord]:
        """I48: the review list. Live records tagged ``src:inferred`` that have fewer than
        ``read.inferred_min_support`` distinct supporting user turns (all inferred records
        with ``include_supported``), lowest support first."""
        return await ingest_write.review_inferred(
            self, namespace, include_supported=include_supported
        )

    async def _sensitivity_scope_open(self, query: str) -> bool:
        return await ingest_write._sensitivity_scope_open(self, query)

    async def _scope_check(self, query: str) -> Callable[[MemoryRecord], bool] | None:
        return await ingest_write._scope_check(self, query)

    async def _write_screened(
        self,
        storage: RecordsStore,
        ns: str,
        record: MemoryRecord,
        memory_type: str,
        actor: str,
    ) -> tuple[MemoryRecord, str]:
        return await ingest_write._write_screened(self, storage, ns, record, memory_type, actor)

    @scope_guard.viewer_guarded("retrieve")
    @_passive_scoped
    @tap.traced("read")
    @_ops.staged("read")  # T5-06
    @timed_read  # RT-02: read p95 into the SLO ledger (a pass-through when it is off)
    async def retrieve(
        self,
        namespace: str = "default",
        memory_type: str | None = None,
        group_id: str | None = None,
        tags: list[str] | None = None,
        include_held: bool = False,
        purpose: str | None = None,
        include_passive: bool = False,
    ) -> list[MemoryRecord]:
        """P0 read path: relational listing. ``group_id``/``tags`` (D2) narrow to
        a sub-scope within the namespace; tags match records carrying ALL of the
        given tags.

        #11: records the firewall holds (quarantined) are left out unless
        ``include_held=True``, the operator's audit view; :meth:`list_quarantined`
        is the review queue.

        ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``.
        #53: records of PASSIVE sessions are left out unless ``include_passive=True``
        or ``group_id`` names their group."""
        return await read_router.retrieve(
            self,
            namespace,
            memory_type,
            group_id,
            tags,
            include_held,
            purpose,
            include_passive,
        )

    @scope_guard.viewer_guarded("search", native=True)
    @_passive_scoped
    @_snapshot_scoped
    @tap.traced("read")
    @_ops.staged("read")  # T5-06
    @timed_read  # RT-02: read p95 into the SLO ledger (a pass-through when it is off)
    async def search(
        self,
        query: str,
        namespace: str = "default",
        top_k: int = constants.SEARCH_TOP_K,
        group_id: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        ledger_id: str | None = None,
        sessions: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        memory_types: Sequence[str] | None = None,
        tags_any: Sequence[str] | None = None,
        focal_entity: str | None = None,
        purpose: str | None = None,
        include_passive: bool = False,
        *,
        valid_from_after: DateBound | None = None,
        valid_from_before: DateBound | None = None,
        valid_to_after: DateBound | None = None,
        valid_to_before: DateBound | None = None,
        recorded_after: DateBound | None = None,
        recorded_before: DateBound | None = None,
        date_filter_mode: str = "and",
        viewer: str | None = None,
        graph_filter: QualifierFilter | None = None,
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

        The gates run before the cut to ``top_k``: when archived, quarantined or
        filtered-out records fill the first window, the legs are queried again
        with a wider window (up to 64x) until ``top_k`` live candidates are found
        or the index is exhausted.

        Returns ``(record, score)`` pairs sorted by the M1 composite score
        (recency/relevance/importance + utility), not raw cosine — a stale
        near-duplicate loses to a fresher, proven-useful memory. Each search
        appends one RETRIEVE event so access stats (reinforcement, M1) update
        through the write door like every other mutation. Both E8 stages are
        off by default: results are bit-identical to the plain pipeline.

        ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``.
        #53: records of PASSIVE sessions (session lifecycle) are left out unless
        ``include_passive=True``, ``session_id`` names their session, or ``group_id``
        names their group. With the lifecycle off no record is passive.

        Date filters (#37, :class:`~memspine.core.read_filters.DateFilter`): bounds on
        ``valid_from`` / ``valid_to`` / ``recorded_at`` (``*_after`` inclusive,
        ``*_before`` exclusive; datetimes, dates or ISO text; an open ``valid_to`` is
        later than any date), combined by ``date_filter_mode`` (``and``: every bound,
        ``or``: any). They restrict every leg to the matching records BEFORE fusion and
        the ``top_k`` cut (each leg looks over the whole namespace), so a filter never
        costs recall. No bound: unchanged.

        ``graph_filter`` (RM-93, :class:`~memspine.services.graph.qualifiers.QualifierFilter`):
        graph evidence comes only from compiled claim edges whose typed qualifiers pass
        the filter (unknown never passes; erased edges never). Graph paths that walk
        unqualified association or mention links are skipped for that read, so no graph
        result bypasses the filter. Non-graph legs are unaffected. None: unchanged.
        """
        wait_s = self._config().read.wait_for_pending_s
        if wait_s > 0:  # W2-06c: bounded wait for pending outbox extraction (partial on timeout)
            await ingest_windows.wait_for_pending(self, namespace, wait_s)
        hits = await read_router.search(
            self,
            query,
            namespace,
            top_k,
            group_id,
            tags,
            session_id,
            ledger_id,
            sessions,
            roles,
            memory_types,
            tags_any,
            focal_entity,
            purpose,
            include_passive,
            valid_from_after=valid_from_after,
            valid_from_before=valid_from_before,
            valid_to_after=valid_to_after,
            valid_to_before=valid_to_before,
            recorded_after=recorded_after,
            recorded_before=recorded_before,
            date_filter_mode=date_filter_mode,
            viewer=viewer,
            graph_filter=graph_filter,
        )
        return hits

    async def _after_read(self, verb: str, namespace: str, result: Any) -> Any:
        """FU-ASSET: the single post-read hook, called by ``scope_guard.viewer_guarded`` once per
        outermost read verb, after the viewer filter. W2-11 ``write.asset_describer: on_read``
        describes the asset records the caller is about to get. Off: returns ``result`` as is."""
        if self._config().write.asset_describer == "on_read":
            return await asset_describer.after_read(self, namespace, result)
        return result

    async def _date_allowed(self, ns: str) -> tuple[set[str], int] | None:
        return await read_timeview._date_allowed(self, ns)

    async def _vector_leg(
        self,
        ns: str,
        query_vector: list[float],
        fetch_k: int,
    ) -> list[VectorHit]:
        return await read_legs._vector_leg(self, ns, query_vector, fetch_k)

    async def _lexical_leg(self, ns: str, text: str, fetch_k: int) -> list[LexicalHit]:
        return await read_legs._lexical_leg(self, ns, text, fetch_k)

    async def _tie_keys(self, record_ids: list[str]) -> dict[str, Any]:
        return await read_fusion._tie_keys(self, record_ids)

    async def _gate_hits(
        self,
        ns: str,
        ranked: list[tuple[str, float]],
        group_id: str | None,
        tags: list[str] | None,
        memory_type: str | None = None,
    ) -> list[tuple[MemoryRecord, float]]:
        return await read_gate._gate_hits(self, ns, ranked, group_id, tags, memory_type)

    async def _search(
        self,
        query: str,
        namespace: str,
        top_k: int,
        *,
        group_id: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        keep_k: int,
        memory_type: str | None = None,
        hide: Callable[[MemoryRecord], bool] | None = None,
        probes: Sequence[str] = (),
        fused_legs: Sequence[list[LegHit]] = (),
        search_pool: int = 0,
    ) -> list[tuple[MemoryRecord, float]]:
        return await read_router._search(
            self,
            query,
            namespace,
            top_k,
            group_id=group_id,
            tags=tags,
            session_id=session_id,
            keep_k=keep_k,
            memory_type=memory_type,
            hide=hide,
            probes=probes,
            fused_legs=fused_legs,
            search_pool=search_pool,
        )

    def _reply_budget(self, budget_tokens: int) -> int:
        """ContextPipe: the budget left once ``read.reply_reserve_tokens`` is kept free."""
        return compose_pack._reply_budget(self, budget_tokens)

    @scope_guard.viewer_guarded("assemble", native=True)
    @_passive_scoped
    @_snapshot_scoped
    @tap.traced("read")
    @_ops.staged("read")  # T5-06
    @timed_read
    async def assemble(
        self,
        query: str,
        namespace: str = "default",
        budget_tokens: int = constants.ASSEMBLE_BUDGET_TOKENS,
        top_k: int = constants.ASSEMBLE_TOP_K,
        shared: bool = False,
        session_id: str | None = None,
        ledger_id: str | None = None,
        sessions: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        memory_types: Sequence[str] | None = None,
        tags_any: Sequence[str] | None = None,
        focal_entity: str | None = None,
        purpose: str | None = None,
        include_passive: bool = False,
        *,
        as_of: DateBound | None = None,
        viewer: str | None = None,
    ) -> AssembledContext:
        """Retrieval + M12/E2 assembly: MMR-selected, cache-aware-ordered context.

        Persona and other stable records sit before ``boundary_index``; volatile
        episodic/working content after it — feed the prefix to provider caching.

        ``shared=True`` (G9) assembles over own + granted records via
        ``shared_search``, so a multi-agent turn sees exactly what its grants allow
        (trust-capped, and under ``integrity.*`` attenuated and admitted at theta).

        ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``.
        #53: PASSIVE-session records enter only with ``include_passive=True`` or when
        ``session_id`` names their session (see :meth:`search`).

        ``as_of`` (W7): as in :meth:`read`.
        """
        return await compose_windows.assemble(
            self,
            query,
            namespace,
            budget_tokens,
            top_k,
            shared,
            session_id,
            ledger_id,
            sessions,
            roles,
            memory_types,
            tags_any,
            focal_entity,
            purpose,
            include_passive,
            as_of=as_of,
            viewer=viewer,
        )

    async def _assemble(
        self,
        query: str,
        namespace: str,
        budget_tokens: int,
        top_k: int,
        *,
        shared: bool,
        session_id: str | None,
    ) -> AssembledContext:
        """:meth:`assemble` without the purpose scope and the read audit."""
        return await compose_windows._assemble(
            self, query, namespace, budget_tokens, top_k, shared=shared, session_id=session_id
        )

    async def _procedural_blocks(
        self, ns: str, query: str, session_id: str | None
    ) -> tuple[list[MemoryRecord], list[MemoryRecord]]:
        """W17e / W17a read blocks: the task state pinned for ``session_id`` (with
        ``memories.procedural.policies.task_state``) and the advisory lessons for the
        query (with ``lessons: {inject: true}``). Both empty by default."""
        return await compose_pack._procedural_blocks(self, ns, query, session_id)

    @staticmethod
    def _place_procedural(
        assembled: AssembledContext, pinned: list[MemoryRecord], lessons: list[MemoryRecord]
    ) -> AssembledContext:
        """W17e: the task state right after the persona (stable prefix); W17a: the
        lessons last, after every piece of evidence, never in place of it."""
        return compose_pack._place_procedural(assembled, pinned, lessons)

    async def _expand_trajectories(
        self, ns: str, assembled: AssembledContext, budget_tokens: int
    ) -> AssembledContext:
        """N27 (``memories.procedural.policies.trajectory: {expand: true}``): a hit
        on a trajectory step or head brings its neighbouring steps (``radius``,
        default 1) right after it, inside its group, at most ``cap`` (default 20)
        added states, while the budget allows."""
        return await compose_pack._expand_trajectories(self, ns, assembled, budget_tokens)

    async def _assemble_core(
        self,
        query: str,
        ns: str,
        budget_tokens: int,
        top_k: int,
        *,
        shared: bool = False,
        session_id: str | None = None,
        hide: Callable[[MemoryRecord], bool] | None = None,
        probes: Sequence[str] = (),
        legs: Sequence[list[LegHit]] = (),
    ) -> AssembledContext:
        """:meth:`assemble` without the reply reserve and the final render (callers
        apply both once, so the replay read can extend the context first).

        ``hide`` (G1b/G3b): candidates a read header already carries leave.
        ``probes`` (#35): extra search texts fused into the search as RRF legs.
        ``legs`` (#36): precomputed ranked legs fused into the search by RRF."""
        return await compose_windows._assemble_core(
            self,
            query,
            ns,
            budget_tokens,
            top_k,
            shared=shared,
            session_id=session_id,
            hide=hide,
            probes=probes,
            legs=legs,
        )

    @staticmethod
    def _place_lead(
        assembled: AssembledContext,
        standing: list[MemoryRecord],
        timelines: list[MemoryRecord],
    ) -> AssembledContext:
        """H22: standing preferences right after the pinned persona (stable prefix,
        they change only when the user states a new one); timelines open the
        volatile part (they depend on the query). An abstained assembly keeps the
        standing block but gets no timelines (they came from weak candidates)."""
        return compose_pack._place_lead(assembled, standing, timelines)

    async def _claims_only(
        self,
        ns: str,
        scored: list[tuple[MemoryRecord, float]],
        *,
        expand: bool,
        hide: Callable[[MemoryRecord], bool] | None = None,
    ) -> list[tuple[MemoryRecord, float]]:
        """B9 facts-only: low-trust raw records leave; their mined facts may stand in.

        A record below ``integrity.claims_only_below`` (view trust) that is not
        itself a mined fact, the pinned persona or a lead block is removed. With
        ``expand``, each live, unflagged atomic fact mined from it takes its place
        once, prefixed :data:`constants.CLAIM_MARKER` and scored like the record it
        replaces. Facts already in the list are not repeated, and facts ``hide``
        leaves out (a read header shows them, labelled there) do not stand in.
        """
        return await compose_pack._claims_only(self, ns, scored, expand=expand, hide=hide)

    async def _decorate(
        self,
        ns: str,
        scored: list[tuple[MemoryRecord, float]],
        *,
        expand_claims: bool = True,
        hide: Callable[[MemoryRecord], bool] | None = None,
    ) -> list[tuple[MemoryRecord, float]]:
        """The context-entry transforms shared by every read mode (projection only).

        B9 facts-only (low-trust raw records replaced by their mined claims, or
        dropped when ``expand_claims`` is off), H1 relative dates (each record
        against its own event time), the E1 instruction-flag wrapper, the C4'
        current-state view, then the B6 untrusted-note wrapper. ``hide`` keeps
        B9 from putting back a fact a read header already shows.
        """
        return await compose_pack._decorate(
            self, ns, scored, expand_claims=expand_claims, hide=hide
        )

    def _render(
        self, query: str, assembled: AssembledContext, budget_tokens: int | None = None
    ) -> AssembledContext:
        """The final presentation step shared by every read mode.

        H16 time order for ordering questions, H5 dated render and H22 gap
        markers. A gap marker goes on a record whose chronological predecessor in
        the volatile part is at least a day older, whatever the placement order.

        N4: the dated render adds text after selection, so ``tokens_used`` is
        recounted on the rendered content with the assembly counter. With
        ``budget_tokens``, volatile records are dropped lowest priority first (the
        input order is the priority order) until the rendered context fits, never
        below one record. The plain render changes nothing.
        """
        return compose_render._render(self, query, assembled, budget_tokens)

    def _render_volatile(
        self, volatile: list[MemoryRecord], *, gap_markers: bool
    ) -> list[MemoryRecord]:
        """H5 dated render of the volatile part, with H22 gap markers when enabled."""
        return compose_render._render_volatile(self, volatile, gap_markers=gap_markers)

    def chat_messages(
        self, message: str, context: str = "", *, condition: str | None = None
    ) -> list[dict[str, str]]:
        """Render the ``chat`` prompt for the caller's own model call.

        memspine does not answer; this hands back the messages to send, with the
        memory context in place. ``prompts.selection.chat`` picks the variant (the
        ``assistant`` template selects ``chat@dated``, H12); ``condition`` overrides it
        per call (``"dated"``, or ``""`` for the base prompt). C2:
        ``prompts.selection.chat_by_shape`` (e.g. ``{temporal: infer, inference: infer}``)
        picks the condition by ``message``'s question shape; unset, nothing changes.
        """
        return compose_render.chat_messages(self, message, context, condition=condition)

    @staticmethod
    def final_answer(reply: str) -> str:
        """#34: the short answer of a reply to a reasoning chat prompt (``chat@dated3``):
        the text after its last ``Answer:`` line, ``<think>`` blocks dropped; a reply
        without the marker comes back whole (stripped)."""
        return final_answer(reply)

    async def verify_answer(
        self,
        question: str,
        answer: str,
        context: AssembledContext | Sequence[MemoryRecord] | str,
    ) -> AnswerVerification:
        """#39: is ``answer`` supported by ``context``? One ``verify_answer`` role call
        (the ``chat`` role when that one is not bound), prompt ``verify_answer``.

        ``context`` is a read's :class:`AssembledContext`, its records, or plain text;
        the prompt sees it as numbered lines (one per record, whitespace collapsed, or
        one per non-empty text line). Returns ``supported``, the ``evidence_ids`` of the
        supporting lines (record ids, or ``L<n>`` for a text context) and, when the
        answer is not supported but the context supports another, ``revised_answer``
        (else None). Raises :class:`MissingServiceError` when neither role is bound and
        :class:`~memspine.exceptions.LLMError` when the reply is unusable. Nothing is
        read or written; no read path calls it."""
        return await compose_answer.verify_answer(self, question, answer, context)

    @scope_guard.viewer_guarded("read", native=True)
    @_passive_scoped
    @_snapshot_scoped
    @tap.traced("read")
    @_ops.staged("read")  # T5-06
    @timed_read  # RT-02: read p95 into the SLO ledger (a pass-through when it is off)
    async def read(
        self,
        query: str,
        namespace: str = "default",
        mode: str | None = None,
        budget_tokens: int = constants.ASSEMBLE_BUDGET_TOKENS,
        top_k: int = constants.ASSEMBLE_TOP_K,
        replay_window: int = 2,
        compose_pool: int = 3,
        session_id: str | None = None,
        ledger_id: str | None = None,
        sessions: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        memory_types: Sequence[str] | None = None,
        tags_any: Sequence[str] | None = None,
        focal_entity: str | None = None,
        purpose: str | None = None,
        include_passive: bool = False,
        *,
        valid_from_after: DateBound | None = None,
        valid_from_before: DateBound | None = None,
        valid_to_after: DateBound | None = None,
        valid_to_before: DateBound | None = None,
        recorded_after: DateBound | None = None,
        recorded_before: DateBound | None = None,
        date_filter_mode: str = "and",
        as_of: DateBound | None = None,
        viewer: str | None = None,
        graph_filter: QualifierFilter | None = None,
    ) -> ReadResult:
        """C7': mode-routed read. Rules decide; no model on the read path.

        ``mode=None`` uses ``read.default_mode`` (``auto`` unless a template pins one).

        - ``full``: every live, admitted record in the namespace, chronological,
          when it fits ``budget_tokens`` (else falls back to ``retrieve``);
        - ``replay``: :meth:`assemble`, then each retrieved episodic turn is
          expanded to ``replay_window`` neighbouring raw turns of its session
          (the hit first, then neighbours nearest first, skipping any that do
          not fit);
        - ``retrieve``: exactly :meth:`assemble`;
        - ``compose`` (H3): for aggregation questions. Pools ``compose_pool x top_k``
          candidates from the query and its core-terms probe (rank-fused with
          ``read.rrf_k``), then picks them session-diversely (the best hit of each
          session in turn) up to ``2 x top_k`` records and the budget,
          chronological. It abstains, applies the relative floor and drops near
          duplicates like assembly;
        - ``auto``: ``full`` if it fits; else ``compose`` for aggregation questions;
          else ``replay`` when episodic memory is on and a hit is episodic; else
          ``retrieve``.

        Every mode keeps ``read.reply_reserve_tokens`` free and gets the same
        presentation: relative-date annotation, the current-state view, the
        wrappers, dated render and gap markers.

        Full and replay honour the same gates as search: erased (DELETED),
        superseded, quarantined, grant and cue records never enter, and under
        ``integrity.enabled`` nothing below the admission threshold does (with
        live re-evaluation, judged on the effective trust); the instruction-flag
        and untrusted-note wrappers apply as in assembly.

        ``session_id`` keys the B0 read ledger (as in :meth:`assemble`), for the read
        headers and the routed read alike.

        ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``.
        #53: PASSIVE-session records enter only with ``include_passive=True`` or when
        ``session_id`` names their session (see :meth:`search`).

        Date filters (#37): as in :meth:`search`, for every search of the read (the
        headers' included), the records a ``full`` read lists and the neighbours a
        replay or compose read adds. The pinned persona and the lead section are not
        dated evidence and are not filtered. No bound: unchanged.

        ``as_of`` (W7, plan v3.2): read the world as memory says it stood at that time
        (valid time): only records begun by then (unless the caller's own
        ``valid_from_before`` says otherwise), a fact superseded after it counts as the
        current one, and relative phrases in the question resolve against it. For
        "what did memory know then" add ``recorded_before``. None: unchanged.

        ``graph_filter`` (RM-93): as in :meth:`search`, for every search of the read.
        """
        return await read_router.read(
            self,
            query,
            namespace,
            mode,
            budget_tokens,
            top_k,
            replay_window,
            compose_pool,
            session_id,
            ledger_id,
            sessions,
            roles,
            memory_types,
            tags_any,
            focal_entity,
            purpose,
            include_passive,
            valid_from_after=valid_from_after,
            valid_from_before=valid_from_before,
            valid_to_after=valid_to_after,
            valid_to_before=valid_to_before,
            recorded_after=recorded_after,
            recorded_before=recorded_before,
            date_filter_mode=date_filter_mode,
            as_of=as_of,
            viewer=viewer,
            graph_filter=graph_filter,
        )

    async def _read(
        self,
        query: str,
        namespace: str,
        mode: str | None,
        budget_tokens: int,
        top_k: int,
        replay_window: int,
        compose_pool: int,
        session_id: str | None,
    ) -> ReadResult:
        return await read_router._read(
            self,
            query,
            namespace,
            mode,
            budget_tokens,
            top_k,
            replay_window,
            compose_pool,
            session_id,
        )

    async def _agentic_fires(self, query: str, read: Any, records: Sequence[MemoryRecord]) -> bool:
        return await read_deep._agentic_fires(self, query, read, records)

    async def _agentic_extend(
        self,
        query: str,
        ns: str,
        result: ReadResult,
        budget_tokens: int,
        *,
        hide: Callable[[MemoryRecord], bool] | None,
        session_id: str | None,
    ) -> ReadResult:
        return await read_deep._agentic_extend(
            self,
            query,
            ns,
            result,
            budget_tokens,
            hide=hide,
            session_id=session_id,
        )

    async def _deep_extend(
        self,
        query: str,
        ns: str,
        result: ReadResult,
        budget_tokens: int,
        *,
        hide: Callable[[MemoryRecord], bool] | None,
        session_id: str | None,
    ) -> ReadResult:
        return await read_deep._deep_extend(
            self,
            query,
            ns,
            result,
            budget_tokens,
            hide=hide,
            session_id=session_id,
        )

    def _agentic_assemble(
        self,
        query: str,
        result: ReadResult,
        added: Sequence[MemoryRecord],
        displaced: Sequence[MemoryRecord],
        info: dict[str, Any],
        notes: Sequence[MemoryRecord] = (),
    ) -> ReadResult:
        return read_deep._agentic_assemble(self, query, result, added, displaced, info, notes)

    async def _event_ledger_extend(
        self, query: str, ns: str, result: ReadResult, budget_tokens: int
    ) -> ReadResult:
        return await read_deep._event_ledger_extend(self, query, ns, result, budget_tokens)

    async def _read_routed(
        self,
        query: str,
        ns: str,
        mode: str,
        budget_tokens: int,
        top_k: int,
        replay_window: int,
        compose_pool: int,
        *,
        hide: Callable[[MemoryRecord], bool] | None = None,
        full_hide: Callable[[MemoryRecord], bool] | None = None,
        session_id: str | None = None,
    ) -> ReadResult:
        return await read_router._read_routed(
            self,
            query,
            ns,
            mode,
            budget_tokens,
            top_k,
            replay_window,
            compose_pool,
            hide=hide,
            full_hide=full_hide,
            session_id=session_id,
        )

    async def _attach_antecedents(
        self,
        ns: str,
        hits: Sequence[MemoryRecord],
        chosen: list[MemoryRecord],
        seen: set[str],
        best_window: set[str],
        used: int,
        budget_tokens: int,
        read_cfg: Any,
    ) -> int:
        return await read_deep._attach_antecedents(
            self,
            ns,
            hits,
            chosen,
            seen,
            best_window,
            used,
            budget_tokens,
            read_cfg,
        )

    def _list_mode_fires(self, query: str) -> bool:
        return read_request._list_mode_fires(self, query)

    # -- I28: the optional decider (OpenDecider-nano) at the read path's decision points --

    def _decider_active(self, task: str) -> bool:
        config = self._config()
        slot = read_decider_slot(config)
        if slot is None:
            return False  # no model.decider.read (or heuristic): the heuristic rules (I28)
        if task == "sensitivity_scope":
            return config.read.sensitivity_gate == "decider"
        return task in slot.tasks

    def set_decider(self, decider: Decider | None) -> None:
        """I28: use ``decider`` for the read-path decision points configured by
        ``model.decider.read.tasks`` (tests, or a shared instance). None: build from the
        config."""
        self._decider_override = decider

    def _decider_adapter(self) -> Decider:
        override = getattr(self, "_decider_override", None)
        if override is not None:
            return override  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        slot = read_decider_slot(self._config())
        key: Any = (
            None
            if slot is None
            else (slot.provider, tuple(sorted(slot.settings.items())), slot.workers)
        )
        cached = getattr(self, "_decider_cache", None)
        if cached is None or cached[0] != key:
            if cached is not None:
                # A replaced decider keeps its weight lease until teardown releases it
                # (an in-flight decision may still be using it).
                self.__dict__.setdefault("_retired_deciders", []).append(cached[1])
            cached = (
                key,
                # RM-73: weights shared per process; this Engine's teardown releases.
                build_read_decider(slot, registry=self._registry()),
            )
            self._decider_cache = cached
        return cached[1]

    async def _release_deciders(self) -> None:
        """Release the decider weight leases this Engine holds (other Engines keep theirs)."""
        held = list(self.__dict__.pop("_retired_deciders", []))
        cached = self.__dict__.pop("_decider_cache", None)
        if cached is not None:
            held.append(cached[1])
        for decider in held:
            release = getattr(decider, "aclose", None)
            if callable(release):
                await release()

    async def _decided(self, task: str, question: str, context: str | None, heuristic: str) -> str:
        return await read_request._decided(self, task, question, context, heuristic)

    async def _list_fires(self, query: str) -> bool:
        return await read_request._list_fires(self, query)

    async def _bridge_fires(
        self,
        query: str,
        read: Any,
        scored: Sequence[tuple[MemoryRecord, float]],
    ) -> bool:
        return await read_request._bridge_fires(self, query, read, scored)

    async def _gate_raw_tops(self, ns: str, text: str, legs: Sequence[str]) -> dict[str, float]:
        return await read_gate._gate_raw_tops(self, ns, text, legs)

    async def _store_calibration(
        self, ns: str, legs: Sequence[str]
    ) -> tuple[dict[str, Calibration], bool]:
        return await read_gate._store_calibration(self, ns, legs)

    async def _calibrated_gate(
        self, query: str, result: ReadResult, ns: str | None = None
    ) -> ReadResult:
        return await read_gate._calibrated_gate(self, query, result, ns)

    async def _gate_bypassed(self, query: str, ns: str | None, result: ReadResult) -> bool:
        return await read_gate._gate_bypassed(self, query, ns, result)

    async def _relevance_gate(
        self, query: str, result: ReadResult, ns: str | None = None
    ) -> ReadResult:
        return await read_gate._relevance_gate(self, query, result, ns)

    async def _replay_neighbour(self, record_id: str, ns: str) -> MemoryRecord | None:
        """C7': a replayed neighbour turn, gated, inflated and decorated; None if not shown.

        #37: a neighbour outside the active date filter is not shown."""
        return await compose_sections._replay_neighbour(self, record_id, ns)

    async def _cards_section(
        self,
        ns: str,
        query: str,
        budget_tokens: int,
        session_id: str | None = None,
        *,
        strong: bool = False,
    ) -> MemoryRecord | None:
        """G1b: the cards header, or None (``read.cards: off``, or no fact to show).

        The namespace's mined facts relevant to ``query`` come from the same hybrid
        search, restricted to ``atomic_fact`` records, so they pass every search
        gate (status, quarantine, admission, live re-evaluation). Each gets the
        per-record wrappers, then one ``[said YYYY-MM-DD] Entity: fact`` line (the
        date it was said: its earliest source turn; no date without one), best
        first while the block, rendered exactly so, fits ``read.cards_budget_share x
        budget_tokens``; the kept lines are shown in said order. Under B9 a card
        whose source turns sit below ``integrity.claims_only_below`` carries
        :data:`constants.CLAIM_MARKER`, as its claim would in the routed read.
        """
        return await compose_sections._cards_section(
            self, ns, query, budget_tokens, session_id, strong=strong
        )

    async def _from_low_trust(
        self, parents: list[MemoryRecord], threshold: float, _depth: int = 0
    ) -> bool:
        """B9 for a card: a source turn below ``threshold`` (view trust) makes it a claim.

        A parent that is itself a mined fact (a #30 list card's parents) is judged on
        its own source turns: the card is a claim when any of its facts would be."""
        return await compose_sections._from_low_trust(self, parents, threshold, _depth)

    @staticmethod
    def _cards_text(
        cards: list[MemoryRecord],
        said: dict[str, datetime] | None = None,
        claims: set[str] | None = None,
        event_dates: bool = False,
        *,
        happened_first: bool = False,
    ) -> str:
        return compose_sections._cards_text(
            cards, said, claims, event_dates, happened_first=happened_first
        )

    async def _applies_to_person(self, ns: str, query: str) -> bool:
        return await read_lift._applies_to_person(self, ns, query)

    async def _slots_section(self, ns: str, query: str, budget_tokens: int) -> MemoryRecord | None:
        """W5 (plan v3.2, ``read.profile_slots_header``): the Λ-profile block: for each
        person the question names (else the asker, ``user``), their current keyed
        STATE facts (home, origin, job, favourites, attitudes …; one per key, the live
        one) as one line each. Slots tagged ``sensitive:*`` stay out unless
        ``read.profile_sensitive``. Under ``read.profile_scope_gate`` only for
        questions it applies to. None when nothing is known."""
        return await compose_sections._slots_section(self, ns, query, budget_tokens)

    async def _subject_card(self, ns: str, query: str, budget_tokens: int) -> MemoryRecord | None:
        """I50 (``read.profile_subject_card``): the per-subject profile card. The live
        ``kind:state`` facts of ``ns`` are rolled up per subject (the record's ``sub:`` tags,
        else its entity); only the subjects the question's resolved persons name get a block
        (a person, the asker for "I / my", the assistant for "you", ``mother@caroline`` for
        "Caroline's mother"). One attribute holding several live values (cardinality many)
        lists them in time order; a single-valued one is its live (latest) value. The same
        sensitivity and I33 relevance gates as the slots block. None when no subject matches."""
        return await compose_sections._subject_card(self, ns, query, budget_tokens)

    async def _facts_to_sources(
        self, ns: str, scored: list[tuple[MemoryRecord, float]]
    ) -> list[tuple[MemoryRecord, float]]:
        """N54: each mined fact (``atomic_fact``) replaced, at its rank and score, by
        its live source turns (its parents); a turn already listed is not repeated.
        A fact whose sources are all gone or gated keeps its own slot."""
        return await compose_sections._facts_to_sources(self, ns, scored)

    async def _recent_section(self, ns: str, query: str, budget_tokens: int) -> MemoryRecord | None:
        """C2 (``read.recent_exchanges``): the namespace's last N live episodic turns,
        oldest first, within ``RECENT_SHARE`` of the budget, leaving out a turn that is
        the question itself (the in-flight message). The turns stay readable below."""
        return await compose_sections._recent_section(self, ns, query, budget_tokens)

    async def _session_digest_section(
        self, ns: str, query: str, budget_tokens: int, session_id: str | None = None
    ) -> MemoryRecord | None:
        """N31 (``read.session_digest``; Memori session summaries, made extractive):
        for the episodic sessions of the first ``SESSION_DIGEST_HITS`` hits, one line
        each of the ``SESSION_DIGEST_SENTENCES`` sentences sharing most content words
        with the question, within ``SESSION_DIGEST_SHARE`` of the budget. The block's
        parents are the turns quoted; they are not hidden from the read."""
        return await compose_sections._session_digest_section(
            self, ns, query, budget_tokens, session_id
        )

    async def _novelty_section(
        self, ns: str, query: str, budget_tokens: int
    ) -> MemoryRecord | None:
        """G32 (plan v3.2, ``read.novelty_exclusions``): for a request for something new
        ("a book I haven't read"), a header listing what memory says the asker already
        likes, does or has had (keyed facts whose attribute is ``likes``,
        ``activities``, ``favourite_*``, ``pets`` or a list card), so the answer can
        avoid repeating it. None for any other question or when nothing is known."""
        return await compose_sections._novelty_section(self, ns, query, budget_tokens)

    async def _profile_section(
        self, ns: str, query: str, budget_tokens: int, session_id: str | None = None
    ) -> MemoryRecord | None:
        """G3b: the profile header, or None (``read.profile_header`` off, or nothing).

        Candidates are the H14 profile insights (``reflect_profile`` deposits:
        reflective records from the ``reflection`` channel) from the same search,
        restricted to reflective records, so every search gate applies. Insights
        that name a person in the query come first; when none does, the most
        relevant insights are used. Lines are kept best first while the block fits
        ``read.profile_budget_share x budget_tokens``.

        #40: under ``read.profile_header_packing`` the packed block replaces it
        (:meth:`_packed_profile_section`).
        """
        return await compose_sections._profile_section(self, ns, query, budget_tokens, session_id)

    async def _packed_profile_section(
        self, ns: str, query: str, budget_tokens: int, session_id: str | None = None
    ) -> MemoryRecord | None:
        """#40: the packed profile header, or None (nothing relevant fits).

        Three searches, each gated like any search and each shown only when its hits
        pass M12 abstention: the session summaries (semantic, ``consolidation``
        channel), the H14 profile observations (reflective ``reflected:`` insights) and
        the other hits (neither, and no mined fact while the cards header shows them).
        Each candidate gets the per-record wrappers (marker escaping, instruction and
        untrusted-note wrappers, H1 dates), then :func:`pack_profile` keeps summaries,
        then observations, then hits while the block fits ``read.profile_header_budget``
        tokens, never more than :data:`constants.PROFILE_PACK_MAX_SHARE` of the budget.
        The routed read then leaves the packed records out (they are the block's parents).
        """
        return await compose_sections._packed_profile_section(
            self, ns, query, budget_tokens, session_id
        )

    async def _read_headers(
        self,
        ns: str,
        query: str,
        budget_tokens: int,
        session_id: str | None = None,
        *,
        strong: bool = False,
    ) -> list[MemoryRecord]:
        """G1b/G3b: the cards header, then the profile header (each optional).

        ``session_id`` keys their searches in the B0 read ledger, like the read's own.
        F5 (``read.verbatim_raw_only``): a verbatim question gets none."""
        return await compose_pack._read_headers(
            self, ns, query, budget_tokens, session_id, strong=strong
        )

    async def _entity_summary_section(
        self, ns: str, query: str, allowance: int
    ) -> MemoryRecord | None:
        """GP-6 (``read.entity_summaries``): the "About" block, or None.

        One ``About <Name>: …`` line per entity the query names (the graph leg's
        seeds) that has a live entity summary (``summarize_entities`` stage), in
        seed order, while the block fits ``allowance``. A summary passes the graph
        admission gate (status, quarantine, trust floor, integrity) and the
        per-record context wrappers; the block's parents are the summaries shown,
        so the read below leaves them out."""
        return await compose_sections._entity_summary_section(self, ns, query, allowance)

    def _count_allowance(self, query: str, budget_tokens: int) -> int:
        """E3: the tokens kept for the occurrences block; 0 when ``read.count_timeline``
        is off, the query is not a count question, or the share fits no line."""
        return compose_pack._count_allowance(self, query, budget_tokens)

    def _duration_section(
        self,
        ns: str,
        query: str,
        assembled: AssembledContext,
        headers: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        """N10 (plan v3.2, ``read.span_line``): for a duration question ("how long
        after …", "how many weeks between …"), one line computed by the engine from
        the two retrieved turns that best match the question and carry different
        days: ``A (date) -> B (date): N days (about W weeks / M months)``. The reader
        no longer does the date arithmetic. Unchanged when off, for other questions,
        or when two dated matches are not found."""
        return compose_sections._duration_section(self, ns, query, assembled, headers)

    def _count_section(
        self,
        ns: str,
        query: str,
        assembled: AssembledContext,
        allowance: int,
        headers: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        """E3: ``headers`` led by the occurrences block when there is one.

        Its lines are the distinct dated mentions of the counted event
        (:func:`count_terms`, :func:`mentions_event`) among the episodic records the
        read put in the volatile context, so every read gate already applied; mined
        facts, lead blocks and wrapped records (instruction-flagged or untrusted, which
        stay in the context wrapped) are left out. Same-day mentions of one event count
        once (:func:`distinct_occurrences`). Lines are kept oldest first while the block
        fits ``allowance``. The mentions stay in the context: the block points at them.
        """
        return compose_sections._count_section(self, ns, query, assembled, allowance, headers)

    def _cap_lead_blocks(
        self, headers: list[MemoryRecord], count_share: int, budget_tokens: int
    ) -> tuple[list[MemoryRecord], int]:
        """H12 (ADR-055, ``read.lead_budget_share``): the lead blocks and the count
        reserve, capped together at that share of ``budget_tokens``.

        The occurrences reserve (count questions only) is kept first, as far as it
        fits; then whole header blocks leave in :data:`constants.LEAD_BLOCK_DROP_ORDER`
        (profile, entity summaries, graph facts, cards; the later of two same-kind
        blocks first) until the rest fits. Unset: both returned unchanged."""
        return compose_pack._cap_lead_blocks(self, headers, count_share, budget_tokens)

    @staticmethod
    def _headers_cost(headers: list[MemoryRecord]) -> int:
        return compose_pack._headers_cost(headers)

    async def _cluster_legs(
        self,
        ns: str,
        scored: list[tuple[MemoryRecord, float]],
        fetch_k: int,
    ) -> list[list[LegHit]]:
        return await read_legs._cluster_legs(self, ns, scored, fetch_k)

    async def _session_capped(
        self,
        query: str,
        ns: str,
        want: int,
        cap: int,
        *,
        session_id: str | None,
        hide: Callable[[MemoryRecord], bool] | None,
        probes: Sequence[str],
        legs: Sequence[list[LegHit]],
    ) -> list[tuple[MemoryRecord, float]]:
        return await read_fusion._session_capped(
            self,
            query,
            ns,
            want,
            cap,
            session_id=session_id,
            hide=hide,
            probes=probes,
            legs=legs,
        )

    async def _raw_evidence_strong(
        self, ns: str, query: str, top_k: int, session_id: str | None
    ) -> bool:
        return await read_gate._raw_evidence_strong(self, ns, query, top_k, session_id)

    def _cards_gated(self, query: str, *, strong: bool = False) -> bool:
        """ADR-055 addendum (``read.cards_only_aggregate``): True when ``query`` gets
        no cards header and no mined fact in its routed read.

        Only with ``read.cards: header`` and the key on, and only for a question that
        is neither a list/count question (``is_aggregation`` / ``is_count``) nor a
        date question (``is_temporal``): date questions keep the ``cards_skip_temporal``
        / ``cards_temporal`` behaviour unchanged, except under
        ``read.cards_skip_hides_facts``: a date question that skips the cards header
        (``cards_skip_temporal`` with ``cards_temporal: skip``) then reads with every
        mined fact hidden too."""
        return compose_pack._cards_gated(self, query, strong=strong)

    @staticmethod
    def _header_hide(
        headers: list[MemoryRecord], *, all_facts: bool = True, hide_facts: bool = False
    ) -> Callable[[MemoryRecord], bool] | None:
        """What the routed read leaves out: every record a header shows, and with
        the cards header every mined fact (facts reach the context through it).
        ``all_facts=False`` (a ``full`` read) leaves out only the shown records.
        ``hide_facts`` (``read.cards_only_aggregate`` gating the cards header off)
        leaves out every mined fact even without a cards header, in a ``full`` read
        too, so the question reads as with mining off."""
        return compose_pack._header_hide(headers, all_facts=all_facts, hide_facts=hide_facts)

    def _header_abstains(self, hits: list[tuple[MemoryRecord, float]]) -> bool:
        """A header is evidence: it is shown only when its own hits pass M12 abstention."""
        return compose_pack._header_abstains(self, hits)

    def _attach_headers(
        self, assembled: AssembledContext, headers: list[MemoryRecord]
    ) -> AssembledContext:
        """G1b/G3b: the headers open the volatile part (after the stable prefix),
        cards first.

        Each header passed M12 abstention on its own hits, so it is evidence: when
        the routed read abstained because the headers carry what it would have
        shown (A-1/A-3: mined facts hidden, or B9 dropping their low-trust source
        turns), the headers still go in and the read no longer abstains."""
        return compose_pack._attach_headers(self, assembled, headers)

    async def _best_source_turn(self, fact: MemoryRecord) -> MemoryRecord | None:
        """H6: the eligible parent turn sharing the most words with ``fact``."""
        return await compose_pack._best_source_turn(self, fact)

    async def _compose(
        self,
        query: str,
        ns: str,
        budget_tokens: int,
        top_k: int,
        pool: int,
        *,
        hide: Callable[[MemoryRecord], bool] | None = None,
        extra_probes: Sequence[str] = (),
        replay_window: int = 2,
        session_id: str | None = None,
        legs: Sequence[list[LegHit]] = (),
        rewrites: Sequence[str] | None = None,
    ) -> ReadResult:
        """H3: session-diverse, wider-recall read for aggregation questions.

        ``extra_probes`` (G2a: the LLM planner's subqueries) join the query, its
        core terms and the P4 rewrites; every probe's hits are rank-fused.
        ``legs`` (#36) are fused into the query's own search. ``rewrites`` (#38): the
        P4 rewrites already fetched for this query (None: fetch them here)."""
        return await compose_windows._compose(
            self,
            query,
            ns,
            budget_tokens,
            top_k,
            pool,
            hide=hide,
            extra_probes=extra_probes,
            replay_window=replay_window,
            session_id=session_id,
            legs=legs,
            rewrites=rewrites,
        )

    async def _compose_checked(
        self,
        query: str,
        ns: str,
        budget_tokens: int,
        top_k: int,
        pool: int,
        *,
        hide: Callable[[MemoryRecord], bool] | None,
        extra_probes: Sequence[str],
        replay_window: int,
        session_id: str | None,
        legs: Sequence[list[LegHit]],
    ) -> ReadResult:
        """#38: a compose read, checked once for completeness (``read.completeness_check``).

        The first compose result goes to the ``sufficiency`` role (+1 call); when it is
        judged incomplete, ``sufficiency@missing`` writes up to
        :data:`constants.COMPLETENESS_MAX_QUERIES` missing-information queries (+1 call)
        and the compose read runs once more with them as extra probes. One round by default
        (``read.completeness_rounds``, G-17, allows up to 3);
        an abstained read, a complete verdict, no query or any failure keeps the first
        result. The P4 rewrites are fetched once for both reads."""
        return await compose_windows._compose_checked(
            self,
            query,
            ns,
            budget_tokens,
            top_k,
            pool,
            hide=hide,
            extra_probes=extra_probes,
            replay_window=replay_window,
            session_id=session_id,
            legs=legs,
        )

    async def _missing_info_queries(self, query: str, context: AssembledContext) -> list[str]:
        return await read_deep._missing_info_queries(self, query, context)

    def _window_sides(self, window: int) -> tuple[int, int]:
        """The neighbour window as (turns before, turns after) a hit: ``window`` on both
        sides unless ``read.replay_window_before`` / ``replay_window_after`` override one."""
        return compose_windows._window_sides(self, window)

    async def _expand_neighbours(
        self,
        ns: str,
        chosen: list[MemoryRecord],
        session_ids: dict[str, list[str]],
        window: int,
        budget_tokens: int,
        used: int,
    ) -> int:
        """G2c: add each chosen turn's +-``window`` session neighbours, in place.

        Hits are expanded in selection order, neighbours nearest first (older on a
        tie). A neighbour passes the same gates and decoration as in replay mode;
        one that does not fit the budget is skipped. Returns the new token count.
        """
        return await compose_windows._expand_neighbours(
            self, ns, chosen, session_ids, window, budget_tokens, used
        )

    async def _dedupe_candidates(
        self, scored: list[tuple[MemoryRecord, float]]
    ) -> list[tuple[MemoryRecord, float]]:
        """I31 (``read.dedupe``): the candidates without near-duplicates; what was dropped
        goes to ``search_forensics()["dedupe_dropped"]`` as ``(dropped, kept, similarity)``."""
        return await compose_pack._dedupe_candidates(self, scored)

    def _profile_line_relevant(self, query: str, text: str) -> bool:
        """I33 (``read.profile_relevance_gate``): whether one profile / preference line
        bears on ``query``. ``off``: always. ``overlap``: they share a content word.
        This is the seam for the decider's relevance check (not merged): replace the
        body, keep the signature. A question-less call (session start) is never gated."""
        return compose_pack._profile_line_relevant(self, query, text)

    def _budget_factor(self, budget_tokens: int) -> float:
        """I20 (``read.replay_budget_scaling``): how much of the reference budget the
        routed read has left, in [0.25, 1]; 1.0 when scaling is off or the budget is
        at least the reference."""
        return compose_pack._budget_factor(self, budget_tokens)

    async def _window_turns(
        self,
        ns: str,
        ids: list[str],
        at: int,
        window: int,
        *,
        budget_tokens: int,
        used: int,
        seen: set[str],
        hit: MemoryRecord | None = None,
        single: bool = False,
        factor: float = 1.0,
    ) -> list[tuple[str, MemoryRecord, int]]:
        """The turns of one hit's window to add: ``(record id, turn, token cost)``, hit
        first (when ``hit`` is given), then neighbours nearest first (older on a tie).

        ``turns`` unit (default): +-``window`` turns per side (:meth:`_window_sides`,
        scaled by ``factor`` under I20). ``tokens`` unit (I6): each side takes neighbours
        until its token allowance is used; a neighbour that would pass it closes the
        side. Either way a turn that is gated, already seen, or does not fit what is left
        of ``budget_tokens`` is skipped; it never costs the hit its place."""
        return await compose_windows._window_turns(
            self,
            ns,
            ids,
            at,
            window,
            budget_tokens=budget_tokens,
            used=used,
            seen=seen,
            hit=hit,
            single=single,
            factor=factor,
        )

    def _context_eligible(self, record: MemoryRecord) -> bool:
        """C7': the search-time gates, for records reached without a search (stored trust)."""
        return compose_pack._context_eligible(self, record)

    async def _live_view(self, record: MemoryRecord) -> MemoryRecord | None:
        """C7': :meth:`_context_eligible` with live re-evaluation (B4').

        Under ``integrity.live_reevaluation`` admission is judged on the record's
        effective trust (its parents' current state), which also replaces the stored
        trust in the returned copy, as :meth:`search` does. None = not eligible.
        """
        return await compose_render._live_view(self, record)

    @staticmethod
    def _render_dated(record: MemoryRecord) -> MemoryRecord:
        """H5: ``[YYYY-MM-DD Day] content`` for episodic and semantic records."""
        return compose_render._render_dated(record)

    def _annotate_dates(self, record: MemoryRecord) -> MemoryRecord:
        """H1: ``[= absolute date]`` after each relative-time phrase (projection only).

        #29: a happened-tagged fact is resolved against the day it was said
        (:func:`date_anchor`), never against a ``valid_from`` moved to the event day."""
        return compose_render._annotate_dates(self, record)

    @staticmethod
    def _wrap_instruction(record: MemoryRecord) -> MemoryRecord:
        """E1: an instruction-flagged record enters a context window wrapped as data.

        #11: every stored record passes here on its way into a context, so this is
        also where the engine's own markers inside stored text are defanged
        (:func:`memspine.core.escaping.escape_markers`), before any wrapper or
        label is added. Engine-built lead blocks (marked by
        :attr:`MemoryRecord.is_engine_block`, which no stored record or tag can
        set) are left alone, and a B9 claim keeps its ``CLAIM`` prefix (only the
        mined text after it is escaped)."""
        return compose_render._wrap_instruction(record)

    def _wrap_untrusted(self, record: MemoryRecord) -> MemoryRecord:
        """B6: a record below ``integrity.untrusted_wrap_below`` is labelled as data."""
        return compose_render._wrap_untrusted(self, record)

    def _wrap_for_context(self, record: MemoryRecord) -> MemoryRecord:
        """C7': the per-record wrappers (H1 dates, E1 instruction flag, B6 untrusted
        note) without the current-state view; :meth:`_decorate` is the full set."""
        return compose_render._wrap_for_context(self, record)

    async def set_persona(self, namespace: str, text: str) -> MemoryRecord:
        """Pin the persona block (M13.1): first token of the E2 stable prefix.

        Updating supersedes in place: the persona keeps ONE stable record_id
        per namespace (version bumped, prior text archived to history) so the
        stable prefix stays stable instead of accumulating contradictory
        personas that paging can never evict.
        """
        return await memory_working.set_persona(self, namespace, text)

    async def forget(
        self,
        record_id: str,
        namespace: str = "default",
        hard: bool = False,
        cascade: bool | None = None,
        *,
        actor: str = "user",
        reason: str | None = None,
    ) -> None:
        """Forget one memory (M7).

        Soft (default): FORGET event → status=DELETED in the read model,
        vector row removed; the log keeps the history.

        Hard (``hard=True``, the P4 cascade): the row leaves the read model
        entirely AND every log payload carrying its identifying data is
        redacted — GDPR-erasure semantics in an append-only design. Legal holds
        block it. The erased text's embedding and extraction cache entries are
        purged and a SQLite WAL is checkpointed (#43).

        ``cascade`` (default: the value of ``hard``) also forgets, the same way,
        every record of the namespace derived from this one through
        ``source.parents`` (mined facts, cues, reflections), transitively. A
        derived record repeats the erased content, so erasure is not complete
        without it (#43).

        ``actor`` and ``reason`` (#49) are recorded in a ``memory.audit`` event
        under ``audit.actions``.
        """
        return await trust_erasure.forget(
            self, record_id, namespace, hard, cascade, actor=actor, reason=reason
        )

    async def erase_subject(
        self,
        subject: str,
        namespace: str = "default",
        *,
        actor: str = "user",
        reason: str | None = None,
    ) -> list[str]:
        """#43 per-subject erasure: hard-forget every record of ``namespace``
        about ``subject``, with its descendants.

        A record is about the subject when its fact key's ``entity`` equals
        ``subject`` (case-insensitive) or its ``source.principal`` is
        ``subject``. The subject's entity node id is also renamed to an opaque
        id in the community-partition history. ``actor`` and ``reason`` go into a
        ``memory.audit`` event under ``audit.actions`` (record ids only, never the
        subject's name). Returns the erased record ids."""
        return await trust_erasure.erase_subject(
            self, subject, namespace, actor=actor, reason=reason
        )

    async def erase_namespace(
        self, namespace: str, *, actor: str = "user", reason: str | None = None
    ) -> list[str]:
        """#43 per-namespace erasure: hard-forget every record of ``namespace``
        (any type and status) in one log pass. A legal hold on the namespace
        refuses the whole call before anything is touched. ``actor`` and
        ``reason`` go into a ``memory.audit`` event under ``audit.actions``.
        Returns the ids."""
        return await trust_erasure.erase_namespace(self, namespace, actor=actor, reason=reason)

    async def _descendants(self, storage: RecordsStore, ns: str, seeds: Sequence[str]) -> list[str]:
        """Every record of ``ns`` derived from ``seeds``, transitively, in discovery
        order (seeds excluded), that is still in the read model.

        Derivation is read from ``source.parents`` and, for summaries written
        before they carried parents, from the ``member_record_ids`` their WRITE
        (``consolidation``/``reflection``) and CONSOLIDATE events name. A summary
        of a forgotten member is forgotten with it (cascade), never re-derived."""
        return await trust_erasure._descendants(self, storage, ns, seeds)

    @staticmethod
    async def _log_derivations(storage: RecordsStore, ns: str) -> dict[str, list[str]]:
        """parent id -> ids of the records of ``ns`` whose log events name it: in
        a WRITE snapshot's ``source.parents`` or a ``member_record_ids`` list
        (one pass over the log)."""
        return await trust_erasure._log_derivations(storage, ns)

    async def _forget_many(
        self,
        storage: RecordsStore,
        ns: str,
        record_ids: Sequence[str],
        hard: bool,
        *,
        subject_names: Sequence[str] = (),
    ) -> None:
        """Forget ``record_ids`` (lock held). Hard: every legal hold is checked
        before the first FORGET, the log is redacted for all ids in one pass, then
        caches are purged and the WAL checkpointed. The same pass renames, in the
        community-partition markers, the entity node ids of ``subject_names`` and
        of the erased records' entities that no longer have a graph edge."""
        await trust_erasure._forget_many(
            self, storage, ns, record_ids, hard, subject_names=subject_names
        )
        if hard and self._sql is not None:
            from memspine.ingest.message_store import tombstone_records

            await tombstone_records(self._sql.engine, record_ids)

    async def _erased_entity_nodes(
        self, ns: str, names: Sequence[str], *, forced: Sequence[str] = ()
    ) -> dict[str, str]:
        """Entity node id -> opaque id for the erased ``names`` (KB-12 partition
        markers store node ids, which spell the name). A name in ``forced`` (the
        erased subject) is always renamed; another only once its node has no edge
        left (no live record mentions it). Empty without a graph store."""
        return await trust_erasure._erased_entity_nodes(self, ns, names, forced=forced)

    async def _forget_target(
        self, storage: RecordsStore, ns: str, record_id: str, hard: bool
    ) -> MemoryRecord | None:
        """The record to forget after the scope and legal-hold checks; None when
        a hard forget finds no row (an idempotent retry of the log redaction)."""
        return await trust_erasure._forget_target(self, storage, ns, record_id, hard)

    def _erasable_texts(self, record: MemoryRecord) -> list[str]:
        """The texts of ``record`` a cache may be keyed on: its (inflated) content
        and every archived version."""
        return trust_erasure._erasable_texts(self, record)

    async def _retract_compiled_edges(self, namespace: str, record_ids: Sequence[str]) -> None:
        """F-02c (``graph.edge_compiler_v2``): compiled graph edges of erased records go."""
        from memspine.derive.pipeline.stages.edge_compiler_v2 import retract_after_forget

        await retract_after_forget(self, namespace, record_ids)

    async def _scrub_profile_cards(
        self, namespace: str, record_ids: Sequence[str], hard: bool
    ) -> None:
        """FU-CLOSE (``offline.profile_card_v2``): a forgotten turn leaves the current profile
        card now (a new card marker), and on a hard forget its entry text is redacted from the
        older card markers in the log. Lock held by the forget flow."""
        if not self._config().offline.profile_card_v2:
            return
        from memspine.derive.pipeline.stages.profile_erasure import scrub_profile_cards

        await scrub_profile_cards(self, namespace, record_ids, hard)

    async def _purge_caches(self, texts: Sequence[str]) -> None:
        """#43: drop the embedding and extraction cache entries of erased texts, and
        the read-side caches keyed by text (N60 sentence vectors, N30 date spans)."""
        return await trust_erasure._purge_caches(self, texts)

    async def _forget_locked(
        self, storage: RecordsStore, ns: str, record_id: str, hard: bool, *, redact: bool = True
    ) -> None:
        """Forget one record (lock held): FORGET event, log redaction when
        ``redact`` and hard, and the memory types' delete hooks."""
        return await trust_erasure._forget_locked(self, storage, ns, record_id, hard, redact=redact)

    async def vet(self, draft: str, namespace: str = "default") -> list[VetFlag]:
        """G26 (plan v3.2, TWIST): check a draft reply against the namespace's current
        keyed facts. Each draft sentence that names a fact's entity and attribute but
        not its current value, or negates the current value, is flagged with that
        fact and up to three superseded values (its history). Read-only, no model."""
        return await trust_firewall.vet(self, draft, namespace)

    async def rating_profile(self, namespace: str = "default") -> dict[str, dict[str, float]]:
        """G12 (plan v3.2, LaMP / MemoryCD): the user's behaviour profile from logged
        records tagged ``rating:<number>`` (and ``domain:<name>``; untagged -> ``all``):
        per domain the mean, the standard deviation and the count of ratings. A
        recommender reads the mean as the user's level and the deviation as how
        selective they are. Read-only, no model."""
        return await handle.rating_profile(self, namespace)

    @scope_guard.viewer_guarded("brief")
    async def brief(
        self,
        namespace: str = "default",
        budget_tokens: int = constants.ASSEMBLE_BUDGET_TOKENS,
        *,
        summaries: int = 3,
        recent_turns: int = 6,
    ) -> list[MemoryRecord]:
        """G-20 (SimpleMem session-start injection): context for the start of a new
        session, with no question. In order, while they fit ``budget_tokens``:
        - the profile block (``read.profile_slots_header``), when on and known;
        - the newest session summaries (``session_summary`` / consolidation records);
        - the last ``recent_turns`` episodic turns, oldest first.
        Every record passes the read gates (status, quarantine, consent, trust), and the
        blocks carry their own markers. No model call."""
        return await handle.brief(
            self, namespace, budget_tokens, summaries=summaries, recent_turns=recent_turns
        )

    @scope_guard.viewer_guarded("get_record")
    async def get_record(self, record_id: str, namespace: str = "default") -> MemoryRecord | None:
        """F-16c: one record by id, only from ``namespace`` (exact match). A record that lives in
        another tenant or owner is refused (``scope.refused``, ``cross_tenant`` / ``cross_owner``)
        and reads as missing. Status is not filtered (an archived record is returned). ``viewer=``
        hides a record the viewer may not see; a multi-party namespace needs one under
        ``isolation.viewer_required``."""
        ns = validate_namespace(namespace)
        record = await self._require_started().get_record(record_id)
        if record is None or not scope_guard.record_in_scope("get_record", ns, record.namespace):
            return None
        return record

    async def get_envelope(
        self, record_id: str, namespace: str = "default"
    ) -> AssertionEnvelope | None:
        """FU-GAPS: the compiled assertion envelope a window fact was written from (time axes,
        evidence roles, integration verdict), or None when the record has none (it was not
        written under ``write.envelope_v2``, or does not exist in ``namespace``). Same scope rules
        as :meth:`get_record`."""
        record = await self.get_record(record_id, namespace)
        if record is None or record.fingerprint.envelope is None:
            return None
        return AssertionEnvelope.model_validate(record.fingerprint.envelope)

    @scope_guard.viewer_guarded("list_records")
    async def list_records(
        self, namespace: str = "default", memory_type: str | None = None
    ) -> list[MemoryRecord]:
        """F-16c: every stored record of ``namespace`` (optionally one memory type), unfiltered by
        status. The multi-party viewer rule applies like every other read verb."""
        storage = self._require_started()
        return await storage.list_records(validate_namespace(namespace), memory_type)

    @scope_guard.viewer_guarded("namespace_stats")
    async def namespace_stats(self, namespace: str = "default") -> dict[str, Any]:
        """I5 (isolation review 2026-10-08): how big one user's memory is. Counts of
        live records by memory type and status, distinct conversations, quarantined
        and erased records, an estimate of stored tokens, and the time span covered.
        Read-only; no model call."""
        return await handle.namespace_stats(self, namespace)

    @scope_guard.viewer_guarded("conversation")
    async def conversation(
        self,
        namespace: str = "default",
        session_id: str = "",
        roles: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        """I6: the live turns of one conversation (``ingest_messages(session_id=...)``),
        in time order, read through the indexed ``session_key`` column; ``roles`` keeps
        only some speakers. Passes the same context gates as a read (erased, quarantined
        or untrusted turns are left out)."""
        return await handle.conversation(self, namespace, session_id, roles)

    @scope_guard.viewer_guarded("session_memories")
    async def session_memories(
        self, namespace: str = "default", session_record_ids: Sequence[str] = ()
    ) -> list[MemoryRecord]:
        """N19 (plan v3.2, HaluMem ``get_session_memories``): the live derived records
        (mined facts, summaries, reflections, cards) whose ``source.parents`` include
        any of ``session_record_ids`` — what memory extracted from those turns, oldest
        first. Read-only; the raw turns themselves are not listed."""
        return await handle.session_memories(self, namespace, session_record_ids)

    async def forget_requests(
        self, namespace: str = "default", top_k: int = 5
    ) -> list[dict[str, object]]:
        """G25 (plan v3.2, ``memories.episodic.policies.forget_detector``): the user's
        "forget that" requests in ``namespace`` and, for each, the earlier live records
        that best match what it names (:func:`core.forget_request.forget_target`).

        Read-only: nothing is forgotten here. The caller confirms the candidates and
        calls :meth:`forget` (``hard=True`` for erasure), so a false detection never
        deletes a memory."""
        return await trust_erasure.forget_requests(self, namespace, top_k)

    async def verify_forget(
        self, record_id: str, namespace: str = "default", *, probe: str | None = None
    ) -> dict[str, object]:
        """M7 ``forget --verify``: prove erasure across every store we own.

        Uses the SAME payload walker as the redactor (``retained_fields`` ↔
        ``redact_record``) so the proof cannot share a blind spot with the
        erasure. Every identifying field counts (#2): content, fingerprint, fact
        key, tags, history and dedup sketches; ``log_retained_fields`` names
        those still found. A record derived from this one (``source.parents``)
        that is still in the read model (found transitively, see
        :meth:`_descendants`) also keeps ``clean`` false. An unverifiable vector
        backend, a vector table whose older versions were not purged, and an
        ephemeral (unpersisted) log are reported as *unproven*, never silently as
        clean. ``residual_risks`` names what the proof cannot cover (deleted
        terms in an unmerged Tantivy segment).

        SEC-C2/ADR-018: scoped to ``namespace``. A record that still exists in
        another namespace raises the anti-oracle error — a caller must not probe
        erasure state of a namespace it does not own. (Post-hard-delete the row
        is absent, which is the normal verify path and proceeds.)
        """
        return await trust_erasure.verify_forget(self, record_id, namespace, probe=probe)

    # ── data-subject rights + audit (#46-#50) ─────────────────────────────────

    @property
    def is_started(self) -> bool:
        """Whether :meth:`start` has completed (and :meth:`stop` has not run)."""
        return self._started

    @property
    def config(self) -> MemspineConfig:
        """The effective configuration of a started engine (read-only by convention)."""
        self._require_started()
        return self._config()

    @property
    def documents(self) -> DocumentStore | None:
        """ADR-081 ``storage.documents``: the started engine's document store (None when the
        slot is not configured). Documents put with ``record_ids`` are removed by a hard
        forget or erasure of those records once no other record references them."""
        self._require_started()
        return self._documents

    def _consent_ok(self, record: MemoryRecord) -> bool:
        """#50: whether the current read's purpose may see ``record``."""
        return trust_consent._consent_ok(self, record)

    async def consent_grant(
        self, owner: str, purpose: str = "*", *, grantee: str | None = None
    ) -> int:
        """T5-03 (``consent.v2``): ``owner`` opts in to ``purpose``; returns the generation."""
        return (await trust_consent.grant(self, owner, purpose, grantee=grantee)).generation

    async def consent_revoke(
        self, owner: str, purpose: str = "*", *, grantee: str | None = None
    ) -> int:
        """T5-03: ``owner`` withdraws ``purpose``: its records stop being served (not deleted)."""
        return (await trust_consent.revoke(self, owner, purpose, grantee=grantee)).generation

    async def consent_do_not_derive(self, owner: str, subject: str, on: bool = True) -> int:
        """T5-03: switch derivation off (or on) for one subject of ``owner``'s memory (G-11)."""
        return (await trust_consent.set_do_not_derive(self, owner, subject, on)).generation

    def _consent_context(self, context: AssembledContext) -> AssembledContext:
        """#50: drop records an assembled context may not carry for this purpose.

        The gates already keep them out of every search leg; this catches the
        records assembly adds from elsewhere (persona, full-mode listing)."""
        return ops_trust._consent_context(self, context)

    async def _audit_head_hash(self) -> str:
        """The current chain head, loaded once from the log (the last audit event)."""
        return await trust_audit._audit_head_hash(self)

    async def _append_audit(
        self, kind: EventKind, ns: str, actor: str, body: dict[str, object]
    ) -> None:
        """#49: append one chained audit event (serialized, so the chain is linear)."""
        return await trust_audit._append_audit(self, kind, ns, actor, body)

    async def bulk_read_alerts(
        self,
        namespace: str = "default",
        *,
        window: timedelta = timedelta(hours=1),
        max_records: int = constants.BULK_READ_MAX_RECORDS,
    ) -> list[dict[str, object]]:
        """G31 (plan v3.2): principals whose reads in the last ``window`` touched more
        than ``max_records`` distinct records of ``namespace`` (an extraction pattern:
        membership inference or scraping by repeated queries). Built from the
        ``READ_AUDIT`` events, so it needs ``audit.reads``; read-only."""
        return await trust_audit.bulk_read_alerts(
            self, namespace, window=window, max_records=max_records
        )

    async def _audit_read(self, verb: str, namespace: str, record_ids: Sequence[str]) -> None:
        """#49: a READ_AUDIT event for one read verb (``audit.reads`` only)."""
        return await trust_audit._audit_read(self, verb, namespace, record_ids)

    async def _audit_action(
        self,
        action: str,
        ns: str,
        record_ids: Sequence[str],
        *,
        actor: str,
        reason: str | None,
        **extra: object,
    ) -> None:
        """#49: an AUDIT event for a governance action (``audit.actions`` only)."""
        return await trust_audit._audit_action(
            self, action, ns, record_ids, actor=actor, reason=reason, **extra
        )

    async def verify_audit_chain(self) -> AuditChainReport:
        """#49: validate the hash chain over the persisted audit events.

        Each ``memory.read_audit`` / ``memory.audit`` event stores the previous
        event's hash; an edited, inserted or removed audit event breaks a link.
        A rolling log anchors at its oldest surviving audit event; an ephemeral
        log persists nothing, so its report covers no events."""
        return await trust_audit.verify_audit_chain(self)

    async def audit_chain_ok(self) -> bool:
        """#49: True when the audit hash chain verifies (see :meth:`verify_audit_chain`);
        under ``audit.chain_v2`` the LIB-41 chain (:meth:`verify_audit_chain_v2`)."""
        return await trust_audit.audit_chain_ok(self)

    async def verify_audit_chain_v2(self) -> ChainReport:
        """T5-05 (``audit.chain_v2``): re-read the hash-chained audit records and verify the
        sequence, the links, each hash and the time order. A rolling log anchors at its oldest
        surviving record; an ephemeral log persists none."""
        return await trust_audit_v2.verify(self)

    def erasure_status(self) -> dict[str, object]:
        """T5-04 (``privacy.erasure_v2``): generation, erased / hidden / queued counts and the
        purge jobs (state, done, total) by manifest id."""
        return trust_erasure_v2.status(self)

    def erasure_queue(self, *, include_dropped: bool = False) -> list[dict[str, object]]:
        """T5-04: the re-derivation queue (record id, action, remaining live sources) the
        offline runner consumes; ids only. Entries ``privacy.erasure_redact_hidden`` marked
        ``dropped_no_rederive`` (hard-forgotten, nothing to re-derive) show only with
        ``include_dropped``."""
        return trust_erasure_v2.queue(self, include_dropped=include_dropped)

    async def erasure_release(self, record_ids: Sequence[str], namespace: str = "default") -> int:
        """T5-04: hidden ids the offline runner re-derived (or dropped) become visible again;
        erased ids never do. Returns how many were released."""
        return await trust_erasure_v2.release(self, record_ids, namespace)

    async def erasure_resume_purges(self) -> list[str]:
        """T5-04: re-run every purge job that is not done; returns the manifest ids now done."""
        return await trust_erasure_v2.resume_purges(self)

    def retention_boundary(self, namespace: str = "default") -> RetentionBoundary | None:
        """T5-05 (``retention.v2``): the last retention boundary and rebuild mode planned for
        ``namespace`` (None before the first pass)."""
        return trust_retention_v2.boundary(self, namespace)

    async def export(
        self,
        namespace: str = "default",
        *,
        subject: str | None = None,
        include_history: bool = True,
        include_events: bool = False,
    ) -> list[str]:
        """#46 subject-access export: the namespace's data as JSONL lines.

        Line 1 is a header (``type: export``); then one ``type: record`` line per
        live, archived or held record (soft-forgotten records are left out), sorted
        by ``(recorded_at, record_id)``, with content inflated and provenance
        (``source``) kept; ``include_history`` keeps archived versions. With
        ``subject``, only records about it (fact-key ``entity`` equal, case-folded,
        or ``source.principal`` equal, as in :meth:`erase_subject`) and the records
        derived from them. ``include_events`` adds the namespace's log events in seq
        order (only those that mention an exported record when ``subject`` is set),
        as stored, so hard-erased content is already redacted; the content of
        soft-forgotten records is scrubbed from them too. Same data, same bytes.
        """
        storage = self._require_started()
        ns = validate_namespace(namespace)
        listed = await storage.list_records(ns)
        forgotten = [r.record_id for r in listed if r.status is RecordStatus.DELETED]
        records = [r for r in listed if r.status is not RecordStatus.DELETED]
        if subject is not None:
            wanted = subject.casefold()
            seeds = [
                r.record_id
                for r in records
                if (r.entity is not None and r.entity.casefold() == wanted)
                or r.source.principal == subject
            ]
            keep = {*seeds, *await self._descendants(storage, ns, seeds)}
            records = [r for r in records if r.record_id in keep]
        inflated: list[MemoryRecord] = []
        for record in records:
            try:
                inflated.append(self._inflate.inflate(record))
            except StorageError:
                _log.warning("export.inflate_failed", namespace=ns, record_id=record.record_id)
                inflated.append(record)
        inflated.sort(key=lambda r: (r.recorded_at, r.record_id))
        ids = {r.record_id for r in inflated}
        lines = [
            export_line(
                "export",
                {
                    "format": 1,
                    "namespace": ns,
                    "subject": subject,
                    "records": len(inflated),
                    "include_history": include_history,
                    "include_events": include_events,
                },
            )
        ]
        lines.extend(
            export_line("record", {"record": export_record(r, include_history=include_history)})
            for r in inflated
        )
        if include_events:
            after = 0
            while True:
                batch = await storage.read_events(after_seq=after, limit=1000)
                if not batch:
                    break
                for event in batch:
                    if event.namespace != ns:
                        continue
                    if subject is not None and not payload_mentions(event.payload, ids):
                        continue
                    payload = orjson.loads(orjson.dumps(event.payload))
                    for record_id in forgotten:
                        redact_record(payload, record_id)
                    lines.append(
                        export_line(
                            "event",
                            {
                                "seq": event.seq,
                                "kind": event.kind.value,
                                "ts": event.ts.isoformat(),
                                "actor": event.actor,
                                "payload": payload,
                            },
                        )
                    )
                after = max(e.seq for e in batch if e.seq is not None)
        with read_scope(None) as outer:
            if outer:
                await self._audit_read("export", ns, [r.record_id for r in inflated])
        await self._audit_action(
            "export",
            ns,
            [r.record_id for r in inflated],
            actor=current_principal() or "operator",
            reason=None,
            subject=subject,
        )
        return lines

    async def correct(
        self,
        target: str | tuple[str, str],
        new_value: str,
        *,
        actor: str = "user",
        reason: str = "",
        namespace: str = "default",
        source: SourceInfo | None = None,
        tags: Sequence[str] | None = None,
    ) -> MemoryRecord:
        """#47 user-direct correction (rectification): supersede a live record.

        ``target`` is a record id or an ``(entity, attribute)`` fact key (its current
        fact). The old record is archived with ``evolve_to`` pointing at the new one,
        and the new record's WRITE event carries ``correction: {supersedes, actor,
        reason}``. Unlike :meth:`write`, the conflict ladder does not run: an
        explicit correction is never CONTESTed or rejected as lower-trust
        (``contest_lower_trust``). The firewall still screens the new value
        (redaction, instruction flags); a quarantined correction supersedes nothing.
        Disputes on the key are cleared. Returns the new record.

        ``tags`` (I65) are added to the new record; a ``src:*`` tag among them replaces
        the old record's ``src:*`` tags (``memory_confirm`` flips ``src:assistant-proposed``
        to ``src:user-confirmed``). Engine-reserved tags are dropped.
        """
        return await ingest_write.correct(
            self,
            target,
            new_value,
            actor=actor,
            reason=reason,
            namespace=namespace,
            source=source,
            tags=tags,
        )

    async def expire_retention(self, now: datetime | None = None) -> dict[str, object]:
        """#48: hard-forget every record past its retention class's TTL.

        Soft-forgotten records count too: a soft forget keeps the content in the
        read model and the log, so past the TTL it is hard-erased like any other.
        The age is measured from ``recorded_at``. A record whose type's ``retention``
        policy refuses deletion (legal hold, regulated PII: ``may_delete``) is kept,
        and so is one any of whose descendants would be; the rest go through
        :meth:`forget` (``hard=True``, cascading), each with an audit reason naming
        the class. No classes configured: nothing runs."""
        return await trust_horizon.expire_retention(self, now)

    def _retention(self, record: MemoryRecord) -> RetentionPolicy:
        """The ``retention`` policy bound for ``record``'s memory type."""
        return trust_horizon._retention(self, record)

    async def _withheld_texts(self) -> list[str]:
        """#50: texts of records above ``consent.remote_llm_max_tier`` (every namespace)."""
        return await ops_trust._withheld_texts(self)

    def _remote_view(self, role: str, records: Sequence[MemoryRecord]) -> list[MemoryRecord]:
        """#50: ``records`` as a prompt for ``role`` may carry them. When ``role`` is
        bound to a remote provider under ``consent.remote_llm_max_tier``, a record
        above the tier (a lead block or derived record carries its parts' highest
        tier) shows :data:`WITHHELD_MARKER` instead of its text, before the prompt
        is rendered; the provider-boundary text match stays as the backstop."""
        return trust_privacy._remote_view(self, role, records)

    async def audit_taint(
        self, record_id: str, namespace: str = "default", cross_namespace: bool = False
    ) -> TaintReport:
        """E1 blast-radius audit: origin + every derivation, from the log.

        SEC-C3/ADR-018: scoped to ``namespace``. The seed record must belong to
        the caller's namespace; a foreign or unknown seed raises the ADR-014
        anti-oracle error. The taint WALK itself is not weakened — derivation
        edges are namespace-local post-P5/P6, so scoping the seed is sufficient.

        ``cross_namespace=True`` is the MTI forensic walk (G4): it also follows
        declared ``derived_from`` lineage across grants and lists reader
        namespaces that EXPOSE events show received tainted content. It reveals
        descendants outside ``namespace``, so it is an operator-level call.
        """
        return await trust_quarantine.audit_taint(self, record_id, namespace, cross_namespace)

    async def quarantine(
        self,
        record_id: str,
        namespace: str = "default",
        reason: str = "operator_quarantine",
        actor: str = "operator",
    ) -> MemoryRecord:
        """Quarantine an existing record (operator action).

        The same lifecycle event the firewall's own path uses (a DECAY_TRANSITION
        setting ``quarantined``), through the door, so it replays and is auditable.
        Model-facing reads stop serving the record at once; under
        ``integrity.live_reevaluation`` its descendants' effective trust drops to 0.
        Namespace-scoped like ``forget``: a foreign or missing id raises the same
        ``ConflictError`` (no existence oracle). Idempotent on a held record.
        """
        return await trust_quarantine.quarantine(self, record_id, namespace, reason, actor)

    async def rollback_taint(
        self,
        record_id: str,
        namespace: str = "default",
        actor: str = "operator",
        *,
        strict: bool = False,
    ) -> dict[str, list[str]]:
        """MG-9 repair: archive a poison seed and every content-tainted descendant.

        Uses the cross-namespace walk. Descendants that inherited taint only by
        *absorbing* it in a dedup merge (``merged@`` — the survivor kept its own
        content) are returned for review, not archived: their content is not the
        poison's (Paper A, Def. taint, T_c vs T_a). Archival is a DECAY_TRANSITION
        through the door, so it replays and is itself auditable.

        R2-9: a clean fact the poison displaced (``superseded_by_taint``) is
        restored as the current fact when nothing else holds its key now, and a
        contest the poison opened no longer marks the survivors ``disputed``.
        Restored ids are returned under ``"restored"``.

        #64: the walk needs the seed's origin WRITE in the log. When the log no longer
        holds it (``event_log.mode: ephemeral``, or a ``rolling`` window that pruned
        it), descendants cannot be traced: ``strict=True`` raises
        :class:`RollbackUnavailableError`; otherwise the call logs
        ``memory.rollback_beyond_window``, archives the seed alone (its ``valid_to``
        closed) and returns its id under ``"untraced"``.
        """
        return await trust_quarantine.rollback_taint(
            self, record_id, namespace, actor, strict=strict
        )

    async def _lineage_window(self, report: TaintReport, verb: str, strict: bool) -> bool:
        """#64: True when the log no longer holds the seed's origin WRITE, so the
        taint walk saw none of its descendants. Raises with ``strict``; else warns."""
        return await trust_quarantine._lineage_window(self, report, verb, strict)

    async def _undo_displacement(
        self, seed: str, report: TaintReport, removed: Sequence[str], actor: str
    ) -> list[str]:
        """R2-9: after a rollback/repair, give the fact keys back to clean records.

        1. A ``superseded_by_taint`` record (archived by the poison's UPDATE or
           INVALIDATE) is reactivated as the current fact, unless another
           record holds its key now (a later clean statement wins).
        2. On every key a removed record sat on, the ``disputed`` tag is cleared
           when no live contender is left (the contest was the poison's).
        """
        return await trust_quarantine._undo_displacement(self, seed, report, removed, actor)

    async def verify_integrity(
        self, key: bytes | None = None, expected_head: str | None = None
    ) -> IntegrityReport:
        """B2': offline integrity pass over the whole event log.

        Recomputes a SHA-256 (or, with ``key``, HMAC-SHA256) chain over every
        event, re-checks every stored fingerprint, and recomputes the monotone
        trust invariant for every WRITE that declares parents, from the log alone.
        Store ``chain_head`` outside the engine; pass it back as ``expected_head``
        to detect any later rewrite of history. Without ``key`` the head is only
        meaningful against such an external anchor.
        """
        return await trust_quarantine.verify_integrity(self, key, expected_head)

    async def repair_taint(
        self,
        record_id: str,
        namespace: str = "default",
        actor: str = "operator",
        *,
        strict: bool = False,
    ) -> dict[str, list[str]]:
        """B3: counterfactual repair (rollback that keeps benign knowledge).

        Like :meth:`rollback_taint`, content-tainted records are archived, but a
        consolidation SUMMARY that absorbed the seed is not simply thrown away:
        it is rebuilt from its untainted members with the same deterministic
        extractive summariser, so the result is exactly what consolidation would
        have produced had the seed never been written (tested). Summaries with
        no untainted member left are archived. Merge survivors go to review.

        #64: like :meth:`rollback_taint`, a seed whose origin WRITE the log no longer
        holds raises :class:`RollbackUnavailableError` with ``strict=True``, else is
        archived alone and returned under ``"untraced"``.
        """
        return await ops_trust.repair_taint(self, record_id, namespace, actor, strict=strict)

    async def _assess_write(self, record: MemoryRecord) -> FirewallVerdict:
        """Gather the firewall's namespace context: nearest-neighbour
        similarities (embedding outlier) + recent contents (MINJA prefixes).

        Quarantined rows are EXCLUDED from both signals: a cluster of similar
        poison writes must not dampen each other's outlier score or supply the
        MINJA bridge prefixes (they are held content, not namespace truth).

        The neighbour signal is the ``ANOMALY_MIN_NEIGHBOURS`` nearest NON-held
        rows: when the plain top-k holds a held row, the query is widened by the
        namespace's held count and the held rows dropped (#63). Filtering after a
        plain top-k let held rows shrink the count, and which equal-score row fell
        inside the cut depended on the vector store's tie order; this way neither
        the count nor the scores do.
        """
        return await ops_trust._assess_write(self, record)

    async def _gated_assess(self, record: MemoryRecord) -> MemoryRecord:
        """The full firewall gate as an injectable seam (E1): resource ingest
        and reflections get the SAME anomaly context as Engine.write — a
        context-free assess would silently disable the embedding-outlier and
        MINJA defenses on exactly the RAG-poisoning surface."""
        return await trust_firewall._gated_assess(self, record)

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
        return await trust_firewall._corroborate(self, namespace, incoming)

    async def _release_change(self, namespace: str, held: MemoryRecord) -> dict[str, object]:
        """The lifecycle delta that lifts ``held`` out of quarantine (corroboration
        promotion or an operator approval)."""
        return await trust_firewall._release_change(self, namespace, held)

    async def list_quarantined(self, namespace: str = "default") -> list[MemoryRecord]:
        """#3: the records of ``namespace`` the firewall (or an operator) is holding,
        oldest first: the review queue for :meth:`approve_quarantined` and
        :meth:`reject_quarantined`."""
        return await trust_quarantine.list_quarantined(self, namespace)

    async def _held_record(self, ns: str, record_id: str) -> MemoryRecord:
        """A record of ``ns`` currently held in quarantine, or the anti-oracle error."""
        return await trust_quarantine._held_record(self, ns, record_id)

    async def approve_quarantined(
        self,
        record_id: str,
        namespace: str = "default",
        actor: str = "operator",
        reason: str = "operator_approved",
        principal: str | None = None,
    ) -> MemoryRecord:
        """#3: release a held record after review, as ``actor`` (logged on the event).

        The record leaves quarantine the way corroboration would release it: a
        semantic fact whose key already has another active fact becomes that
        fact's predecessor; a procedural skill resumes its ladder stage. A
        missing, foreign or not-held id raises ``ConflictError``.

        ``principal`` is the reviewer's authenticated identity when the caller
        knows it. A reviewer whose principal (or actor) is the held record's
        ``source.principal`` is refused with ``ConflictError``: an author cannot
        release its own held write."""
        return await trust_quarantine.approve_quarantined(
            self, record_id, namespace, actor, reason, principal
        )

    async def reject_quarantined(
        self,
        record_id: str,
        namespace: str = "default",
        actor: str = "operator",
        reason: str = "operator_rejected",
    ) -> MemoryRecord:
        """#3: reject a held record after review, as ``actor`` (logged on the event).

        The record is archived, stays flagged ``quarantined`` and is tagged
        ``quarantine_rejected``, so no later corroboration can release it and the
        audit trail keeps it; ``forget(hard=True)`` erases it. A missing, foreign
        or not-held id raises ``ConflictError``."""
        return await trust_quarantine.reject_quarantined(self, record_id, namespace, actor, reason)

    async def feedback(
        self,
        record_id: str,
        signal: str,
        *,
        note: str | None = None,
        actor: str = "user",
        namespace: str = "default",
        channel: str | None = None,
        new_value: str | int | float | bool | None = None,
        new_text: str | None = None,
        rating: int | None = None,
        quote: str | None = None,
        turn: int | None = None,
        source_id: str | None = None,
    ) -> MemoryRecord:
        """#54: record a user's ``like`` / ``dislike`` / ``note`` on one record.

        O4-11 (``write.feedback_v2``, off): the keyword arguments after ``namespace`` and the kinds
        ``correction`` / ``rejection`` / ``confirmation`` / ``rating`` are explicit-feedback only;
        see :func:`memspine.api.ops_trust.feedback`. With the flag off they are refused.

        Appends one FEEDBACK event through the door; the record projector keeps the
        per-record counts (``scoring.likes`` / ``dislikes`` / ``notes``). With
        ``read.scoring.utility_weight > 0`` the ranking's utility term then includes
        ``tanh((likes - dislikes) / FEEDBACK_UTILITY_SCALE)``, bounded in (-1, 1), so
        a repeated like cannot dominate relevance. A record with no feedback scores
        exactly as before.

        ``note`` is optional with a like or dislike and required for ``"note"``. It
        is screened like message content (``firewall.redact_secrets`` / ``pii``),
        cut to ``FEEDBACK_NOTE_MAX_CHARS`` and stored in the event only, under
        ``content``, so a hard forget of the record erases it too. Namespace-scoped
        like :meth:`quarantine`: a missing, foreign or forgotten id raises the same
        ``ConflictError``. Returns the record with its updated counts.
        """
        return await ops_trust.feedback(
            self,
            record_id,
            signal,
            note=note,
            actor=actor,
            namespace=namespace,
            channel=channel,
            new_value=new_value,
            new_text=new_text,
            rating=rating,
            quote=quote,
            turn=turn,
            source_id=source_id,
        )

    async def correction_latency(self, namespace: str = "default") -> LatencyStats:
        """O4-11 (``write.feedback_v2``): turns from a wrong memory first being surfaced to its
        explicit correction, from the log (one turn = one search of the namespace)."""
        return await ops_trust.correction_latency(self, namespace)

    def _config(self) -> MemspineConfig:
        assert self._resolved is not None
        return self._resolved.config

    async def _build_reranker(self, settings: RerankSettings) -> Reranker | None:
        """Share one lazy provider build across concurrent callers (one per distinct
        resolved chain entry)."""
        mode = settings.mode
        if self._provider_tearing_down:
            raise MemspineError("reranker construction cannot start during provider teardown")
        if settings in self._reranker_build_results:
            result = self._reranker_build_results[settings]
            return None if result is None else result.service
        async with self._reranker_build_lock:
            if self._provider_tearing_down:
                raise MemspineError("reranker construction cannot start during provider teardown")
            if settings in self._reranker_build_results:
                result = self._reranker_build_results[settings]
                return None if result is None else result.service
            task = self._reranker_build_tasks.get(settings)
            if task is None or task.done():
                task = asyncio.create_task(
                    self._perform_reranker_build(settings), name=f"memspine-reranker-build-{mode}"
                )
                self._reranker_build_tasks[settings] = task
        return await asyncio.shield(task)

    async def _perform_reranker_build(self, settings: RerankSettings) -> Reranker | None:
        """Build once through the provider descriptor and record lifecycle ownership."""
        mode = settings.mode
        if self._provider_resources is None:
            raise MemspineError("reranker construction requires a started engine")
        from memspine.providers.contexts import RerankerBuildContext, SharedBuildContext

        cfg = self._config()
        context = RerankerBuildContext(
            shared=SharedBuildContext(
                resources=self._provider_resources,
                owner_id=self._provider_owner_id or "pending-owner",
                health_timeout_s=cfg.runtime.provider_services.health_timeout_s,
                registry=self._registry(),
                sql_pool=cfg.runtime.shared_resources.sql,
                http_pool=cfg.runtime.shared_resources.http,
            ),
            chunk_chars=settings.chunk_chars,
            chunk_overlap=settings.chunk_overlap,
        )
        try:
            built = await build_reranker_result(
                settings,
                context,
                explicit_deployment_id=cfg.deployment.id is not None,
            )
        except (MissingServiceError, ConnectionError, TimeoutError) as exc:
            _log.warning("rerank.provider_unavailable", mode=mode, error=type(exc).__name__)
            built = None
        self._reranker_build_results[settings] = built
        if built is not None:
            logical = f"reranker.{mode}"
            suffix = 1
            while logical in self._provider_results:
                suffix += 1
                logical = f"reranker.{mode}#{suffix}"
            self._provider_results[logical] = built
            return built.service
        return None

    async def _rerank_provider(self) -> Reranker | None:
        """The E8 reranker of ``model.rerank.chain[0]`` (D-51, ADR-082), constructed lazily
        on first use (cross-encoder weights must not load at engine start). The read gates
        use it; the rerank stage falls back through the rest of the chain. An unavailable
        adapter is skip-logged ONCE and stays off — retrieval quality degrades, retrieval
        never fails. None when the chain is empty (reranking off)."""
        rerank_cfg = self._config().model.rerank
        if not rerank_cfg.chain:
            return None
        if self._rerank_unavailable or self._reranker is not None:
            return self._reranker
        # The provider factory owns lazy construction and classifies approved
        # unavailability; configuration and programming failures still propagate.
        self._reranker = await self._build_reranker(
            read_rerank.stage_settings(rerank_cfg.chain[0], rerank_cfg)
        )
        if self._reranker is None:
            self._rerank_unavailable = True
        elif hasattr(self._reranker, "unscored_tail"):
            # R3-07: the Jina window tail is unscored (None), never 0.0.
            self._reranker.unscored_tail = True
        return self._reranker

    async def _static_embedding_prefilter(
        self,
        query: str,
        candidates: list[tuple[MemoryRecord, float]],
        top_k: int,
    ) -> list[tuple[MemoryRecord, float]]:
        return await read_rerank._static_embedding_prefilter(self, query, candidates, top_k)

    @scope_guard.viewer_guarded("timeline")
    async def timeline(
        self,
        namespace: str = "default",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MemoryRecord]:
        """Chronological episodic timeline (M13.2), optionally windowed."""
        return await read_timeview.timeline(self, namespace, start, end)

    @scope_guard.viewer_guarded("sessions")
    async def sessions(
        self, namespace: str = "default", gap_minutes: int = constants.SESSION_GAP_MINUTES
    ) -> list[Session]:
        """Derived session boundaries over the episodic timeline (M13.2)."""
        return await read_timeview.sessions(self, namespace, gap_minutes)

    async def ingest_data(
        self,
        path: str | Path,
        namespace: str = "default",
        pii_tier: PiiTier = PiiTier.NONE,
    ) -> list[MemoryRecord]:
        """Ingest a document (D-29): extract → chunk → resource records."""
        return await handle.ingest_data(self, path, namespace, pii_tier)

    async def ingest_asset(
        self,
        path_or_uri: str | Path,
        namespace: str = "default",
        *,
        mime: str | None = None,
        size: int | None = None,
        content_hash: str | None = None,
        alt_text: str | None = None,
    ) -> MemoryRecord | None:
        """W2-11: store an image/attachment REFERENCE (hash + path/URI, no bytes) and, per
        ``write.asset_describer``, describe it at ingest or on first read. Refused while ``off``."""
        return await ingest_gateway.ingest_asset(
            self,
            path_or_uri,
            namespace,
            mime=mime,
            size=size,
            content_hash=content_hash,
            alt_text=alt_text,
        )

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
        return await memory_procedural.add_skill(self, content, name, namespace, actor, source)

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
        return await memory_procedural.record_plan(self, task, content, namespace, actor, source)

    async def _write_procedural(self, record: MemoryRecord, actor: str) -> MemoryRecord:
        return await memory_procedural._write_procedural(self, record, actor)

    async def promote_skill(
        self,
        record_id: str,
        namespace: str = "default",
        dry_run_passed: bool = False,
    ) -> MemoryRecord:
        """One legal step up draft→staged→verified→active; verified→active
        requires ``dry_run_passed=True`` (M13.4)."""
        return await memory_procedural.promote_skill(self, record_id, namespace, dry_run_passed)

    async def deprecate_skill(self, record_id: str, namespace: str = "default") -> MemoryRecord:
        """Retire a skill/plan (terminal, M13.4)."""
        return await memory_procedural.deprecate_skill(self, record_id, namespace)

    async def skills(
        self,
        namespace: str = "default",
        kind: str | None = None,
        usable_only: bool = True,
    ) -> list[MemoryRecord]:
        """Procedural records; by default only the usable (ACTIVE) set (M13.4)."""
        return await memory_procedural.skills(self, namespace, kind, usable_only)

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
        return await ops_memory.recall_plan(self, task, namespace, min_similarity)

    # ── plan v3.2 procedural rows W17a-f, N24, N26-N29 (ADR-060; no model calls) ──

    def _procedural_policy(self) -> dict[str, Any]:
        """``memories.procedural.policies`` as configured (empty when unset)."""
        return memory_procedural._procedural_policy(self)

    def _require_procedural(self) -> ProceduralMemory:
        return memory_procedural._require_procedural(self)

    @staticmethod
    def _is_live_advisory(record: MemoryRecord, kind: str) -> bool:
        """A lesson / exemplar that is still offered: advisory stage, held status
        (out of ordinary search), never quarantined, not retired."""
        return memory_procedural._is_live_advisory(record, kind)

    async def _append_feedback(self, ns: str, record_id: str, signal: str, note: str = "") -> None:
        """N28: one FEEDBACK event (caller holds the namespace write lock)."""
        return await memory_procedural._append_feedback(self, ns, record_id, signal, note)

    async def record_outcome(
        self,
        task: str,
        outcome: str,
        namespace: str = "default",
        *,
        action: str | None = None,
        error: str | None = None,
        next_action: str | None = None,
        reward: float | None = None,
        tool: str | None = None,
        used_ids: Sequence[str] = (),
        derived_from: Sequence[str] = (),
        lesson: bool = True,
        actor: str = "assistant",
    ) -> MemoryRecord | None:
        """W17a + N28: record a structured outcome receipt.

        Each plan or lesson in ``used_ids`` (what was injected for this run) gets a
        helpful (success) or harmful (failure) count, a FEEDBACK like / dislike on
        the log. With ``memories.procedural.policies.auto_verify_on_reward`` a
        reward of at least ``OUTCOME_AUTO_VERIFY_REWARD`` advances a used staged
        plan to verified (``active`` still needs the dry-run gate).

        With ``lesson`` (default), the receipt also becomes a ``kind="lesson"``
        record: the fixed-field template of :func:`lesson_text`, at the
        ``advisory`` stage (never executable, never promotable), trust capped at
        its ``derived_from`` parents (the trajectory's steps). A receipt whose key
        (trigger words, action signature, error class) matches a live lesson adds
        a repeat note to it instead of a new row. Returns the lesson (new or
        repeated) or None when ``lesson`` is false.
        """
        return await memory_procedural.record_outcome(
            self,
            task,
            outcome,
            namespace,
            action=action,
            error=error,
            next_action=next_action,
            reward=reward,
            tool=tool,
            used_ids=used_ids,
            derived_from=derived_from,
            lesson=lesson,
            actor=actor,
        )

    async def _repeat_lesson(
        self, storage: RecordsStore, ns: str, key_tag: str, note: str
    ) -> MemoryRecord | None:
        """N28: the live lesson carrying ``key_tag`` with one more repeat note, or None."""
        return await memory_procedural._repeat_lesson(self, storage, ns, key_tag, note)

    async def _quarantine_lesson(
        self, storage: RecordsStore, ns: str, held: MemoryRecord, reasons: list[str]
    ) -> None:
        """N24 (``memories.procedural.policies.quarantine_lesson``): a quarantine
        verdict becomes an advisory lesson naming the source, the reasons and the
        content hash, never the held text. Repeats from one source signature add
        notes (N28). The caller holds the namespace write lock."""
        return await memory_procedural._quarantine_lesson(self, storage, ns, held, reasons)

    async def _similar_advisory(
        self, ns: str, query: str, kind: str, min_similarity: float
    ) -> list[tuple[MemoryRecord, float]]:
        """Live advisory records of ``kind`` by cosine of ``query`` to their trigger."""
        return await ops_memory._similar_advisory(self, ns, query, kind, min_similarity)

    async def recall_lessons(
        self,
        task: str,
        namespace: str = "default",
        k: int = constants.LESSON_BLOCK_MAX,
        min_similarity: float = constants.LESSON_MIN_SIMILARITY,
    ) -> list[tuple[MemoryRecord, float]]:
        """W17a: the live lessons whose task trigger is most similar to ``task``,
        ranked by ``cos x (1 + helpful) / (1 + harmful)`` (N28), at most ``k``.
        Each returned lesson's use rides the log as a RETRIEVE."""
        return await memory_procedural.recall_lessons(self, task, namespace, k, min_similarity)

    async def recall_plans(
        self,
        task: str,
        namespace: str = "default",
        k: int | None = None,
        min_similarity: float = constants.PLAN_RECALL_MIN_SIMILARITY,
        include_lessons: bool = False,
    ) -> list[tuple[MemoryRecord, float]]:
        """W17b: the top-``k`` usable plans for ``task`` (default
        ``memories.procedural.policies.recall_k``, else ``PLAN_RECALL_K``).

        Candidates clear ``min_similarity`` on the task embedding, are ranked by
        ``cos x (1 + helpful) / (1 + harmful)`` (N28; plain cosine with no
        outcomes), and plans whose failures dominate (at least
        ``OUTCOME_DEMOTE_MIN_HARMFUL`` harmful, more harmful than helpful) are
        pruned. One plan per action signature is kept (diversity). With
        ``include_lessons`` the live lessons compete in the same ranking. Each
        hit's use rides the log as a RETRIEVE."""
        return await ops_memory.recall_plans(
            self, task, namespace, k, min_similarity, include_lessons
        )

    async def prune_experience(self, namespace: str = "default") -> list[str]:
        """N28: retire (deprecate, terminal) plans and lessons whose failures
        dominate, or that were used at least ``OUTCOME_RETIRE_MIN_USED`` times with
        a helpful share below ``OUTCOME_RETIRE_MIN_RATIO``. Returns retired ids."""
        return await memory_procedural.prune_experience(self, namespace)

    async def record_trajectory(
        self,
        goal: str,
        steps: Sequence[TrajectoryStep | Mapping[str, str]],
        outcome: str,
        namespace: str = "default",
        *,
        reward: float | None = None,
        trajectory_id: str | None = None,
        start: str | None = None,
        actor: str = "assistant",
    ) -> list[MemoryRecord]:
        """W17c + N27: a trajectory as one episodic group (``group_id`` =
        ``trajectory_id``, new when None). Each step is one record (tag
        ``step:<i>``) holding its action, page and the observation as a line diff
        against the previous step; the head record (``trajectory_head``) is the
        deterministic manifest of :func:`trajectory_manifest`. Returns the head
        first, then the steps; pass the step ids as ``derived_from`` of the plan or
        lesson the run produced."""
        return await memory_procedural.record_trajectory(
            self,
            goal,
            steps,
            outcome,
            namespace,
            reward=reward,
            trajectory_id=trajectory_id,
            start=start,
            actor=actor,
        )

    async def trajectory_window(
        self,
        record_id: str,
        namespace: str = "default",
        radius: int = constants.TRAJECTORY_WINDOW_RADIUS,
        cap: int = constants.TRAJECTORY_WINDOW_CAP,
    ) -> list[MemoryRecord]:
        """N27: the steps around a trajectory hit (a step: within ``radius``; the
        head: the first steps), inside its group only, at most ``cap`` states, in
        step order. A record outside any trajectory gives an empty list."""
        return await memory_procedural.trajectory_window(self, record_id, namespace, radius, cap)

    async def _task_state_record(self, ns: str, task_id: str) -> MemoryRecord | None:
        return await memory_procedural._task_state_record(self, ns, task_id)

    async def _put_task_state(
        self, ns: str, state: TaskState, prior: MemoryRecord | None, reason: str
    ) -> MemoryRecord:
        """Keyed supersede in place (the persona pattern): one stable record per
        task, the prior state archived to its history, every version on the log."""
        return await memory_procedural._put_task_state(self, ns, state, prior, reason)

    async def set_task_state(
        self,
        task_id: str,
        goal: str,
        namespace: str = "default",
        *,
        subgoals: Sequence[str | Mapping[str, str]] = (),
        constraints: Sequence[str] = (),
        budget_left: float | None = None,
    ) -> TaskState:
        """W17e: open (or reset) the task-state record of ``task_id``: goal,
        constraints, subgoals (all ``pending``) and budget. The record lives in
        working memory (channel ``task_state``, ``group_id`` = ``task_id``); with
        ``memories.procedural.policies.task_state`` a read whose ``session_id`` is
        the task pins it right after the persona."""
        return await memory_procedural.set_task_state(
            self,
            task_id,
            goal,
            namespace,
            subgoals=subgoals,
            constraints=constraints,
            budget_left=budget_left,
        )

    async def task_state(self, task_id: str, namespace: str = "default") -> TaskState | None:
        """W17e: the current state of ``task_id`` (None when never opened)."""
        return await memory_procedural.task_state(self, task_id, namespace)

    async def update_subgoal(
        self,
        task_id: str,
        subgoal_id: str,
        status: str,
        namespace: str = "default",
        *,
        receipt_id: str | None = None,
        result: str | None = None,
        budget_left: float | None = None,
    ) -> TaskState:
        """W17e: move one subgoal to ``pending`` / ``done`` / ``failed`` (a keyed
        supersede of the task state). ``done`` requires ``receipt_id``."""
        return await memory_procedural.update_subgoal(
            self,
            task_id,
            subgoal_id,
            status,
            namespace,
            receipt_id=receipt_id,
            result=result,
            budget_left=budget_left,
        )

    async def close_task(self, task_id: str, namespace: str = "default") -> TaskState:
        """W17e: close the task (an explicit done, or a fired prospective watch):
        the state stays on the log but is no longer pinned in reads."""
        return await memory_procedural.close_task(self, task_id, namespace)

    async def task_search(
        self, task_id: str, query: str, namespace: str = "default", top_k: int = 10
    ) -> list[tuple[MemoryRecord, float]]:
        """W17e: a read scoped to the task first (``group_id`` = ``task_id``), then
        filled from the whole namespace (MemoryArena: all@10 = 0 unscoped)."""
        return await memory_procedural.task_search(self, task_id, query, namespace, top_k)

    async def add_exemplar(
        self,
        text: str,
        label: str,
        namespace: str = "default",
        group: str = "default",
        actor: str = "user",
    ) -> MemoryRecord:
        """W17f: store a labelled exemplar (``kind="mapping"``, advisory stage) in
        the mapping group ``group``, through the firewall like any write."""
        return await memory_procedural.add_exemplar(self, text, label, namespace, group, actor)

    async def classify(
        self,
        text: str,
        namespace: str = "default",
        group: str = "default",
        k: int = constants.KNN_VOTE_K,
    ) -> dict[str, Any]:
        """W17f + N29: the kNN label vote for ``text`` over the group's exemplars
        (BM25 and dense cosine fused by reciprocal rank, score-weighted vote).
        Returns ``label`` (None without a match), ``margin``, ``votes``, the voting
        ``exemplars`` (text, label, score), the ``label_table`` (label -> its most
        distinctive terms) and ``table_text``. The caller's reader may override."""
        return await ops_memory.classify(self, text, namespace, group, k)

    async def review_evidence(
        self,
        record_id: str,
        supporting_ids: Sequence[str],
        contradicting_ids: Sequence[str] = (),
        namespace: str = "default",
    ) -> str:
        """N26: decide a held (``pending_evidence``) record against its evidence.

        ``promote`` (at least one live supporter whose document-type tier reaches
        ``memories.semantic.policies.trust.authority_min_tier``, and no
        contradiction) releases it from quarantine; ``disputed`` (authoritative
        support and a contradiction) and ``pending_evidence`` leave it held.
        Supporters outside the namespace, held or not live count for nothing."""
        return await trust_firewall.review_evidence(
            self, record_id, supporting_ids, contradicting_ids, namespace
        )

    async def _apply_correction(
        self,
        ns: str,
        turn: MemoryRecord,
        correction: Correction,
        recent: Sequence[MemoryRecord],
    ) -> MemoryRecord | None:
        return await ingest_write._apply_correction(self, ns, turn, correction, recent)

    async def reflect(
        self,
        content: str,
        source_record_ids: list[str],
        namespace: str = "default",
        actor: str = "assistant",
        source: SourceInfo | None = None,
    ) -> MemoryRecord:
        """Write a reflection derived from existing records (M13.7); see :meth:`_reflect`."""
        return await derive_graph.reflect(
            self, content, source_record_ids, namespace, actor, source
        )

    async def _reflect(
        self,
        content: str,
        source_record_ids: list[str],
        namespace: str = "default",
        actor: str = "assistant",
        source: SourceInfo | None = None,
        *,
        require_live: bool = False,
    ) -> MemoryRecord | None:
        return await derive_graph._reflect(
            self, content, source_record_ids, namespace, actor, source, require_live=require_live
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
        return await derive_graph.associate(self, src_id, dst_id, namespace, rel, weight)

    async def related(
        self,
        record_id: str,
        namespace: str = "default",
        k: int = 10,
        strategy: str | None = None,
    ) -> list[MemoryRecord]:
        """Records associated with ``record_id`` over the link graph (D-40).
        ``strategy`` (E1: ``ppr`` default | ``bfs`` | ``rrf``) overrides
        ``memories.associative.policies.related.strategy``. Same E1 gate as
        ``search``: only ACTIVATED, never quarantined, never cross-namespace."""
        return await derive_graph.related(self, record_id, namespace, k, strategy)

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
        return await memory_prospective.watch(
            self, content, namespace, due_at, entity, attribute, actor, source
        )

    async def due(
        self, namespace: str = "default", now: datetime | None = None
    ) -> list[MemoryRecord]:
        """Fired-but-unacknowledged watches (M13.8): due time reached or the
        watched fact key invalidated. Read-only and pull-based (ADR-016) —
        a fired watch stays here until ``acknowledge_watch`` archives it.
        ``now`` defaults to the current UTC instant; pass it explicitly to
        own time (tests always should)."""
        return await memory_prospective.due(self, namespace, now)

    async def reminders(
        self,
        namespace: str = "default",
        now: datetime | None = None,
        cues: Sequence[str] = (),
    ) -> list[Reminder]:
        """Advisory reminders from stated plans (O4-10, ``memories.prospective.policies.v2``).
        Read-only and pull-based: it never acts, writes or delivers. See ``ops_advisory``."""
        return await ops_advisory.reminders(self, namespace, now, cues)

    async def suggest_procedures(
        self,
        context_tags: Sequence[str],
        namespace: str = "default",
        limit: int | None = None,
    ) -> list[Suggestion]:
        """Advisory procedure ranking (O4-10, ``memories.procedural.policies.v2``); counts move on
        explicit feedback only. Never executes anything. See ``ops_advisory``."""
        return await ops_advisory.suggest_procedures(self, context_tags, namespace, limit)

    async def acknowledge_watch(self, record_id: str, namespace: str = "default") -> MemoryRecord:
        """Acknowledge a fired watch: archived via a delta event
        (``reason="watch_fired"``), idempotent (M13.8)."""
        return await memory_prospective.acknowledge_watch(self, record_id, namespace)

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
        return await memory_shared.grant(self, to_namespace, namespace, memory_types, actor)

    async def revoke(
        self, to_namespace: str, namespace: str = "default", *, actor: str = "user"
    ) -> MemoryRecord:
        """Revoke ``to_namespace``'s read access to ``namespace`` (R2): the
        grant record is archived via a delta event; raises when no grant is
        live (a typo'd grantee must not read as success). ``actor`` goes into a
        ``memory.audit`` event under ``audit.actions``."""
        return await memory_shared.revoke(self, to_namespace, namespace, actor=actor)

    @scope_guard.viewer_guarded("shared_search")
    async def shared_search(
        self,
        query: str,
        namespace: str = "default",
        top_k: int = constants.SEARCH_TOP_K,
        session_id: str | None = None,
        ledger_id: str | None = None,
        purpose: str | None = None,
    ) -> list[tuple[MemoryRecord, float]]:
        """Own-namespace search plus granted foreign results (see :meth:`_shared_search`).

        ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``
        for own and foreign records alike."""
        return await memory_shared.shared_search(
            self, query, namespace, top_k, session_id, ledger_id, purpose
        )

    async def _shared_search(
        self,
        query: str,
        namespace: str = "default",
        top_k: int = constants.SEARCH_TOP_K,
        session_id: str | None = None,
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
        return await ops_memory._shared_search(self, query, namespace, top_k, session_id)

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
        return await memory_shared.subscribe(self, query, namespace, actor, source)

    async def subscriptions(self, namespace: str = "default") -> list[MemoryRecord]:
        """Live standing-query records in ``namespace`` (ADR-016)."""
        return await memory_shared.subscriptions(self, namespace)

    async def grants_from(self, namespace: str = "default") -> list[Grant]:
        """The live grants ``namespace`` has issued (operator listing surface,
        R2/ADR-016). Scoped to the grantor — a caller lists only its OWN
        grants, never another namespace's."""
        return await memory_shared.grants_from(self, namespace)

    def _require_prospective(self) -> ProspectiveMemory:
        return memory_prospective._require_prospective(self)

    async def _lineage_roots(self, record: MemoryRecord) -> frozenset[str]:
        """W13: the records at the top of ``record``'s ``source.parents`` lineage (a
        record with no parents is its own root). Parents no longer stored count as
        roots under their id. Bounded walk (cycles and depth guarded)."""
        return await trust_quarantine._lineage_roots(self, record)

    async def _independent_roots(self, held: MemoryRecord, incoming: MemoryRecord) -> bool:
        """W13 (plan v3.2, ``integrity.corroboration_roots``): the corroborator's
        lineage roots must not overlap the held record's, nor any earlier
        corroborator's (read from the log). A summary of, or a re-statement derived
        from, the poison's own source turns is then not a second opinion."""
        return await ops_memory._independent_roots(self, held, incoming)

    async def _independent_principal(self, held: MemoryRecord, incoming: MemoryRecord) -> bool:
        """Principal-bound independence (integrity.principal_bound_corroboration).

        The corroborator must name a principal, differ from the held record's
        principal, and differ from every principal that already corroborated
        ``held`` — so k promotions need k distinct principals, not k sessions
        (Paper A, Prop. 4b). Earlier corroborators are read from the log.
        """
        return await trust_quarantine._independent_principal(self, held, incoming)

    def _require_shared(self) -> SharedMemory:
        return memory_shared._require_shared(self)

    async def _evolve_links(self, ns: str, record: MemoryRecord) -> None:
        return await derive_graph._evolve_links(self, ns, record)

    async def sync_prompt_versions(self, namespace: str = "default") -> list[MemoryRecord]:
        """D-43 §4: record each resolved prompt version as a procedural record
        (reference only — the definition lives in prompts/). Idempotent."""
        return await derive_scheduler.sync_prompt_versions(self, namespace)

    def model_calls(self) -> dict[str, int]:
        """LLM calls this engine has made since ``start()``, per role (read and
        write path alike). Empty when no LLM is configured."""
        return admin.model_calls(self)

    def rerank_stats(self) -> dict[str, Any]:
        """E8 reranker use since construction: ``{"mode", "calls", "failures",
        "unavailable"}``. ``calls`` counts rerank attempts, ``failures`` those that
        raised (retrieval fell back to vector order), ``unavailable`` that the adapter
        could not be built and the stage disabled itself. Read-only."""
        return admin.rerank_stats(self)

    def model_usage(self) -> dict[str, dict[str, Any]]:
        """Per-role LLM spend since ``start()``: ``{"model", "calls", "prompt", "completion"}``.

        Token counts are the provider's own usage report where it gives one (OpenAICompat),
        else a four-characters-per-token estimate. Roles never called are omitted.
        """
        return admin.model_usage(self)

    def usage(self, *, reset: bool = False) -> dict[str, dict[str, Any]]:
        """#33: LLM calls and tokens per named prompt since ``start()`` or the last reset.

        Keyed by ``prompt_version`` (``<id>@<version>``; ``"<unnamed>"`` for a caller's
        own messages sent through :meth:`llm`). Each entry holds ``prompt_id``,
        ``prompt_version``, ``roles``, ``calls``, ``input_tokens``, ``output_tokens``,
        ``estimated_calls`` and ``estimated``. Tokens are the provider's own usage
        report when it gives one, else a characters/4 estimate, and such calls are
        counted in ``estimated_calls``. ``reset=True`` clears the counters after the
        snapshot is taken. In-process only; empty when no LLM role is bound.
        """
        return admin.usage(self, reset=reset)

    def structured_stats(self, *, reset: bool = False) -> dict[str, dict[str, Any]]:
        """I69: structured-output outcomes per prompt version: ``calls``, ``clean``,
        ``repaired`` (strict parse failed, the repair net rescued it), ``validation_failed``,
        ``llm_errors``, ``retried`` / ``retry_ok`` / ``retry_failed`` (``llm.structured``),
        ``constrained_retries``, plus ``repair_rate`` and ``failure_rate``. Process-wide
        (shared by engines in one process), in memory only."""
        return admin.structured_stats(self, reset=reset)

    def llm(self, role: str) -> LLMService:
        """The provider bound to a role (D-07/D-22): extract / judge / chat.

        Returns a counting wrapper, not the bare provider: every ``chat`` call made
        through it is added to :meth:`model_calls` under ``role``. Other attributes are
        delegated to the bound provider.
        """
        return admin.llm(self, role)

    async def freshness(self, namespace: str = "default") -> dict[str, Any]:
        """O4-01b (O-06): per derived layer, the gap between the namespace's latest raw-turn seq and
        the minimum ``covers_seq`` of the layer's stamped records, and its ``stale`` flag. Needs
        ``offline.budget_v2`` or ``read.freshness_v2``."""
        self._require_started()
        if self._freshness_tracker is None:
            raise MemspineError("freshness needs offline.budget_v2 or read.freshness_v2")
        return (await read_freshness.report(self, validate_namespace(namespace))).as_dict()

    @tap.traced("job")
    async def sleep(self) -> Any:
        """Run the maintenance sleep cycle now (M2/E7): consolidate → decay →
        compress → prune. All steps are idempotent; P3 gives them teeth.

        Under ``offline.sleep_v2`` returns a ``SleepHandle`` at once instead of the stage stats
        (``await handle.wait()`` gives the stats)."""
        return await derive_scheduler.sleep(self)

    def _inflate_all(self, records: list[MemoryRecord], namespace: str) -> list[MemoryRecord]:
        return derive_scheduler._inflate_all(self, records, namespace)

    async def rebuild(self) -> dict[str, int]:
        """Rebuild every projector from seq 0 (D0.1). Raises off-window (D-45)."""
        storage = self._require_started()
        counts = {
            projector.name: await replay_rebuild(storage, projector)
            for projector in self._projectors
        }
        if self._semantic is not None:
            self._semantic.invalidate_index()  # LSH state rebuilt from fresh rows
        # Replay re-deletes forgotten rows, and the pre-rebuild table lives on in
        # older versions: purge both, as the hard forget did (M7).
        purge = getattr(self._vector, "purge_deleted", None)
        if callable(purge):
            await purge()
        self._passive_sessions.clear()  # #53: passive flags were replayed
        if self._graph is not None:
            # F-16f G1: the replay re-feeds erasure-hidden dependants (their WRITE events stay in
            # the log until re-derivation); drop every lost id from the graph again, as the
            # erasure's graph purge step did (it applied to the removed Neo4j mirror before).
            lost = self.__dict__.get("_erasure_state")
            if lost is not None and lost.lost:
                for rid in lost.lost:
                    await self._graph.delete_node(rid)
                from memspine.memories.associative import entity_cache

                entity_cache.invalidate(self._graph)
        _log.info(EVENT_REBUILD, counts=counts)
        return counts

    def _provider_status_service(self) -> ProviderStatusService:
        self._require_started()
        assert self._resolved is not None
        service = ProviderStatusService(
            health_timeout_s=self._resolved.config.runtime.provider_services.health_timeout_s
        )
        for logical_id, result in self._provider_results.items():
            service.register_build_result(
                result,
                required=result.provider.category
                in {"storage", "vector", "graph", "lexical", "cache"},
                logical_id=logical_id,
            )
        return service

    async def provider_health(self) -> ProviderHealthReport:
        """Run bounded, read-only infrastructure probes; never call a paid model."""
        return await self._provider_status_service().health()

    async def _profile_card_entry_count(self, namespace: str) -> int:
        """Read the derived card inventory without coupling read to derive."""
        from memspine.derive.pipeline.stages.profile_card import read_latest_card

        card = await read_latest_card(self._require_started(), namespace)
        return len(card.entries) if card is not None else 0

    def provider_changes(
        self,
        *,
        revision_guard: RevisionGuard | None = None,
        strategies: Mapping[tuple[ChangeCategory, str], ChangeStrategy] | None = None,
        plan_ttl_s: float | None = None,
        cleanup_quiescence_guard: CleanupQuiescenceGuard | None = None,
    ) -> EngineProviderChanges:
        """Construct explicit administrative change control over this deployment.

        Supply verified category strategies and revision/config sources. This
        call performs no migration, model probe or destructive cleanup.
        """
        self._require_started()
        runtime = self._provider_change_runtime
        supplied = (revision_guard is not None, strategies is not None, plan_ttl_s is not None)
        if any(supplied) and not all(supplied):
            raise ConfigError(
                "provider_changes requires all of revision_guard, strategies, and plan_ttl_s"
            )
        if not any(supplied):
            if runtime is None:
                raise ConfigError(
                    "provider_changes requires revision_guard, strategies, and plan_ttl_s "
                    "or Engine(provider_change_runtime=...)"
                )
            revision_guard = runtime.revision_guard
            strategies = runtime.strategies
            plan_ttl_s = runtime.plan_ttl_s
            cleanup_quiescence_guard = cleanup_quiescence_guard or runtime.cleanup_quiescence_guard
        assert revision_guard is not None and strategies is not None and plan_ttl_s is not None
        if (
            runtime is not None
            and revision_guard is runtime.revision_guard
            and strategies == runtime.strategies
            and plan_ttl_s == runtime.plan_ttl_s
            and cleanup_quiescence_guard is runtime.cleanup_quiescence_guard
            and self._provider_changes_controller is not None
        ):
            return self._provider_changes_controller
        controller = self._build_provider_changes_controller(
            ProviderChangeRuntime(
                revision_guard=revision_guard,
                strategies=strategies,
                plan_ttl_s=plan_ttl_s,
                cleanup_quiescence_guard=cleanup_quiescence_guard,
            )
        )
        if runtime is not None and revision_guard is runtime.revision_guard:
            self._provider_changes_controller = controller
        return controller

    def _build_provider_changes_controller(
        self, runtime: ProviderChangeRuntime
    ) -> EngineProviderChanges:
        self._require_started()
        if self._sql is None:
            raise ConfigError(
                "provider changes need a SQL records store: storage.records.provider "
                f"{self._config().storage.records.provider!r} keeps no change tables"
            )
        assert self._provider_owner_id is not None
        sql = self._sql

        def assert_active() -> None:
            self._require_started()
            if self._sql is not sql:
                raise MemspineError("provider change controller belongs to a previous Engine start")

        coordinator, _ = build_engine_change_support(
            sql=sql,
            revision_guard=runtime.revision_guard,
            strategies=runtime.strategies,
            plan_ttl_s=runtime.plan_ttl_s,
        )
        config = self._resolved.config if self._resolved is not None else None
        projections = config.runtime.service_switches.projections if config is not None else None
        return EngineProviderChanges(
            coordinator=coordinator,
            store=SqlChangeStore(sql.engine),
            deployment_id=self._provider_owner_id,
            assert_active=assert_active,
            cleanup_interval_s=(
                projections.cleanup_interval_s if projections is not None else None
            ),
            cleanup_batch_size=(projections.cleanup_batch_size if projections is not None else 50),
            cleanup_quiescence_guard=runtime.cleanup_quiescence_guard,
        )

    async def provider_catalog(self, *, probe_health: bool = False) -> list[dict[str, object]]:
        """Inspect registered and built services without exposing native settings.

        Each built logical service has its own row, including separate LLM
        roles. Infrastructure health checks are opt-in; no
        model quality calls or model downloads are performed.
        """
        from memspine.providers.inspection import inspect_providers, projection_diagnostics

        self._require_started()
        assert self._resolved is not None
        config = self._resolved.config
        configured: dict[str, str] = {}
        category_names = {
            "storage": config.storage.records.provider,
            "cache": config.storage.cache.provider,
            "vector": config.storage.vector.provider,
            "embedding": config.model.embedding.provider,
            "lexical": config.storage.lexical.provider,
        }
        for logical_id, result in self._provider_results.items():
            configured[logical_id] = category_names.get(
                result.provider.category, result.provider.name
            )
        configured["graph.primary"] = config.storage.graph.provider
        report = await self.readiness() if probe_health else None
        diagnostics = {}
        diagnostic_error = False
        try:
            assert self._provider_owner_id is not None
            if self._sql is not None:  # projection switch diagnostics live in SQL tables
                async with asyncio.timeout(config.runtime.provider_services.health_timeout_s):
                    diagnostics = await projection_diagnostics(self._sql, self._provider_owner_id)
        except Exception:
            diagnostic_error = True
        rows = inspect_providers(
            results=self._provider_results,
            resource_states={}
            if self._provider_resources is None
            else self._provider_resources.states,
            configured=configured,
            health=None if report is None else report.health,
            projector_catchup=None if report is None else report.projector_catchup,
            projections=diagnostics,
        )
        if diagnostic_error:
            for row in rows:
                if row["logical_service_id"] and row["category"] in {"vector", "lexical", "graph"}:
                    row["sanitized_error_code"] = "PROVIDER_INSPECTION_FAILED"
        return rows

    async def readiness(self) -> ReadinessReport:
        """Require responsive infrastructure and caught-up selected projections."""
        storage = self._require_started()
        assert self._resolved is not None
        catchup: dict[str, bool] = {}
        for projector in self._projectors:
            try:
                async with asyncio.timeout(
                    self._resolved.config.runtime.provider_services.health_timeout_s
                ):
                    offset = await storage.get_offset(projector.name)
                    catchup[projector.name] = not await storage.read_events(
                        after_seq=offset, limit=1
                    )
            except Exception:
                catchup[projector.name] = False
        report = await self._provider_status_service().readiness(projector_catchup=catchup)
        return dataclasses.replace(report, warm=self.warm_state())  # RM-74

    def describe(self) -> dict[str, Any]:
        """The effective world (§4 step 7). Only meaningful on a started engine."""
        return admin.describe(self)

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

    def _require_started(self) -> RecordsStore:
        if not self._started or self._storage is None:
            raise MemspineError("Engine not started — call start() first")
        return self._storage

    async def _records(
        self, namespace: str, memory_type: str | None = None, group_id: str | None = None
    ) -> list[MemoryRecord]:
        """I2: ``list_records`` (snapshot-aware via :func:`_snapshot_listings`)."""
        return await self._require_started().list_records(namespace, memory_type, group_id)

    async def _append_and_project(self, event: MemoryEvent) -> None:
        """The one internal path from an event to its projections.

        Inline-runner semantics (P1): the appended event is in hand — apply it
        directly in every mode (startup catch-up already brought projectors to
        the head; set_offset is advance-only, so races cannot regress marks).
        """
        assert self._storage is not None
        snapshot = _READ_SNAPSHOT.get()
        if snapshot:  # I2: a write inside a read: later listings must see it
            snapshot.clear()
        event = await self._inherit_governance(event)
        appended = await self._storage.append_event(event)
        if self._freshness_tracker is not None:  # O4-01b: the raw-turn seq the derived layers cover
            read_freshness.note_event(self._freshness_tracker, appended)
        if self._derive_sched is not None and event.kind is EventKind.WRITE:
            # O4-01a: count the deposit toward the sleep trigger
            await self._derive_sched.on_commit_async(event.namespace, appended.seq or 0)
        read_request._bump_generations(self, event)  # R3-15: read-cache generations
        if appended.seq is None:  # pragma: no cover - write door always assigns seq
            raise MemspineError("write door returned an event without seq")
        if self._neighbour_batches and event.kind in (EventKind.WRITE, EventKind.FORGET):
            for neighbour_batch in self._neighbour_batches.get(event.namespace, ()):
                neighbour_batch.observe(appended)  # #63: rows the vector store gains or loses
        batch = self._batch_offsets
        for projector in self._projectors:
            await projector.apply(appended)
            if batch is not None:
                batch[projector.name] = appended.seq  # checkpointed at the flush
            else:
                await self._storage.set_offset(projector.name, appended.seq)
        if inspect_obs.inspect_enabled():  # TI-1: per-record index membership, sleep per-item notes
            if appended.kind is EventKind.WRITE:
                inspect_obs.index_record(
                    appended, self._projectors, getattr(self._embedder, "embedder_id", None)
                )
            inspect_obs.note_event(appended)
        if self._query_encoder is not None:
            self._evict_from_encoder(event)

    def _evict_from_encoder(self, event: MemoryEvent) -> None:
        return read_legs._evict_from_encoder(self, event)

    async def _inherit_governance(self, event: MemoryEvent) -> MemoryEvent:
        """#50: a derived record's purposes and PII tier follow its parents.

        A WRITE whose record names ``source.parents`` (summaries, mined facts, list
        cards, entity and community summaries, surprise facts, cues, reflections,
        graph facts) gets the intersection of its parents' purposes and its own
        (:func:`inherited_consent`) and the highest of their PII tiers. Only
        parents in the record's own namespace count (a foreign id is no oracle).
        The event is returned unchanged when nothing changes, so logs without
        tagged or PII-tiered parents stay byte-identical."""
        return await trust_firewall._inherit_governance(self, event)

    @asynccontextmanager
    async def _projection_batch(self) -> AsyncIterator[None]:
        """Group the projection work of many events (one ``ingest_messages`` call).

        Every event is still appended and applied one at a time, so the log, the
        records and every read in between are exactly as without the batch.
        Two costs move to the end: projectors may hold per-event commits (see
        :meth:`Projector.begin_batch`), and the high-water marks are written
        once per projector instead of once per event. A mark is written only
        after that projector's flush succeeds, so a projection is never marked
        ahead of what it holds; after a crash catch-up re-applies the tail
        (applies are idempotent). Nested batches join the outermost one."""
        if self._batch_offsets is not None:
            yield
            return
        self._batch_offsets = {}
        for projector in self._projectors:
            projector.begin_batch()
        try:
            yield
        finally:
            offsets, self._batch_offsets = self._batch_offsets, None
            await self._flush_projections(offsets)

    async def _flush_projections(self, offsets: Mapping[str, int]) -> None:
        """Flush each projector, then checkpoint the ones that flushed."""
        assert self._storage is not None
        failure: BaseException | None = None
        for projector in self._projectors:
            try:
                await projector.flush()
            except Exception as exc:
                # Its mark stays behind, so catch-up re-applies its tail.
                failure = failure or exc
                continue
            seq = offsets.get(projector.name)
            if seq is not None:
                await self._storage.set_offset(projector.name, seq)
        if failure is not None:
            raise failure

    def _namespace_lock(self, namespace: str) -> asyncio.Lock:
        """The same per-namespace lock every write verb holds — handed to
        pipelines so their read-then-write units serialize with forget (M5)."""
        return self._write_locks.setdefault(namespace, asyncio.Lock())

    async def _derived_parents_gone(
        self, namespace: str, parents: Sequence[str], site: str
    ) -> bool:
        """I72: True (and audited) when any parent of a derived deposit is no longer live.

        Called with the namespace lock held, immediately before a sleep-stage deposit:
        the stage read its parents, awaited an LLM call outside the lock, and a hard
        forget (and its cascade) may have run in between. A parent that is missing,
        erased, archived or quarantined means the derived text may restate erased
        content, so the deposit is skipped, never written. The skip is logged and,
        under ``audit.actions``, recorded as a ``derived_deposit_skipped`` audit event
        naming the site and the vanished parent ids (ids only, never content)."""
        return await trust_erasure._derived_parents_gone(self, namespace, parents, site)

    async def _write_derived(
        self,
        storage: RecordsStore,
        ns: str,
        record: MemoryRecord,
        memory_type: str,
        actor: str,
        *,
        parents: Sequence[str],
        trust_cap: list[float] | None = None,
        site: str,
    ) -> MemoryRecord:
        """FU-SAFE: the derived-write door for callers outside ``derive`` (procedural lessons)."""
        return await derive_write.write_derived(
            self, storage, ns, record, memory_type, actor,
            parents=parents, trust_cap=trust_cap, site=site,
        )  # fmt: skip

    async def _link_derived_summary(
        self, namespace: str, record: MemoryRecord, parents: Sequence[str], site: str
    ) -> None:
        """FU-SAFE: lineage edges for a summary a legacy stage appended as a raw WRITE event."""
        storage = self._require_started()
        stored = await storage.get_record(record.record_id)
        await derive_write.link_derived(storage, namespace, stored or record, parents, site)

    async def _offline_live(self, records: list[MemoryRecord]) -> list[MemoryRecord]:
        """FU-SAFE (T5-05): the records an offline stage may read (consent, erasure, quarantine)."""
        return await trust_consent.offline_live(self, records)

    def _pipeline_ctx(self) -> PipelineContext:
        assert self._storage is not None and self._resolved is not None
        return PipelineContext(
            storage=self._storage,
            config=self._resolved.config,
            append_event=self._append_and_project,
            summarize=self._summarize,
            summarize_incremental=self._build_summarize_incremental(),
            predict_episode=self._build_episode_predictor(),
            calibrate=self._build_calibrator(),
            deposit_surprise=self._deposit_surprise_fact,
            extract_edges=self._extract_edges,
            extract_session_edges=self._extract_session_edges,
            adjudicate_edge=self._build_edge_adjudicator(),
            find_entities=(
                self._entity_finder() if self._extract_session_edges is not None else None
            ),
            mine_facts=self._build_fact_miner(),
            project_assertions=self._build_assertion_projector(),
            date_facts=self._build_fact_dater(),
            deposit_fact=self._deposit_mined_fact,
            deposit_list_card=self._deposit_list_card,
            label_classes=self._build_class_labeller(),
            anticipate=self._build_anticipator(),
            deposit_cues=self._deposit_anticipated_cues,
            reflect=self._build_reflector(),
            deposit_reflection=self._deposit_profile_reflection,
            close_call=derive_builders._build_close_call(self),
            deposit_session_record=self._deposit_session_record,
            screen=self._screen_derived,
            write_fact=self._write_extracted_fact,
            write_summary=self._write_offline_summary,
            erasure_engine=self if self._config().privacy.erasure_rederive else None,
            link_derived=(
                self._link_derived_summary if self._config().write.lineage_table else None
            ),
            live_gate=(
                self._offline_live
                if self._config().consent.v2 or self._config().privacy.erasure_v2
                else None
            ),
            # Only when associative projects it (ADR-015): an explicit-config
            # graph store without the projector would reorganize a stale graph.
            graph=self._graph if self._associative is not None else None,
            lock=self._namespace_lock,
            parents_gone=self._derived_parents_gone,
            expire_retention=self.expire_retention,
            summarize_entities=self._build_entity_summarizer(),
            resolve_entities=self._build_entity_resolver(),
            embed=self._embedder.embed if self._embedder is not None else None,
            window_edges=(
                self._windows.take_edges
                if self._windows is not None and "edges" in self._windows.sections
                else None
            ),
            sensitivity_model=self._sensitivity_model(),
            topic_tagger=topic_hook(self._config(), self._topic_tagger),
        )

    def _build_topic_tagger(self, config: MemspineConfig) -> Any | None:
        """RM-106: the opt-in GLiNER topic tagger from ``model.extraction.topic`` (offline,
        ``local_files_only``); None when the slot is absent. Building never loads a model (the
        pool loads lazily); a failure is logged and leaves the topic source off."""
        slot = config.model.extraction.topic
        if slot is None or self._gliner_pool is None:
            return None
        try:
            from memspine.config.schema.model import gliner_settings
            from memspine.derive.topics import GlinerTopicTagger

            return GlinerTopicTagger(
                self._gliner_pool,
                gliner_settings(config, "topic"),
                labels=slot.labels,
                min_confidence=slot.min_confidence,
            )
        except Exception as exc:
            _log.warning("topic.tagger_unavailable", error=f"{type(exc).__name__}: {exc}")
            return None

    def _sensitivity_model(self) -> Any | None:
        """RM-99: the opt-in GLiNER sensitivity detector (``model.extraction.sensitivity``),
        built once per Engine so its pool lease is reused; None when the slot is absent."""
        config = self._config()
        if config.model.extraction.sensitivity is None or self._gliner_pool is None:
            return None
        pool = self._gliner_pool
        cached = self.__dict__.get("_sensitivity_detector")
        if cached is None or cached[0] is not pool:  # a restart builds a new pool
            from memspine.config.schema.model import gliner_settings
            from memspine.derive.sensitivity import GlinerSensitivityDetector

            detector = GlinerSensitivityDetector(pool, gliner_settings(config, "sensitivity"))
            cached = (pool, detector)
            self.__dict__["_sensitivity_detector"] = cached
        return cached[1]

    # ── W1: adaptive-window write (call 1), session close (call 2 hook) ───────────

    def _build_windows(self, config: MemspineConfig) -> WindowPipeline | None:
        return ingest_windows._build_windows(self, config)

    async def _window_session_closed(self, info: SessionClosed) -> dict[str, object]:
        return await ingest_windows._window_session_closed(self, info)

    async def _forget_learned_aliases(self, namespace: str) -> None:
        """RM-18: drop the W2-13 aliases ``namespace`` learned (namespace erasure)."""
        await ingest_windows.forget_learned_aliases(self, namespace)

    async def flush_windows(
        self, namespace: str | None = None, *, final: bool = True
    ) -> dict[str, Any]:
        """W1: run every closed window's call now (all namespaces, or one) and return the
        write-quality counters. ``final=True`` also closes the open windows and fires the
        session-closed hook for the sessions still open: the end of a stream or a batch (the
        eval harness calls it before reads and before the sleep cycle). A no-op returning
        ``{}`` when ``write.window_extract`` is off."""
        return await ingest_windows.flush_windows(self, namespace, final=final)

    async def close_session(
        self, namespace: str = "default", session_id: str | None = None
    ) -> dict[str, Any]:
        """W1: the real session-closed hook. Cuts the session's open window, runs the
        namespace's queued window calls in order, then fires the session-close stage (call 2:
        a stub today, it only records a ``session_closed`` MARKER). ``session_id=None``: the
        namespace's current session. Works without ``write.window_extract`` too (then only the
        stage runs, and a ``session_id`` is required)."""
        return await ingest_windows.close_session(self, namespace, session_id)

    def cost_report(self, namespace: str | None = None) -> dict[str, Any]:
        """W2-12 (``economy.ledger_v2``): model calls, tokens in/out, latency and embed calls of
        the writes since ``start()``, in total and split per layer, stage (``window_extract``,
        each sleep stage) and namespace; ``namespace`` filters. Engine-wide sleep stages sit under
        namespace ``"-"``. ``budget`` is None without ``economy.budget``. ``{}`` when the flag is
        off."""
        ledger = self._cost_ledger
        if ledger is None:
            return {}
        ledger.flush()
        report = ledger.report(namespace)
        if namespace is not None and self._windows is not None:
            report["deferred_windows"] = self._windows.deferred_count(namespace)
        return report

    def ops_report(self, namespace: str | None = None) -> dict[str, Any]:
        """T5-06 (G-20): one report over the ops ledger (``observability.ops_ledger``: runs,
        errors, items, dropped, deferred and p50 / p95 per read verb, write verb, window extract
        and sleep stage), the SLO ledger (``runtime.scheduler_v2``: read p95, lag, backlog) and the
        write cost ledger (``economy.ledger_v2``: model calls, tokens, embed calls, budget). A
        ledger that is off contributes nothing; ``namespace`` filters the cost rows. Ids, counts
        and durations only."""
        slo_ledger = self._binding.slo_ledger
        return _ops.ops_report(
            self._ops_ledger,
            slo=slo_ledger.report() if slo_ledger is not None else None,
            cost=self._cost_ledger,
            namespace=namespace,
            otel=self._otel.stats() if self._otel is not None else None,
        )

    async def resume_deferred(self, namespace: str) -> int:
        """W2-12: derive the windows a spent ``economy.budget`` paused (they were kept in the
        outbox, marked underived). Does nothing while the budget is still spent; returns the
        number of windows derived."""
        self._require_started()
        if self._windows is None:
            return 0
        return await self._windows.resume_deferred(namespace)

    def window_stats(self, *, reset: bool = False) -> dict[str, Any]:
        """G-W13 write-quality counters of the window write (``{}`` when it is off): windows and
        why they closed, calls per layer, cache hits, parse failures, sections dropped, items
        dropped as ungrounded, deposits, cancellations, windows per session, calls per turn."""
        return admin.window_stats(self, reset=reset)

    async def _deposit_window_episode(
        self,
        namespace: str,
        text: str,
        parents: list[str],
        valid_from: datetime,
        window_id: str,
        *,
        require_live: Sequence[str] = (),
    ) -> MemoryRecord | None:
        return await ingest_windows._deposit_window_episode(
            self, namespace, text, parents, valid_from, window_id, require_live=require_live
        )

    def _build_entity_summarizer(self) -> SummarizeEntities | None:
        return derive_builders._build_entity_summarizer(self)

    def _build_entity_resolver(self) -> ResolveBatch | None:
        return derive_builders._build_entity_resolver(self)

    #: G24: the decision planner's options, label -> (read mode, description). Plain
    #: labels with cue-word descriptions; tuned offline on a frozen LoCoMo set
    #: (``evals/prereg/G24_gliner2_planner*.md``, ADR-052).
    _READ_OPTIONS: ClassVar[dict[str, tuple[str, str]]] = {
        "count or list": (
            "compose",
            "how many times, how often, how many things; what things, which items, all of the",
        ),
        "reason or feeling": (
            "replay",
            "why, what motivated or inspired, how someone felt or reacted, what someone said",
        ),
        "single fact": ("retrieve", "what, where, who, when: one specific thing"),
    }

    def _decision_provider(self) -> Any:
        """H24: the ``model.decider.route`` decision provider, built lazily; None when the slot
        is absent (or when gliner2 was missing at start under ``strict_services: false``)."""
        config = self._config()
        if config.model.decider.route is None or getattr(self, "_decision_off", False):
            return None
        if getattr(self, "_decision", None) is None:
            from memspine.config.schema.model import gliner_settings
            from memspine.providers.decider import build_decision_provider

            if self._gliner_pool is None:
                raise ConfigError("decision GLiNER provider needs the engine GLiNER pool")
            settings = gliner_settings(config, "route")
            # RM-113: the model.decider descriptor builds it over the extraction pool.
            self._decision = build_decision_provider(settings, self._gliner_pool)
        return self._decision

    async def _plan_read_mode(self, query: str) -> str | None:
        return await read_request._plan_read_mode(self, query)

    async def _query_contract(self, query: str) -> QueryContract:
        return await read_request._query_contract(self, query)

    async def _llm_query_contract(self, query: str, rules: QueryContract) -> QueryContract | None:
        return await read_request._llm_query_contract(self, query, rules)

    async def _llm_read_plan(self, query: str) -> ReadPlan | None:
        return await read_request._llm_read_plan(self, query)

    async def _query_rewrite_probes(self, query: str) -> list[str]:
        return await read_request._query_rewrite_probes(self, query)

    def _build_summarize(self) -> Summarize | None:
        return derive_builders._build_summarize(self)

    def _build_summarize_incremental(self) -> SummarizeIncremental | None:
        return derive_builders._build_summarize_incremental(self)

    def _predict_calibrate_role(self, role: str) -> str | None:
        return derive_builders._predict_calibrate_role(self, role)

    def _build_episode_predictor(self) -> PredictEpisode | None:
        return derive_builders._build_episode_predictor(self)

    def _build_calibrator(self) -> CalibrateEpisode | None:
        return derive_builders._build_calibrator(self)

    async def _deposit_surprise_fact(
        self,
        namespace: str,
        text: str,
        entity: str | None,
        attribute: str | None,
        parents: list[str],
        valid_from: datetime,
        session_key: str,
        *,
        kind: str | None = None,
        **_views: Any,
    ) -> MemoryRecord | None:
        return await derive_integrate._deposit_surprise_fact(
            self,
            namespace,
            text,
            entity,
            attribute,
            parents,
            valid_from,
            session_key,
            kind=kind,
            **_views,
        )

    async def _relevance_filter(
        self, query: str, candidates: list[tuple[MemoryRecord, float]]
    ) -> list[tuple[MemoryRecord, float]]:
        return await read_gate._relevance_filter(self, query, candidates)

    def _build_reflector(self) -> Any:
        return derive_builders._build_reflector(self)

    async def _deposit_profile_reflection(
        self, namespace: str, content: str, evidence_ids: list[str], session_key: str
    ) -> MemoryRecord | None:
        return await derive_integrate._deposit_profile_reflection(
            self, namespace, content, evidence_ids, session_key
        )

    async def _deposit_session_record(
        self,
        namespace: str,
        kind: str,
        text: str,
        parents: list[str],
        valid_from: datetime,
        session_id: str,
        *,
        extends: str | None = None,
        when_text: str | None = None,
        participants: Sequence[str] = (),
        idem_key: str = "",
        folded: bool = False,
    ) -> MemoryRecord | None:
        """O4-02: a call-2 summary or episode through the derived-write door."""
        return await derive_integrate._deposit_session_record(
            self,
            namespace,
            kind,
            text,
            parents,
            valid_from,
            session_id,
            extends=extends,
            when_text=when_text,
            participants=participants,
            idem_key=idem_key,
            folded=folded,
        )

    def _build_anticipator(self) -> Any:
        return derive_builders._build_anticipator(self)

    async def _deposit_anticipated_cues(
        self, namespace: str, record_id: str, cues: list[str], session_key: str
    ) -> list[MemoryRecord]:
        return await derive_integrate._deposit_anticipated_cues(
            self, namespace, record_id, cues, session_key
        )

    def _build_assertion_projector(self) -> Any:
        return derive_builders._build_assertion_projector(self)

    def _build_fact_miner(self) -> Any:
        return derive_builders._build_fact_miner(self)

    def _consolidation_option(self, name: str, default: Any) -> Any:
        """One ``memories.episodic.policies.consolidation`` option as configured."""
        options = self._memory_policy(self._config(), "episodic").get("consolidation")
        return options.get(name, default) if isinstance(options, dict) else default

    def _build_fact_dater(self) -> Any:
        return derive_builders._build_fact_dater(self)

    async def _deposit_mined_fact(
        self,
        namespace: str,
        text: str,
        entity: str | None,
        attribute: str | None,
        parents: list[str],
        valid_from: datetime,
        session_key: str,
        *,
        kind: str | None = None,
        happened: str | None = None,
        said: str | None = None,
        persons: list[str] | None = None,
        location: str | None = None,
        topic: str | None = None,
        extra_tags: Sequence[str] = (),
        require_live: Sequence[str] = (),
        trust_cap: float | None = None,
        envelope: dict[str, Any] | None = None,
    ) -> MemoryRecord | None:
        return await derive_integrate._deposit_mined_fact(
            self,
            namespace,
            text,
            entity,
            attribute,
            parents,
            valid_from,
            session_key,
            kind=kind,
            happened=happened,
            said=said,
            persons=persons,
            location=location,
            topic=topic,
            extra_tags=extra_tags,
            require_live=require_live,
            trust_cap=trust_cap,
            envelope=envelope,
        )

    async def _auto_watch(
        self, ns: str, plan: MemoryRecord, sources: list[MemoryRecord]
    ) -> MemoryRecord | None:
        return await derive_integrate._auto_watch(self, ns, plan, sources)

    def _build_class_labeller(self) -> LabelClasses | None:
        return derive_builders._build_class_labeller(self)

    async def _deposit_list_card(
        self, namespace: str, card: ListCard | None, replaces: list[str]
    ) -> MemoryRecord | None:
        return await derive_integrate._deposit_list_card(self, namespace, card, replaces)

    async def _write_extracted_fact(
        self, record: MemoryRecord, trust_cap: list[float]
    ) -> MemoryRecord:
        return await derive_integrate._write_extracted_fact(self, record, trust_cap)

    async def _write_offline_summary(
        self,
        namespace: str,
        record: MemoryRecord,
        parents: Sequence[str],
        layer: str,
        site: str,
        trust_cap: list[float],
    ) -> MemoryRecord:
        return await derive_integrate._write_offline_summary(
            self, namespace, record, parents, layer, site, trust_cap
        )

    def _edge_extract_callable(self, max_rounds: int, condition: str | None = None) -> ExtractEdges:
        return derive_builders._edge_extract_callable(self, max_rounds, condition)

    def _build_edge_extractor(self, config: MemspineConfig) -> ExtractEdges | None:
        return derive_builders._build_edge_extractor(self, config)

    def _build_session_edge_extractor(self, config: MemspineConfig) -> ExtractEdges | None:
        return derive_builders._build_session_edge_extractor(self, config)

    def _build_edge_adjudicator(self) -> Callable[[str, str, str, str], Awaitable[str]] | None:
        return derive_builders._build_edge_adjudicator(self)

    def _entity_finder(self) -> FindEntities | None:
        """#20: the decision provider's ``entities`` hook (GLiNER2), or None."""
        provider = self._decision_provider()
        hook = getattr(provider, "entities", None) if provider is not None else None
        return hook if callable(hook) else None

    @staticmethod
    def _memory_policy(config: MemspineConfig, memory_type: str) -> dict[str, Any]:
        return memory_policy(config, memory_type)


# RM-75: every public coroutine verb runs with this Engine's binding (gate, ledgers, tap).
# RM-75: every coroutine method, private ones too: the pipeline context callbacks, the facade
# helpers called from ``api.ops_*`` / ``api.handle`` and the window and sleep callbacks run as
# this Engine even when their caller (a direct pipeline call, a host thread) is unbound.
_bindings.bind_methods(Engine)
