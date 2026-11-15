"""Derive: deposits of derived records (facts, cues, reflections, list cards) (F-15)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from memspine.config import constants
from memspine.core.event_date import SAID_PREFIX, happened_tag
from memspine.core.fact_views import view_tags
from memspine.derive import role_filter
from memspine.derive.rule_source_snapshot import rule_input_snapshot
from memspine.derive.write import write_derived
from memspine.exceptions import MemspineError
from memspine.models.encoders.query.perspective import inherit_perspective_tags
from memspine.models.encoders.query.temporal_resolve import resolve
from memspine.observability import tap
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordFingerprint, RecordStatus, SourceInfo
from memspine.store.scope import validate_namespace
from memspine.store.source_binding import source_binding
from memspine.workers.list_cards import LIST_CARD_KEY_PREFIX, ListCard

#: The anticipated-question cue tag (the value the read and ingest paths share).
CUE_TAG = constants.CUE_TAG


# _fact_value and _REPLACED_CONTENTS live in trust.pipeline.state (F-12); one shared ContextVar.
from memspine.trust.pipeline.state import _REPLACED_CONTENTS  # noqa: E402


async def add_cues(
    eng: Any,
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
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    target = await storage.get_record(record_id)
    if require_live and (target is None or target.namespace != ns):
        await eng._derived_parents_gone(ns, [record_id], "anticipate")
        return []
    if target is None or target.namespace != ns:
        raise MemspineError(f"cue target {record_id!r} not found in namespace {ns!r}")
    # R2-4: cue text is LLM- or caller-authored, so the default role is the
    # non-privileged "assistant" (never "system"): the firewall's instruction
    # and protected-key checks apply to it in full.
    base = source or SourceInfo(role="assistant", channel="anticipation")
    # R2-11: a cue on an assistant claim is itself only an assistant claim.
    claim = ["assistant_claim"] if "assistant_claim" in target.tags else []
    written: list[MemoryRecord] = []
    for cue in cues:
        record = MemoryRecord(
            namespace=ns,
            memory_type="semantic",
            content=cue,
            source=base.model_copy(update={"parents": [record_id]}),
            tags=list(dict.fromkeys([CUE_TAG, *extra_tags, *claim])),
        )
        cap = [target.trust]
        integrity_cap = await eng._parent_trust_cap(ns, [record_id])
        if integrity_cap:
            cap = [*cap, *integrity_cap]
        async with eng._write_locks.setdefault(ns, asyncio.Lock()):
            if require_live and await eng._derived_parents_gone(ns, [record_id], "anticipate"):
                break  # I72: the target was forgotten while the cues were authored
            written.append(
                await write_derived(
                    eng,
                    storage,
                    ns,
                    record,
                    "semantic",
                    actor,
                    parents=[record_id],
                    trust_cap=cap,
                    site="anticipate",
                )
            )
    if eng._config().read.query_encoder != "none":
        for cue_record in written:
            eng._encoder().observe(cue_record)  # #61: the write-time index
    return written


async def _deposit_surprise_fact(
    eng: Any,
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
    """#62: one predict-calibrate surprise through the write door.

    Like a mined fact (C6'): LLM-authored, so the non-privileged ``assistant``
    role; trust capped at the least-trusted turn; an event drops its attribute
    so it is ADDed beside the person's other events. Tagged ``surprise_fact``
    and ``calibrated:<session key>`` (the session it came from; the stage's done check is its
    ``stage_done`` marker)."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    sources = [r for r in [await storage.get_record(p) for p in parents] if r is not None]
    tags = [constants.SURPRISE_FACT_TAG, f"calibrated:{session_key}"]
    if kind is not None:
        tags.append(f"kind:{kind}")
        if kind != "state" and f"{entity}.{attribute}" not in (
            eng._config().firewall.protected_keys
        ):
            attribute = None
    if sources and all("assistant_claim" in r.tags for r in sources):
        tags.append("assistant_claim")  # R2-11
    record = MemoryRecord(
        namespace=ns,
        memory_type="semantic",
        content=text,
        source=SourceInfo(role="assistant", channel="calibration", parents=list(parents)),
        entity=entity,
        attribute=attribute,
        valid_from=valid_from,
        tags=tags,
    )
    cap = [min(r.trust for r in sources)] if sources else None
    integrity_cap = await eng._parent_trust_cap(ns, parents)
    if integrity_cap:
        cap = [*(cap or []), *integrity_cap]
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if await eng._derived_parents_gone(ns, parents, "predict_calibrate"):  # I72
            return None
        return await write_derived(
            eng,
            storage,
            ns,
            record,
            "semantic",
            "system",
            trust_cap=cap,
            site="predict_calibrate",
        )


async def _deposit_profile_reflection(
    eng: Any, namespace: str, content: str, evidence_ids: list[str], session_key: str
) -> MemoryRecord | None:
    """H14: a profile insight through the governed ``reflect`` door."""
    # R2-4: the insight is LLM-authored: non-privileged role, trust capped.
    return await eng._reflect(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        content,
        evidence_ids,
        namespace=namespace,
        actor="system",
        source=SourceInfo(
            role="assistant", channel="reflection", message_id=f"reflected:{session_key}"
        ),
        require_live=True,
    )


async def _deposit_anticipated_cues(
    eng: Any, namespace: str, record_id: str, cues: list[str], session_key: str
) -> list[MemoryRecord]:
    """H8: anticipated cues through the governed ``add_cues`` door (system source)."""
    return await eng.add_cues(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        record_id,
        cues,
        namespace=namespace,
        source=SourceInfo(role="assistant", channel="anticipation"),
        actor="system",
        extra_tags=[f"anticipated:{session_key}"],
        require_live=True,
    )


async def _pinned_evidence_current(
    eng: Any,
    storage: Any,
    ns: str,
    parents: Sequence[str],
    envelope: dict[str, Any],
) -> list[MemoryRecord] | None:
    """Fence every cited source snapshot immediately before an envelope deposit.

    Rules envelopes carry system-authored full rule-input snapshots for all cited turns.
    Every envelope must still cite each parent at its current version and keep its exact
    quoted span. The caller holds the namespace write lock through the following write.
    """
    parent_ids = set(parents)
    if not parent_ids or len(parent_ids) != len(parents):
        return None
    producer = envelope.get("producer")
    rules = isinstance(producer, dict) and producer.get("kind") == "rules"
    registry = envelope.get("registry")
    reply_origin = (
        rules
        and len(parent_ids) > 1
        and isinstance(registry, dict)
        and registry.get("predicate") == "move_origin"
    )
    extras = envelope.get("extras")
    hashes = extras.get("rule_input_snapshots") if isinstance(extras, dict) else None
    if rules and (
        not isinstance(hashes, dict)
        or set(hashes) != parent_ids
        or any(not isinstance(value, str) for value in hashes.values())
    ):
        return None
    evidence = envelope.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return None
    role_bindings = envelope.get("role_bindings", [])
    if not isinstance(role_bindings, list):
        return None
    cited = list(evidence)
    for binding in role_bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get("evidence"), list):
            return None
        cited.extend(binding["evidence"])
    current: dict[str, MemoryRecord] = {}
    for source_id in parent_ids:
        source = await storage.get_record(source_id)
        if source is None:
            return None
        source = eng._inflate.inflate(source)
        if (
            source.namespace != ns
            or source.status is not RecordStatus.ACTIVATED
            or source.quarantined
            or source.instruction_flag
        ):
            return None
        if rules:
            assert isinstance(hashes, dict)
            if rule_input_snapshot(source) != hashes[source_id]:
                return None
            if reply_origin and (
                source.source.role != "user" or role_filter.block_reason(source.tags) is not None
            ):
                return None
        current[source_id] = source
    seen: set[str] = set()
    for item in cited:
        if not isinstance(item, dict):
            return None
        turn = item.get("turn")
        if turn is None and item.get("role") == "context":
            continue
        if not isinstance(turn, dict):
            return None
        cited_source_id = turn.get("source_id")
        version = turn.get("source_version")
        source = current.get(cited_source_id) if isinstance(cited_source_id, str) else None
        if source is None or type(version) is not int or version != source.version:
            return None
        seen.add(source.record_id)
        span = item.get("span")
        if span is None:
            continue
        if not isinstance(span, dict):
            return None
        start, end, quote = span.get("start"), span.get("end"), span.get("quote")
        if (
            type(start) is not int
            or type(end) is not int
            or not (0 <= start < end <= len(source.content))
            or not isinstance(quote, str)
            or source.content[start:end] != quote
        ):
            return None
    if seen != parent_ids:
        return None
    if reply_origin:
        link = extras.get("rule_reply_link") if isinstance(extras, dict) else None
        if not isinstance(link, dict):
            return None
        reply_id = link.get("reply_record")
        parent_id = link.get("parent_record")
        message_id = link.get("parent_message")
        if (
            not isinstance(reply_id, str)
            or not isinstance(parent_id, str)
            or not isinstance(message_id, str)
            or not message_id
            or reply_id == parent_id
            or set((reply_id, parent_id)) != parent_ids
        ):
            return None
        reply, parent = current[reply_id], current[parent_id]
        reply_message, parent_message = reply.source.message, parent.source.message
        if (
            not isinstance(reply_message, dict)
            or not isinstance(parent_message, dict)
            or reply_message.get("reply_to") != message_id
            or parent_message.get("message_id") != message_id
        ):
            return None
        tags = [
            tag[len(constants.REPLY_TO_PREFIX) :]
            for tag in reply.tags
            if tag.startswith(constants.REPLY_TO_PREFIX)
        ]
        if tags != [parent_id]:
            return None
        if not all(
            any(
                isinstance(item, dict)
                and item.get("role") == role
                and isinstance(item.get("turn"), dict)
                and item["turn"].get("source_id") == source_id
                for item in evidence
            )
            for role, source_id in (("support", reply_id), ("binding", parent_id))
        ):
            return None
    binding_hash = envelope.get("source_binding_hash")
    if binding_hash is not None:
        provenance = envelope.get("provenance")
        primary_source_id = provenance.get("source_id") if isinstance(provenance, dict) else None
        primary = current.get(primary_source_id) if isinstance(primary_source_id, str) else None
        # A model window may use a synthetic provenance.source_id for the window;
        # its binding hash still names a cited source line. Rules candidates use
        # the actual support source id. In either case the hash must match a
        # freshly loaded, evidence-pinned parent.
        candidates = [primary] if primary is not None else list(current.values())
        if not any(source_binding(source).binding_hash == binding_hash for source in candidates):
            return None
    return list(current.values())


