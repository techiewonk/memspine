"""Trust: erasure moved out of the engine (F-12)."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Sequence
from typing import Any

from memspine.config import constants
from memspine.core.forget_request import forget_target
from memspine.core.policies.retention import RetentionPolicy
from memspine.exceptions import ConflictError, MemspineError, StorageError
from memspine.memories.associative.entities import ENTITY_PREFIX, canonical_entity, entity_node_id
from memspine.models.encoders.query.shape import content_words
from memspine.models.encoders.query.temporal_query import forget_mentions
from memspine.observability.logging import EVENT_FORGET, get_logger
from memspine.scope import system_scope
from memspine.services.storage.sql_base import SqlStorage
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.scope import validate_namespace
from memspine.trust.erasure import retained_fields
from memspine.trust.pipeline import erasure_v2
from memspine.trust.pipeline.state import _as_options_dict

_log = get_logger(__name__)


async def forget(
    eng: Any,
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
    if hard and erasure_v2.enabled(eng):  # T5-04: plan over the lineage, hide, purge
        return await erasure_v2.forget(
            eng, record_id, namespace, cascade, actor=actor, reason=reason
        )
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    # Same per-namespace lock as write(): a hard delete's read-hold-check-
    # redact sequence must not interleave with a concurrent write or its
    # corroboration read-modify-write on the same record.
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        ids = [record_id]
        if hard if cascade is None else cascade:
            ids.extend(await eng._descendants(storage, ns, [record_id]))
        elif cascade is None:  # FU-HARDERASE: a soft forget still hides the turn's window facts
            ids.extend(await _sole_source_window_facts(storage, ns, record_id))
        await eng._forget_many(storage, ns, ids, hard)
    await eng._audit_action("forget", ns, ids, actor=actor, reason=reason, hard=hard)


async def _sole_source_window_facts(storage: SqlStorage, ns: str, record_id: str) -> list[str]:
    """FU-HARDERASE: live window facts (mined verbatim from turns; tag ``window_fact``) whose only
    parent is ``record_id``. A soft forget has no cascade by default (generic derived records,
    summaries and cues stay), but a window fact repeats the forgotten turn's content, so it must
    stop being served with it. A fact that also cites another turn is kept."""
    return [
        c.record_id
        for c in await storage.list_children(ns, [record_id])
        if c.status is not RecordStatus.DELETED
        and "window_fact" in c.tags
        and set(map(str, c.source.parents)) <= {record_id}
    ]


async def erase_subject(
    eng: Any,
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
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    wanted = subject.casefold()
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        seeds = [
            r.record_id
            for r in await storage.list_records(ns)
            if (r.entity is not None and r.entity.casefold() == wanted)
            or r.source.principal == subject
        ]
        ids = [*seeds, *await eng._descendants(storage, ns, seeds)]
        await eng._forget_many(storage, ns, ids, hard=True, subject_names=[subject])
    await eng._audit_action("erase_subject", ns, ids, actor=actor, reason=reason)
    return ids


async def erase_namespace(
    eng: Any, namespace: str, *, actor: str = "user", reason: str | None = None
) -> list[str]:
    """#43 per-namespace erasure: hard-forget every record of ``namespace``
    (any type and status) in one log pass. A legal hold on the namespace
    refuses the whole call before anything is touched. ``actor`` and
    ``reason`` go into a ``memory.audit`` event under ``audit.actions``.
    Returns the ids."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        ids = [r.record_id for r in await storage.list_records(ns)]
        await eng._forget_many(storage, ns, ids, hard=True)
        documents = getattr(eng, "_documents", None)
        if documents is not None:  # ADR-081: every document of the namespace goes too
            await documents.erase_namespace(ns)
        drop = getattr(eng._vector, "drop_namespace", None)
        if drop is not None:  # I4: per-namespace tables: the user's table goes too
            await drop(ns)
        await eng._forget_learned_aliases(ns)  # RM-18: the namespace's learned aliases go too
    await eng._audit_action("erase_namespace", ns, ids, actor=actor, reason=reason)
    return ids


