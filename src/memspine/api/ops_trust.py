from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from memspine.compose.pack import AssembledContext, estimate_tokens
from memspine.compose.pipeline.sections import _without_entity
from memspine.config import constants
from memspine.core.fact_chain import fact_projection_on
from memspine.derive.pipeline.stages.index import DERIVED_STAGES, stage_marker
from memspine.exceptions import ConflictError, MemspineError
from memspine.ingest.write import _NEIGHBOUR_BATCH, _message_sender, _screen_text
from memspine.observability.logging import EVENT_FEEDBACK, get_logger
from memspine.services.vector.base import VectorHit
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.store.scope import grant_allows, validate_namespace
from memspine.trust.firewall import FirewallVerdict
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
    grants_cache: dict[str, Mapping[str, frozenset[str] | None]] = {}
    policy = eng._integrity()
    check_grants = policy.enabled and policy.live_reevaluation

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
            return value
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
        for parent_id in record.source.parents:
            parent = await storage.get_record(parent_id)
            parent_ns = parent.namespace if parent is not None else record.namespace
            view = policy.view_trust(await walk(parent_id), parent_ns, record.namespace)
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
        memo[rid] = value
        return value

    return await walk(record_id)


def _consent_context(eng: Any, context: AssembledContext) -> AssembledContext:
    """#50: drop records an assembled context may not carry for this purpose.

    The gates already keep them out of every search leg; this catches the
    records assembly adds from elsewhere (persona, full-mode listing)."""
    if not eng._config().consent.enforce:
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


async def feedback(
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
    return updated


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
            return await vector_store.query(
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
    return eng._firewall.assess(
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
