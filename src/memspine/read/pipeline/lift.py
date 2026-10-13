# ruff: noqa: E501
"""Read path: lift moved out of the engine (F-10b)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from memspine.core.owner_check import store_vocab
from memspine.core.read_filters import active_as_of, active_date_filter, active_record_scope
from memspine.models.encoders.query.perspective import (
    PerspectiveOptions,
    QuestionPerspective,
    acknowledges,
    active_asker,
    as_of_subject_ok,
    factor,
    match_subject,
    perspective_leg,
    record_view,
    refine_with_decider,
    resolve_question,
    resolve_write,
)
from memspine.models.encoders.query.shape import is_personal
from memspine.models.encoders.query.temporal_query import SPEAKER_PREFIX, LegHit, NamedLeg
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.read import lift as liblift
from memspine.read.lead import query_names
from memspine.read.lift import Candidate, Dropped, LiftPolicy
from memspine.read.pipeline.state import _FORENSICS
from memspine.schemas.durable.lineage import LineageEdge
from memspine.store.records import MemoryRecord, RecordStatus

_log = get_logger(__name__)


def _perspective_options(eng: Any) -> PerspectiveOptions:
    """I39: ``memories.episodic.policies.perspective`` (off unless set)."""
    return PerspectiveOptions.parse(
        eng._memory_policy(eng._config(), "episodic").get("perspective")
    )


async def _persp_state_for(eng: Any, ns: str) -> dict[str, Any]:
    """I39: the participants seen in ``ns`` (seeded once from the stored tags) and the
    speaker of the latest annotated turn."""
    state = eng._persp_state.get(ns)
    if state is None:
        known: set[str] = set(eng._perspective_options().speakers)  # I51
        acked: set[str] = set()  # I47: assistant records a later user turn acknowledged
        try:
            for r in await eng._records(ns):
                for tag in r.tags:
                    if tag.startswith(("spk:", "addr:")):
                        known.add(tag.split(":", 1)[1])
                    elif tag.startswith(SPEAKER_PREFIX):
                        known.add(tag[len(SPEAKER_PREFIX) :])
                    elif tag.startswith("ack:"):
                        acked.add(tag[4:])
        except Exception as exc:  # an enhancer, never a gate
            _log.warning("perspective.seed_failed", namespace=ns, error=str(exc))
        state = {"known": known, "last": None, "acked": acked, "assistant": None}
        eng._persp_state[ns] = state
    return state


async def _annotate_perspective(
    eng: Any,
    ns: str,
    content: str,
    role: str,
    tags: list[str] | None,
    opts: PerspectiveOptions,
    when: datetime | None = None,
) -> list[str]:
    """I39: the tags of ``content``'s perspective. Explicit ``spk:`` / ``addr:`` tags (from
    ``write_messages`` ``speaker`` / ``addressee``) beat the rules; a ``decider`` mode lets
    the decider port answer the typed stance questions (``is_fact`` ...) above its
    confidence floor. Never raises: a failure leaves the tags as they were."""
    try:
        state = await eng._persp_state_for(ns)
        explicit_spk = explicit_addr = None
        kept: list[str] = []
        for tag in tags or []:
            if tag.startswith("spk:"):
                explicit_spk = tag[4:]
            elif tag.startswith("addr:"):
                explicit_addr = tag[5:]
            else:
                kept.append(tag)
        p = resolve_write(
            content,
            speaker=explicit_spk,
            role=role,
            addressee=explicit_addr,
            known=state["known"],
            previous=state["last"],
            axes=opts.axes,
            when=when or eng._clock(),
        )
        read = eng._config().read
        if opts.mode == "decider" and read.decider != "heuristic":
            adapter = eng._decider_adapter()

            async def decide(task: str, text: str) -> tuple[str, float | None]:
                d = await adapter.decide(task, text, None)
                return d.label, d.confidence

            p = await refine_with_decider(
                p, content.split(":", 1)[-1], decide, read.decider_min_confidence
            )
        if p.spk:
            state["known"].add(p.spk)
            state["last"] = p.spk
        if p.addr:
            state["known"].add(p.addr)
        out = [*kept, *(t for t in p.tags(opts.axes) if t not in kept)]
        if opts.ack and p.spk != "assistant" and state.get("assistant") is not None:
            # I47: the next non-assistant turn after an assistant turn either
            # acknowledges it (the claim becomes user-confirmed) or leaves it proposed
            prev_id, prev_text = state["assistant"]
            state["assistant"] = None
            reply = re.sub(r"^\s*[\w'-]{1,30}(?: [\w'-]{1,30})?:\s+", "", content, count=1)
            said_yes = acknowledges(reply)
            if opts.mode == "decider" and read.decider != "heuristic":
                try:
                    d = await eng._decider_adapter().decide("acknowledges", reply, prev_text)
                    if d.confidence is not None and d.confidence >= read.decider_min_confidence:
                        said_yes = d.label == "ack"
                except Exception:  # the rules' answer stands
                    pass
            if said_yes:
                state["acked"].add(prev_id)
                out.append(f"ack:{prev_id}")
        return out
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("perspective.annotate_failed", namespace=ns, error=str(exc))
        return list(tags or [])


async def _question_perspective(eng: Any, ns: str, query: str) -> QuestionPerspective:
    """I39: who asks and who / what the question is about (see ``resolve_question``)."""
    state = await eng._persp_state_for(ns)
    return resolve_question(
        query,
        asker=eng._config().read.perspective_asker
        or active_asker()
        or eng._perspective_options().owner,
        known=state["known"],
    )


async def _entity_vocabulary(eng: Any, ns: str) -> frozenset[str]:
    records = [r for r in await eng._records(ns) if not r.quarantined]
    cached = eng._entity_vocab.get(ns)
    if cached is None or cached[0] != len(records):
        state = await eng._persp_state_for(ns)
        vocab = store_vocab((r.content for r in records), state["known"])
        cached = eng._entity_vocab[ns] = (len(records), vocab)
    return cached[1]


async def _perspective_leg(
    eng: Any, ns: str, qp: QuestionPerspective, vector_hits: Sequence[Any]
) -> list[list[LegHit]]:
    """``read.perspective_mode``: the subject vote leg (records about the question's
    subject, in the vector order)."""
    try:
        top_k = eng._config().read.list_vote_top_k
        records = [r for r in await eng._records(ns) if not r.quarantined]
        ids = perspective_leg(qp, records, [h.record_id for h in vector_hits], top_k)
        leg = NamedLeg("perspective", [LegHit(rid, 1.0) for rid in ids])
        return [leg] if leg else []
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.perspective_leg_failed", namespace=ns, error=str(exc))
        return []


def _apply_perspective(
    eng: Any,
    candidates: list[tuple[MemoryRecord, float]],
    qp: QuestionPerspective,
) -> list[tuple[MemoryRecord, float]]:
    """``read.perspective_mode``: reweight each candidate's relevance by the axes in
    ``read.perspective_axes`` (``subject_filter``: also drop candidates about someone
    else, keeping at least ``perspective_min_keep``). Records without perspective tags
    are neutral. The effect is recorded in the forensics as ``perspective``."""
    read = eng._config().read
    axes = set(read.perspective_axes)
    scored: list[tuple[MemoryRecord, float, bool]] = []
    factors: dict[str, dict[str, float]] = {}
    as_of = active_as_of()
    for rec, rel in candidates:
        rv = record_view(rec)
        state = eng._persp_state.get(rec.namespace)
        if state is not None and rec.record_id in state.get("acked", ()):
            rv = replace(rv, acked=True)  # I47: a user turn acknowledged this claim
        if (
            read.perspective_as_of_subject
            and as_of is not None
            and rec.status is not RecordStatus.ACTIVATED
            and qp.bound
            and not as_of_subject_ok(qp, rv)
        ):
            continue  # I42: another subject's history is not part of this point in time
        mult, parts = factor(qp, rv, read.perspective_weight, axes)
        if parts:
            factors[rec.record_id] = {k: round(v, 3) for k, v in parts.items()}
        about_other = (
            "subject" in axes and qp.bound and rv.annotated and match_subject(qp, rv) == 0.0
        ) or "ack" in parts
        scored.append((rec, rel * mult, about_other))
    out = sorted(scored, key=lambda t: t[1], reverse=True)
    dropped: list[str] = []
    if read.perspective_mode == "subject_filter":
        kept = [t for t in out if not t[2]]
        floor = min(max(read.perspective_min_keep, 0), len(out))
        if len(kept) < floor:
            kept += [t for t in out if t[2]][: floor - len(kept)]
            kept.sort(key=lambda t: t[1], reverse=True)
        dropped = [t[0].record_id for t in out if t not in kept]
        out = kept
    if (fx := _FORENSICS.get()) is not None:
        fx["perspective"] = {
            **qp.meta(),
            "mode": read.perspective_mode,
            "factors": factors,
            "dropped": dropped,
        }
    return [(rec, rel) for rec, rel, _ in out]


async def _applies_to_person(eng: Any, ns: str, query: str) -> bool:
    """W9 (``read.profile_scope_gate``): the question is about the asker, a choice
    they face, or a person memory knows (an entity, a ``person:`` tag or a
    ``speaker:`` tag); not general knowledge ("France" is not a known person)."""
    if is_personal(query):
        return True
    names = {n.lower() for n in query_names(query)}
    if not names:
        return False
    for record in await eng._records(ns):
        known = {t.split(":", 1)[1] for t in record.tags if t.startswith(("person:", "speaker:"))}
        if record.entity:
            known.add(record.entity.lower())
        if names & known:
            return True
    return False


# --- R3-08: lift derived hits to source turns before the rerank (LIB-11)
# ----------------------------


def _is_derived(record: MemoryRecord) -> bool:
    """A derived record: mined or consolidated from parents (``source.parents``), not itself a
    turn."""
    return bool(record.source.parents) and record.memory_type != "episodic"


async def lineage_from_records(
    eng: Any, derived: Sequence[MemoryRecord], max_depth: int
) -> tuple[dict[str, list[LineageEdge]], dict[str, MemoryRecord]]:
    """The LIB-11 lineage input from today's record links (adapter; there is no lineage table yet,