async def _descendants(eng: Any, storage: SqlStorage, ns: str, seeds: Sequence[str]) -> list[str]:
    """Every record of ``ns`` derived from ``seeds``, transitively, in discovery
    order (seeds excluded), that is still in the read model.

    Derivation is read from ``source.parents`` and, for summaries written
    before they carried parents, from the ``member_record_ids`` their WRITE
    (``consolidation``/``reflection``) and CONSOLIDATE events name. A summary
    of a forgotten member is forgotten with it (cascade), never re-derived."""
    members_of = await eng._log_derivations(storage, ns)
    seen = set(seeds)
    found: list[str] = []
    frontier = list(seeds)
    while frontier:
        children = [c.record_id for c in await storage.list_children(ns, frontier)]
        children += [child for parent in frontier for child in members_of.get(parent, ())]
        frontier = [c for c in dict.fromkeys(children) if c not in seen]
        seen.update(frontier)
        found.extend(frontier)
    present: list[str] = []
    for record_id in found:
        record = await storage.get_record(record_id)
        if record is not None and record.namespace == ns:
            present.append(record_id)
    return present


async def _log_derivations(storage: SqlStorage, ns: str) -> dict[str, list[str]]:
    """parent id -> ids of the records of ``ns`` whose log events name it: in
    a WRITE snapshot's ``source.parents`` or a ``member_record_ids`` list
    (one pass over the log)."""
    children: dict[str, list[str]] = {}

    def link(members: object, child: object) -> None:
        if isinstance(members, list) and isinstance(child, str) and child:
            for member in members:
                children.setdefault(str(member), []).append(child)

    after = 0
    while True:
        batch = await storage.read_events(after_seq=after)
        if not batch:
            return children
        for event in batch:
            if event.namespace != ns:
                continue
            payload = event.payload or {}
            if event.kind is EventKind.WRITE:
                snapshot = payload.get("record")
                child = snapshot.get("record_id") if isinstance(snapshot, dict) else None
                source = snapshot.get("source") if isinstance(snapshot, dict) else None
                if isinstance(source, dict):
                    # The log keeps lineage after the row is gone, so the walk
                    # crosses an already-erased intermediate record.
                    link(source.get("parents"), child)
                for key in ("consolidation", "reflection"):
                    derivation = payload.get(key)
                    if isinstance(derivation, dict):
                        link(derivation.get("member_record_ids"), child)
            elif event.kind is EventKind.CONSOLIDATE:
                link(payload.get("member_record_ids"), payload.get("summary_record_id"))
        assert batch[-1].seq is not None
        after = batch[-1].seq


