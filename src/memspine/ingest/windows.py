"""Session windows: reopen, build, flush, close (F-09)."""

from __future__ import annotations

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
    Sequence,
)
from datetime import datetime
from typing import Any, cast

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.window import WindowSettings
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.derive.failure_ledger import FailureLedger
from memspine.derive.grounding_stage import build_grounding_stage
from memspine.derive.items import ItemValidator, RepairAsk
from memspine.derive.known_sources import prepass_known, resolver_known
from memspine.derive.online_extractor import OnlineExtractor
from memspine.derive.pipeline.stages.close import session_close
from memspine.derive.pipeline.stages.context import SessionClosed
from memspine.derive.prepass import Prepass, prepass_stage
from memspine.derive.prompt_context import KnownEntity, KnownSource, PromptContextBuilder
from memspine.derive.resolve_stage import ResolveSettings, build_resolve_stage
from memspine.derive.segmenter import SegmentationSettings
from memspine.derive.write import write_derived
from memspine.exceptions import (
    ConfigError,
)
from memspine.memories.episodic.lifecycle import session_of
from memspine.models.encoders.query.perspective import (
    inherit_perspective_tags,
)
from memspine.observability.logging import get_logger
from memspine.prompts.structured import STRUCTURED_PROMPT_ID, STRUCTURED_VERSION
from memspine.read.pending_barrier import BarrierResult, PendingBarrier
from memspine.schemas.durable.common import OwnerScope
from memspine.services.storage.records_port import RecordsStore
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import (
    MemoryRecord,
    RecordStatus,
    SourceInfo,
)
from memspine.store.scope import validate_namespace
from memspine.workers.windows import (
    SECTION_MODES,
    EnvelopeWrite,
    PromptWindowExtractor,
    WindowPipeline,
)

_log = get_logger(__name__)

#: F-04: builds the durable outbox runner for ``workers.outbox``. ``ingest`` sits below ``runtime``,
#: so it cannot import the builder; ``memspine.runtime.container`` registers it when it is imported.
OutboxFactory = Callable[[Any, MemspineConfig], Any]
_outbox_factory: OutboxFactory | None = None


def register_outbox_factory(factory: OutboxFactory | None) -> None:
    global _outbox_factory
    _outbox_factory = factory


async def _reopen_session(eng: Any, storage: RecordsStore, ns: str, session: str | None) -> None:
    """#53: a write to a PASSIVE session reopens it (one SESSION ``active`` event
    listing its members), so the whole conversation is back in default reads."""
    if session is None:
        return
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        passive = eng._passive_sessions.get(ns)
        records: list[MemoryRecord] | None = None
        if passive is None:  # first episodic write here since start or a sleep
            records = await storage.list_records(ns, "episodic")
            passive = {
                sid for r in records if r.scoring.passive and (sid := session_of(r)) is not None
            }
            eng._passive_sessions[ns] = passive
        if session not in passive:
            return
        if records is None:
            records = await storage.list_records(ns, "episodic")
        members = sorted(
            r.record_id
            for r in records
            if session_of(r) == session and r.status is not RecordStatus.DELETED
        )
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.SESSION,
                namespace=ns,
                actor="system",
                payload={
                    "session_id": session,
                    "state": "active",
                    "record_ids": members,
                    "reason": "new_write",
                },
            )
        )
        passive.discard(session)
    _log.info("memory.session_reopened", namespace=ns, session_id=session)


async def _load_source_record(
    storage: RecordsStore, inflate: Any, record_id: str
) -> MemoryRecord | None:
    """Load a grounding source with reversible cold storage encoding removed."""
    record = await storage.get_record(record_id)
    return inflate.inflate(record) if record is not None else None


def end_session(eng: Any, namespace: str = "default", session_id: str | None = None) -> None:
    """B0: forget a session's read ledger (``session_id=None``: the namespace's
    anonymous ledger, which no read or write ever clears on its own)."""
    key = eng._ledger_key(validate_namespace(namespace), session_id)
    eng._read_ledger.pop(key, None)
    eng._ledger_written.discard(key)