async def _deposit_mined_fact(
    eng: Any,
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
    supersedes: str | None = None,
) -> MemoryRecord | None:
    """C6': one mined fact through the write door (firewall, ladder, MTI).

    RM-22 / RM-54: ``supersedes`` is the record id of the prior claim this one replaces (a
    resolved correction by the same sender) or closes (a cancelled commitment). Under the same
    lock, before the write, the prior claim is archived with claim status ``superseded`` and
    ``evolve_to`` the new record (it stays readable as known history); a prior claim that is no
    longer live is left alone and the fact is written without superseding anything.

    FU-GAPS: ``envelope`` (the extraction envelope) is the compiled envelope as JSON; it is kept in
    the record's fingerprint blob (``fingerprint.envelope``), no migration.

    W2-06a: ``trust_cap`` (envelope) is the envelope's system-set source trust; it joins the
    parents' cap, so a stored fact is never more trusted than its cited line's role allows.

    E01: ``extra_tags`` ride along (the write-time projection tags its facts
    ``fact_projection`` / ``projected:<session>``); nothing else about the path changes.

    W1: ``require_live`` are more record ids (the other turns of the window the fact was
    read from) that must still be live under the lock, besides the parents; one gone
    and the fact is not written (I72). They are checked, not recorded as parents.

    #28: ``persons`` / ``location`` / ``topic`` (``consolidation.mine_multiview``)
    are tagged ``person:<name>``, ``loc:<place>``, ``topic:<class>`` (normalised,
    :func:`~memspine.core.fact_views.view_tags`); the statement is kept as mined.

    #29: ``happened`` (the fact's happened date) is tagged ``happened:<date>``,
    and ``said`` (the day its relative phrases were resolved against) ``said:<date>``,
    so a read resolves them against that day, not the moved ``valid_from``.

    G1a: ``kind="event"`` drops the attribute, so the fact is ADDed beside the
    person's other events instead of superseding them; ``kind="state"`` keeps
    the (entity, attribute) key. The kind is tagged ``kind:<kind>``. None
    (an unclassified caller) keeps the attribute as given.

    Trust is capped at the least-trusted source turn even with integrity
    off, exactly like a consolidation summary: derived content is never
    more trusted than what it was derived from (E1).
    """
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    sources = [r for r in [await storage.get_record(p) for p in parents] if r is not None]
    planned = attribute == "plans"  # G33: before an event's attribute is dropped
    tags = ["atomic_fact", f"mined:{session_key}"]
    if happened:
        tags.append(happened_tag(happened))
        if said:
            tags.append(f"{SAID_PREFIX}{said}")
    tags.extend(t for t in view_tags(persons or [], location, topic) if t not in tags)
    tags.extend(t for t in extra_tags if t not in tags)
    if kind is not None:
        tags.append(f"kind:{kind}")
        protected = eng._config().firewall.protected_keys
        if kind != "state" and f"{entity}.{attribute}" not in protected:
            # A protected (entity, attribute) key keeps its attribute whatever
            # the miner called it, so "kind: event" cannot dodge the check.
            attribute = None
    if sources and all("assistant_claim" in r.tags for r in sources):
        # R2-11: a fact mined only from assistant turns stays an assistant claim.
        tags.append("assistant_claim")
    if sources and eng._perspective_options().on:
        # I55: the fact is about what its source turn(s) are about, so the conflict
        # perspective_key (subject / scope / polarity) works on it too.
        tags.extend(t for t in inherit_perspective_tags(sources) if t not in tags)
    record = MemoryRecord(
        namespace=ns,
        memory_type="semantic",
        content=text,
        # R2-4: LLM-authored, so the non-privileged "assistant" role: the
        # protected-key, size and instruction checks apply (a "system" role
        # skipped them, and could even corroborate quarantined records).
        source=SourceInfo(role="assistant", channel="mining", parents=list(parents)),
        entity=entity,
        attribute=attribute,
        valid_from=valid_from,
        tags=tags,
        **({"fingerprint": RecordFingerprint(envelope=envelope)} if envelope is not None else {}),  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    )
    cap = [min(r.trust for r in sources)] if sources else None
    integrity_cap = await eng._parent_trust_cap(ns, parents)
    if integrity_cap:
        cap = [*(cap or []), *integrity_cap]
    if trust_cap is not None:
        cap = [*(cap or []), trust_cap]
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        site = "window_extract" if require_live else "mine_facts"
        if await eng._derived_parents_gone(ns, [*parents, *require_live], site):  # I72
            return None
        if envelope is not None:
            current_sources = await _pinned_evidence_current(eng, storage, ns, parents, envelope)
            if current_sources is None:
                tap.event("write.extract.rejected", reason="stale_evidence", site=site)
                return None
            cap = [*(cap or []), min(source.trust for source in current_sources)]
        evidence_roles: dict[str, str] = {}
        if envelope is not None:
            for evidence in envelope.get("evidence", []):
                if not isinstance(evidence, dict):
                    continue
                turn = evidence.get("turn")
                if not isinstance(turn, dict):
                    continue
                source_id, role = turn.get("source_id"), evidence.get("role")
                if source_id in parents and role in ("support", "binding", "context"):
                    evidence_roles[str(source_id)] = str(role)
        if supersedes is not None:
            await _close_superseded(eng, storage, ns, supersedes, record)
        written = await write_derived(
            eng,
            storage,
            ns,
            record,
            "semantic",
            "system",
            trust_cap=cap,
            site=site,
            roles=evidence_roles or None,
        )
    if planned and eng._consolidation_option("auto_watch", False):
        await eng._auto_watch(ns, written, sources)
    return written


