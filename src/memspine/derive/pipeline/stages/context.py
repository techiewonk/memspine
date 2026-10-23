"""Derive: the pipeline context, its callable types and shared helpers (F-15)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from memspine.config.schema import MemspineConfig
from memspine.core.policies.trust import TrustPolicy
from memspine.derive.pipeline.stages.index import SessionIndex
from memspine.exceptions import ConfigError
from memspine.memories.associative.resolution import Embed, ResolveBatch
from memspine.memories.semantic.write_pipeline import ExtractEdges, ScreenDerived
from memspine.observability.logging import get_logger
from memspine.prompts.models import AnticipatedCue, AssertionOut, ExtractedEdge, ExtractedFact
from memspine.services.graph.base import GraphStore
from memspine.store.events import MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.trust.firewall import Firewall, FirewallVerdict
from memspine.workers.list_cards import DepositListCard, LabelClasses

_log = get_logger(__name__)


class PipelineStorage(Protocol):
    """The narrow storage slice pipelines may touch — keeps this module free of
    any concrete backend import (anti-lock-in, D-17). Reads only: every write
    goes through ``PipelineContext.append_event`` (the write door)."""

    async def prune_events(self, older_than: datetime) -> int: ...

    async def list_namespaces(self) -> list[str]: ...

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...


AppendEvent = Callable[[MemoryEvent], Awaitable[None]]


#: The engine's semantic write door for one extract_graph fact, called with the
#: namespace lock held: (unscreened record, parent trust cap) -> the stored record
#: (firewall screening, M5 dedup and the M4 conflict ladder all apply).
WriteDerivedFact = Callable[[MemoryRecord, list[float]], Awaitable[MemoryRecord]]


#: Offline wiring (O4-06, O4-09): the derived-write door for a summary record, called with the
#: namespace lock held: (namespace, record, parent ids, layer label, site, parent trust cap) -> the
#: stored record (lineage and trust cap with ``write.lineage_table``, firewall screening always).
WriteSummary = Callable[
    [str, MemoryRecord, Sequence[str], str, str, list[float]], Awaitable[MemoryRecord]
]


Summarize = Callable[[str], Awaitable[str]]


#: #20: text -> the entity names a decision provider (GLiNER2) finds in it.
FindEntities = Callable[[str], Awaitable[list[str]]]


#: #56: (previous summary, new turns) -> the updated summary (``summarize@incremental``).
SummarizeIncremental = Callable[[str, str], Awaitable[str]]


#: #62: (opening cue, session date, known statements) -> predicted facts, one per line.
PredictEpisode = Callable[[str, str, list[str]], Awaitable[str]]


#: #62: (prediction, dated transcript, known statements) -> the facts the transcript
#: states that the known statements do not (``calibrate`` v2, ADR-049).
CalibrateEpisode = Callable[[str, str, list[str]], Awaitable[list[ExtractedFact]]]


# ``ExtractEdges`` (re-exported): LLM edge extraction for the graphiti-style
# write path (C2). Takes source text plus an optional :class:`EdgeContext`,
# returns the (reflexion-merged) relationship edges. None on the context => the
# extract_graph pipeline self-skips — no LLM role or the feature is off.
#: H14: reflector (``reflect`` role): session turns -> (insight, evidence indices).
Reflect = Callable[[list[str]], Awaitable[list[tuple[str, list[int]]]]]


#: H14: engine-side reflection deposit (namespace, insight, evidence record ids, key).
DepositReflection = Callable[[str, str, list[str], str], Awaitable[object]]


#: H8: anticipator (``anticipate`` role): numbered session text -> cues.
Anticipate = Callable[[str], Awaitable[list[AnticipatedCue]]]


#: H8: engine-side cue deposit (namespace, target record id, cue texts, session key).
DepositCues = Callable[[str, str, list[str], str], Awaitable[object]]


#: C6': LLM fact miner (``extract`` role): session text -> atomic facts.
MineFacts = Callable[[str], Awaitable[list[ExtractedFact]]]


#: E01 write-time arm (``memories.semantic.policies.fact_projection``): numbered session
#: transcript -> subject-relation-object assertions with source spans (``extract@assertions``).
ProjectAssertions = Callable[[str], Awaitable[list[AssertionOut]]]


#: #29: (transcript, fact statements) -> {1-based fact index: date} for the facts the
#: model could date (one batched call).
DateFacts = Callable[[str, list[str]], Awaitable[dict[int, str]]]


#: #48: run the engine's retention expiry; returns the stage stats.
ExpireRetention = Callable[[], Awaitable[dict[str, object]]]


#: GP-6 (#17): ``[(entity name, dated fact lines)]`` -> {1-based index: summary}
#: (one batched LLM call, at most ``ENTITY_SUMMARY_BATCH`` entities).
SummarizeEntities = Callable[[list[tuple[str, list[str]]]], Awaitable[dict[int, str]]]


@dataclass
class WindowEdgeBatch:
    """W1 (call 1 -> call 3): the relationship edges one ``window_extract`` call returned.

    ``covered`` are the live turns the call read (so the sleep stage does not extract them
    again, whether or not any edge came out); each edge carries the record ids of the turns
    it cites. Non-durable by design: lost on a crash, the sleep stage then extracts the
    uncovered turns itself."""

    window_id: str
    covered: list[str]
    edges: list[tuple[list[str], ExtractedEdge]] = field(default_factory=list)


#: ``take(namespace)``: the pending call-1 edge batches of a namespace (and forget them).
WindowEdges = Callable[[str], list[WindowEdgeBatch]]


@dataclass
class SessionClosed:
    """W1: a session ended (explicit ``close_session``, a new session starting in the same
    namespace, or the end of a stream). ``windows`` counts the windows it produced. The
    interface of call 2 (summary, episode narrative, guarded profile updates)."""

    namespace: str
    session_id: str
    windows: int = 0


#: Call 2: handler of a closed session; returns its stats. None installed => the stub.
SessionCloseHandler = Callable[[SessionClosed], Awaitable[dict[str, object]]]


#: O4-02: the call-2 model call: chat messages (stable prefix, data body) -> the raw reply.
#: None => ``offline.session_close_v2`` has no LLM role to call and the stage reports ``skipped``.
CloseCall = Callable[[list[dict[str, str]]], Awaitable[str]]


class DepositSessionRecord(Protocol):
    """O4-02: engine-side deposit of one call-2 record (a session summary or an episode) through
    the derived-write door: (namespace, kind ``summary`` | ``episode``, text, parent ids,
    valid_from, session id) plus the validated extras. Returns None when a parent is no longer
    live (I72) or the record was refused; the deposit takes the namespace lock itself."""

    def __call__(
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
    ) -> Awaitable[MemoryRecord | None]: ...


class DepositFact(Protocol):
    """C6': engine-side deposit of one mined fact through the write door
    (namespace, text, entity, attribute, parent ids, event time, session key);
    ``kind`` (G1a) is ``state`` / ``event``, or None for an unclassified fact;
    ``happened`` / ``said`` (#29) the fact's happened date and the day it was said;
    ``persons`` / ``location`` / ``topic`` (#28) its multi-view fields;
    ``require_live`` (W1) more record ids that must still be live, besides the parents;
    ``trust_cap`` (W2-06a) a further trust ceiling (the envelope's source trust)."""

    def __call__(
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
    ) -> Awaitable[object]: ...


#: Per-namespace write serialization: ``lock(namespace)`` yields the same
#: async context manager the engine's write verbs hold, so a pipeline's
#: read-then-write unit cannot interleave with a concurrent forget cascade.
NamespaceLock = Callable[[str], AbstractAsyncContextManager[None]]


def _no_lock(_namespace: str) -> AbstractAsyncContextManager[None]:
    """Default no-op lock: contexts built directly (tests, bare runs) get the
    single-writer behaviour they already have without engine plumbing."""
    return nullcontext()


@dataclass
class PipelineContext:
    storage: PipelineStorage
    config: MemspineConfig
    #: The engine's append-through-the-write-door callable. None => read-only
    #: context (e.g. bare prune runs); mutating pipelines then report "skipped".
    append_event: AppendEvent | None = None
    #: Optional LLM summarizer (summarize role, D-43). None => deterministic
    #: extractive fallback (N6) — consolidation never *requires* an LLM.
    summarize: Summarize | None = None
    #: #56: the incremental summary update (``session_summary.incremental``). None
    #: => every summary is written from all its turns (``summarize`` or extractive).
    summarize_incremental: SummarizeIncremental | None = None
    #: #62: the predict and calibrate calls (``consolidation.predict_calibrate``).
    #: Any of the three None => the predict_calibrate stage self-skips.
    predict_episode: PredictEpisode | None = None
    calibrate: CalibrateEpisode | None = None
    deposit_surprise: DepositFact | None = None
    #: The association graph projection (D-26/P6). None => associative memory
    #: is disabled and the reorganizer reports "skipped".
    graph: GraphStore | None = None
    #: Per-namespace write lock (the engine passes its write-verb locks so
    #: reorganize cannot race forget's delete cascade — a forgotten node must
    #: not be resurrected by a late LINK projection). No-op by default.
    lock: NamespaceLock = _no_lock
    #: LLM edge extractor for the C2 graphiti-style pipeline. None => the
    #: extract_graph stage self-skips (feature off or no extract_edges LLM role).
    extract_edges: ExtractEdges | None = None
    #: #20: the session-level extractor (``extract_edges@session``: numbered turns
    #: in, edges with ``episode_indices`` out). None => record-level extraction.
    extract_session_edges: ExtractEdges | None = None
    #: G-2: the ``invalidate_edge`` adjudicator: (existing fact, its valid_from,
    #: incoming fact, its valid_from) -> add | update | invalidate | noop. None => the
    #: cross-key contradiction check self-skips.
    adjudicate_edge: Callable[[str, str, str, str], Awaitable[str]] | None = None
    #: #20: the decision provider's entity finder (GLiNER2): text -> entity names,
    #: the allowed-entity list of a session extraction. None => no list.
    find_entities: FindEntities | None = None
    #: C6' atomic-fact mining. Both None => the mine_facts stage self-skips.
    mine_facts: MineFacts | None = None
    #: E01 write-time projection. None => the project_facts stage self-skips.
    project_assertions: ProjectAssertions | None = None
    #: #29: the batched LLM date fill (``consolidation.mine_event_dates_llm``).
    date_facts: DateFacts | None = None
    #: #30: the list-card deposit and the per-person class labeller
    #: (``consolidation.list_cards``). No deposit => the step self-skips.
    deposit_list_card: DepositListCard | None = None
    label_classes: LabelClasses | None = None
    #: H8 anticipatory cues. Both None => the anticipate stage self-skips.
    anticipate: Anticipate | None = None
    #: H14 profile reflection. Both None => the reflect_profile stage self-skips.
    reflect: Reflect | None = None
    deposit_reflection: DepositReflection | None = None
    deposit_cues: DepositCues | None = None
    deposit_fact: DepositFact | None = None
    #: N2: the engine's write-door firewall for derived records this module
    #: appends itself (extract_graph facts). None (bare contexts): a local,
    #: context-free firewall from the config's trust policy screens instead.
    screen: ScreenDerived | None = None
    #: The engine's semantic write door for extract_graph facts (lock held), so a
    #: background ``state`` edge supersedes through the ladder like a write-time
    #: one (GP-1). None (bare contexts): the fact is screened and appended raw.
    write_fact: WriteDerivedFact | None = None
    #: One incremental log index shared by the derived stages of a cycle.
    session_index: SessionIndex = field(default_factory=lambda: SessionIndex())
    #: #48: the engine's retention expiry (hard forget through its forget path).
    #: None => the retention_expire stage reports "skipped".
    expire_retention: ExpireRetention | None = None
    #: GP-6 (#17): the batched entity summariser (``summarize_entity`` role).
    #: None => entities over the free length get an extractive summary.
    summarize_entities: SummarizeEntities | None = None
    #: GP-7 (#18): the batched entity resolver (``resolve_entity@batch``) and the
    #: embedder that ranks candidates. None => ``resolve: llm`` acts as ``rules``.
    resolve_entities: ResolveBatch | None = None
    embed: Embed | None = None
    #: I72: ``(namespace, parent ids, site) -> True`` when any parent of a derived
    #: deposit is no longer live (audited by the engine). Called under the
    #: namespace lock right before the write. None => a local check, no audit.
    parents_gone: Callable[[str, Sequence[str], str], Awaitable[bool]] | None = None
    #: W1 (call 3 wiring): the pending edges of ``write.window_extract`` call 1. None =>
    #: ``extract_graph`` extracts every edge itself, as before.
    window_edges: WindowEdges | None = None
    #: W1 (call 2 interface): the session-closed handler. None => the stub.
    close_session: SessionCloseHandler | None = None
    #: O4-02 (``offline.session_close_v2``): the call-2 model call and the record deposit. Either
    #: None => the v2 close reports ``skipped`` and the stub runs as before.
    close_call: CloseCall | None = None
    deposit_session_record: DepositSessionRecord | None = None
    #: O4-06 / O4-09: the derived-write door for community, entity and multi-resolution summaries
    #: (``offline.communities_v2`` / ``offline.decay_v2``). None (bare contexts) => screened and
    #: appended raw.
    write_summary: WriteSummary | None = None
    #: FU-SLEEP (``offline.sleep_scoped``): the one namespace a sleep job sweeps. None (default) =>
    #: every stage sweeps every namespace, as before. See :func:`sweep_namespaces`.
    scope_namespace: str | None = None
    #: FU-ERASE (``privacy.erasure_rederive``): the engine whose erasure queue the
    #: ``erasure_rederive`` stage consumes. None (bare contexts) => the stage self-skips.
    erasure_engine: Any | None = None
    #: FU-SAFE (T5-05; ``consent.v2`` / ``privacy.erasure_v2``): keeps the records an offline stage
    #: may read: not quarantined, erasure-visible, consent-allowed, derivable. None (flags off, bare
    #: contexts) => every record is kept, as before. Stages go through :func:`live_records`.
    live_gate: Callable[[list[MemoryRecord]], Awaitable[list[MemoryRecord]]] | None = None
    #: FU-SAFE (``write.lineage_table``): ``(namespace, record, parent ids, site)`` writes the
    #: lineage edges of a summary a legacy stage appended as a raw WRITE event. None (flag off, bare
    #: contexts) => no edges, as before. See ``offline_door.stamp_summary`` / ``link_summary``.
    link_derived: Callable[[str, MemoryRecord, Sequence[str], str], Awaitable[None]] | None = None
    #: RM-99: the opt-in offline model detector of the sensitivity labelling layer
    #: (``model.extraction.sensitivity``; a ``derive.sensitivity.SensitivityModel``). None
    #: => deterministic labels only (vocabulary defaults and rule/pattern detectors).
    sensitivity_model: Any | None = None
    #: RM-106: the opt-in topic tagger (``model.extraction.topic``; a
    #: ``semantics.domains.AnyTopicTagger``), already gated by the binding. None => no
    #: ``gliner_topic`` domain source on the compile path.
    topic_tagger: Any | None = None


async def gate_records(ctx: PipelineContext, records: list[MemoryRecord]) -> list[MemoryRecord]:
    """FU-SAFE: ``records`` minus what ``ctx.live_gate`` refuses (the identity with no gate)."""
    gate = getattr(ctx, "live_gate", None)  # duck-typed contexts in tests have no gate
    return records if gate is None else await gate(records)


async def live_records(
    ctx: PipelineContext, namespace: str, memory_type: str | None = None
) -> list[MemoryRecord]:
    """FU-SAFE: the one way an offline/sleep stage lists raw records to read their content. With
    ``consent.v2`` or ``privacy.erasure_v2`` on, an erased or hidden record, a consent-refused or
    do-not-derive one and a quarantined one are dropped; with both off it is ``list_records``.
    Stages that must see every row (erasure re-derivation, retention, idempotency checks) call
    ``ctx.storage.list_records`` directly."""
    return await gate_records(ctx, await ctx.storage.list_records(namespace, memory_type))


async def live_record(ctx: PipelineContext, record_id: str) -> MemoryRecord | None:
    """FU-SAFE: ``get_record`` through the same gate (None when absent or refused)."""
    record = await ctx.storage.get_record(record_id)
    gate = getattr(ctx, "live_gate", None)
    if record is None or gate is None:
        return record
    return record if await gate([record]) else None


async def sweep_namespaces(ctx: PipelineContext) -> list[str]:
    """The namespaces a namespace-sweeping stage visits: all of them, or only the job's own when
    ``ctx.scope_namespace`` is set (FU-SLEEP). A scope with no records yields an empty sweep."""
    found = await ctx.storage.list_namespaces()
    if ctx.scope_namespace is None:
        return found
    return [ns for ns in found if ns == ctx.scope_namespace]


async def _parents_gone(
    ctx: PipelineContext, namespace: str, parent_ids: Sequence[str], site: str
) -> bool:
    """I72: True when a parent of the derived record about to be written is gone
    (erased, archived or quarantined). Call with the namespace lock held."""
    if ctx.parents_gone is not None:
        return await ctx.parents_gone(namespace, parent_ids, site)
    for parent_id in parent_ids:
        parent = await ctx.storage.get_record(parent_id)
        if parent is None or parent.status is not RecordStatus.ACTIVATED or parent.quarantined:
            _log.warning("memory.derived_deposit_skipped", namespace=namespace, site=site)
            return True
    return False


Pipeline = Callable[[PipelineContext], Awaitable[dict[str, object]]]


def _policy_options(
    ctx: PipelineContext, memory_type: str, policy: str
) -> dict[str, object] | None:
    mem = ctx.config.memories.get(memory_type)
    if mem is None:
        return None
    raw = mem.policies.get(policy)
    return dict(raw) if isinstance(raw, dict) else None


def _sweep_stats(counter: str, count: int, errors: list[str]) -> dict[str, object]:
    """Partial progress is progress: report what succeeded AND what failed,
    never collapse a half-finished sweep into a bare error (D-18)."""
    if errors:
        return {"status": "partial", counter: count, "errors": errors}
    return {"status": "ok", counter: count}


def _int_option(opts: dict[str, object], key: str, default: int) -> int:
    raw = opts.get(key, default)
    value = int(raw) if isinstance(raw, (int, float, str)) else default
    if value < 1:
        raise ConfigError(f"memories.associative.policies.entity_summaries.{key} must be >= 1")
    return value


def _local_screen(ctx: PipelineContext) -> ScreenDerived:
    """The context-free firewall for bare pipeline contexts (no engine).

    Same trust matrix, instruction check and quarantine rule as the engine's
    door, without the namespace anomaly context; with ``firewall.enabled`` off
    it scores trust only, like the engine's ablation arm. The parent cap is the
    plain minimum (integrity's derivation decay is the engine's concern).
    """
    firewall = Firewall(TrustPolicy.bind(_policy_options(ctx, "semantic", "trust")))
    enabled = ctx.config.firewall.enabled

    async def screen(
        record: MemoryRecord, trust_cap: list[float]
    ) -> tuple[MemoryRecord, list[str]]:
        verdict = (
            firewall.assess(record)
            if enabled
            else FirewallVerdict(trust=firewall.policy.trust_at_write(record.source))
        )
        stamped = verdict.apply(record)
        stamped = stamped.model_copy(update={"trust": min([stamped.trust, *trust_cap])})
        return stamped, (verdict.reasons if verdict.quarantine else [])

    return screen