def _build_windows(eng: Any, config: MemspineConfig) -> WindowPipeline | None:
    """``write.window_extract``: the window manager, or None when off. Hard-fails at start
    (D-10) when the layers it needs are missing, never silently at the first write."""
    mode = config.write.window_extract
    if mode == "off":
        return None
    no_model = eng._llm is None or eng._prompts is None or "window_extract" not in eng._llm.roles
    rule_only = bool(no_model and config.write.online_extractor)
    if no_model and not rule_only:
        raise ConfigError(
            f"write.window_extract={mode!r} needs an LLM role: set model.llm.window_extract"
        )
    if rule_only and mode != "facts":
        raise ConfigError(
            "the non-generative online extractor supports write.window_extract='facts'; "
            "graph mentions require the GLiNER annotation stage and summaries are separate"
        )
    if eng._semantic is None or eng._episodic is None:
        raise ConfigError(
            f"write.window_extract={mode!r} needs memories.episodic and memories.semantic "
            "enabled (turns are episodic, facts land in the semantic layer)"
        )
    sections = SECTION_MODES[mode]
    role = config.model.llm.get("window_extract")
    storage = eng._storage
    assert storage is not None  # built in start() before the memory types
    asynchronous = config.write.window_async
    # runtime.container.client_is_memory, inlined: ingest sits below runtime
    if asynchronous and eng._sql is not None and eng._sql.is_memory:
        # Like the sleep scheduler: one shared connection, no WAL, so a background call
        # would collide with foreground writes ("table is locked"). Batch mode instead.
        _log.warning(
            "window.async_disabled_on_memory_db",
            detail="write.window_async ignored for in-memory SQL storage; "
            "call flush_windows() (sleep() and close_session() do)",
        )
        asynchronous = False
    outbox = None
    if config.workers.outbox:
        if _outbox_factory is None:
            raise ConfigError("workers.outbox=true but no outbox factory is registered")
        outbox = _outbox_factory(eng, config)
    envelope = _envelope_write(eng, config) if "facts" in sections and not rule_only else None
    pre = _prepass(eng, config)
    resolve = getattr(eng, "_entity_resolve_stage", None)
    context_builder = PromptContextBuilder(
        _known_from_store(eng),
        cap=config.write.prompt_known_cap,
        context_turns=config.write.prompt_context_turns,
        prepass=prepass_known(pre) if pre is not None else None,
        resolver=resolver_known(resolve) if config.write.resolver and resolve else None,
    )

    async def source_record(record_id: str) -> MemoryRecord | None:
        """Load retained source text in its canonical, usable representation.

        Cold-tier compression is lossless storage encoding. Background work may
        resume after the source became cold, so grounding must never interpret
        the materialized row's empty compressed placeholder as source text.
        """
        return await _load_source_record(storage, eng._inflate, record_id)

    class _RuleOnlyExtractor:
        identity = "personal-rules:1"

        async def __call__(self, sections: Any, variables: Any) -> dict[str, Any]:
            raise AssertionError("rule-only extraction never calls a model adapter")

    extractor = (
        _RuleOnlyExtractor()
        if rule_only
        else PromptWindowExtractor(
            eng._llm.for_role("window_extract"),
            eng._prompts.for_role("window_extract"),
            str(role.settings.get("model", "")) if role is not None else "",
            partial=eng._prompts.partial,
            use_schema=config.write.prompt_schema == "auto",
        )
    )

    # BI-06: a configured local extractor may opt into grounded fact candidates.
    # The ordinary semantic LLM extractor intentionally does not implement this
    # method, so rule-only mode can never fall back to generation.
    configured_fact_extractor = None
    semantic_store = getattr(eng, "_semantic", None)
    configured_semantic = getattr(semantic_store, "_extractor", None)
    extract_grounded = getattr(configured_semantic, "extract_grounded_facts", None)
    if callable(extract_grounded) and getattr(configured_semantic, "generative", None) is False:
        grounded_method = cast(
            Callable[[MemoryRecord, OwnerScope], Awaitable[Any]], extract_grounded
        )

        class _ConfiguredNonGenerativeExtractor:
            identity = str(
                getattr(configured_semantic, "model_id", type(configured_semantic).__name__)
            )

            async def extract(self, record: MemoryRecord, scope: OwnerScope) -> Any:
                return await grounded_method(record, scope)

        configured_fact_extractor = _ConfiguredNonGenerativeExtractor()

    return WindowPipeline(
        settings=WindowSettings(
            max_turns=config.write.window_max_turns,
            max_tokens=config.write.window_max_tokens,
            topic_k=config.write.window_topic_k,
            idle_s=config.write.window_idle_s,
        ),
        sections=sections,
        extractor=extractor,
        get_record=source_record,
        deposit_fact=eng._deposit_mined_fact,
        deposit_episode=eng._deposit_window_episode if "episode" in sections else None,
        embed=eng._embedder.embed if eng._embedder is not None else None,
        lock=eng._namespace_lock,
        cache=eng._cache,
        use_cache=config.write.window_cache,
        asynchronous=asynchronous,
        on_session_closed=eng._window_session_closed,
        outbox=outbox,
        segmentation=_segmentation(config, model_batching=not rule_only),
        role_policy=config.write.role_policy,
        envelope=envelope,
        context_builder=context_builder,
        prepass=_prepass(eng, config),
        mark_disputed=_mark_disputed(eng) if config.write.integration else None,
        rule_only=rule_only,
        non_generative_extractor=configured_fact_extractor,
        remove_derived=_remove_window_derived(eng),
    )