async def _close_superseded(
    eng: Any, storage: Any, ns: str, target_id: str, successor: MemoryRecord
) -> bool:
    """RM-22 / RM-54 (lock held): archive the live prior claim ``target_id`` as superseded by
    ``successor``. Its canonical envelope keeps every field but ``status`` (``superseded``);
    the record keeps its text and evidence, so a known-time read before the change still finds
    it. False (nothing written) when the target is not a live claim of ``ns``."""
    from memspine.trust.pipeline.erasure_state import visible_id

    target = await storage.get_record(target_id)
    if (
        target is None
        or target.namespace != ns
        or target.status is not RecordStatus.ACTIVATED
        or target.quarantined
        or not target.fingerprint.envelope
        or not visible_id(eng, target_id)
    ):
        return False
    now = eng._clock()
    envelope = dict(target.fingerprint.envelope)
    proof = target.fingerprint.restatement_support
    proof_is_valid = False
    if proof is not None:
        from memspine.trust.restatement import valid_proof

        proof_is_valid = bool(valid_proof(target))
    envelope["status"] = "superseded"
    fingerprint_updates: dict[str, Any] = {"envelope": envelope}
    if proof is not None:
        if proof_is_valid:
            from memspine.trust.restatement import claim_digest

            digest = claim_digest(envelope)
            if digest is not None:
                fingerprint_updates["restatement_support"] = proof.model_copy(
                    update={"claim_digest": digest}
                )
            else:
                fingerprint_updates["restatement_support"] = None
        else:
            # An invalid pre-transition proof must never become valid by matching the
            # system's superseded envelope digest after this lifecycle change.
            fingerprint_updates["restatement_support"] = None
    closed = target.model_copy(
        update={
            "valid_to": successor.valid_from,
            "superseded_at": now,
            "status": RecordStatus.ARCHIVED,
            "evolve_to": successor.record_id,
            "tags": [t for t in target.tags if t != "disputed"],
            "fingerprint": target.fingerprint.model_copy(update=fingerprint_updates),
        }
    )
    await eng._append_and_project(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor="system",
            payload={
                "record": closed.model_dump(mode="json"),
                "correction": {
                    "supersedes": target_id,
                    "actor": "system",
                    "reason": "rule_claim_change",
                },
            },
        )
    )
    if eng._semantic is not None:
        eng._semantic.invalidate_index(ns)
    return True


