"""Trust: quarantine moved out of the engine (F-12)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from memspine.config import constants
from memspine.exceptions import ConflictError, RollbackUnavailableError
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.scope import validate_namespace
from memspine.trust.audit import IntegrityReport, TaintReport, trace_taint, verify_events
from memspine.trust.pipeline.state import _QUARANTINE_REJECTED_TAG, _TAINT_ARCHIVED_TAG

_log = get_logger(__name__)


def _taint_archive_delta(record: MemoryRecord) -> dict[str, object]:
    """Rollback/repair archive patch. An open interval is closed at its start, so
    an archived poison never stays the M4 incumbent (``find_active_fact`` keys
    on ``valid_to IS NULL``) and never reads as "current" (R2-9). The record is
    also tagged :data:`_TAINT_ARCHIVED_TAG` (N3)."""
    change: dict[str, object] = {"status": RecordStatus.ARCHIVED.value}
    if _TAINT_ARCHIVED_TAG not in record.tags:  # N3: durable, outlives the log
        change["tags_add"] = [_TAINT_ARCHIVED_TAG]  # B-8: add-only, never replaces
    if record.valid_to is None:
        change["valid_to"] = record.valid_from.isoformat()
    return change


async def _taint_archived_ids(eng: Any) -> set[str]:
    """Records whose latest status change was a taint rollback or repair (from the log).

    Scanned incrementally: the last sequence number seen is kept per storage
    instance, so repeated reads only read new events.
    """
    storage = eng._require_started()
    state: tuple[Any, list[int], set[str]] | None = getattr(eng, "_taint_scan", None)
    if state is None or state[0] is not storage:
        state = (storage, [0], set[str]())
        eng._taint_scan = state
    _, cursor, ids = state
    while True:
        events = await storage.read_events(after_seq=cursor[0], limit=1000)
        if not events:
            break
        for event in events:
            payload = event.payload or {}
            if event.kind is EventKind.DECAY_TRANSITION:
                target = str(payload.get("record_id", ""))
                reason = str(payload.get("reason", ""))
                if reason.startswith(("taint_rollback:", "taint_repair:")):
                    ids.add(target)
                elif (payload.get("set") or {}).get("status") == RecordStatus.ACTIVATED.value:
                    ids.discard(target)
            elif event.kind is EventKind.WRITE:
                rec = payload.get("record") or {}
                if rec.get("status") == RecordStatus.ACTIVATED.value:
                    ids.discard(str(rec.get("record_id", "")))
        seqs = [e.seq for e in events if e.seq is not None]
        if not seqs:
            break
        cursor[0] = max(seqs)
    return ids


async def audit_taint(
    eng: Any, record_id: str, namespace: str = "default", cross_namespace: bool = False
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
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    report = await trace_taint(storage, record_id, follow_parents=cross_namespace)
    record = await storage.get_record(record_id)
    seed_ns = record.namespace if record is not None else report.origin_namespace
    if seed_ns != ns:
        raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
    return report


async def quarantine(
    eng: Any,
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
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        record = await storage.get_record(record_id)
        if record is None or record.namespace != ns:
            raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
        if record.quarantined and record.status is RecordStatus.QUARANTINED:
            return record
        # B-2: only a live record can be held. A forgotten (DELETED) or
        # archived (incl. taint-archived) record would otherwise be revived
        # through quarantine -> corroboration; it raises the same error as a
        # missing id, so the call is not an existence oracle.
        if record.status not in (RecordStatus.ACTIVATED, RecordStatus.RESOLVING):
            raise ConflictError(f"no such record {record_id!r} in namespace {ns!r}")
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=ns,
                actor=actor,
                payload={
                    "record_id": record_id,
                    "set": {"quarantined": True, "status": RecordStatus.QUARANTINED.value},
                    "transition": f"{record.status.value}->quarantined",
                    "reason": reason,
                },
            )
        )
        updated = await storage.get_record(record_id)
        assert updated is not None
        return updated


async def rollback_taint(
    eng: Any,
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
    storage = eng._require_started()
    report = await eng.audit_taint(record_id, namespace, cross_namespace=True)
    untraced = await eng._lineage_window(report, "rollback_taint", strict)
    archived: list[str] = []
    review: list[str] = []
    targets = [(record_id, "seed"), *report.descendants.items()]
    for target_id, proof in targets:
        if proof.startswith("merged@"):
            review.append(target_id)
            continue
        if proof.startswith("superseded_by_taint@"):
            continue  # displaced, not derived: restored below
        record = await storage.get_record(target_id)
        if record is None or record.status in (RecordStatus.ARCHIVED, RecordStatus.DELETED):
            continue
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=record.namespace,
                actor=actor,
                payload={
                    "record_id": target_id,
                    "set": _taint_archive_delta(record),
                    "transition": f"{record.status.value}->archived",
                    "reason": f"taint_rollback:{record_id}",
                },
            )
        )
        archived.append(target_id)
    restored = await eng._undo_displacement(record_id, report, archived, actor)
    _log.warning(
        "memory.taint_rollback",
        namespace=namespace,
        seed=record_id,
        archived=len(archived),
        restored=len(restored),
        review=len(review),
    )
    result = {"archived": archived, "restored": restored, "review": review}
    if untraced:
        result["untraced"] = [record_id]
    return result


async def _lineage_window(eng: Any, report: TaintReport, verb: str, strict: bool) -> bool:
    """#64: True when the log no longer holds the seed's origin WRITE, so the
    taint walk saw none of its descendants. Raises with ``strict``; else warns."""
    if report.origin_seq is not None:
        return False
    storage = eng._require_started()
    mode = storage.mode.value
    detail = (
        "the event log does not hold this record's origin WRITE "
        f"(event_log.mode={mode}: "
        + (
            "events are never persisted"
            if storage.mode is EventLogMode.EPHEMERAL
            else "pruned or never logged"
        )
        + "); its descendants cannot be traced (D-45, ADR-011)"
    )
    if strict:
        raise RollbackUnavailableError(f"{verb} {report.record_id!r}: {detail}")
    _log.warning(
        "memory.rollback_beyond_window",
        verb=verb,
        record_id=report.record_id,
        event_log_mode=mode,
        detail=detail + "; falling back to archiving the seed alone (valid_to closed)",
    )
    return True


async def _undo_displacement(
    eng: Any, seed: str, report: TaintReport, removed: Sequence[str], actor: str
) -> list[str]:
    """R2-9: after a rollback/repair, give the fact keys back to clean records.

    1. A ``superseded_by_taint`` record (archived by the poison's UPDATE or
       INVALIDATE) is reactivated as the current fact, unless another
       record holds its key now (a later clean statement wins).
    2. On every key a removed record sat on, the ``disputed`` tag is cleared
       when no live contender is left (the contest was the poison's).
    """
    storage = eng._require_started()
    restored: list[str] = []
    for target_id, proof in report.descendants.items():
        if not proof.startswith("superseded_by_taint@"):
            continue
        rec = await storage.get_record(target_id)
        if (
            rec is None
            or rec.status is not RecordStatus.ARCHIVED
            or rec.quarantined
            or rec.entity is None
            or rec.attribute is None
        ):
            continue
        holder = await storage.find_active_fact(rec.namespace, rec.entity, rec.attribute)
        if holder is not None and holder.status is RecordStatus.ACTIVATED:
            continue
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=rec.namespace,
                actor=actor,
                payload={
                    "record_id": target_id,
                    "set": {
                        "status": RecordStatus.ACTIVATED.value,
                        "valid_to": None,
                        "superseded_at": None,
                        "evolve_to": None,
                    },
                    "transition": "archived->activated",
                    "reason": f"taint_restore:{seed}",
                },
            )
        )
        restored.append(target_id)
    keys: set[tuple[str, str, str]] = set()
    for rid in removed:
        rec = await storage.get_record(rid)
        if rec is not None and rec.memory_type == "semantic" and rec.entity and rec.attribute:
            keys.add((rec.namespace, rec.entity, rec.attribute))
    for ns, entity, attribute in sorted(keys):
        live = [
            r
            for r in await storage.list_records(ns, "semantic")
            if r.entity == entity
            and r.attribute == attribute
            and r.status is RecordStatus.ACTIVATED
            and not r.quarantined
            and "disputed" in r.tags
        ]
        if any(r.valid_to is not None for r in live):
            continue  # a clean contender still disputes the key
        for r in live:
            cleared = r.model_copy(update={"tags": [t for t in r.tags if t != "disputed"]})
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace=ns,
                    actor=actor,
                    payload={"record": cleared.model_dump(mode="json")},
                )
            )
    return restored


async def verify_integrity(
    eng: Any, key: bytes | None = None, expected_head: str | None = None
) -> IntegrityReport:
    """B2': offline integrity pass over the whole event log.

    Recomputes a SHA-256 (or, with ``key``, HMAC-SHA256) chain over every
    event, re-checks every stored fingerprint, and recomputes the monotone
    trust invariant for every WRITE that declares parents, from the log alone.
    Store ``chain_head`` outside the engine; pass it back as ``expected_head``
    to detect any later rewrite of history. Without ``key`` the head is only
    meaningful against such an external anchor.
    """
    storage = eng._require_started()
    events: list[MemoryEvent] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            break
        events.extend(batch)
        after = max(event.seq for event in batch if event.seq is not None)
    policy = eng._integrity()
    view = (
        policy.view_trust
        if policy.enabled
        else (lambda t, g, r: t if g == r else min(t, constants.TRUST_RETRIEVED_CAP))
    )
    return verify_events(events, view, key=key, expected_head=expected_head)


async def list_quarantined(eng: Any, namespace: str = "default") -> list[MemoryRecord]:
    """#3: the records of ``namespace`` the firewall (or an operator) is holding,
    oldest first: the review queue for :meth:`approve_quarantined` and
    :meth:`reject_quarantined`."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    held = [
        r for r in await storage.list_quarantined(ns) if r.status is RecordStatus.QUARANTINED
    ]
    held.sort(key=lambda r: (r.recorded_at, r.record_id))
    return eng._inflate_all(held, ns)


