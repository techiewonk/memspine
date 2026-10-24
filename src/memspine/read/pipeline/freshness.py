"""O4-01b (O-06): the read-path adapter for ``read.freshness`` and ``read.freshness_fallback``.

Adapts today's records and the event log to the pure :mod:`memspine.read.freshness` library: it
keeps the engine's :class:`~memspine.read.freshness.SourceSeqTracker` current, builds the
per-layer report of a namespace from the stored ``covers_seq`` stamps, records it in the read
trace, and, when asked, turns the raw turns newer than a stale layer into one more fused leg.
"""

from __future__ import annotations

from typing import Any

from memspine.config import constants
from memspine.models.encoders.query.temporal_query import LegHit
from memspine.observability import tap
from memspine.read.freshness import FreshnessReport, SourceSeqTracker, build_report
from memspine.read.pipeline.state import _FORENSICS
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import RecordStatus

__all__ = ["apply", "bootstrap", "fallback_leg", "note_event", "report"]


def _raw_write(event: MemoryEvent) -> tuple[str, str] | None:
    """``(namespace, record_id)`` when the event wrote a raw turn (an episodic record with no
    parents); derived and other writes do not advance the source seq."""
    if event.kind is not EventKind.WRITE:
        return None
    rec = event.payload.get("record")
    if not isinstance(rec, dict) or rec.get("memory_type") != "episodic":
        return None
    source = rec.get("source")
    if isinstance(source, dict) and source.get("parents"):
        return None
    rid = rec.get("record_id")
    return (event.namespace, str(rid)) if rid else None


def note_event(tracker: SourceSeqTracker, event: MemoryEvent) -> None:
    """Called for each appended event (``event.seq`` assigned): keeps the tracker current."""
    hit = _raw_write(event)
    if hit is not None and event.seq is not None:
        tracker.note(hit[0], event.seq, hit[1])


async def bootstrap(eng: Any, tracker: SourceSeqTracker) -> int:
    """Fill the tracker from the event log at start. A log that cannot serve reads (ephemeral)
    leaves it empty: freshness then reports no gap rather than a wrong one."""
    storage = eng._storage  # called during start, before the engine is marked started
    after, seen = 0, 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            return seen
        for event in batch:
            note_event(tracker, event)
            seen += 1
        after = batch[-1].seq or after
        if len(batch) < 1000:
            return seen


async def report(eng: Any, ns: str) -> FreshnessReport:
    """The freshness of every derived layer of ``ns`` against its latest raw-turn seq."""
    read = eng._config().read
    tracker: SourceSeqTracker = eng._freshness_tracker
    covers: dict[str, list[int | None]] = {}
    for rec in await eng._records(ns):
        if rec.layer == "L0" or rec.status is not RecordStatus.ACTIVATED or rec.quarantined:
            continue
        if constants.LEAD_TAG in rec.tags or rec.source.channel in ("lead", "persona"):
            continue
        covers.setdefault(rec.layer, []).append(rec.fingerprint.covers_seq)
    return build_report(ns, tracker.latest(ns), covers, read.freshness_stale_gap)


def fallback_leg(eng: Any, ns: str, rep: FreshnessReport) -> list[LegHit]:
    """The raw turns newer than the stale layers, newest first, as a ranked leg."""
    mark = rep.stale_from
    if mark is None:
        return []
    cap = eng._config().read.freshness_fallback_max
    tracker: SourceSeqTracker = eng._freshness_tracker
    pairs = tracker.beyond(ns, mark, cap)
    return [LegHit(rid, 1.0 / (rank + 1)) for rank, (_, rid) in enumerate(pairs)]


async def apply(eng: Any, ns: str) -> list[LegHit]:
    """One search's freshness step: report (trace + tap), and the fallback leg when it is on and a
    layer is stale. Returns the leg to fuse (empty = nothing to add)."""
    tracker = getattr(eng, "_freshness_tracker", None)
    if tracker is None:
        return []
    rep = await report(eng, ns)
    fallback = eng._config().read.freshness_fallback
    leg = fallback_leg(eng, ns, rep) if fallback and rep.stale else []
    if (fx := _FORENSICS.get()) is not None:
        fx["freshness"] = {**rep.as_dict(), "fallback_added": len(leg)}
    tap.event(
        "read.freshness",
        namespace=ns,
        latest_seq=rep.latest_seq,
        stale=rep.stale,
        stale_layers=list(rep.stale_layers),
        gaps={x.layer: x.gap for x in rep.layers},
        threshold=rep.threshold,
        fallback=fallback,
        fallback_added=len(leg),
    )
    return leg