def _remove_window_derived(eng: Any) -> Any:
    """RM-141: the compensation door of the window stage. Hard-forgets the derived records one
    window deposited (facts and summaries carry ``window:<id>``), so a projection the outbox
    fence refused after the commit leaves nothing behind. Returns how many were removed."""

    async def remove(namespace: str, window_id: str) -> int:
        storage = eng._require_started()
        tag = f"window:{window_id}"
        ids = [
            r.record_id
            for r in await storage.list_records(namespace, "semantic")
            if tag in r.tags and r.status is not RecordStatus.DELETED
        ]
        for record_id in ids:
            await eng.forget(
                record_id, namespace=namespace, hard=True, actor="system", reason="window_fenced"
            )
        return len(ids)

    return remove


def _mark_disputed(eng: Any) -> Any:
    """FU-GAPS: the tag-update door for the older side of a contest (replays like any event)."""

    async def mark(namespace: str, record_id: str) -> bool:
        record = await eng._storage.get_record(record_id)
        if (
            record is None
            or record.namespace != namespace
            or record.status is not RecordStatus.ACTIVATED
            or "disputed" in record.tags
        ):
            return False
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": record_id,
                    "set": {"tags_add": ["disputed"]},
                    "transition": "fact->integration_contest",
                    "reason": "integration_contest",
                },
            )
        )
        return True

    return mark


#: W2-06b: the entity list is rebuilt from the graph every this many windows (it is a scan).
_KNOWN_REFRESH_EVERY = 4


def _known_from_store(eng: Any) -> KnownSource:
    """KNOWN from the RM-130 entity index (W2-06b; DS-11): canonical names and evidence-cited
    aliases with their provenance and visibility. The P-23 pre-pass (W2-13) and the online
    resolver (W2-09) plug in through the builder's ``prepass`` / ``resolver`` hooks and are not
    read here."""
    from memspine.memories.associative.known_entities import index_known_entities

    cache: dict[str, tuple[int, list[KnownEntity]]] = {}

    async def source(ns: str, _text: str) -> list[KnownEntity]:
        age, known = cache.get(ns, (_KNOWN_REFRESH_EVERY, []))
        if age < _KNOWN_REFRESH_EVERY:
            cache[ns] = (age + 1, known)
            return known
        assoc = getattr(eng, "_associative", None)
        if assoc is None:
            return []
        known = await index_known_entities(eng._storage, ns)
        cache[ns] = (1, known)
        return known

    return source