async def _held_record(eng: Any, ns: str, record_id: str) -> MemoryRecord:
    """A record of ``ns`` currently held in quarantine, or the anti-oracle error."""
    record = await eng._require_started().get_record(record_id)
    if (
        record is None
        or record.namespace != ns
        or not record.quarantined
        or record.status is not RecordStatus.QUARANTINED
    ):
        raise ConflictError(f"no quarantined record {record_id!r} in namespace {ns!r}")
    return record


async def approve_quarantined(
    eng: Any,
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
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        held = await eng._held_record(ns, record_id)
        author = held.source.principal
        if author is not None and author in (principal, actor):
            raise ConflictError(
                f"record {record_id!r} was written by {author!r}, who cannot approve it"
            )
        change = await eng._release_change(ns, held)
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=ns,
                actor=actor,
                payload={
                    "record_id": record_id,
                    "set": change,
                    "transition": "quarantined->released",
                    "reason": reason,
                },
            )
        )
        _log.warning(
            "memory.quarantine_approved", namespace=ns, record_id=record_id, actor=actor
        )
        updated = await eng._require_started().get_record(record_id)
        assert updated is not None
    await eng._audit_action(
        "approve_quarantined", ns, [record_id], actor=actor, reason=reason, principal=principal
    )
    return updated


