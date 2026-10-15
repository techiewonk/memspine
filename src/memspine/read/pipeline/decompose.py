"""R14-M-12 / OBS-10 (``read.decompose_v2``): map-reduce read for multi-entity questions.

Called from ``router._read_planned`` in place of ``eng._read`` (so the read scopes, the read
cache, the contract header, consent and the audit all wrap the merged result exactly once).

MAP: one sub-read per entity through ``eng._read`` (the existing read path), with the entity as
the focal entity and an equal share of the context budget. REDUCE: the evidence records are
merged under one labelled lead block per entity, preceded by a combine hint. No model call.
Anything that does not fit the pattern returns ``None`` and the caller reads as before.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from memspine.compose.pack import AssembledContext, estimate_tokens
from memspine.config import constants
from memspine.models.encoders.query.temporal_query import _speaker_name
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.read import decompose as rules
from memspine.read.pipeline.state import _FOCAL_ENTITY, ReadResult
from memspine.store.records import MemoryRecord

_log = get_logger(__name__)

#: True inside a sub-read: a sub-question is never decomposed again.
_INSIDE: ContextVar[bool] = ContextVar("memspine_decompose_inside", default=False)

_CAPITAL = re.compile(r"\b[A-Z][\w'-]{2,}")


async def known_names(eng: Any, ns: str) -> set[str]:
    """Participants of the namespace: speakers of its episodic turns and record entities."""
    names: set[str] = set()
    for record in await eng._require_started().list_records(ns, "episodic"):
        speaker = _speaker_name(record)
        if speaker:
            names.add(speaker)
        if record.entity:
            names.add(record.entity)
    return names


def _is_evidence(record: MemoryRecord) -> bool:
    return constants.LEAD_TAG not in record.tags and record.source.channel not in (
        "lead",
        "persona",
    )


def _share(eng: Any, budget_tokens: int, n: int) -> int:
    """Per-entity budget: the reply reserve stays whole, the rest is split equally."""
    usable = eng._reply_budget(budget_tokens)
    return (budget_tokens - usable) + max(1, usable // n)


def _about(record: MemoryRecord, entity: str) -> bool:
    """The record is by ``entity`` (speaker) or names it."""
    speaker = _speaker_name(record)
    if speaker and speaker.lower() == entity.lower():
        return True
    return bool(re.search(rf"(?<!\w){re.escape(entity)}(?!\w)", record.content, re.IGNORECASE))


def merge(
    eng: Any,
    ns: str,
    plan: rules.DecomposePlan,
    results: list[ReadResult],
    budget_tokens: int,
) -> ReadResult | None:
    """REDUCE: labelled per-entity blocks plus the combine hint, inside the budget.
    None when no sub-read found evidence."""
    persona: list[MemoryRecord] = []
    retrieved: list[list[MemoryRecord]] = []
    for result in results:
        mine: list[MemoryRecord] = []
        for record in result.context.records:
            if record.source.channel == "persona":
                if all(p.record_id != record.record_id for p in persona):
                    persona.append(record)
            elif _is_evidence(record):
                mine.append(record)
        retrieved.append(mine)
    # A record retrieved for several entities is listed once, in the block of the entity it is
    # about (its speaker, else a name in its text), else the first block that retrieved it.
    owner: dict[str, int] = {}
    for i, mine in enumerate(retrieved):
        for record in mine:
            if record.record_id not in owner:
                owner[record.record_id] = i
            if _about(record, plan.entities[i]) and not _about(
                record, plan.entities[owner[record.record_id]]
            ):
                owner[record.record_id] = i
    blocks = [
        (entity, [r for r in mine if owner[r.record_id] == i])
        for i, (entity, mine) in enumerate(zip(plan.entities, retrieved, strict=True))
    ]
    if not any(mine for _, mine in blocks):
        return None
    every = [r for _, mine in blocks for r in mine]
    records: list[MemoryRecord] = [*persona]
    boundary = len(records)
    hint = rules.combine_hint(plan)
    records.append(eng._lead_record(ns, hint, every))
    tokens = estimate_tokens(hint)
    for entity, mine in blocks:
        header = rules.entity_header(entity, len(mine))
        records.append(eng._lead_record(ns, header, mine or every))
        tokens += estimate_tokens(header)
        records.extend(mine)
        tokens += sum(estimate_tokens(r.content) for r in mine)
    tokens += sum(estimate_tokens(p.content) for p in persona)
    mode = results[0].mode
    return ReadResult(
        mode,
        AssembledContext(records=records, boundary_index=boundary, tokens_used=tokens),
    )


async def decomposed_read(
    eng: Any,
    query: str,
    ns: str,
    mode: str | None,
    budget_tokens: int,
    top_k: int,
    replay_window: int,
    compose_pool: int,
    session_id: str | None,
) -> ReadResult | None:
    """The map-reduce read, or None when ``query`` is not a multi-entity question (or the
    split gave nothing better than the ordinary read): the caller then reads as before."""
    cfg = eng._config().read
    if not cfg.decompose_v2 or _INSIDE.get():
        return None
    if not _CAPITAL.search(query):  # cheap gate before the store is listed
        return None
    plan = rules.plan(query, await known_names(eng, ns), cfg.decompose_v2_max_entities)
    if plan is None:
        tap.event("read.decompose", fired=False)
        return None
    share = _share(eng, budget_tokens, len(plan.entities))

    def leg(entity: str, sub: str) -> Callable[[], Awaitable[ReadResult]]:
        async def run() -> ReadResult:
            inside = _INSIDE.set(True)
            focal = _FOCAL_ENTITY.set(entity)
            try:
                return await eng._read(
                    sub, ns, mode, share, top_k, replay_window, compose_pool, session_id
                )
            finally:
                _FOCAL_ENTITY.reset(focal)
                _INSIDE.reset(inside)

        return run

    legs = [leg(e, s) for e, s in zip(plan.entities, plan.sub_queries, strict=True)]
    if cfg.parallel_legs:
        results = list(await asyncio.gather(*(run() for run in legs)))
    else:
        results = [await run() for run in legs]
    if any(r.mode == "full" for r in results):
        # the whole store fits one share: nothing to split, the ordinary read has it all
        tap.event("read.decompose", fired=False, reason="full", entities=len(plan.entities))
        return None
    merged = merge(eng, ns, plan, results, budget_tokens)
    tap.event(
        "read.decompose",
        fired=merged is not None,
        entities=len(plan.entities),
        combine=plan.combine,
        count=plan.count,
        share_tokens=share,
        per_entity=[len([r for r in x.context.records if _is_evidence(r)]) for x in results],
    )
    return merged