F-16a).

    A derived record points at its source turns through ``record.source.parents``. Each parent becomes
    one ``support`` edge (child -> parent). A parent that is itself derived (parents of its own) is
    followed up to ``max_depth`` levels. Returns ``(lineage, records)``: child id -> edges, and every
    parent record fetched. A parent that no longer exists yields an edge but no record: the library
    then treats it as a root, and the visibility check rejects it, which drops the derived hit (POL-2).
    """
    storage = eng._require_started()
    lineage: dict[str, list[LineageEdge]] = {}
    fetched: dict[str, MemoryRecord] = {}
    frontier = list(derived)
    for _ in range(max_depth):
        nxt: list[MemoryRecord] = []
        for rec in frontier:
            if rec.record_id in lineage:
                continue
            edges = [
                LineageEdge(child=rec.record_id, parent=pid, role="support")
                for pid in dict.fromkeys(rec.source.parents)
                if pid != rec.record_id
            ]
            lineage[rec.record_id] = edges
            for edge in edges:
                if edge.parent in fetched:
                    continue
                parent = await storage.get_record(edge.parent)
                if parent is None:
                    continue
                fetched[edge.parent] = parent
                if _is_derived(parent):
                    nxt.append(parent)
        frontier = nxt
    return lineage, fetched


async def lift_stage(
    eng: Any,
    ns: str,
    candidates: list[tuple[MemoryRecord, float]],
    protected: dict[str, str],
    *,
    group_id: str | None,
    tags: list[str] | None,
    memory_type: str | None,
    hide: Callable[[MemoryRecord], bool] | None,
    max_turns: int,
) -> list[tuple[MemoryRecord, float]]:
    """R3-08 (``read.lift_before_rerank``): lift, re-admit, dedupe, cap; the rerank runs after this.

    Derived hits are replaced by their visible source turns (a lifted turn inherits the hit's rank
    and relevance). Every lifted turn passes the read gates again (status, quarantine, consent,
    trust, the date/scope filters, the header ``hide``). The pool is capped at its incoming size, protected hits
    first. Returns ``(record, relevance)`` pairs in prior order.
    """
    derived = [rec for rec, _ in candidates if _is_derived(rec)]
    if not derived:
        return candidates
    max_depth = LiftPolicy().max_depth
    lineage, fetched = await lineage_from_records(eng, derived, max_depth)
    turn_ids = list(
        dict.fromkeys(e.parent for edges in lineage.values() for e in edges if e.parent not in lineage)
    )
    records: dict[str, MemoryRecord] = {rec.record_id: rec for rec, _ in candidates}
    visible: set[str] = set(records)
    if turn_ids:
        gated = await eng._gate_hits(ns, [(tid, 0.0) for tid in turn_ids], group_id, tags, memory_type)
        date_filter = active_date_filter()
        scope = active_record_scope()
        for rec, _ in gated:
            if date_filter is not None and not date_filter.matches(rec):
                continue
            if scope is not None and not scope.matches(rec):
                continue
            if hide is not None and hide(rec):
                continue
            records.setdefault(rec.record_id, rec)
            visible.add(rec.record_id)
    pol = LiftPolicy(is_turn=lambda rid: rid in fetched and fetched[rid].memory_type == "episodic")
    lib_in = [
        Candidate(
            record_id=rec.record_id,
            kind="fact" if _is_derived(rec) else "turn",
            rank=float(i),
            prior_score=score,
            protected=rec.record_id in protected,
            family=protected.get(rec.record_id),
        )
        for i, (rec, score) in enumerate(candidates)
    ]
    dropped: list[Dropped] = []
    lifted = liblift.lift(
        lib_in,
        lineage,
        lambda rid: rid in visible,
        max_turns_per_hit=max_turns,
        policy=pol,
        dropped=dropped,
    )
    admitted: list[Candidate] = []
    for c in lifted:  # ER-22: every expansion is admitted again
        if c.record_id in visible:
            admitted.append(c)
        else:
            dropped.append(Dropped(c.record_id, "not_visible_at_readmit", "readmit"))
    deduped = liblift.dedupe(admitted)
    capped = liblift.cap(deduped, len(candidates))
    kept = {c.record_id for c in capped}
    dropped.extend(Dropped(c.record_id, "over_cap", "cap") for c in deduped if c.record_id not in kept)
    if tap.enabled():
        tap.event(
            "read.lift.stage",
            n_in=len(candidates),
            derived=len(derived),
            lifted=[c.record_id for c in capped if c.lifted],
            out=[c.record_id for c in capped],
            dropped=[{"id": d.record_id, "reason": d.reason, "stage": d.stage} for d in dropped],
        )
    if (fx := _FORENSICS.get()) is not None:
        fx["lift"] = {
            "lifted": [c.record_id for c in capped if c.lifted],
            "dropped": [(d.record_id, d.reason) for d in dropped],
        }
    return [
        (records[c.record_id], c.prior_score if c.prior_score is not None else 0.0)
        for c in capped
        if c.record_id in records
    ]