async def _forget_many(
    eng: Any,
    storage: SqlStorage,
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
    ids = list(dict.fromkeys(record_ids))
    if not ids:
        return
    records: list[MemoryRecord | None] = []
    for rid in ids:
        records.append(await eng._forget_target(storage, ns, rid, hard))
    texts = [t for r in records if r is not None for t in eng._erasable_texts(r)]
    for rid in ids:
        await eng._forget_locked(storage, ns, rid, hard, redact=False)
    await eng._scrub_profile_cards(ns, ids, hard)  # FU-CLOSE: card entries leave with their turn
    if not hard:
        return
    names = [*subject_names, *(r.entity for r in records if r is not None and r.entity)]
    renames = await eng._erased_entity_nodes(ns, names, forced=subject_names)
    redacted = (
        await storage.redact_event_payloads(ids, node_renames=renames)
        if renames
        else await storage.redact_event_payloads(ids)
    )
    # D-18: the hard-delete cascade escalates to alert severity.
    _log.error(
        EVENT_FORGET,
        namespace=ns,
        record_id=ids[0],
        hard=True,
        cascaded=len(ids) - 1,
        redacted=len(redacted),
    )
    await eng._purge_caches(texts)
    # ADR-081 storage.documents: documents whose last referencing record was erased go too.
    documents = getattr(eng, "_documents", None)
    if documents is not None:
        await documents.erase_records(ns, ids)
    # The vector delete only marks rows deleted; the purge drops their bytes
    # and the older table versions that still return them (verify_forget
    # reports vector history unproven when it did not run).
    purge = getattr(eng._vector, "purge_deleted", None)
    if callable(purge) and not await purge():
        _log.warning("memory.forget_vector_purge_incomplete", namespace=ns, record_id=ids[0])
    if eng._sql is not None:
        await eng._sql.checkpoint()


async def _erased_entity_nodes(
    eng: Any, ns: str, names: Sequence[str], *, forced: Sequence[str] = ()
) -> dict[str, str]:
    """Entity node id -> opaque id for the erased ``names`` (KB-12 partition
    markers store node ids, which spell the name). A name in ``forced`` (the
    erased subject) is always renamed; another only once its node has no edge
    left (no live record mentions it). Empty without a graph store."""
    if eng._graph is None:
        return {}
    forced_keys = {canonical_entity(n) for n in forced}
    renames: dict[str, str] = {}
    for name in dict.fromkeys(names):
        canonical = canonical_entity(name)
        node = entity_node_id(ns, canonical)
        if not canonical or node in renames:
            continue
        if canonical not in forced_keys and await eng._graph.edges_of(node):
            continue
        renames[node] = f"{ENTITY_PREFIX}{ns}:#erased-{secrets.token_hex(8)}"
    return renames


async def _forget_target(
    eng: Any, storage: SqlStorage, ns: str, record_id: str, hard: bool
) -> MemoryRecord | None:
    """The record to forget after the scope and legal-hold checks; None when
    a hard forget finds no row (an idempotent retry of the log redaction)."""
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
        return None
    if record.namespace != ns:
        raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
    if hard:
        retention = RetentionPolicy.bind(
            _as_options_dict(eng._memory_policy(eng._config(), record.memory_type).get("retention"))
        )
        if retention.on_legal_hold(record):
            raise MemspineError(
                f"record {record_id} is under legal hold — hard delete refused (M7)"
            )
    return record


def _erasable_texts(eng: Any, record: MemoryRecord) -> list[str]:
    """The texts of ``record`` a cache may be keyed on: its (inflated) content
    and every archived version."""
    texts = [h.content for h in record.history if h.content]
    try:
        texts.append(eng._inflate.inflate(record).content)
    except StorageError:
        texts.append(record.content)
    return [t for t in dict.fromkeys(texts) if t]


async def _purge_caches(eng: Any, texts: Sequence[str]) -> None:
    """#43: drop the embedding and extraction cache entries of erased texts, and
    the read-side caches keyed by text (N60 sentence vectors, N30 date spans)."""
    erased = set(texts)
    for key in [k for k in eng._sentence_vectors if k[1] in erased]:
        del eng._sentence_vectors[key]
    forget_mentions(erased)
    forget_embedding = getattr(eng._embedder, "forget", None)
    for text in texts:
        if callable(forget_embedding):
            await forget_embedding(text)
        if eng._cached_extractor is not None:
            await eng._cached_extractor.forget(text)


async def _forget_locked(
    eng: Any, storage: SqlStorage, ns: str, record_id: str, hard: bool, *, redact: bool = True
) -> None:
    """Forget one record (lock held): FORGET event, log redaction when
    ``redact`` and hard, and the memory types' delete hooks."""
    await eng._forget_target(storage, ns, record_id, hard)
    await eng._append_and_project(
        MemoryEvent(
            kind=EventKind.FORGET,
            namespace=ns,
            actor="user",
            payload={"record_id": record_id, "hard": hard},
        )
    )
    if eng._config().graph.edge_compiler_v2:  # F-02c: its compiled graph edges go with it
        await eng._retract_compiled_edges(ns, [record_id])
    if hard and redact:
        redacted = await storage.redact_event_payloads(record_id)
        # D-18: the hard-delete cascade escalates to alert severity.
        _log.error(
            EVENT_FORGET, namespace=ns, record_id=record_id, hard=True, redacted=len(redacted)
        )
    # M7 delete hooks: every enabled memory type drops derived state that
    # references the record (e.g. the semantic LSH cache stops probing it).
    for memory in (
        eng._working,
        eng._semantic,
        eng._episodic,
        eng._resource,
        eng._procedural,
        eng._reflective,
        eng._associative,
        eng._prospective,
        eng._shared,
    ):
        if memory is not None:
            await memory.on_forget(ns, record_id)
    if eng._windows is not None:
        # W1: leaves its open window, cancels queued / running windows that held it,
        # drops pending call-1 edges that cite it and purges cached replies (lock held).
        await eng._windows.on_forget(ns, record_id)
    _log.info(EVENT_FORGET, namespace=ns, record_id=record_id)


async def forget_requests(
    eng: Any, namespace: str = "default", top_k: int = 5
) -> list[dict[str, object]]:
    """G25 (plan v3.2, ``memories.episodic.policies.forget_detector``): the user's
    "forget that" requests in ``namespace`` and, for each, the earlier live records
    that best match what it names (:func:`core.forget_request.forget_target`).

    Read-only: nothing is forgotten here. The caller confirms the candidates and
    calls :meth:`forget` (``hard=True`` for erasure), so a false detection never
    deletes a memory."""
    ns = validate_namespace(namespace)
    storage = eng._require_started()
    out: list[dict[str, object]] = []
    for request in await storage.list_records(ns, "episodic"):
        if constants.FORGET_REQUEST_TAG not in request.tags:
            continue
        target = forget_target(request.content) or request.content
        # Lexical first: an earlier live record holding most of what the request
        # names (M6 demo: the vector search alone missed the original turn).
        wanted = content_words(target)
        lexical = sorted(
            (
                r
                for r in await eng._records(ns)
                if r.status is RecordStatus.ACTIVATED
                and not r.quarantined
                and r.record_id != request.record_id
                and constants.FORGET_REQUEST_TAG not in r.tags
                and r.valid_from <= request.valid_from
                and wanted
                and len(wanted & content_words(r.content)) / len(wanted)
                >= constants.FORGET_MATCH_SHARE
            ),
            key=lambda r: (
                -len(wanted & content_words(r.content)),
                r.memory_type != "episodic",
            ),
        )
        with system_scope():
            hits = await eng.search(target, namespace=ns, top_k=top_k + 1)
        ranked = [r.record_id for r in lexical] + [
            r.record_id
            for r, _ in hits
            if r.record_id != request.record_id
            and constants.FORGET_REQUEST_TAG not in r.tags
            and r.valid_from <= request.valid_from
        ]
        candidates = list(dict.fromkeys(ranked))[:top_k]
        out.append({"request_id": request.record_id, "target": target, "candidates": candidates})
    return out


async def verify_forget(
    eng: Any, record_id: str, namespace: str = "default", *, probe: str | None = None
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
    storage = eng._require_started()
    existing = await storage.get_record(record_id)
    if existing is not None and existing.namespace != namespace:
        raise ConflictError(f"no such record {record_id!r} in namespace {namespace!r}")
    record_absent = existing is None
    vector_absent: bool | None = None
    exists = getattr(eng._vector, "exists", None)
    if callable(exists):
        vector_absent = not await exists(record_id)
    # A deleted vector can survive in older table versions until purged.
    # None => the backend cannot prove its history clean (unproven).
    vector_history_absent: bool | None = None
    history_absent = getattr(eng._vector, "history_absent", None)
    if callable(history_absent):
        vector_history_absent = await history_absent(record_id)
    # The Tantivy lexical index holds raw content when hybrid is on, so
    # erasure is not proven until it too is inspected. None => no lexical store
    # is owned (hybrid off) — nothing to erase, so it cannot block ``clean``.
    lexical_absent: bool | None = None
    if eng._lexical is not None:
        lexical_absent = not await eng._lexical.exists(record_id)
    descendants = await eng._descendants(storage, validate_namespace(namespace), [record_id])
    # F4: under ``privacy.erasure_v2`` a dependant is not a leak when it is kept (another live
    # parent) or hidden and queued for re-derivation; only a ``leaked`` one blocks ``clean``.
    v2 = bool(eng._config().privacy.erasure_v2)
    split: dict[str, list[str]] | None = None
    if v2:
        from memspine.trust.pipeline import erasure_v2

        split = await erasure_v2.classify_descendants(eng, storage, record_id, descendants)
    blocking = split["leaked"] if split is not None else descendants
    log_verifiable = storage.can_rebuild  # ephemeral persists nothing to prove
    retained: set[str] = set()
    after = 0
    while log_verifiable:
        batch = await storage.read_events(after_seq=after)
        if not batch:
            break
        for event in batch:
            retained |= retained_fields(event.payload, record_id)
        assert batch[-1].seq is not None
        after = batch[-1].seq
    log_clean = not retained
    # W14 (plan v3.2): erasure proven on RECALL, not only on the stores: the
    # erased text, searched for, must bring back no record holding it or a
    # near-duplicate (a copy, a derived summary, a re-statement) in this namespace.
    residual: list[str] | None = None
    if probe is not None and probe.strip():
        wanted = content_words(probe)
        residual = []
        with system_scope():
            probe_hits = await eng.search(
                probe, namespace=namespace, top_k=constants.RESIDUAL_PROBE_TOP_K
            )
        for hit, _ in probe_hits:
            words = content_words(hit.content)
            overlap = len(wanted & words) / max(1, len(wanted))
            if probe.strip().lower() in hit.content.lower() or (
                wanted and overlap >= constants.RESIDUAL_PROBE_OVERLAP
            ):
                residual.append(hit.record_id)
    clean = (
        record_absent
        and log_verifiable
        and log_clean
        and not residual
        and not blocking
        and vector_absent is True
        and vector_history_absent is True
        and lexical_absent is not False  # True (absent) or None (no store) both pass
    )
    return {
        "record_id": record_id,
        "record_absent": record_absent,
        "vector_absent": vector_absent,  # None => backend cannot prove it
        "vector_history_absent": vector_history_absent,  # None => unproven
        "lexical_absent": lexical_absent,  # None => no lexical store owned
        # Tantivy drops a deleted document's terms from its inverted index only
        # when its segment is merged, which the engine cannot force: the
        # document (and its id) is gone, but its terms can linger on disk.
        "residual_risks": ["lexical_terms_until_segment_merge"] if eng._lexical is not None else [],
        "log_verifiable": log_verifiable,
        "log_redacted": log_clean,
        "log_retained_fields": sorted(retained),
        "descendants_remaining": descendants,
        # F4 (erasure_v2 only; None under v1): hidden-pending-rederive is not a leak
        "erasure_v2": v2,
        "descendants_hidden_pending_rederive": split["hidden_pending_rederive"] if split else None,
        "descendants_kept": split["kept"] if split else None,
        "descendants_leaked": split["leaked"] if split else None,
        # W14: records the probe still recalls (None: no probe given).
        "residual_recall": residual,
        "clean": clean,
    }


async def _derived_parents_gone(
    eng: Any, namespace: str, parents: Sequence[str], site: str
) -> bool:
    """I72: True (and audited) when any parent of a derived deposit is no longer live.

    Called with the namespace lock held, immediately before a sleep-stage deposit:
    the stage read its parents, awaited an LLM call outside the lock, and a hard
    forget (and its cascade) may have run in between. A parent that is missing,
    erased, archived or quarantined means the derived text may restate erased
    content, so the deposit is skipped, never written. The skip is logged and,
    under ``audit.actions``, recorded as a ``derived_deposit_skipped`` audit event
    naming the site and the vanished parent ids (ids only, never content)."""
    storage = eng._require_started()
    gone: list[str] = []
    for parent_id in dict.fromkeys(parents):
        parent = await storage.get_record(parent_id)
        if (
            parent is None
            or parent.status is not RecordStatus.ACTIVATED
            or parent.quarantined
            or not erasure_v2.visible_id(eng, parent_id)  # T5-04: erased or hidden by a plan
        ):
            gone.append(parent_id)
    if not gone:
        return False
    _log.warning("memory.derived_deposit_skipped", namespace=namespace, site=site, gone=gone)
    await eng._audit_action(
        "derived_deposit_skipped",
        namespace,
        gone,
        actor="system",
        reason="parent_not_live",
        site=site,
    )
    return True
