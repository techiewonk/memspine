"""Derive stage: standing_answers, and the read-side block (O4-08, LIB-32; opt-in L5).

Sleep side (``offline.standing``): precomputed answers for the current value of each keyed state fact,
built from value histories, rebuilt only for the groups that changed, evicted by usefulness.
Read side (``read.standing``): :func:`standing_block` picks the fresh answers for the topic entities of a
read (``select_for_context``) and the guarded standing instructions, as one lead block.

An answer is a derived semantic record (channel ``standing``): its text is ``render_answer``; the question
key, dependency fingerprint, sequence, contested flag, trust and values ride as tags; its parents are the
supporting facts. Answers are caches: losing one costs a rebuild, never a fact.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

from memspine.config import constants
from memspine.core.escaping import escape_markers
from memspine.derive import standing as lib
from memspine.derive.fact_policy import FactPurpose
from memspine.derive.histories import value_history
from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    _parents_gone,
    live_records,
    sweep_namespaces,
)
from memspine.derive.pipeline.stages.offline_common import (
    STANDING_CHANNEL,
    archive_records,
    fact_envelope,
    is_input_fact,
    short_hash,
    split_statement,
    tag_value,
)
from memspine.derive.resolve import normalise_name
from memspine.observability import tap
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo

__all__ = [
    "HIT_LOG",
    "STANDING_TAG",
    "answer_from_record",
    "guarded_instructions",
    "standing_answers",
    "standing_block",
]

STANDING_TAG = "standing_answer"
ANSWERS_HEADER = (
    "STANDING ANSWERS (precomputed from the stored facts; the evidence below is authoritative)"
)
INSTRUCTIONS_HEADER = "STANDING INSTRUCTIONS (stated by the user)"

#: Process-local read log the sleep stage drains for usefulness (hits and stale skips). Lost on restart,
#: which only means an answer is rebuilt later; it is never a source of facts.
HIT_LOG: deque[lib.HitEvent] = deque(maxlen=5000)


# --------------------------------------------------------------------------- record <-> answer


def _live_hash(records: list[MemoryRecord], subject: str, attribute: str) -> str:
    """Hash of the ids of the live state facts of one group (subject, attribute normalised)."""
    ids = sorted(
        r.record_id
        for r in records
        if r.status in (RecordStatus.ACTIVATED, RecordStatus.RESOLVING)
        and is_input_fact(r, FactPurpose.STANDING)
        and "kind:state" in r.tags
        and normalise_name(r.entity or "") == subject
        and normalise_name(split_statement(r)[0]) == attribute
    )
    return short_hash(*ids)


def answer_from_record(r: MemoryRecord) -> lib.StandingAnswer | None:
    """The standing answer a stored record encodes, or ``None`` when its tags are not ours."""
    canonical = tag_value(r.tags, "saq:")
    fp = tag_value(r.tags, "saf:")
    if canonical is None or fp is None:
        return None
    parts = canonical.split("|")
    if len(parts) != 6:
        return None
    tenant, owner, subject, attribute, view, at = parts
    try:
        key = lib.QuestionKey(
            tenant,
            owner,
            subject,
            attribute,
            view,  # type: ignore[arg-type]
            datetime.fromisoformat(at) if at else None,
        )
    except ValueError:
        return None
    values = tuple(t[len("sav:") :] for t in r.tags if t.startswith("sav:"))
    return lib.StandingAnswer(
        id=key.answer_id,
        key=key,
        values=values,
        support_ids=tuple(r.source.parents),
        groups=(key.group,),
        fingerprint=fp,
        computed_at_seq=int(tag_value(r.tags, "sas:") or 0),
        contested="sac:1" in r.tags,
        trust=float(tag_value(r.tags, "sat:") or 0.0),
    )


def _answer_tags(a: lib.StandingAnswer, live_hash: str) -> list[str]:
    tags = [
        STANDING_TAG,
        f"sa:{a.id}",
        f"saq:{a.key.canonical}",
        f"saf:{a.fingerprint}",
        f"sas:{a.computed_at_seq}",
        f"sar:{live_hash}",
        f"sat:{a.trust:.6f}",
    ]
    if a.contested:
        tags.append("sac:1")
    tags.extend(f"sav:{v}" for v in a.values)
    return tags


# --------------------------------------------------------------------------- sleep stage


def _state_envelopes(records: list[MemoryRecord]) -> list[Any]:
    return [
        e
        for r in records
        if is_input_fact(r, FactPurpose.STANDING)
        and "kind:state" in r.tags
        and (e := fact_envelope(r)) is not None
    ]


async def standing_answers(ctx: PipelineContext) -> dict[str, object]:
    """O4-08: build, refresh and evict the standing answers of each namespace, once per sleep.

    Per namespace, under the write lock (no model call): the keyed state facts are read as assertions and
    projected into value histories; each stored answer whose dependency fingerprint no longer matches is
    invalidated by group (``invalidate``) and rebuilt (``refresh``); new groups get an answer up to
    ``offline.standing_max_answers``; ``eviction_candidates`` drops the idle or surplus ones. Records are
    written through the derived-write door (parents = the supporting facts, trust capped at the weakest).
    """
    if not ctx.config.offline.standing:
        return {"status": "skipped", "reason": "offline.standing is off"}
    if ctx.append_event is None or ctx.write_fact is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    cfg = ctx.config.offline
    hit_log = list(HIT_LOG)
    built = stale = evicted = emptied = skipped_parents = 0
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        async with ctx.lock(namespace):
            records = await live_records(ctx, namespace, "semantic")
            by_record = {r.record_id: r for r in records}
            stored: dict[str, MemoryRecord] = {}
            retire: dict[str, str] = {}  # record id -> reason
            for r in sorted(records, key=lambda x: x.record_id):
                if (
                    r.source.channel != STANDING_CHANNEL
                    or r.status is not RecordStatus.ACTIVATED
                    or answer_from_record(r) is None
                ):
                    continue
                a = answer_from_record(r)
                assert a is not None
                if a.id in stored:
                    retire[r.record_id] = "duplicate"
                else:
                    stored[a.id] = r
            existing = [a for r in stored.values() if (a := answer_from_record(r)) is not None]
            seq = max((a.computed_at_seq for a in existing), default=0) + 1
            hist = value_history(_state_envelopes(records))
            # invalidation by changed group (ER-28 B): compare the stored fingerprint with the current one
            probe = {a.id: a for a in lib.build([a.key for a in existing], hist)}
            changed = frozenset(
                g for a in existing if probe[a.id].fingerprint != a.fingerprint for g in a.groups
            )
            stale_ids = lib.invalidate(existing, changed)
            rebuilt = lib.refresh(existing, stale_ids, hist, computed_at_seq=seq)
            stale += len(stale_ids)
            for sid in stale_ids:
                retire[stored[sid].record_id] = "stale"
            # new groups
            have = {g for a in rebuilt for g in a.groups}
            fresh_keys = []
            for line in sorted(
                hist.lines,
                key=lambda ln: max(
                    (s.effective_from for s in ln.spans if s.effective_from is not None),
                    default=datetime.min.replace(tzinfo=UTC),
                ),
                reverse=True,
            ):
                k = line.key
                g = lib.GroupKey.of(k.tenant, k.owner, k.subject, k.attribute)
                if g in have or not (line.current() or line.open_contests()):
                    continue
                fresh_keys.append(lib.QuestionKey(g.tenant, g.owner, g.subject, g.attribute))
            answers = [a for a in rebuilt if not a.empty]
            emptied += len(rebuilt) - len(answers)
            for a in rebuilt:
                if a.empty and a.id in stored:
                    retire[stored[a.id].record_id] = "empty"
            # eviction: idle (only once reads were logged), poor hit rate, or over the cap
            stats = lib.usefulness(answers, hit_log)
            evict = set(
                lib.eviction_candidates(
                    answers,
                    stats,
                    now_seq=seq,
                    idle_after=cfg.standing_idle_after if hit_log else 10**9,
                    max_answers=cfg.standing_max_answers,
                )
            )
            for aid in sorted(evict):
                if aid in stored:
                    retire[stored[aid].record_id] = "evicted"
            evicted += len(evict)
            answers = [a for a in answers if a.id not in evict]
            room = max(cfg.standing_max_answers - len(answers), 0)
            answers_new = [
                a for a in lib.build(fresh_keys[:room], hist, computed_at_seq=seq) if not a.empty
            ]
            to_write = [a for a in answers if a.id in stale_ids] + answers_new
            if retire:
                await archive_records(
                    ctx.append_event, namespace, sorted(retire), "standing->retired", "standing"
                )
            for a in to_write:
                parents = [p for p in a.support_ids if p in by_record]
                if not parents or await _parents_gone(ctx, namespace, parents, STANDING_CHANNEL):
                    skipped_parents += 1
                    continue
                cap = [a.trust, *(by_record[p].trust for p in parents)]
                record = MemoryRecord(
                    namespace=namespace,
                    memory_type="semantic",
                    content=lib.render_answer(a),
                    valid_from=max(by_record[p].valid_from for p in parents),
                    source=SourceInfo(
                        role=constants.DERIVED_ROLE,
                        channel=STANDING_CHANNEL,
                        message_id=a.id,
                        parents=parents,
                    ),
                    tags=_answer_tags(a, _live_hash(records, a.key.subject, a.key.attribute)),
                )
                try:
                    out = await ctx.write_fact(record, cap)
                except Exception as exc:  # one bad answer must not lose the rest
                    errors.append(f"{namespace}: standing deposit failed: {exc}")
                    continue
                built += out is not None and not out.quarantined
    stats_out: dict[str, object] = {
        "status": "ok" if not errors else "partial",
        "built": built,
        "stale": stale,
        "evicted": evicted,
        "emptied": emptied,
        "skipped_parents": skipped_parents,
        "errors": errors,
    }
    if tap.enabled():
        tap.event(
            "job.standing",
            built=built,
            stale=stale,
            evicted=evicted,
            emptied=emptied,
            skipped_parents=skipped_parents,
        )
    return stats_out


# --------------------------------------------------------------------------- read side


def guarded_instructions(
    eng: Any, records: list[MemoryRecord], ns: str, budget: int
) -> list[lib.StandingInstruction]:
    """The standing instructions that pass the library guards (firm user source, trust, live), within budget."""
    from memspine.compose.pipeline.render import (
        record_envelope,
    )  # lazy: compose imports this module
    from memspine.read.lead import is_standing_instruction

    read_cfg = eng._config().read
    live = {
        r.record_id for r in records if r.status is RecordStatus.ACTIVATED and not r.quarantined
    }
    found: list[lib.StandingInstruction] = []
    for seq, r in enumerate(sorted(records, key=lambda x: (x.valid_from, x.record_id))):
        if (
            r.source.role != "user"
            or "atomic_fact" in r.tags
            or r.status is not RecordStatus.ACTIVATED
            or r.quarantined
            or not is_standing_instruction(r.content, wide=read_cfg.standing_patterns == "wide")
        ):
            continue
        inst = lib.propose_instruction(
            record_envelope(r), created_seq=seq, min_trust=read_cfg.standing_min_trust
        )
        if inst is not None:
            found.append(inst)
    kept = lib.live_instructions(found, live)
    return lib.select_instructions(kept, tenant=ns, owner=ns, budget=budget)


async def standing_block(
    eng: Any, ns: str, scored: list[tuple[MemoryRecord, float]]
) -> MemoryRecord | None:
    """O4-08 (``read.standing``): one lead block of fresh standing answers and guarded instructions.

    Topic entities come from the best-scored keyed semantic hits, as for the timelines; each entity's
    question key is its top hit's attribute. A stored answer whose supporting facts changed since the build
    (the live fact set of its group differs, or a parent is gone) is stale: never shown, the reader uses the
    live evidence, and the skip is logged for the next eviction pass. Read-only.
    """
    read_cfg = eng._config().read
    storage = eng._require_started()
    records = await storage.list_records(ns, "semantic")
    std_records: dict[str, MemoryRecord] = {}
    answers: list[lib.StandingAnswer] = []
    for r in records:
        if r.source.channel != STANDING_CHANNEL or r.status is not RecordStatus.ACTIVATED:
            continue
        a = answer_from_record(r)
        if a is None or r.quarantined:
            continue
        std_records[a.id] = r
        answers.append(a)
    live_ids = {r.record_id for r in records if r.status is RecordStatus.ACTIVATED}
    stale_ids = [
        a.id
        for a in answers
        if tag_value(std_records[a.id].tags, "sar:")
        != _live_hash(records, a.key.subject, a.key.attribute)
        or any(p not in live_ids for p in a.support_ids)
    ]
    answers = lib.mark_stale(answers, stale_ids)
    top_seq = max((a.computed_at_seq for a in answers), default=0)
    budget = read_cfg.standing_answers_budget
    chosen: list[lib.StandingAnswer] = []
    skipped: list[str] = []
    seen_subjects: set[str] = set()
    topics = 0
    for record, _ in scored:
        if record.memory_type != "semantic" or not record.entity:
            continue
        subject = normalise_name(record.entity)
        if subject in seen_subjects:
            continue
        seen_subjects.add(subject)
        topics += 1
        attribute = normalise_name(split_statement(record)[0])
        key = lib.QuestionKey(ns, ns, subject, attribute)
        sel = lib.select_for_context(
            answers, key, budget - sum(len(lib.render_answer(a).split()) for a in chosen)
        )
        chosen.extend(a for a in sel.answers if a not in chosen)
        skipped.extend(sel.skipped_stale)
        if topics >= read_cfg.timeline_entities:
            break
    lines = [escape_markers(lib.render_answer(a)) for a in chosen]
    parts = [std_records[a.id] for a in chosen]
    if not read_cfg.standing_instructions:
        pool = await storage.list_records(ns, "episodic")
        used = sum(len(line.split()) for line in lines)
        instructions = guarded_instructions(eng, [*pool, *records], ns, max(budget - used, 0))
        by_id = {r.record_id: r for r in [*pool, *records]}
        text_lines = [f"- {escape_markers(i.text)}" for i in instructions]
        parts_i = [by_id[s] for i in instructions for s in i.source_ids if s in by_id]
    else:
        text_lines, parts_i = [], []
    HIT_LOG.extend(lib.HitEvent(a.id, top_seq, "hit") for a in chosen)
    HIT_LOG.extend(lib.HitEvent(i, top_seq, "miss") for i in skipped)
    if tap.enabled():
        tap.event(
            "compose.standing",
            selected=len(chosen),
            skipped_stale=len(skipped),
            stale_total=len(stale_ids),
            instructions=len(text_lines),
        )
    if not lines and not text_lines:
        return None
    body = [ANSWERS_HEADER, *(f"- {line}" for line in lines)] if lines else []
    if text_lines:
        body += [INSTRUCTIONS_HEADER, *text_lines]
    return eng._lead_record(ns, "\n".join(body), [*parts, *parts_i])  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