def _envelope_write(eng: Any, config: MemspineConfig) -> EnvelopeWrite:
    """W2-06a (the base extraction envelope): compiler, validator, one-shot repair, dead-letter list.
    The repair call uses the router-bound LLM of the extraction, so the cost ledger counts it."""
    llm = eng._llm.for_role("window_extract")
    # RM-111 WP5: the dead letters live in the storage database (encrypted with it on SQLite,
    # shared on Postgres); a ``:memory:`` store keeps them in memory.
    sql, resources = getattr(eng, "_sql", None), getattr(eng, "_provider_resources", None)
    if (
        sql is not None
        and resources is not None
        and not sql.is_memory
        and "sync_sql" in sql.capabilities
    ):
        ledger = FailureLedger(sql.sync_lease(resources, "dead_letters").engine)
    else:
        ledger = FailureLedger(":memory:")
    compiler = EnvelopeCompiler(prompt_hash=f"{STRUCTURED_PROMPT_ID}@{STRUCTURED_VERSION}")
    learned = None
    if config.write.learned_persist:  # FU-GAPS: the kind set and the aliases survive a restart
        from memspine.derive.learned_state import LearnedState

        learned = LearnedState(
            lambda: eng._storage, kinds=compiler.kinds, prepass=_prepass(eng, config)
        )
    return EnvelopeWrite(
        compiler=compiler,
        validator=ItemValidator(compiler),
        repair=RepairAsk(llm, max_tokens=4096),
        ledger=ledger,
        stages=_envelope_stages(eng, config),
        report_stages=config.write.online_extractor,
        learned=learned,
    )


def _envelope_stages(eng: Any, config: MemspineConfig) -> tuple[Any, ...]:
    """W2-06c: the post-compile stage list. Each W2 package appends ONE builder line here, gated
    on its own flag (see ``derive/envelope_stages.py`` for the order)."""
    stages: list[Any] = []
    if config.write.temporal:  # W2-14, order 100
        from memspine.derive.temporal_stage import build_stage

        stages.append(build_stage(trust_valid_from=not config.write.stamping))
    if config.write.integration:  # W2-10 (order 400)
        stages.append(_integration_stage(eng, config))
    if config.write.grounding:
        stages.append(build_grounding_stage(eng, config))
    if config.write.coverage:  # W2-15, order 250
        stages.append(_coverage_stage(eng, config, tuple(stages)))
    pre = _prepass(eng, config)
    if pre is not None:  # W2-13: cross-check only, order 500
        stages.append(prepass_stage(pre))
    if config.write.resolver:  # W2-09: online entity resolution (order 300)
        stages.append(_resolve_stage(eng, config))
    return tuple(stages)


def _coverage_stage(eng: Any, config: MemspineConfig, earlier: tuple[Any, ...]) -> Any:
    """W2-15: coverage check + one targeted re-extraction on the router-bound window LLM,
    inside a W2-12 cost scope (stage ``coverage_reextract``) so the ledger counts the call."""
    from contextlib import nullcontext

    from memspine.derive.coverage_stage import COVERAGE_COST_STAGE, make_coverage_stage
    from memspine.economy import ledger as costs

    w = config.write

    def scope(namespace: str) -> Any:
        if costs.installed() is None:
            return nullcontext()
        return costs.cost_scope(namespace=namespace, layer="facts", stage=COVERAGE_COST_STAGE)

    stage = make_coverage_stage(
        eng._llm.for_role("window_extract"),
        earlier=earlier,
        threshold=w.coverage_threshold,
        max_targets=w.coverage_max_targets,
        context_lines=w.coverage_context_lines,
        known=_known_from_store(eng),
        known_cap=w.prompt_known_cap,
        scope=scope,
        partial=getattr(eng._prompts, "partial", None),
    )
    eng._coverage_stage = stage  # per-window numbers for screens (``stage.stats``)
    return stage.stage()


def _integration_stage(eng: Any, config: MemspineConfig) -> Any:
    """W2-10: the integration ladder stage over the namespace's live semantic facts."""
    from memspine.derive.integration_stage import DefaultVerdict, integration_stage

    async def list_live(namespace: str) -> Any:
        return await eng._storage.list_records(namespace, "semantic")

    return integration_stage(
        list_live,
        cardinality=config.write.integration_cardinality,
        default=DefaultVerdict(defer_without_hints=not config.write.integration_decide_unhinted),
    )


