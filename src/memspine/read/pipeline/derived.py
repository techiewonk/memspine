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
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memspine.config import constants
from memspine.core.event_date import happened_of
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
