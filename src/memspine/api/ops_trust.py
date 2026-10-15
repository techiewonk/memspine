from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from memspine.compose.pack import AssembledContext, estimate_tokens
from memspine.compose.pipeline.render import record_envelope
from memspine.compose.pipeline.sections import _without_entity
from memspine.config import constants
from memspine.core.fact_chain import fact_projection_on
from memspine.derive import feedback as fb
from memspine.derive.pipeline.stages.index import DERIVED_STAGES, stage_marker
from memspine.exceptions import ConflictError, MemspineError
from memspine.ingest.write import _NEIGHBOUR_BATCH, _message_sender, _screen_text
from memspine.observability import tap
from memspine.observability.logging import EVENT_FEEDBACK, get_logger
from memspine.schemas.durable.evidence import TurnRef
from memspine.services.vector.base import VectorHit
from memspine.store.events import EventKind, EventLogMode, MemoryEvent, fingerprint_payload
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.store.scope import grant_allows, validate_namespace
from memspine.trust import horizon as _horizon
from memspine.trust.firewall import FirewallVerdict
from memspine.trust.horizon import Grant
from memspine.trust.pipeline import gates_v2
from memspine.trust.pipeline.quarantine import _taint_archive_delta
from memspine.trust.pipeline.state import _REPLACED_CONTENTS, _as_options_dict
from memspine.trust.privacy import pii_rank
from memspine.workers.list_cards import fact_statement

_log = get_logger(__name__)


async def effective_trust(eng: Any, record_id: str) -> float:
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
    storage = eng._require_started()
    memo: dict[str, float] = {}
    chains: dict[str, tuple[Grant, ...]] = {}  # T5-01: the longest cross-namespace chain below
    grants_cache: dict[str, Mapping[str, frozenset[str] | None]] = {}
    policy = eng._integrity()
    check_grants = policy.enabled and policy.live_reevaluation
    chained = policy.enabled and policy.horizon_v2

    async def walk(rid: str) -> float:
        if rid in memo:
            return memo[rid]
        record = await storage.get_record(rid)
        if (
            record is None
            or record.quarantined
            or record.status in (RecordStatus.ARCHIVED, RecordStatus.DELETED)
        ):
            memo[rid] = 0.0
            return 0.0
        value = record.trust
        if not policy.enabled:
            memo[rid] = value  # pre-MTI: stored trust, held records still fail closed
            return value  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        reader = record.namespace
        if check_grants:
            sender = _message_sender(record)
            if sender is not None:
                # B5: a message crossed sender -> receiver, and its parents
                # were read by the sender.
                if sender not in await eng._grants_cached(record.namespace, grants_cache):
                    memo[rid] = 0.0
                    return 0.0
                reader = sender
        longest: tuple[Grant, ...] = ()
        for parent_id in record.source.parents:
            parent = await storage.get_record(parent_id)
            parent_ns = parent.namespace if parent is not None else record.namespace
            view = policy.view_trust(await walk(parent_id), parent_ns, record.namespace)
            if chained:
                # T5-01: the cross-namespace edges of a lineage are one grant chain.
                below = chains.get(parent_id, ())
                if parent_ns != record.namespace:
                    below = (*below, policy.grant(parent_ns, record.namespace))
                if len(below) > len(longest):
                    longest = below
            if (
                check_grants
                and parent is not None
                and not grant_allows(
                    reader,
                    parent.namespace,
                    parent.memory_type,
                    await eng._grants_cached(reader, grants_cache),
                )
            ):
                view = 0.0  # N6: the grant that carried this parent is gone
            value = min(value, view * policy.derivation_decay)
        if chained:
            chains[rid] = longest
            visible = _horizon.chain_visible(longest)
            if tap.enabled() and longest:
                tap.event(
                    "trust.horizon",
                    id=rid,
                    chain=[f"{g.grantor}:{g.grantee}" for g in longest],
                    attenuation=_horizon.chain_attenuation(longest),
                    visible=visible,
                )
            if not visible:
                value = 0.0  # past the horizon: fail closed
        memo[rid] = value
        return value  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    return await walk(record_id)


