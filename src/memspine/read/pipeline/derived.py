"""GTEMP: derived records (graph edge facts, summaries) in a temporal read.

Diagnosis (``docs/v2/GTEMP.md``): on the GC graph base derived records take about half of the
reranked slots (so the turn windows shrink) and carry the date of the session they were mined
from, or no date, so a "when" question reads a session date instead of the event date.

Two default-off switches, both projection only (stored records never change):

* ``read.derived_dates`` -> :func:`redate_derived`: a derived record without a happened date is
  shown with its best source turn's date (so the dated render and the relative-date annotation
  use the day the source turn was said); on a temporal-shaped question a derived record that
  still has no date is dropped.
* ``read.derived_cap`` -> :func:`place_derived`: at most N derived records, placed after the
  turn evidence.

GLIFT (``docs/v2/GLIFT.md``) adds ``read.derived_lift`` -> :func:`derived_lift_stage`: derived
hits only FIND turns; before the rerank each is replaced by its source turns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from memspine.config import constants
from memspine.core.event_date import happened_of
from memspine.core.read_filters import active_date_filter, active_record_scope
from memspine.models.encoders.query.shape import is_temporal
from memspine.observability import tap
from memspine.store.records import MemoryRecord


def is_derived_record(record: MemoryRecord) -> bool:
    """A mined or consolidated record (it has parents) that is not itself a turn."""
    return bool(record.source.parents) and record.memory_type != "episodic"


def _words(text: str) -> set[str]:
    return {w for w in text.lower().split() if w}


async def source_turn(eng: Any, ns: str, record: MemoryRecord) -> MemoryRecord | None:
    """The parent turn sharing most words with ``record`` (a later one on a tie), or None."""
    storage = eng._require_started()
    mine = _words(record.content)
    best: tuple[float, Any, MemoryRecord] | None = None
    for pid in dict.fromkeys(record.source.parents):
        parent = await storage.get_record(pid)
        if parent is None or parent.memory_type != "episodic" or parent.namespace != ns:
            continue
        other = _words(parent.content)
        overlap = len(mine & other) / (len(mine | other) or 1)
        key = (overlap, parent.valid_from)
        if best is None or key > (best[0], best[1]):
            best = (overlap, parent.valid_from, parent)
    return None if best is None else best[2]


async def redate_derived(
    eng: Any, ns: str, query: str, scored: Sequence[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """``read.derived_dates``: see the module docstring. Order and scores are kept."""
    drop_dateless = is_temporal(query)
    out: list[tuple[MemoryRecord, float]] = []
    redated: list[str] = []
    dropped: list[str] = []
    for record, score in scored:
        if not is_derived_record(record) or happened_of(record) is not None:
            out.append((record, score))
            continue
        src = await source_turn(eng, ns, record)
        if src is not None:
            tags = [t for t in record.tags if t != constants.UNDATED_TAG]
            shown = record.model_copy(update={"valid_from": src.valid_from, "tags": tags})
            out.append((shown, score))
            redated.append(record.record_id)
        elif drop_dateless and constants.UNDATED_TAG in record.tags:
            dropped.append(record.record_id)
        else:
            out.append((record, score))
    if tap.enabled():
        tap.event("read.derived_dates", redated=redated, dropped=dropped, temporal=drop_dateless)
    return out


def place_derived(
    stable: Sequence[MemoryRecord], turns: Sequence[MemoryRecord], cap: int
) -> tuple[list[MemoryRecord], list[MemoryRecord]]:
    """``read.derived_cap``: returns ``(front, rest)``. ``front`` keeps persona and lead blocks;
    ``rest`` is the turns, then at most ``cap`` derived records in their incoming (rank) order."""
    front: list[MemoryRecord] = []
    derived: list[MemoryRecord] = []
    for rec in stable:
        if constants.LEAD_TAG not in rec.tags and is_derived_record(rec):
            derived.append(rec)
        else:
            front.append(rec)
    kept = derived[:cap]
    if tap.enabled():
        tap.event("read.derived_cap", cap=cap, kept=[r.record_id for r in kept],
                  dropped=[r.record_id for r in derived[cap:]])
    return front, [*turns, *kept]


# --- GLIFT: derived hits only find turns -----------------------------------------------------

_WORD = re.compile(r"[a-z0-9']+")


def _evidence_ids(record: MemoryRecord) -> list[str]:
    """The turn ids a derived record cites in its envelope (``support`` and ``binding`` lines;
    ``context`` lines never count as evidence), in citation order."""
    env = record.fingerprint.envelope
    if not isinstance(env, dict):
        return []
    out: list[str] = []
    for ev in env.get("evidence") or []:
        if not isinstance(ev, dict) or ev.get("role") not in ("support", "binding"):
            continue
        turn = ev.get("turn")
        sid = turn.get("source_id") if isinstance(turn, dict) else None
        if isinstance(sid, str) and sid and sid not in out:
            out.append(sid)
    return out


async def _source_turns(
    eng: Any, record: MemoryRecord, cache: dict[str, MemoryRecord | None], max_depth: int = 3
) -> tuple[list[MemoryRecord], str]:
    """The episodic source turns of ``record`` and how they were found (``evidence`` = the
    envelope's cited lines, ``parents`` = ``source.parents``). A derived parent is followed to its
    own turns, up to ``max_depth`` levels; a parent that no longer exists is skipped."""
    storage = eng._require_started()

    async def fetch(rid: str) -> MemoryRecord | None:
        if rid not in cache:
            cache[rid] = await storage.get_record(rid)
        return cache[rid]

    ids = _evidence_ids(record)
    how = "evidence"
    if not ids:
        ids = list(dict.fromkeys(p for p in record.source.parents if p != record.record_id))
        how = "parents"
    turns: dict[str, MemoryRecord] = {}
    frontier = ids
    for _ in range(max_depth):
        nxt: list[str] = []
        for rid in frontier:
            rec = await fetch(rid)
            if rec is None:
                continue
            if rec.memory_type == "episodic":
                turns.setdefault(rec.record_id, rec)
            else:
                nxt.extend(_evidence_ids(rec) or rec.source.parents)
        frontier = list(dict.fromkeys(i for i in nxt if i not in turns))
        if not frontier:
            break
    return list(turns.values()), how


def _word_overlap(query: str, text: str) -> float:
    a, b = set(_WORD.findall(query.lower())), set(_WORD.findall(text.lower()))
    return len(a & b) / (len(a | b) or 1)


async def _similarity(
    eng: Any, ns: str, query: str, query_vector: Sequence[float] | None, turns: list[MemoryRecord]
) -> tuple[dict[str, float], str]:
    """Query similarity of each turn: the cosine with its STORED vector (no model call) when the
    vector store can score ids, else word overlap with the query."""
    store = getattr(eng, "_vector", None)
    scorer = getattr(store, "scores_for", None)
    emb = getattr(eng, "_embedder", None)
    if scorer is not None and emb is not None and query_vector is not None:
        try:
            ids = [t.record_id for t in turns]
            got = await scorer(ns, list(query_vector), emb.embedder_id, ids)
            if got:
                return {t.record_id: got.get(t.record_id, -1.0) for t in turns}, "vector"
        except Exception:  # an enhancer, never a gate: fall back to the lexical signal
            pass
    return {t.record_id: _word_overlap(query, t.content) for t in turns}, "lexical"


async def derived_lift_stage(
    eng: Any,
    ns: str,
    query: str,
    query_vector: Sequence[float] | None,
    candidates: list[tuple[MemoryRecord, float]],
    protected: dict[str, str],
    *,
    mode: str,
    max_parents: int,
    group_id: str | None,
    tags: list[str] | None,
    memory_type: str | None,
    hide: Callable[[MemoryRecord], bool] | None,
) -> list[tuple[MemoryRecord, float]]:
    """``read.derived_lift`` (GLIFT): replace each derived hit by its source turns (pre-rerank).

    The narrowest lineage wins: the envelope's cited turns, else ``source.parents``. With more than
    ``max_parents`` of them, ``evidence+match`` keeps the ``max_parents`` most similar to the query
    (stored-vector cosine), ``evidence`` keeps the first ``max_parents``. A lifted turn takes the
    derived hit's score (the larger one when it is already in the pool), passes the read gates again
    (status, quarantine, consent, trust, date and scope filters, ``hide``) and is deduplicated; a
    derived hit with no visible source turn is dropped. Lead (header) records are left as they are.
    The pool is cut back to its incoming size, protected hits first. Order: by score, stable.
    """
    derived = [
        rec
        for rec, _ in candidates
        if is_derived_record(rec) and constants.LEAD_TAG not in rec.tags
    ]
    if not derived:
        return candidates
    cache: dict[str, MemoryRecord | None] = {}
    lineage: dict[str, tuple[list[MemoryRecord], str]] = {}
    for rec in derived:
        lineage[rec.record_id] = await _source_turns(eng, rec, cache)
    want = list(dict.fromkeys(t.record_id for ts, _ in lineage.values() for t in ts))
    visible: dict[str, MemoryRecord] = {}
    if want:
        gated = await eng._gate_hits(ns, [(rid, 0.0) for rid in want], group_id, tags, memory_type)
        date_filter, scope = active_date_filter(), active_record_scope()
        for rec, _ in gated:
            if date_filter is not None and not date_filter.matches(rec):
                continue
            if scope is not None and not scope.matches(rec):
                continue
            if hide is not None and hide(rec):
                continue
            visible[rec.record_id] = rec
    per_kind: dict[str, dict[str, int]] = {}
    lifted_ids: list[str] = []
    dropped_ids: list[str] = []
    merged: dict[str, tuple[MemoryRecord, float]] = {}
    order: list[str] = []

    def put(rec: MemoryRecord, score: float) -> None:
        if rec.record_id not in merged:
            order.append(rec.record_id)
            merged[rec.record_id] = (rec, score)
        elif score > merged[rec.record_id][1]:
            merged[rec.record_id] = (merged[rec.record_id][0], score)

    for rec, score in candidates:
        if rec.record_id not in lineage:
            put(rec, score)
            continue
        kind = rec.type_tag or rec.memory_type
        stat = per_kind.setdefault(kind, {"in": 0, "lifted": 0, "dropped": 0})
        stat["in"] += 1
        turns = [visible[t.record_id] for t in lineage[rec.record_id][0] if t.record_id in visible]
        if not turns:
            stat["dropped"] += 1
            dropped_ids.append(rec.record_id)
            continue
        if len(turns) > max_parents and mode == "evidence+match":
            sim, _src = await _similarity(eng, ns, query, query_vector, turns)
            turns = sorted(turns, key=lambda t: -sim[t.record_id])  # stable: lineage order on ties
        for turn in turns[:max_parents]:
            put(turn, score)
            lifted_ids.append(turn.record_id)
            stat["lifted"] += 1
    out = [merged[rid] for rid in order]
    out.sort(key=lambda pair: -pair[1])
    cap = len(candidates)
    if len(out) > cap:
        first = [p for p in out if p[0].record_id in protected]
        rest = [p for p in out if p[0].record_id not in protected]
        out = sorted([*first, *rest[: max(cap - len(first), 0)]], key=lambda pair: -pair[1])
    if tap.enabled():
        tap.event(
            "read.derived_lift",
            n_in=len(candidates),
            derived=len(derived),
            lifted=list(dict.fromkeys(lifted_ids)),
            dropped=dropped_ids,
            per_kind=per_kind,
            mode=mode,
        )
    return out