async def _auto_watch(
    eng: Any, ns: str, plan: MemoryRecord, sources: list[MemoryRecord]
) -> MemoryRecord | None:
    """G33 (plan v3.2, ``consolidation.auto_watch``): a mined plan whose source turn
    names a future time ("next week", "in two months", "on 3 June") becomes a
    prospective watch due then (H1 rules against the turn's own date). Needs the
    prospective memory type; a plan without a resolvable future date gets none."""
    if eng._prospective is None or plan.quarantined:
        return None
    for source in sources:
        said = source.valid_from
        for found in resolve(source.content, said, week=eng._config().read.relative_week):
            due = datetime(found.first.year, found.first.month, found.first.day, tzinfo=UTC)
            if due.date() > said.date():
                return await eng.watch(plan.content, namespace=ns, due_at=due, actor="system")  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
    return None


async def _deposit_list_card(
    eng: Any, namespace: str, card: ListCard | None, replaces: list[str]
) -> MemoryRecord | None:
    """#30: archive the cards ``card`` replaces, then write it through the door.

    Under the namespace lock, a card whose facts are no longer all live (a forget
    raced the step) is not written and nothing is archived: the next cycle
    re-derives it. The card is LLM-free derived content with the non-privileged
    ``assistant`` role, its trust capped at its least trusted fact (E1) and, under
    integrity, at the facts' view trust. ``card=None`` only archives.
    """
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        sources: list[MemoryRecord] = []
        if card is not None:
            for parent_id in card.parents:
                parent = await storage.get_record(parent_id)
                if (
                    parent is None
                    or parent.namespace != ns
                    or parent.status is not RecordStatus.ACTIVATED
                    or parent.quarantined
                ):
                    await eng._derived_parents_gone(ns, card.parents, "list_cards")  # I72
                    return None
                sources.append(parent)
        for record_id in replaces:
            old = await storage.get_record(record_id)
            if (
                old is None
                or constants.LIST_CARD_TAG not in old.tags
                or old.status is not RecordStatus.ACTIVATED
            ):
                continue
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.DECAY_TRANSITION,
                    namespace=ns,
                    actor="system",
                    payload={
                        "record_id": record_id,
                        "set": {
                            "status": RecordStatus.ARCHIVED.value,
                            "superseded_at": datetime.now(UTC).isoformat(),
                        },
                        "transition": "list_card->superseded",
                        "reason": "list_card_rederived",
                    },
                )
            )
        if card is None:
            return None
        record = MemoryRecord(
            namespace=ns,
            memory_type="semantic",
            content=card.text,
            source=SourceInfo(role="assistant", channel="list_card", parents=list(card.parents)),
            entity=card.person,
            attribute=None,
            valid_from=card.valid_from,
            tags=list(card.tags),
        )
        if sources and all("assistant_claim" in r.tags for r in sources):
            record.tags.append("assistant_claim")  # R2-11: every fact is a claim
        cap = [min(r.trust for r in sources)] if sources else None
        integrity_cap = await eng._parent_trust_cap(ns, card.parents)
        if integrity_cap:
            cap = [*(cap or []), *integrity_cap]
        # Every earlier version of this group's card (any status) repeats its prefix.
        group = {t for t in card.tags if t.startswith(LIST_CARD_KEY_PREFIX)}
        replaced = frozenset(
            r.content
            for r in await storage.list_records(ns, "semantic")
            if constants.LIST_CARD_TAG in r.tags and group & set(r.tags)
        )
        token = _REPLACED_CONTENTS.set(replaced)
        try:
            return await write_derived(
                eng, storage, ns, record, "semantic", "system", trust_cap=cap, site="list_card"
            )
        finally:
            _REPLACED_CONTENTS.reset(token)