def _consent_context(eng: Any, context: AssembledContext) -> AssembledContext:
    """#50: drop records an assembled context may not carry for this purpose.

    The gates already keep them out of every search leg; this catches the
    records assembly adds from elsewhere (persona, full-mode listing)."""
    if not (eng._config().consent.enforce or eng._config().consent.v2):
        return context
    kept: list[MemoryRecord] = []
    boundary = context.boundary_index
    tokens = context.tokens_used
    for index, record in enumerate(context.records):
        if eng._consent_ok(record):
            kept.append(record)
            continue
        tokens -= estimate_tokens(record.content)
        if index < context.boundary_index:
            boundary -= 1
    if len(kept) == len(context.records):
        return context
    return AssembledContext(
        records=kept,
        boundary_index=boundary,
        abstained=context.abstained or not kept,
        tokens_used=max(0, tokens),
        evidence=context.evidence,
    )


async def _withheld_texts(eng: Any) -> list[str]:
    """#50: texts of records above ``consent.remote_llm_max_tier`` (every namespace)."""
    limit = eng._config().consent.remote_llm_max_tier
    if limit is None or eng._storage is None:
        return []
    cap = pii_rank(limit)
    texts: list[str] = []
    for ns in await eng._storage.list_namespaces():
        for record in await eng._storage.list_records(ns):
            if record.status is RecordStatus.DELETED or pii_rank(record.pii_tier) <= cap:
                continue
            own = eng._erasable_texts(record)
            texts.extend(own)
            if record.entity:
                # The shapes timelines, cards and entity summaries render: the
                # text without its leading entity name or fact key.
                texts.extend(_without_entity(t, record.entity) for t in own)
                texts.append(fact_statement(record))
    return list(dict.fromkeys(t for t in texts if t))


_V2_KINDS = ("correction", "rejection", "confirmation", "rating")
_LEGACY_SIGNALS = ("like", "dislike", "note")
#: Names that stand for software acting on its own: never allowed to give feedback (O-25).
_AGENT_ACTORS = frozenset({"assistant", "agent", "system", "tool"})


async def feedback(
    eng: Any,
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
    """#54 + O4-11: record explicit feedback on one record.

    ``write.feedback_v2`` off (default): the ``like`` / ``dislike`` / ``note`` verb exactly as
    before (see :func:`_legacy_feedback`); the v2 keywords are refused.

    On: ``channel`` (default ``api``) must be a deliberate one (``user_message``, ``ui_control``,
    ``operator_edit``, ``api``); silence, dwell, regeneration, classifier and other inferred signals
    are refused. The actor must be the memory owner (``"user"`` or the namespace), or a name in
    ``write.feedback_operators`` (never for a confirmation); an agent never may. The signals are
    the three above plus ``correction`` (needs ``new_value``, optional ``new_text``),
    ``rejection``, ``confirmation`` and ``rating`` (needs ``rating`` 1..5, logged only). A
    correction, rejection or confirmation never edits the record: it becomes a bound assertion
    (the owner's new value, a negation of the recalled one, a restatement) written through the
    write door and the conflict ladder; the first written record is returned. ``source_id`` names
    the turn carrying the feedback (default: this call); ``turn`` the read-turn index (default: the
    number of reads of the namespace so far, which ``Engine.correction_latency`` uses).
    """
    cfg = eng._config().write
    extra = any(
        v is not None for v in (channel, new_value, new_text, rating, quote, turn, source_id)
    )
    if not cfg.feedback_v2:
        if extra:
            raise MemspineError("feedback keywords need write.feedback_v2: true")
        return await _legacy_feedback(
            eng, record_id, signal, note=note, actor=actor, namespace=namespace
        )
    return await _feedback_v2(
        eng,
        record_id,
        signal,
        note=note,
        actor=actor,
        namespace=namespace,
        channel=channel or "api",
        new_value=new_value,
        new_text=new_text,
        rating=rating,
        quote=quote,
        turn=turn,
        source_id=source_id,
    )


def _feedback_refused(signal: str, channel: str, reason: str, message: str) -> MemspineError:
    if tap.enabled():
        tap.event(
            "api.feedback",
            kind=signal,
            channel=channel,
            accepted=False,
            reason=reason,
            assertions=0,
        )
    return MemspineError(f"feedback refused ({reason}): {message}")


async def _events_of(eng: Any, ns: str, kinds: tuple[EventKind, ...]) -> list[MemoryEvent]:
    """The namespace's events of ``kinds``, oldest first (one scan of the log)."""
    if eng._config().event_log.mode is EventLogMode.EPHEMERAL:
        return []
    storage = eng._require_started()
    found: list[MemoryEvent] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after)
        if not batch:
            return found
        found.extend(e for e in batch if e.kind in kinds and e.namespace == ns)
        last = batch[-1].seq
        if last is None:
            return found
        after = last