def _prepass(eng: Any, config: MemspineConfig) -> Prepass | None:
    """W2-13 (``write.prepass``): ONE pre-pass per engine, shared by the skip and the stage."""
    if config.write.prepass == "off":
        return None
    shared: Prepass | None = getattr(eng, "_window_prepass", None)
    if shared is None:
        from memspine.config.schema.model import gliner_settings

        settings = gliner_settings(config, "prepass") if config.write.prepass == "gliner" else None
        shared = Prepass(
            config.write.prepass,
            gliner_pool=getattr(eng, "_gliner_pool", None),
            gliner_settings=settings,
        )
        eng._window_prepass = shared
    return shared


async def forget_learned_aliases(eng: Any, namespace: str) -> None:
    """RM-18: forget every pre-pass alias ``namespace`` learned, in memory and, with a
    ``learned.alias.reset`` marker, for the replay at the next start. A no-op without a pre-pass."""
    pre: Prepass | None = getattr(eng, "_window_prepass", None)
    if pre is None:
        return
    from memspine.derive.learned_state import alias_reset_event

    pre.forget(namespace)
    await eng._require_started().append_event(alias_reset_event(namespace))


def _resolve_stage(eng: Any, config: MemspineConfig) -> Any:
    """W2-09 (``write.resolver``): the resolve stage; event log and embedder bound lazily."""
    w = config.write
    stage = build_resolve_stage(
        ResolveSettings(
            high=w.resolve_high,
            grey=w.resolve_grey,
            max_cache=w.resolve_cache,
        ),
        embed=eng._embedder.embed if eng._embedder is not None else None,
        storage=lambda: eng._storage,
    )
    eng.__dict__["_entity_resolve_stage"] = stage
    return stage.as_stage()


def _segmentation(config: MemspineConfig, *, model_batching: bool = True) -> SegmentationSettings:
    """W2-05: The base segmenter and optional batching settings."""
    w = config.write
    return SegmentationSettings(
        gap_s=w.segment_gap_s,
        calendar_day_cut=w.segment_calendar_day_cut,
        grow_tokens=w.segment_grow_tokens,
        max_turns_cap=w.segment_max_turns,
        batch_max_tokens=w.batch_max_tokens,
        batch_max_windows=w.batch_max_windows if model_batching else 1,
        batch_max_age_s=w.batch_max_age_s,
    )


async def _window_session_closed(eng: Any, info: SessionClosed) -> dict[str, object]:
    """The session-closed hook of the window manager: the call-2 stage (a stub today)."""
    return await session_close(eng._pipeline_ctx(), info)


async def flush_windows(
    eng: Any, namespace: str | None = None, *, final: bool = True
) -> dict[str, Any]:
    """W1: run every closed window's call now (all namespaces, or one) and return the
    write-quality counters. ``final=True`` also closes the open windows and fires the
    session-closed hook for the sessions still open: the end of a stream or a batch (the
    eval harness calls it before reads and before the sleep cycle). A no-op returning
    ``{}`` when ``write.window_extract`` is off."""
    eng._require_started()
    if eng._windows is None:
        return {}
    ns = validate_namespace(namespace) if namespace is not None else None
    return await online_extractor(eng).flush(ns, final=final)


def online_extractor(eng: Any) -> OnlineExtractor:
    """W2-06c: the named entry for online extraction over the engine's window pipeline."""
    return OnlineExtractor(eng._windows)


def unresolved_annotations(eng: Any, namespace: str = "default") -> list[dict[str, Any]]:
    """RM-42: the namespace's current unresolved fact candidates, as source-bound unasserted
    annotations (reason, span, producer revision). Never facts; empty without the pipeline."""
    eng._require_started()
    pipeline = eng._windows
    if pipeline is None:
        return []
    return pipeline.unresolved_annotations(validate_namespace(namespace))  # type: ignore[no-any-return]


