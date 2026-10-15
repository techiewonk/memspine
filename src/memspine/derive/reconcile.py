"""Reconcile backlog and contradiction sweep (LIB-37; decisions O-10, O-11, O-12, O-13; S3-1, I-5, F-01).

Pure and unwired (wiring is O4-03): no I/O, no clock read (``now`` is passed in), no model call. Depends on
``memspine.schemas`` and the merged derive libraries ``integrate`` (LIB-02) and ``resolve`` (LIB-04).

What it does
------------
``sweep(scope_records, registry, ...)`` revisits what the online path could not settle and returns a
:class:`SweepReport` holding a deterministic :class:`ReconcilePlan`. Nothing is mutated; :func:`apply_plan`
is a pure helper that returns the updated records (used by the wiring package and by the tests).

1. *Pending entity merges (O-10, O-11, I-5).* A :class:`PendingMerge` is a mention that the online ladder
   kept under a provisional entity id because it was uncertain. With the evidence accumulated since (more
   context lines, other surface forms, explicit attributes) every in-scope candidate is re-judged with
   LIB-04 ``resolve_pair``. Exactly one ``same`` and nothing doubtful gives a merge; only ``different``
   verdicts give a split (the provisional entity is confirmed as a separate entity); anything else stays in
   the queue with one more attempt. A candidate from another owner scope is never considered (S3-1).
2. *Re-keying (I-5, F-01).* A confirmed merge re-points every assertion that mentions the provisional id
   (subject or object) to the target entity. The assertion key is re-written when it carries the provisional
   id, and the old key digest is kept as an alias (``key_aliases``). A-B-A rule: aliases never contain the
   current key digest and never repeat, so a key that returns to an earlier value drops that value from the
   aliases and keeps the one it left.
3. *Contradiction sweep (O-12, O-13).* The stored records are projected through the confirmed merges and
   grouped per ``(tenant, owner, subject, attribute)`` (optionally via an ``attribute_families`` map for
   cross-key predicates). Each group is re-folded with LIB-02 ``integrate`` over the whole group, whose
   decision is the difference between the order-independent fold and what is stored. Contests, supersedes
   and retirements deposited in different sessions that the online path missed come out as status changes,
   closures and contest flags.
4. *Queue (O-10 B).* :class:`ReconcileQueue` is plain immutable state: items to revisit with a priority and
   an attempt count, acknowledged items (a tombstone so an acknowledged pending merge is never re-applied),
   and the size or age cap test that fires the early reconcile-only job.

Idempotence: applying the plan with :func:`apply_plan` and sweeping again (with the returned queue) gives an
empty plan. The fold is a function of the set of records, so the plan does not depend on input order.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from memspine.derive.integrate import (
    CardinalityRegistry,
    Closure,
    integrate,
    normalise_name,
    ordering_key,
    record_id,
)
from memspine.derive.resolve import (
    Candidate,
    MentionView,
    PairContext,
    PairDecision,
    resolve_pair,
)
from memspine.schemas.durable.assertion import (
    AssertionEnvelope,
    AssertionStatus,
    IntegrationVerdict,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "PRIORITY",
    "ContestFlag",
    "EntityMerge",
    "EntitySplit",
    "KeyChange",
    "PendingMerge",
    "QueueItem",
    "QueueKind",
    "ReconcilePlan",
    "ReconcileQueue",
    "StatusChange",
    "SweepReport",
    "Unresolved",
    "apply_plan",
    "merge_aliases",
    "sweep",
]

QueueKind = Literal["pending_merge", "contested_group", "unbound", "late_arrival"]

DEFAULT_MAX_ATTEMPTS = 5
PRIORITY: Mapping[str, int] = {
    "pending_merge": 40,
    "late_arrival": 30,
    "contested_group": 20,
    "unbound": 10,
}

# Reasons from resolve_pair that mean "these two are not the same thing" (evidence, not just no match).
_STRONG_DIFFERENT = frozenset(
    {
        "type_mismatch",
        "numeric_mismatch",
        "designator_mismatch",
        "homonym_context",
        "exonym_other_place",
    }
)


# --------------------------------------------------------------------------- queue (pure state)


@dataclass(frozen=True)
class QueueItem:
    """One thing to revisit. ``ref`` is a pending id, a group reference or a record id, by ``kind``."""

    kind: QueueKind
    ref: str
    tenant: str
    owner: str
    priority: int = 0
    attempts: int = 0
    first_seen: datetime | None = None
    last_attempt: datetime | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.tenant, self.owner, self.ref)


def _order(i: QueueItem) -> tuple[int, int, datetime | None, str, str, str, str]:
    # highest priority first, then the oldest, then fully deterministic tie-breaks
    return (
        -i.priority,
        0 if i.first_seen is not None else 1,
        i.first_seen,
        i.kind,
        i.tenant,
        i.owner,
        i.ref,
    )


@dataclass(frozen=True)
class ReconcileQueue:
    """Immutable backlog state: every method returns a new queue (O-10).

    ``done`` holds acknowledged ``pending_merge`` items so an acknowledged merge is never re-applied
    ("an acknowledged row is never re-applied"); other kinds are dropped without a tombstone because a
    group can become contested again later.
    """

    items: tuple[QueueItem, ...] = ()
    done: frozenset[tuple[str, str, str, str]] = frozenset()

    def __len__(self) -> int:
        return len(self.items)

    def _by_key(self) -> dict[tuple[str, str, str, str], QueueItem]:
        return {i.key: i for i in self.items}

    def get(self, kind: QueueKind, tenant: str, owner: str, ref: str) -> QueueItem | None:
        return self._by_key().get((kind, tenant, owner, ref))

    def enqueue(self, item: QueueItem) -> ReconcileQueue:
        """Add ``item``. A repeat keeps the earliest ``first_seen``, the attempts and the higher priority."""
        if item.key in self.done:
            return self
        by = self._by_key()
        cur = by.get(item.key)
        if cur is not None:
            firsts = [d for d in (cur.first_seen, item.first_seen) if d is not None]
            first = min(firsts) if firsts else None
            item = replace(
                cur,
                priority=max(cur.priority, item.priority),
                first_seen=first,
            )
        by[item.key] = item
        return ReconcileQueue(tuple(sorted(by.values(), key=_order)), self.done)

    def enqueue_many(self, items: Iterable[QueueItem]) -> ReconcileQueue:
        q = self
        for i in items:
            q = q.enqueue(i)
        return q

    def attempted(
        self, kind: QueueKind, tenant: str, owner: str, ref: str, now: datetime | None = None
    ) -> ReconcileQueue:
        by = self._by_key()
        key = (kind, tenant, owner, ref)
        cur = by.get(key)
        if cur is None:
            return self
        by[key] = replace(cur, attempts=cur.attempts + 1, last_attempt=now)
        return ReconcileQueue(tuple(sorted(by.values(), key=_order)), self.done)

    def acknowledge(self, kind: QueueKind, tenant: str, owner: str, ref: str) -> ReconcileQueue:
        """Remove the item and remember it as done (so it is never re-applied)."""
        key = (kind, tenant, owner, ref)
        rest = tuple(i for i in self.items if i.key != key)
        return ReconcileQueue(rest, self.done | {key})

    def drop(self, kind: QueueKind, tenant: str, owner: str, ref: str) -> ReconcileQueue:
        """Remove the item without a tombstone (it may be queued again later)."""
        key = (kind, tenant, owner, ref)
        rest = tuple(i for i in self.items if i.key != key)
        return ReconcileQueue(rest, self.done)

    def ordered(self) -> tuple[QueueItem, ...]:
        return tuple(sorted(self.items, key=_order))

    def ready(self, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> tuple[QueueItem, ...]:
        """Items still worth retrying, highest priority and oldest first."""
        return tuple(i for i in self.ordered() if i.attempts < max_attempts)

    def parked(self, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> tuple[QueueItem, ...]:
        return tuple(i for i in self.ordered() if i.attempts >= max_attempts)

    def oldest_age(self, now: datetime) -> float:
        """Age in seconds of the oldest dated item (0.0 when none is dated)."""
        ages = [
            (now - i.first_seen).total_seconds() for i in self.items if i.first_seen is not None
        ]
        return max(ages) if ages else 0.0

    def over_cap(
        self, now: datetime, *, max_items: int | None = None, max_age_seconds: float | None = None
    ) -> bool:
        """The O-10 B trigger: the backlog passes its size cap or its oldest item passes the age cap."""
        if max_items is not None and len(self.items) > max_items:
            return True
        return max_age_seconds is not None and self.oldest_age(now) > max_age_seconds


# --------------------------------------------------------------------------- inputs and outputs


@dataclass(frozen=True)
class PendingMerge:
    """A mention the online ladder left under a provisional entity id (I-5), with the evidence so far.

    ``evidence`` holds the context lines (the original one plus any gathered later); ``other_names`` other
    surface forms met for the same provisional entity (for example a later full name); ``candidates`` the
    entity ids it might merge with; ``attrs`` explicit disambiguators (surname, city, role).
    """

    pending_id: str
    scope: OwnerScope
    provisional_id: str
    name: str
    type: str = ""
    evidence: tuple[str, ...] = ()
    other_names: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    lang: str | None = None
    attrs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityMerge:
    pending_id: str
    scope: OwnerScope
    from_id: str  # the provisional id
    into_id: str
    reason: str


@dataclass(frozen=True)
class EntitySplit:
    """Confirmed distinct: the provisional entity stays a separate entity of its own."""

    pending_id: str
    scope: OwnerScope
    entity_id: str
    against: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Unresolved:
    pending_id: str
    reason: str
    attempts: int
    parked: bool
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeyChange:
    """Re-point one assertion after a confirmed merge: new key, aliases and mention entity ids."""

    record_id: str
    from_entity: str
    to_entity: str
    old_digest: str
    new_key: AssertionKey
    new_digest: str
    key_aliases: tuple[str, ...]  # the full alias list after the change
    roles: tuple[str, ...]  # which mentions are re-pointed: "subject", "object"


@dataclass(frozen=True)
class StatusChange:
    record_id: str
    old: AssertionStatus
    new: AssertionStatus
    group: str


@dataclass(frozen=True)
class ContestFlag:
    """A group holding contested records after the sweep; ``newly`` are those flagged by this plan."""

    group: str
    record_ids: tuple[str, ...]
    newly: tuple[str, ...]


@dataclass(frozen=True)
class ReconcilePlan:
    merges: tuple[EntityMerge, ...] = ()
    splits: tuple[EntitySplit, ...] = ()
    rekeys: tuple[KeyChange, ...] = ()
    status_changes: tuple[StatusChange, ...] = ()
    closures: tuple[Closure, ...] = ()
    contest_flags: tuple[ContestFlag, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.merges
            or self.splits
            or self.rekeys
            or self.status_changes
            or self.closures
            or self.contest_flags
        )


@dataclass(frozen=True)
class SweepReport:
    plan: ReconcilePlan
    queue: ReconcileQueue
    unresolved: tuple[Unresolved, ...] = ()
    open_contests: tuple[str, ...] = ()  # groups still contested after the plan
    waiting_binding: tuple[str, ...] = ()  # record ids that are not bound yet
    groups_scanned: int = 0
    records_scanned: int = 0

    @property
    def is_noop(self) -> bool:
        return self.plan.is_empty


# --------------------------------------------------------------------------- key aliases (F-01)


def merge_aliases(existing: Sequence[str], old_digest: str, new_digest: str) -> tuple[str, ...]:
    """Alias list after a key moves from ``old_digest`` to ``new_digest`` (A-B-A safe).

    Keeps earlier aliases in order, appends the key being left, removes duplicates and never lists the
    current key. A key that goes A -> B -> A therefore ends with ``[B]`` and not ``[A, B]``.
    """
    out: list[str] = []
    for a in (*existing, old_digest):
        if a != new_digest and a not in out:
            out.append(a)
    return tuple(out)


# --------------------------------------------------------------------------- helpers


def _scope_key(s: OwnerScope) -> tuple[str, str]:
    return (s.tenant, s.owner)


def _subject_key(e: AssertionEnvelope) -> str | None:
    s = e.subject
    if s is None:
        return None
    return s.entity_id or s.surface.strip().casefold()


def _attr_name(e: AssertionEnvelope, families: Mapping[str, str] | None) -> str:
    a = normalise_name(e.attribute or e.kind)
    if families:
        fam = families.get(a)
        if fam is not None:
            return normalise_name(fam)
    return a


def _end_at(e: AssertionEnvelope) -> datetime | None:
    ev = e.time.event
    return ev.end.at if ev is not None and ev.end is not None else None


def _integrable(e: AssertionEnvelope) -> bool:
    # hidden records other than merged duplicates are ignored by the fold (and by us)
    return e.status != "hidden" or (e.verdict is not None and e.verdict.op == "merge")


def _group_ref(tenant: str, owner: str, subject: str | None, attr: str) -> str:
    return f"{tenant}/{owner}/{subject if subject is not None else ''}/{attr}"


def _repoint(
    e: AssertionEnvelope, from_id: str, to_id: str
) -> tuple[AssertionEnvelope, tuple[str, ...]]:
    roles: list[str] = []
    upd: dict[str, object] = {}
    if e.subject is not None and e.subject.entity_id == from_id:
        upd["subject"] = e.subject.model_copy(update={"entity_id": to_id})
        roles.append("subject")
    if e.object is not None and e.object.entity_id == from_id:
        upd["object"] = e.object.model_copy(update={"entity_id": to_id})
        roles.append("object")
    if not roles:
        return e, ()
    return e.model_copy(update=upd), tuple(roles)


def _rekey(e: AssertionEnvelope, from_id: str, to_id: str) -> AssertionEnvelope:
    """The envelope after re-pointing: mentions, key (when it carries the id) and aliases."""
    moved, roles = _repoint(e, from_id, to_id)
    if not roles:
        return e
    k = e.assertion_key
    new_k = k.model_copy(
        update={
            "src": to_id if k.src == from_id else k.src,
            "dst": to_id if k.dst == from_id else k.dst,
        }
    )
    old_d, new_d = k.digest(), new_k.digest()
    aliases = merge_aliases(e.key_aliases, old_d, new_d) if old_d != new_d else tuple(e.key_aliases)
    return moved.model_copy(update={"assertion_key": new_k, "key_aliases": list(aliases)})


# --------------------------------------------------------------------------- entity resolution


def _judge(
    p: PendingMerge,
    cand: Candidate,
    cand_attrs: Mapping[str, str] | None,
) -> PairDecision:
    """Verdict for one candidate using all accumulated evidence (names, context, attributes)."""
    context = " ".join(x for x in p.evidence if x.strip())
    pctx = PairContext(a_attrs=dict(p.attrs), b_attrs=dict(cand_attrs or {}))
    verdicts: list[PairDecision] = []
    for surface in dict.fromkeys((p.name, *p.other_names)):
        a = MentionView(surface, p.type, context, p.lang)
        for name in dict.fromkeys((cand.name, *cand.aliases)):
            b = MentionView(name, cand.type, cand.context, cand.lang)
            verdicts.append(resolve_pair(a, b, pctx))
    strong = [
        v
        for v in verdicts
        if v.verdict == "different"
        and (v.reason in _STRONG_DIFFERENT or v.reason.startswith("explicit_disambiguator_"))
    ]
    if strong:
        return strong[0]
    same = [v for v in verdicts if v.verdict == "same"]
    if same:
        return same[0]
    pend = [v for v in verdicts if v.verdict == "pending_merge"]
    if pend:
        return pend[0]
    return verdicts[0] if verdicts else PairDecision("different", "no_candidate_name")


def _resolve_pending(
    p: PendingMerge,
    entities: Mapping[str, Candidate],
    entity_attrs: Mapping[str, Mapping[str, str]],
) -> tuple[Literal["merge", "split", "pending"], str, str | tuple[str, ...]]:
    usable: list[Candidate] = []
    for cid in sorted(set(p.candidates)):
        c = entities.get(cid)
        # S3-1: entities never merge across owners (tenant plus owner is the scope)
        if c is not None and _scope_key(c.scope) == _scope_key(p.scope) and cid != p.provisional_id:
            usable.append(c)
    if not usable:
        return "pending", "no_candidate_in_scope", ()
    same: list[tuple[str, str]] = []
    pend: list[tuple[str, str]] = []
    diff: list[tuple[str, str]] = []
    for c in usable:
        d = _judge(p, c, entity_attrs.get(c.entity_id))
        {"same": same, "pending_merge": pend, "different": diff}[d.verdict].append(
            (c.entity_id, d.reason)
        )
    if len(same) == 1 and not pend:
        return "merge", same[0][1], same[0][0]
    if len(same) > 1:
        return "pending", "ambiguous_same", tuple(sorted(i for i, _ in same))
    if pend or same:
        first = (pend or same)[0][1]
        return "pending", first, tuple(sorted(i for i, _ in (*pend, *same)))
    return "split", diff[0][1], tuple(sorted(i for i, _ in diff))


# --------------------------------------------------------------------------- the sweep


def sweep(
    scope_records: Iterable[AssertionEnvelope],
    registry: CardinalityRegistry,
    *,
    pending: Iterable[PendingMerge] = (),
    entities: Iterable[Candidate] = (),
    entity_attrs: Mapping[str, Mapping[str, str]] | None = None,
    queue: ReconcileQueue | None = None,
    now: datetime | None = None,
    attribute_families: Mapping[str, str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> SweepReport:
    """Revisit pending merges and re-fold every (subject, attribute) group; return a plan, mutate nothing.

    ``scope_records`` are the stored assertions to examine (any owners; each owner is folded on its own).
    ``pending`` and ``entities`` carry the entity-resolution backlog and the known entities (with their
    accumulated context); ``queue`` is the backlog state from the previous run. ``attribute_families`` maps a
    normalised attribute (or predicate) to the family it is judged under, so ``moved_to`` can contest
    ``lives_in`` (O-12 stage 2); declare cardinality for the family name in ``registry``.
    """
    q = queue if queue is not None else ReconcileQueue()
    records = list(scope_records)
    ent_by_id = {c.entity_id: c for c in entities}
    attrs = entity_attrs or {}
    scopes = {_scope_key(r.scope) for r in records}

    # ---- 1. pending entity merges
    merges: list[EntityMerge] = []
    splits: list[EntitySplit] = []
    unresolved: list[Unresolved] = []
    for p in sorted(pending, key=lambda x: x.pending_id):
        tenant, owner = _scope_key(p.scope)
        key = ("pending_merge", tenant, owner, p.pending_id)
        if key in q.done:
            continue
        q = q.enqueue(
            QueueItem(
                "pending_merge",
                p.pending_id,
                tenant,
                owner,
                PRIORITY["pending_merge"],
                first_seen=now,
            )
        )
        item = q.get("pending_merge", tenant, owner, p.pending_id)
        assert item is not None
        if item.attempts >= max_attempts:
            unresolved.append(
                Unresolved(p.pending_id, "parked", item.attempts, True, tuple(p.candidates))
            )
            continue
        verdict, reason, detail = _resolve_pending(p, ent_by_id, attrs)
        if verdict == "merge":
            assert isinstance(detail, str)
            merges.append(EntityMerge(p.pending_id, p.scope, p.provisional_id, detail, reason))
            q = q.acknowledge("pending_merge", tenant, owner, p.pending_id)
        elif verdict == "split":
            assert isinstance(detail, tuple)
            splits.append(EntitySplit(p.pending_id, p.scope, p.provisional_id, detail, reason))
            q = q.acknowledge("pending_merge", tenant, owner, p.pending_id)
        else:
            q = q.attempted("pending_merge", tenant, owner, p.pending_id, now)
            item = q.get("pending_merge", tenant, owner, p.pending_id)
            assert item is not None
            cands = detail if isinstance(detail, tuple) else ()
            unresolved.append(
                Unresolved(
                    p.pending_id, reason, item.attempts, item.attempts >= max_attempts, cands
                )
            )

    # ---- 2. re-key: project the stored records through the confirmed merges
    remap: dict[tuple[str, str, str], str] = {}
    for m in merges:
        remap[(*_scope_key(m.scope), m.from_id)] = m.into_id
    rekeys: list[KeyChange] = []
    projected: list[AssertionEnvelope] = []
    for r in records:
        cur = r
        sk = _scope_key(r.scope)
        for (tenant, owner, from_id), to_id in sorted(remap.items()):
            if (tenant, owner) != sk:
                continue
            nxt = _rekey(cur, from_id, to_id)
            if nxt is not cur:
                _, roles = _repoint(cur, from_id, to_id)
                rekeys.append(
                    KeyChange(
                        record_id=record_id(r),
                        from_entity=from_id,
                        to_entity=to_id,
                        old_digest=cur.assertion_key.digest(),
                        new_key=nxt.assertion_key,
                        new_digest=nxt.assertion_key.digest(),
                        key_aliases=tuple(nxt.key_aliases),
                        roles=roles,
                    )
                )
                cur = nxt
        projected.append(cur)

    # ---- 3. contradiction sweep over (owner, subject, attribute) groups
    buckets: dict[tuple[str, str, str | None, str], list[AssertionEnvelope]] = defaultdict(list)
    for e in projected:
        if not _integrable(e):
            continue
        buckets[
            (e.scope.tenant, e.scope.owner, _subject_key(e), _attr_name(e, attribute_families))
        ].append(e)

    changes: list[StatusChange] = []
    closures: list[Closure] = []
    flags: list[ContestFlag] = []
    open_contests: list[str] = []
    final_status: dict[str, AssertionStatus] = {}
    for gk in sorted(buckets, key=lambda k: tuple("" if x is None else x for x in k)):
        recs = buckets[gk]
        ref = _group_ref(*gk)
        folded = [
            e if attribute_families is None else _as_family(e, gk[3]) for e in recs
        ]  # same ids, family attribute
        by_rid = {record_id(e): e for e in recs}
        inc = max(folded, key=ordering_key)
        rest = [e for e in folded if e is not inc]
        d = integrate(inc, rest, registry)
        inc_rid = record_id(inc)
        new_status: dict[str, AssertionStatus] = {record_id(e): e.status for e in recs}
        for rid, st in d.status_changes:
            if rid in new_status and st != new_status[rid]:
                changes.append(StatusChange(rid, new_status[rid], st, ref))
                new_status[rid] = st
        if d.incoming_status != new_status[inc_rid]:
            changes.append(StatusChange(inc_rid, new_status[inc_rid], d.incoming_status, ref))
            new_status[inc_rid] = d.incoming_status
        for c in d.closures:
            e = by_rid.get(c.record_id)  # type: ignore[assignment]  # CI-GREEN: pre-existing typing debt
            if e is not None and _end_at(e) != c.end:
                closures.append(c)
        final_status.update(new_status)
        contested_now = sorted(rid for rid, st in new_status.items() if st == "contested")
        if contested_now:
            open_contests.append(ref)
            newly = tuple(sorted(rid for rid in contested_now if by_rid[rid].status != "contested"))
            if newly:
                flags.append(ContestFlag(ref, tuple(contested_now), newly))

    plan = ReconcilePlan(
        merges=tuple(sorted(merges, key=lambda m: m.pending_id)),
        splits=tuple(sorted(splits, key=lambda s: s.pending_id)),
        rekeys=tuple(sorted(rekeys, key=lambda k: (k.record_id, k.from_entity))),
        status_changes=tuple(sorted(changes, key=lambda c: (c.record_id, c.new))),
        closures=tuple(
            sorted(
                {c.record_id: c for c in closures}.values(),
                key=lambda c: c.record_id,
            )
        ),
        contest_flags=tuple(sorted(flags, key=lambda f: f.group)),
    )

    # ---- 4. queue maintenance for the scopes we looked at
    open_set = set(open_contests)
    for it in q.items:
        if (it.tenant, it.owner) not in scopes:
            continue
        if it.kind == "contested_group" and it.ref not in open_set:
            q = q.drop("contested_group", it.tenant, it.owner, it.ref)
        elif it.kind == "late_arrival":
            q = q.drop("late_arrival", it.tenant, it.owner, it.ref)
        elif it.kind == "unbound":
            e = next((x for x in projected if record_id(x) == it.ref), None)  # type: ignore[assignment]  # CI-GREEN: pre-existing typing debt
            if e is None or e.assessment.binding == "bound":
                q = q.drop("unbound", it.tenant, it.owner, it.ref)
    for gk in sorted(buckets, key=lambda k: tuple("" if x is None else x for x in k)):
        ref = _group_ref(*gk)
        if ref in open_set:
            q = q.enqueue(
                QueueItem(
                    "contested_group",
                    ref,
                    gk[0],
                    gk[1],
                    PRIORITY["contested_group"],
                    first_seen=now,
                )
            )
    waiting: list[str] = []
    for e in sorted(projected, key=record_id):
        if e.assessment.binding != "bound" and _integrable(e) and e.status != "hidden":
            rid = record_id(e)
            waiting.append(rid)
            q = q.enqueue(
                QueueItem(
                    "unbound",
                    rid,
                    e.scope.tenant,
                    e.scope.owner,
                    PRIORITY["unbound"],
                    first_seen=now,
                )
            )

    return SweepReport(
        plan=plan,
        queue=q,
        unresolved=tuple(unresolved),
        open_contests=tuple(sorted(open_set)),
        waiting_binding=tuple(waiting),
        groups_scanned=len(buckets),
        records_scanned=len(records),
    )


def _as_family(e: AssertionEnvelope, family: str) -> AssertionEnvelope:
    """Copy of ``e`` judged under ``family`` (the id is unchanged, so decisions map back to the record)."""
    if normalise_name(e.attribute or e.kind) == family:
        return e
    return e.model_copy(update={"attribute": family})


# --------------------------------------------------------------------------- applying a plan (pure)


def apply_plan(
    plan: ReconcilePlan, records: Iterable[AssertionEnvelope]
) -> list[AssertionEnvelope]:
    """Return ``records`` after the plan: re-keys, then status changes, then closures (pure helper)."""
    out: dict[str, AssertionEnvelope] = {record_id(e): e for e in records}
    for k in plan.rekeys:
        e = out.get(k.record_id)
        if e is None:
            continue
        e, _ = _repoint(e, k.from_entity, k.to_entity)
        out[k.record_id] = e.model_copy(
            update={"assertion_key": k.new_key, "key_aliases": list(k.key_aliases)}
        )
    for c in plan.status_changes:
        e = out.get(c.record_id)
        if e is None:
            continue
        upd: dict[str, object] = {"status": c.new}
        if c.new == "hidden":
            upd["verdict"] = IntegrationVerdict(op="merge", rung="reresolved")
        elif e.status == "hidden":
            upd["verdict"] = IntegrationVerdict(op="append", rung="reresolved")
        out[c.record_id] = e.model_copy(update=upd)
    for cl in plan.closures:
        e = out.get(cl.record_id)
        if e is None:
            continue
        ev = e.time.event
        start = ev.start if ev is not None else None
        if cl.end is None:
            new_ev = Interval(start=start) if start is not None else None
        else:
            new_ev = Interval(start=start, end=cl.ref)
        out[cl.record_id] = e.model_copy(
            update={"time": e.time.model_copy(update={"event": new_ev})}
        )
    return list(out.values())
