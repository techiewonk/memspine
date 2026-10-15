"""Trust: firewall moved out of the engine (F-12)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from memspine.config import constants
from memspine.core.integrity import IntegrityPolicy
from memspine.core.vetting import VetFlag, draft_sentences, vet_sentences
from memspine.exceptions import ConflictError
from memspine.memories.procedural.skills import stage_status
from memspine.models.encoders.query.shape import content_words
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import MemoryRecord, PiiTier, RecordStatus, SourceInfo
from memspine.store.scope import grant_allows, validate_namespace
from memspine.trust.pipeline import gates_v2
from memspine.trust.pipeline.state import _CORROBORATION_ROLES, AuthorizeDecision, _fact_value
from memspine.trust.privacy import inherited_consent, inherited_pii

_log = get_logger(__name__)


async def principal_reputation(eng: Any, principal: str) -> float:
    """B7: trust multiplier in (0, 1] for ``principal``, from the event log.

    ``bad`` counts the principal's records that are currently quarantined,
    plus those that were the seed of a taint rollback (descendants archived
    by the same rollback are not blamed on their authors). With a uniform
    Beta prior, the mean is ``(1 + good) / (2 + good + bad)``, and the
    multiplier is ``min(1, 2 * mean)``: 1.0 for a new or clean principal.
    """
    storage = eng._require_started()
    owned: set[str] = set()
    seeds: set[str] = set()
    after = 0
    while True:
        events = await storage.read_events(after_seq=after, limit=1000)
        if not events:
            break
        for event in events:
            payload = event.payload or {}
            if event.kind is EventKind.WRITE:
                rec = payload.get("record") or {}
                if (rec.get("source") or {}).get("principal") == principal:
                    owned.add(str(rec.get("record_id")))
            elif event.kind is EventKind.DECAY_TRANSITION:
                target = str(payload.get("record_id", ""))
                if str(payload.get("reason", "")) == f"taint_rollback:{target}":
                    seeds.add(target)
        after = max(e.seq for e in events if e.seq is not None)
    if not owned:
        return 1.0
    bad = 0
    for record_id in owned:
        if record_id in seeds:
            bad += 1
            continue
        current = await storage.get_record(record_id)
        if current is not None and current.quarantined:
            bad += 1
    good = len(owned) - bad
    mean = (1 + good) / (2 + good + bad)
    return min(1.0, 2.0 * mean)


async def retract(
    eng: Any,
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
    text = f"RETRACTED {entity}.{attribute}" + (f": {reason}" if reason else "")
    return await eng.write(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        text,
        namespace=namespace,
        memory_type="semantic",
        source=source,
        entity=entity,
        attribute=attribute,
        tags=["retract"],
        valid_from=valid_from,
    )


async def send(
    eng: Any,
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
    session_id = ledger_id if ledger_id is not None else session_id  # I9
    storage = eng._require_started()
    sender = validate_namespace(from_namespace)
    receiver = validate_namespace(to_namespace)
    shared = eng._require_shared()
    grants = await shared.grants_to(receiver)
    if sender not in grants:
        raise ConflictError(f"no grant {sender!r} -> {receiver!r}: messages follow grant edges")
    implicit = eng._consume_reads(sender, session_id)
    parents = list(dict.fromkeys([*(derived_from or []), *implicit]))
    source = SourceInfo(role=actor, channel="message", principal=principal, parents=parents)
    record = MemoryRecord(
        namespace=receiver,
        memory_type="episodic",
        content=content,
        source=source,
        tags=["message", f"from:{sender}"],
    )
    policy = eng._integrity()
    base = eng._firewall.policy.trust_at_write(source)
    sender_views = await eng._parent_trust_cap(sender, parents) or []
    sender_views = [*sender_views, *implicit.values()]
    at_sender = min([base, *sender_views])
    crossed = (
        policy.view_trust(at_sender, sender, receiver)
        if policy.enabled
        else min(at_sender, constants.TRUST_RETRIEVED_CAP)
    )
    async with eng._write_locks.setdefault(receiver, asyncio.Lock()):
        return await eng._write_locked(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            storage, receiver, record, "episodic", actor, trust_cap=[crossed]
        )


async def approve(
    eng: Any,
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
    if actor not in ("user", "operator"):
        raise ValueError("only a user or an operator can approve")
    return await eng.write(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        f"{subject} {field}: {value}",
        namespace=namespace,
        memory_type="semantic",
        entity=subject,
        attribute=f"approval:{field}",
        source=SourceInfo(role=actor, channel="approval"),
        valid_from=eng._clock(),
        tags=[constants.APPROVAL_TAG]
        + ([f"{constants.APPROVAL_UNTIL_PREFIX}{valid_until.isoformat()}"] if valid_until else []),
    )


async def _approved(eng: Any, ns: str, subject: str, field: str, value: str) -> bool:
    """G29: a live, exactly matching approval exists for (subject, field, value)."""
    now = eng._clock()
    for record in await eng._records(ns, "semantic"):
        if (
            constants.APPROVAL_TAG in record.tags
            and record.status is RecordStatus.ACTIVATED
            and not record.quarantined
            and record.source.role in ("user", "operator")
            and record.entity == subject
            and record.attribute == f"approval:{field}"
            and record.content.split(":", 1)[-1].strip() == value.strip()
            and record.valid_from <= now
            and all(
                now < datetime.fromisoformat(t[len(constants.APPROVAL_UNTIL_PREFIX) :])
                for t in record.tags
                if t.startswith(constants.APPROVAL_UNTIL_PREFIX)
            )
        ):
            return True
    return False


async def authorize(
    eng: Any,
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
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    if approval is not None and not await eng._approved(ns, *approval):
        return AuthorizeDecision(False, 0.0, None, "no matching approval")
    grants: Mapping[str, frozenset[str] | None] = (
        await eng._shared.grants_to(ns) if eng._shared is not None else {}
    )
    policy = eng._integrity()
    if not evidence_ids:
        return AuthorizeDecision(False, 0.0, None, "no evidence")
    weakest: tuple[float, str] | None = None
    for evidence_id in evidence_ids:
        record = await storage.get_record(evidence_id)
        if (
            record is None
            or record.quarantined
            or record.status is not RecordStatus.ACTIVATED
            or not grant_allows(ns, record.namespace, record.memory_type, grants)
        ):
            return AuthorizeDecision(False, 0.0, evidence_id, "evidence unreadable or held")
        effective = await eng.effective_trust(record.record_id)
        view = (
            policy.view_trust(effective, record.namespace, ns)
            if policy.enabled
            else (
                effective
                if record.namespace == ns
                else min(effective, constants.TRUST_RETRIEVED_CAP)
            )
        )
        if weakest is None or view < weakest[0]:
            weakest = (view, evidence_id)
    assert weakest is not None
    allowed = weakest[0] >= threshold
    return AuthorizeDecision(
        allowed,
        weakest[0],
        weakest[1],
        "ok" if allowed else f"weakest evidence trust {weakest[0]:.3f} < {threshold}",
    )


def _integrity(eng: Any) -> IntegrityPolicy:
    return IntegrityPolicy.from_config(eng._config().integrity)


async def _parent_trust_cap(eng: Any, ns: str, parents: Sequence[str]) -> list[float] | None:
    """View trusts of ``parents`` for a writer in ``ns`` (MTI-D input).

    ``None`` when integrity is off or there are no parents — the write is
    then exactly the pre-MTI write. A parent the writer cannot read (missing,
    not live, or outside every grant) counts as 0.0: a caller must not be
    able to raise trust by naming records it never saw.
    """
    policy = eng._integrity()
    if not policy.enabled or not parents:
        return None
    storage = eng._require_started()
    grants: Mapping[str, frozenset[str] | None] = (
        await eng._shared.grants_to(ns) if eng._shared is not None else {}
    )
    views: list[float] = []
    for parent_id in parents:
        parent = await storage.get_record(parent_id)
        if (
            parent is None
            or parent.quarantined
            # R5-1: an archived or forgotten parent is not live evidence; it
            # counts like an unreadable one (as in effective_trust).
            or parent.status in (RecordStatus.ARCHIVED, RecordStatus.DELETED)
            or not grant_allows(ns, parent.namespace, parent.memory_type, grants)
        ):
            _log.warning("memory.integrity_unreadable_parent", namespace=ns, parent=parent_id)
            views.append(0.0)
            continue
        views.append(policy.view_trust(parent.trust, parent.namespace, ns))
    return views


async def vet(eng: Any, draft: str, namespace: str = "default") -> list[VetFlag]:
    """G26 (plan v3.2, TWIST): check a draft reply against the namespace's current
    keyed facts. Each draft sentence that names a fact's entity and attribute but
    not its current value, or negates the current value, is flagged with that
    fact and up to three superseded values (its history). Read-only, no model."""
    ns = validate_namespace(namespace)
    records = await eng._records(ns, "semantic")
    current = [
        r
        for r in records
        if r.status is RecordStatus.ACTIVATED
        and not r.quarantined
        and r.entity
        and r.attribute
        and r.valid_to is None
    ]
    history: dict[str, list[str]] = {}
    for fact in current:
        past = sorted(
            (
                r
                for r in records
                if r.entity == fact.entity
                and r.attribute == fact.attribute
                and r.record_id != fact.record_id
                and r.status is RecordStatus.ARCHIVED
            ),
            key=lambda r: r.valid_from,
            reverse=True,
        )
        history[fact.record_id] = [r.content for r in past[:3]]
    return vet_sentences(draft_sentences(draft), current, history)


async def _gated_assess(eng: Any, record: MemoryRecord) -> MemoryRecord:
    """The full firewall gate as an injectable seam (E1): resource ingest
    and reflections get the SAME anomaly context as Engine.write — a
    context-free assess would silently disable the embedding-outlier and
    MINJA defenses on exactly the RAG-poisoning surface."""
    verdict = await eng._assess_write(record)
    if verdict.quarantine:
        # Same loud signal as _write_locked — a quarantined ingest chunk or
        # reflection must never look like a normal write in the logs.
        _log.warning(
            "memory.quarantined",
            namespace=record.namespace,
            record_id=record.record_id,
            reasons=verdict.reasons,
        )
    return verdict.apply(record)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def _corroborate(eng: Any, namespace: str, incoming: MemoryRecord) -> None:
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
    storage = eng._require_started()
    integrity = eng._integrity()
    for held in await storage.list_quarantined(namespace):
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
        # B-1: an attribute-less record (a mined event fact) has no key, so
        # only a content-fingerprint match corroborates it; otherwise any
        # trusted write about the same person would count (None == None).
        # #3: a key match alone is not agreement. Two writes on the same key
        # with different values contradict each other; only a write that
        # states the same VALUE corroborates.
        same_fact = (
            held.memory_type == incoming.memory_type
            and held.entity is not None
            and held.attribute is not None
            and held.entity == incoming.entity
            and held.attribute == incoming.attribute
            and _fact_value(held.content) == _fact_value(incoming.content)
        )
        if not (same_content or same_fact):
            continue
        principal_bound = integrity.enabled and integrity.principal_bound_corroboration
        if principal_bound and not await eng._independent_principal(held, incoming):
            continue
        if eng._config().firewall.gates_v2 and not await gates_v2.release_ok(eng, held, incoming):
            continue  # W2-04 / S4-4: only an independent person, operator or system family counts
        by_roots = integrity.corroboration_roots
        if by_roots and not await eng._independent_roots(held, incoming):
            continue
        count = held.corroborations + 1
        change: dict[str, object] = {"corroborations": count}
        promoted = eng._firewall.policy.may_promote(
            held.model_copy(update={"corroborations": count})
        )
        if promoted:
            change.update(await eng._release_change(namespace, held))
        payload: dict[str, object] = {
            "record_id": held.record_id,
            "set": change,
            "transition": "quarantined->activated" if promoted else "corroborated",
            "reason": "quarantine_promoted" if promoted else "corroborated",
        }
        if principal_bound:
            # Durable record of who vouched: the next corroborator is checked
            # against it (replay-safe, no in-memory state).
            payload["principal"] = incoming.source.principal
        if by_roots:
            # W13: the roots that vouched, so the next corroborator is checked;
            # N25: and its wording, so a near-copy of it does not vouch again.
            payload["roots"] = sorted(await eng._lineage_roots(incoming))
            payload["words"] = sorted(content_words(incoming.content))
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload=payload,
            )
        )
        _log.info(
            "memory.quarantine_promoted" if promoted else "memory.corroborated",
            namespace=namespace,
            record_id=held.record_id,
            corroborations=count,
        )


async def _release_change(eng: Any, namespace: str, held: MemoryRecord) -> dict[str, object]:
    """The lifecycle delta that lifts ``held`` out of quarantine (corroboration
    promotion or an operator approval)."""
    storage = eng._require_started()
    change: dict[str, object] = {"quarantined": False}
    if held.memory_type == "procedural" and held.skill_stage is not None:
        # M13.4: release only lifts the quarantine; it must never skip the
        # ladder. The record resumes the status its stage implies (RESOLVING
        # pre-active), and still has to be promoted through verified + the
        # dry-run gate to surface.
        change["status"] = stage_status(held.skill_stage).value
    elif held.memory_type == "semantic":
        # If the corroborators themselves established an active fact on the
        # same key, the released record joins the history as its corroborated
        # predecessor, never a second active fact.
        incumbent = None
        if held.entity is not None and held.attribute is not None:
            incumbent = await storage.find_active_fact(namespace, held.entity, held.attribute)
        if incumbent is not None and incumbent.record_id != held.record_id:
            change["status"] = RecordStatus.ARCHIVED.value
            change["evolve_to"] = incumbent.record_id
            # Clamp so a held record newer than the incumbent never gets an
            # inverted (valid_to < valid_from) interval.
            close_at = max(held.valid_from, incumbent.valid_from)
            change["valid_to"] = close_at.isoformat()
        else:
            change["status"] = RecordStatus.ACTIVATED.value
    else:
        # Non-fact types (episodic, prospective watches, ...): the
        # single-active-fact invariant is semantic-only (ADR-016); a semantic
        # incumbent must never archive e.g. a watch that merely reuses the key
        # columns as its watched target.
        change["status"] = RecordStatus.ACTIVATED.value
    return change


async def review_evidence(
    eng: Any,
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
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    await eng._held_record(ns, record_id)
    policy = eng._firewall.policy
    tiers: list[int | None] = []
    for support_id in supporting_ids:
        support = await storage.get_record(support_id)
        if (
            support is not None
            and support.namespace == ns
            and support.status is RecordStatus.ACTIVATED
            and not support.quarantined
        ):
            tiers.append(policy.source_tier(support))
    contradicted = False
    for other_id in contradicting_ids:
        other = await storage.get_record(other_id)
        if other is not None and other.namespace == ns and not other.quarantined:
            contradicted = True
    verdict = policy.evidence_verdict(tiers, contradicted)
    if verdict == "promote":
        await eng.approve_quarantined(
            record_id, namespace=ns, actor="system", reason="evidence_promoted"
        )
    _log.info("memory.evidence_review", namespace=ns, record_id=record_id, verdict=verdict)
    return verdict  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def _inherit_governance(eng: Any, event: MemoryEvent) -> MemoryEvent:
    """#50: a derived record's purposes and PII tier follow its parents.

    A WRITE whose record names ``source.parents`` (summaries, mined facts, list
    cards, entity and community summaries, surprise facts, cues, reflections,
    graph facts) gets the intersection of its parents' purposes and its own
    (:func:`inherited_consent`) and the highest of their PII tiers. Only
    parents in the record's own namespace count (a foreign id is no oracle).
    The event is returned unchanged when nothing changes, so logs without
    tagged or PII-tiered parents stay byte-identical."""
    if event.kind is not EventKind.WRITE or eng._storage is None:
        return event
    snapshot = event.payload.get("record")
    if not isinstance(snapshot, dict):
        return event
    source = snapshot.get("source")
    raw_parents = source.get("parents") if isinstance(source, dict) else None
    if not raw_parents or not isinstance(raw_parents, list):
        return event
    own_id = snapshot.get("record_id")
    parents: list[MemoryRecord] = []
    for parent_id in dict.fromkeys(str(p) for p in raw_parents):
        if parent_id == own_id:
            continue
        parent = await eng._storage.get_record(parent_id)
        if parent is not None and parent.namespace == snapshot.get("namespace"):
            parents.append(parent)
    if not parents:
        return event
    own_tags = [str(t) for t in snapshot.get("consent_tags") or []]
    own_tier = str(snapshot.get("pii_tier") or PiiTier.NONE.value)
    tag_sets: list[Sequence[str]] = [p.consent_tags for p in parents]
    if own_tags:
        tag_sets.append(own_tags)
    tags = inherited_consent(tag_sets, eng._config().consent.untagged)
    tier = inherited_pii([own_tier, *(p.pii_tier for p in parents)]).value
    if set(tags) == set(own_tags) and tier == own_tier:
        return event
    payload = {
        **event.payload,
        "record": {
            **snapshot,
            "consent_tags": tags if set(tags) != set(own_tags) else own_tags,
            "pii_tier": tier,
        },
    }
    return event.model_copy(
        update={"payload": payload, "fingerprint": fingerprint_payload(payload)}
    )