async def _write_extracted_fact(
    eng: Any, record: MemoryRecord, trust_cap: list[float]
) -> MemoryRecord:
    """C2: one ``extract_graph`` fact through the semantic door (lock held by the
    pipeline): firewall (N2) with the source's trust as cap (plus the MTI parent
    view under integrity), M5 dedup and the M4 ladder, so a background ``state``
    edge supersedes the older value exactly like a write-time (C3) one."""
    storage = eng._require_started()
    cap = list(trust_cap)
    integrity_cap = await eng._parent_trust_cap(record.namespace, record.source.parents)
    if integrity_cap:
        cap.extend(integrity_cap)
    # OW-C: the opt-in L4/L5 stages (observations, standing answers) share this door
    channel = record.source.channel
    derived_site = channel if channel in ("observation", "standing") else None
    derived_layer = {"observation": "L4", "standing": "L5"}.get(channel)
    return await write_derived(
        eng,
        storage,
        record.namespace,
        record,
        "semantic",
        "system",
        trust_cap=cap,
        site=derived_site or "extract_graph",
        layer=derived_layer,
    )


async def _session_record_exists(storage: Any, ns: str, idem_tag: str) -> bool:
    """True when a live session_close record already carries this idempotency tag."""
    for r in await storage.list_records(ns, "semantic"):
        if idem_tag in r.tags and r.status is RecordStatus.ACTIVATED:
            return True
    return False