async def reresolve_unresolved(eng: Any, namespace: str = "default") -> int:
    """RM-42: re-run the rules over the sources with unresolved annotations (call it after a
    derivation policy or semantic registry activation). Returns the number of sources queued."""
    eng._require_started()
    pipeline = eng._windows
    if pipeline is None:
        return 0
    return await pipeline.reresolve_unresolved(validate_namespace(namespace))  # type: ignore[no-any-return]


async def wait_for_pending(
    eng: Any, namespace: str = "default", timeout_s: float = 0.0
) -> BarrierResult:
    """W2-06c: ``read.PendingBarrier`` over the engine outbox (``off`` when ``timeout_s`` <= 0)."""
    return await PendingBarrier(lambda ns, caps, t: readiness(eng, ns, caps, t)).wait(
        validate_namespace(namespace), timeout_s
    )


async def readiness(
    eng: Any,
    namespace: str = "default",
    capabilities: Sequence[str] = ("window",),
    timeout: float = 0.0,  # noqa: ASYNC109 - a barrier budget, not an asyncio timeout
) -> Any:
    """F-04 read barrier (session 7): wait until ``capabilities`` of ``namespace`` cover every item
    enqueued so far, or ``timeout`` seconds pass (``partial``, naming what is missing). ``None``
    when ``workers.outbox`` is off (nothing is queued durably, so there is nothing to wait for).
    The api layer binds this to ``Engine.readiness`` / receipt readiness (F-13a)."""
    eng._require_started()
    pipeline = eng._windows
    outbox = getattr(pipeline, "_outbox", None)
    if outbox is None:
        return None
    return await pipeline.readiness(validate_namespace(namespace), capabilities, timeout)


async def close_session(
    eng: Any, namespace: str = "default", session_id: str | None = None
) -> dict[str, Any]:
    """W1: the real session-closed hook. Cuts the session's open window, runs the
    namespace's queued window calls in order, then fires the session-close stage (call 2:
    a stub today, it only records a ``session_closed`` MARKER). ``session_id=None``: the
    namespace's current session. Works without ``write.window_extract`` too (then only the
    stage runs, and a ``session_id`` is required)."""
    eng._require_started()
    ns = validate_namespace(namespace)
    if eng._windows is not None:
        return await eng._windows.close_session(ns, session_id)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
    if session_id is None:
        return {"status": "skipped", "reason": "no session_id and write.window_extract is off"}
    hook = await session_close(eng._pipeline_ctx(), SessionClosed(ns, session_id))
    return {"status": "ok", "session_id": session_id, "hook": hook}


async def _deposit_window_episode(
    eng: Any,
    namespace: str,
    text: str,
    parents: list[str],
    valid_from: datetime,
    window_id: str,
    *,
    require_live: Sequence[str] = (),
) -> MemoryRecord | None:
    """W1: one episode fragment through the semantic door: derived (non-privileged
    ``assistant`` role, channel ``window_episode``), parents = the cited turns, trust capped
    at the least trusted parent, screened by the firewall, never keyed (no entity or
    attribute, so the ladder ADDs it). Skipped under the lock when a parent or another turn
    of the window is no longer live (I72)."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    sources = [r for r in [await storage.get_record(p) for p in parents] if r is not None]
    tags = ["episode_fragment", "kind:episode", f"window:{window_id}"]
    if sources and all("assistant_claim" in r.tags for r in sources):
        tags.append("assistant_claim")
    if sources and eng._perspective_options().on:
        tags.extend(t for t in inherit_perspective_tags(sources) if t not in tags)
    record = MemoryRecord(
        namespace=ns,
        memory_type="semantic",
        content=text,
        source=SourceInfo(
            role=constants.DERIVED_ROLE,
            channel=constants.WINDOW_EPISODE_CHANNEL,
            parents=list(parents),
        ),
        valid_from=valid_from,
        tags=tags,
    )
    cap = [min(r.trust for r in sources)] if sources else None
    integrity_cap = await eng._parent_trust_cap(ns, parents)
    if integrity_cap:
        cap = [*(cap or []), *integrity_cap]
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if await eng._derived_parents_gone(ns, [*parents, *require_live], "window_extract"):
            return None
        return await write_derived(
            eng, storage, ns, record, "semantic", "system", trust_cap=cap, site="window_episode"
        )