async def reject_quarantined(
    eng: Any,
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
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        held = await eng._held_record(ns, record_id)
        change: dict[str, object] = {
            "status": RecordStatus.ARCHIVED.value,
            "tags_add": [_QUARANTINE_REJECTED_TAG],
        }
        if held.valid_to is None:
            change["valid_to"] = held.valid_from.isoformat()
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=ns,
                actor=actor,
                payload={
                    "record_id": record_id,
                    "set": change,
                    "transition": "quarantined->rejected",
                    "reason": reason,
                },
            )
        )
        _log.warning(
            "memory.quarantine_rejected", namespace=ns, record_id=record_id, actor=actor
        )
        updated = await eng._require_started().get_record(record_id)
        assert updated is not None
    await eng._audit_action("reject_quarantined", ns, [record_id], actor=actor, reason=reason)
    return updated


async def _lineage_roots(eng: Any, record: MemoryRecord) -> frozenset[str]:
    """W13: the records at the top of ``record``'s ``source.parents`` lineage (a
    record with no parents is its own root). Parents no longer stored count as
    roots under their id. Bounded walk (cycles and depth guarded)."""
    storage = eng._require_started()
    roots: set[str] = set()
    seen: set[str] = set()
    frontier: list[tuple[str, list[str]]] = [(record.record_id, list(record.source.parents))]
    while frontier and len(seen) < constants.LINEAGE_ROOTS_MAX_NODES:
        rid, parents = frontier.pop()
        if rid in seen:
            continue
        seen.add(rid)
        if not parents:
            roots.add(rid)
            continue
        for pid in parents:
            parent = await storage.get_record(pid)
            frontier.append((pid, list(parent.source.parents) if parent else []))
    return frozenset(roots)


async def _independent_principal(eng: Any, held: MemoryRecord, incoming: MemoryRecord) -> bool:
    """Principal-bound independence (integrity.principal_bound_corroboration).

    The corroborator must name a principal, differ from the held record's
    principal, and differ from every principal that already corroborated
    ``held`` — so k promotions need k distinct principals, not k sessions
    (Paper A, Prop. 4b). Earlier corroborators are read from the log.
    """
    principal = incoming.source.principal
    if principal is None or principal == held.source.principal:
        return False
    storage = eng._require_started()
    after = 0
    while True:
        events = await storage.read_events(after_seq=after, limit=1000)
        if not events:
            return True
        for event in events:
            if (
                event.kind is EventKind.DECAY_TRANSITION
                and event.payload.get("record_id") == held.record_id
                and event.payload.get("principal") == principal
            ):
                return False
        after = max(event.seq for event in events if event.seq is not None)