async def _deposit_session_record(
    eng: Any,
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
    """O4-02: one call-2 record (session summary or stitched episode) through the derived door.

    Derived (non-privileged ``assistant`` role, channel ``session_close``), parents = the cited
    turns (an extension also names the stub it extends, role ``version_of``), trust capped at
    the least trusted parent, screened by the firewall, never keyed (the ladder ADDs it). Skipped
    under the lock when a parent is no longer live (I72)."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    sources = [r for r in [await storage.get_record(p) for p in parents] if r is not None]
    tags = [f"kind:{kind}", "session_close", f"session:{session_id}"]
    if idem_key:
        tags.append(f"sc:{idem_key[:24]}")
    if extends:
        tags.append(f"extends:{extends}")
    if when_text:
        tags.append(f"when:{when_text[:60]}")
    if folded:
        tags.append("folded")
    if sources and all("assistant_claim" in r.tags for r in sources):
        tags.append("assistant_claim")
    if sources and eng._perspective_options().on:
        tags.extend(t for t in inherit_perspective_tags(sources) if t not in tags)
    parent_ids = list(dict.fromkeys([*parents, *([extends] if extends else [])]))
    record = MemoryRecord(
        namespace=ns,
        memory_type="semantic",
        content=text,
        source=SourceInfo(
            role=constants.DERIVED_ROLE,
            channel=constants.SESSION_CLOSE_CHANNEL,
            message_id=f"session_close:{session_id}",
            parents=parent_ids,
        ),
        valid_from=valid_from,
        tags=tags,
    )
    record = record.model_copy(
        update={
            "axes": record.axes.model_copy(
                update={"participants": list(dict.fromkeys(p for p in participants if p))}
            )
        }
    )
    cap = [min(r.trust for r in sources)] if sources else None
    integrity_cap = await eng._parent_trust_cap(ns, parents)
    if integrity_cap:
        cap = [*(cap or []), *integrity_cap]
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if await eng._derived_parents_gone(ns, parents, "session_close"):
            return None
        if idem_key and await _session_record_exists(storage, ns, f"sc:{idem_key[:24]}"):
            if tap.enabled():
                tap.event("job.close.idem", kind=kind, idem=idem_key[:24])
            return None
        # FU-CLOSE: door type "summary" (plain WRITE route, as OW-D's summaries): the semantic
        # M5 dedup merged a stitched extension (stub text + an addition) back into its stub and
        # lost the addition. Idempotency is the ``sc:`` tag check above, not dedup.
        written = await write_derived(
            eng,
            storage,
            ns,
            record,
            "summary",
            "system",
            parents=parent_ids,
            roles={extends: "version_of"} if extends else None,
            layer="L2",  # MF-01: both close legs (episode, summary) are L2
            trust_cap=cap,
            site="session_close",
        )
    if written.record_id != record.record_id:  # M5 merged it into an existing near-duplicate
        if tap.enabled():
            tap.event("job.close.dedup", kind=kind, into=written.record_id)
        return None
    return written


async def _write_offline_summary(
    eng: Any,
    namespace: str,
    record: MemoryRecord,
    parents: Sequence[str],
    layer: str,
    site: str,
    trust_cap: list[float],
) -> MemoryRecord:
    """O4-06 / O4-09: one community, entity or multi-resolution summary through the derived-write
    door (lock held by the stage): lineage and trust cap with ``write.lineage_table``, firewall
    screening, and the integrity parent view under ``integrity.enabled``.

    The door type is ``"summary"``, not ``"semantic"``: the record stays a semantic record, but it
    takes the plain WRITE route. The semantic pipeline would dedup-merge a summary into a member it
    restates and run the fact ladder on it; consolidation summaries skip both today."""
    storage = eng._require_started()
    cap = list(trust_cap)
    integrity_cap = await eng._parent_trust_cap(namespace, list(parents))
    if integrity_cap:
        cap.extend(integrity_cap)
    return await write_derived(
        eng,
        storage,
        namespace,
        record,
        "summary",
        "system",
        parents=list(parents),
        layer=layer,
        trust_cap=cap,
        site=site,
    )