async def _read_turn(eng: Any, ns: str) -> int | None:
    """The read-turn index: searches the namespace has served so far (None: no log is kept)."""
    if eng._config().event_log.mode is EventLogMode.EPHEMERAL:
        return None
    return len(await _events_of(eng, ns, (EventKind.RETRIEVE,)))


async def _feedback_v2(
    eng: Any,
    record_id: str,
    signal: str,
    *,
    note: str | None,
    actor: str,
    namespace: str,
    channel: str,
    new_value: str | int | float | bool | None,
    new_text: str | None,
    rating: int | None,
    quote: str | None,
    turn: int | None,
    source_id: str | None,
) -> MemoryRecord:
    ns = validate_namespace(namespace)
    if channel in fb.INFERRED_CHANNELS:
        raise _feedback_refused(
            signal, channel, "implicit_channel", f"{channel!r} is an inferred signal"
        )
    if channel not in fb.EXPLICIT_CHANNELS:
        raise _feedback_refused(
            signal, channel, "unknown_channel", f"{channel!r} is not a feedback channel"
        )
    if signal not in (*_LEGACY_SIGNALS, *_V2_KINDS):
        raise MemspineError(
            f"feedback signal must be one of {[*_LEGACY_SIGNALS, *_V2_KINDS]}, got {signal!r}"
        )
    operators = tuple(eng._config().write.feedback_operators)
    if actor in operators:
        who = fb.Actor(principal=actor, kind="operator")
    elif actor in _AGENT_ACTORS:
        who = fb.Actor(principal=actor, kind="agent")
    else:
        who = fb.Actor(principal=ns if actor == "user" else actor, kind="user")
    storage = eng._require_started()
    target = await storage.get_record(record_id)
    if target is None or target.namespace != ns or target.status is RecordStatus.DELETED:
        raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
    kind = signal if signal in _V2_KINDS else "rating"
    eid = (
        "fb_"
        + fingerprint_payload(
            {
                "ns": ns,
                "record": record_id,
                "signal": signal,
                "channel": channel,
                "value": repr(new_value),
                "text": new_text,
                "rating": rating,
                "turn": turn,
                "source": source_id,
                "note": note,
            }
        )[:24]
    )
    try:
        event = fb.FeedbackEvent(
            event_id=eid,
            kind=kind,  # type: ignore[arg-type]
            channel=channel,
            target_ids=(record_id,),
            actor=who,
            owner=ns,
            at=datetime.now(UTC),
            turn=turn,
            source=None
            if kind == "rating"
            else TurnRef(source_id=source_id or f"api:{eid}", source_version=1, seg_index=0),
            quote=quote,
            new_value=new_value,
            new_text=new_text,
            # a like / dislike / note only probes the permission check below
            rating=rating if signal in _V2_KINDS else 3,
        )
        fb.check_feedback(event, operators=operators)
    except fb.FeedbackError as exc:
        raise _feedback_refused(signal, channel, "not_permitted", str(exc)) from exc
    except ValueError as exc:  # pydantic validation of the event shape
        raise _feedback_refused(signal, channel, "invalid", str(exc).splitlines()[0]) from exc

    if signal in _LEGACY_SIGNALS:
        updated = await _legacy_feedback(
            eng, record_id, signal, note=note, actor=actor, namespace=ns
        )
        if tap.enabled():
            tap.event(
                "api.feedback",
                kind=signal,
                channel=channel,
                accepted=True,
                reason="ok",
                assertions=0,
                target=record_id,
            )
        return updated

    written: list[MemoryRecord] = []
    if kind != "rating":
        try:
            drafts = fb.to_assertions(
                event, {record_id: record_envelope(target)}, operators=operators
            )
        except fb.FeedbackError as exc:
            raise _feedback_refused(signal, channel, "not_permitted", str(exc)) from exc
        mem_type = (
            target.memory_type if target.memory_type in ("semantic", "episodic") else "semantic"
        )
        async with eng._write_locks.setdefault(ns, asyncio.Lock()):
            for draft in drafts:
                tags = [f"feedback:{kind}", f"feedback_target:{record_id}"]
                if draft.assessment.modality == "negated":
                    tags.append("pol:neg")
                if draft.src_class == "user_confirmed":
                    tags.append("user_confirmed")
                record = MemoryRecord(
                    namespace=ns,
                    memory_type=mem_type,
                    content=draft.text,
                    entity=target.entity,
                    attribute=target.attribute,
                    group_id=target.group_id,
                    tags=tags,
                    source=SourceInfo(
                        role="user", channel="feedback", principal=draft.provenance.principal
                    ),
                )
                written.append(await eng._write_locked(storage, ns, record, mem_type, actor))
    if turn is None and kind in ("correction", "rejection"):
        turn = await _read_turn(eng, ns)
    payload: dict[str, object] = {
        "record_id": record_id,
        "signal": kind,
        # structure only: the new value, text and quote live in the written record, not the log
        "feedback": event.model_copy(update={"turn": turn}).model_dump(
            mode="json", exclude={"new_value", "new_text", "quote"}
        ),
    }
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        await eng._append_and_project(
            MemoryEvent(kind=EventKind.FEEDBACK, namespace=ns, actor=actor, payload=payload)
        )
    await eng._audit_action("feedback", ns, [record_id], actor=actor, reason=None, signal=kind)
    if tap.enabled():
        tap.event(
            "api.feedback",
            kind=kind,
            channel=channel,
            accepted=True,
            reason="ok",
            assertions=len(written),
            target=record_id,
            written=[w.record_id for w in written],
            quarantined=sum(1 for w in written if w.quarantined),
        )
    if written:
        return written[0]
    refreshed = await storage.get_record(record_id)
    assert refreshed is not None
    return refreshed  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def correction_latency(eng: Any, namespace: str = "default") -> fb.LatencyStats:
    """O4-11 (``write.feedback_v2``): turns between a wrong memory first being surfaced (a search
    that returned it) and the explicit correction or rejection of it, from the log. A turn is one
    search of the namespace. Corrections given with an explicit ``turn`` use the caller's index."""
    if not eng._config().write.feedback_v2:
        raise MemspineError("correction_latency needs write.feedback_v2: true")
    if eng._config().event_log.mode is EventLogMode.EPHEMERAL:
        raise ConflictError("event_log.mode=ephemeral keeps no events to measure from")
    ns = validate_namespace(namespace)
    events = await _events_of(eng, ns, (EventKind.RETRIEVE, EventKind.FEEDBACK))
    exposures: list[fb.ExposureRecord] = []
    given: list[fb.FeedbackEvent] = []
    turn = 0
    for ev in events:
        if ev.kind is EventKind.RETRIEVE:
            turn += 1
            exposures.extend(
                fb.ExposureRecord(record_id=str(r), turn=turn)
                for r in ev.payload.get("record_ids", [])
            )
        elif isinstance(ev.payload.get("feedback"), dict):
            dump = dict(ev.payload["feedback"])
            if dump.get("kind") == "correction":
                dump["new_value"] = "-"  # the log keeps structure only; latency never reads values
            given.append(fb.FeedbackEvent.model_validate(dump))
    stats = fb.correction_latency(given, exposures)
    if tap.enabled():
        tap.event(
            "api.feedback.latency",
            total=stats.total,
            measured=stats.measured,
            unmatched=stats.unmatched,
            unturned=stats.unturned,
            mean=stats.mean,
            p90=stats.p90,
        )
    return stats


async def _legacy_feedback(
    eng: Any,
    record_id: str,
    signal: str,
    *,
    note: str | None = None,
    actor: str = "user",
    namespace: str = "default",
) -> MemoryRecord:
    """#54: record a user's ``like`` / ``dislike`` / ``note`` on one record.

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
    if signal not in ("like", "dislike", "note"):
        raise MemspineError(f"feedback signal must be like, dislike or note, got {signal!r}")
    text = (note or "").strip()
    if signal == "note" and not text:
        raise MemspineError("feedback signal 'note' needs a non-empty note")
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    payload: dict[str, object] = {"record_id": record_id, "signal": signal}
    if text:
        screened = _screen_text(text, eng._config().firewall)[0]
        payload["content"] = screened[: constants.FEEDBACK_NOTE_MAX_CHARS]
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        record = await storage.get_record(record_id)
        if record is None or record.namespace != ns or record.status is RecordStatus.DELETED:
            raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
        await eng._append_and_project(
            MemoryEvent(kind=EventKind.FEEDBACK, namespace=ns, actor=actor, payload=payload)
        )
        updated = await storage.get_record(record_id)
    assert updated is not None
    _log.info(EVENT_FEEDBACK, namespace=ns, record_id=record_id, signal=signal)
    # #49: the signal only; the note stays in the erasable FEEDBACK event.
    await eng._audit_action("feedback", ns, [record_id], actor=actor, reason=None, signal=signal)
    return updated  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def _assess_write(eng: Any, record: MemoryRecord) -> FirewallVerdict:
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
    storage = eng._require_started()
    neighbour_sims: list[float] | None = None
    query_z: float | None = None
    if eng._embedder is not None and eng._vector is not None:
        [vector] = await eng._embedder.embed([record.content])
        signals = eng._firewall.signals
        if signals.query_anomaly:  # N22
            query_z = eng._query_history.assess(
                record.namespace, vector, signals.query_anomaly_kappa
            )
        wanted = constants.ANOMALY_MIN_NEIGHBOURS
        batch = _NEIGHBOUR_BATCH.get()
        if not (
            batch is not None
            and batch.namespace == record.namespace
            and record.content in batch.prefetched
        ):
            batch = None
        embedder = eng._embedder
        vector_store = eng._vector

        async def nearest(top_k: int) -> list[VectorHit]:
            if batch is not None:  # #63: the batched prefetch plus the rows written since
                return await batch.neighbours(record.content, vector, embedder, top_k)
            return await vector_store.query(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
                record.namespace, vector, embedder_id=embedder.embedder_id, top_k=top_k
            )

        hits = await nearest(wanted)
        held = await storage.quarantined_ids([hit.record_id for hit in hits])
        if held:  # widen past the held rows, so `wanted` live ones remain
            hits = await nearest(wanted + await storage.count_quarantined(record.namespace))
            held = await storage.quarantined_ids([hit.record_id for hit in hits])
        neighbour_sims = [hit.score for hit in hits if hit.record_id not in held][:wanted]
    # The 50 most recently recorded live contents, oldest first.
    recent_contents = await storage.recent_contents(record.namespace, 50)
    replaced = _REPLACED_CONTENTS.get()
    if replaced:
        recent_contents = [c for c in recent_contents if c not in replaced]
    if gates_v2.gates_on(eng):  # W2-04: gate 1 through the LIB-38 library
        return gates_v2.assess_insert(
            eng._firewall,
            record,
            neighbour_similarities=neighbour_sims,
            recent_contents=recent_contents,
            query_anomaly=query_z,
        )
    return eng._firewall.assess(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        record,
        neighbour_similarities=neighbour_sims,
        recent_contents=recent_contents,
        query_anomaly=query_z,
    )


async def repair_taint(
    eng: Any,
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
    from memspine.core.policies.consolidation import ConsolidationPolicy

    storage = eng._require_started()
    report = await eng.audit_taint(record_id, namespace, cross_namespace=True)
    untraced = await eng._lineage_window(report, "repair_taint", strict)
    tainted = {record_id, *report.descendants}
    members_of: dict[str, list[str]] = {}
    sessions_of_tainted: set[tuple[str, str]] = set()
    after = 0
    while True:
        events = await storage.read_events(after_seq=after, limit=1000)
        if not events:
            break
        for event in events:
            if event.kind is EventKind.CONSOLIDATE:
                sid = str(event.payload.get("summary_record_id", ""))
                members_of[sid] = [str(m) for m in event.payload.get("member_record_ids", [])]
                session_key = str(event.payload.get("session_key", ""))
                if session_key and tainted & set(members_of[sid]):
                    sessions_of_tainted.add((event.namespace, session_key))
        after = max(event.seq for event in events if event.seq is not None)
    policy = ConsolidationPolicy.bind(
        _as_options_dict(eng._memory_policy(eng._config(), "episodic").get("consolidation"))
    )
    archived: list[str] = []
    rebuilt: list[str] = []
    review: list[str] = []

    async def archive(rid: str, evolve_to: str | None = None) -> None:
        rec = await storage.get_record(rid)
        if rec is None or rec.status in (RecordStatus.ARCHIVED, RecordStatus.DELETED):
            return
        change = _taint_archive_delta(rec)
        if evolve_to is not None:
            change["evolve_to"] = evolve_to
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=rec.namespace,
                actor=actor,
                payload={
                    "record_id": rid,
                    "set": change,
                    "transition": f"{rec.status.value}->archived",
                    "reason": f"taint_repair:{record_id}",
                },
            )
        )
        archived.append(rid)

    for target_id, proof in [(record_id, "seed"), *report.descendants.items()]:
        if proof.startswith("merged@"):
            review.append(target_id)
            continue
        if proof.startswith("superseded_by_taint@"):
            continue  # displaced, not derived: restored below
        members = members_of.get(target_id)
        if not members:
            await archive(target_id)
            continue
        clean = []
        for mid in members:
            if mid in tainted:
                continue
            member = await storage.get_record(mid)
            if member is not None and member.status is RecordStatus.ACTIVATED:
                clean.append(eng._inflate.inflate(member))
        old = await storage.get_record(target_id)
        if not clean or old is None:
            await archive(target_id)
            continue
        summary = MemoryRecord(
            namespace=old.namespace,
            memory_type=old.memory_type,
            content=policy.fallback_summary(clean),
            valid_from=old.valid_from,
            valid_to=old.valid_to,
            source=SourceInfo(
                role=constants.DERIVED_ROLE,  # N2: a derived summary is never privileged
                channel="consolidation",
                message_id=old.source.message_id,
                parents=[m.record_id for m in clean],
            ),
            trust=min(m.trust for m in clean),
            instruction_flag=any(m.instruction_flag for m in clean),
        )
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=old.namespace,
                actor=actor,
                payload={
                    "record": summary.model_dump(mode="json"),
                    "consolidation": {
                        "session_key": old.source.message_id,
                        "member_record_ids": [m.record_id for m in clean],
                        "repaired_from": target_id,
                    },
                },
            )
        )
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.CONSOLIDATE,
                namespace=old.namespace,
                actor=actor,
                payload={
                    "session_key": old.source.message_id,
                    "member_record_ids": [m.record_id for m in clean],
                    "summary_record_id": summary.record_id,
                    "superseded_summary_ids": [target_id],
                    "summarizer": "extractive-repair",
                },
            )
        )
        await archive(target_id, evolve_to=summary.record_id)
        rebuilt.append(summary.record_id)
    restored = await eng._undo_displacement(record_id, report, archived, actor)
    # R2-9: a session the seed was a member of has had its derived records
    # (mined facts, cues, insights) archived above. Clearing the stages'
    # done markers lets the next sleep cycle derive them again, from the
    # clean members only.
    for session_ns, session_key in sorted(sessions_of_tainted):
        stages: tuple[str, ...] = DERIVED_STAGES
        if eng._consolidation_option("predict_calibrate", False):
            stages = (*stages, "predict_calibrate")  # #62: only when it runs
        if fact_projection_on(eng._memory_policy(eng._config(), "semantic")):
            stages = (*stages, "project_facts")  # E01: only when it runs
        for stage in stages:
            await eng._append_and_project(
                stage_marker(session_ns, stage, session_key, cleared=True)
            )
    _log.warning(
        "memory.taint_repair",
        namespace=namespace,
        seed=record_id,
        archived=len(archived),
        rebuilt=len(rebuilt),
        restored=len(restored),
        review=len(review),
    )
    result = {
        "archived": archived,
        "rebuilt": rebuilt,
        "restored": restored,
        "review": review,
    }
    if untraced:
        result["untraced"] = [record_id]
    return result
