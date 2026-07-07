"""Blast-radius audit (E1): ``audit taint`` as a log walk.

Because every mutation is an event (D0.1) and provenance is first-class
(D-42/D-43), tracing a poisoned memory is pure reading: find the origin WRITE,
then every derivation — merges that absorbed it, summaries that consolidated
it, supersession chains that grew from it. No inference, no heuristics; the
report is exactly what the log proves.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from memspine.core.events import EventKind, MemoryEvent

__all__ = ["TaintReport", "trace_taint"]


class _EventSource(Protocol):
    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...


class TaintReport(BaseModel):
    """Everything the log proves about one record's origin and reach."""

    record_id: str
    origin_seq: int | None = None
    origin_actor: str | None = None
    origin_source: dict[str, object] | None = None
    #: record_ids whose state the tainted record influenced, with the proof.
    descendants: dict[str, str] = Field(default_factory=dict)
    #: every event seq that mentions the record (the full evidence trail).
    event_seqs: list[int] = Field(default_factory=list)

    @property
    def blast_radius(self) -> int:
        return len(self.descendants)


async def _all_events(storage: _EventSource) -> list[MemoryEvent]:
    events: list[MemoryEvent] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after)
        if not batch:
            return events
        events.extend(batch)
        assert batch[-1].seq is not None
        after = batch[-1].seq


def _snapshot_id(event: MemoryEvent) -> str | None:
    snapshot = event.payload.get("record")
    if isinstance(snapshot, dict):
        raw = snapshot.get("record_id")
        return str(raw) if raw is not None else None
    return None


async def trace_taint(storage: _EventSource, record_id: str) -> TaintReport:
    """Walk the log once; expand the tainted set transitively (a summary built
    from a tainted episode taints whatever later merges with the summary)."""
    events = await _all_events(storage)
    report = TaintReport(record_id=record_id)
    tainted: set[str] = {record_id}

    changed = True
    while changed:  # transitive closure over derivations
        changed = False
        for event in events:
            assert event.seq is not None
            payload = event.payload
            if event.kind is EventKind.WRITE:
                snapshot_id = _snapshot_id(event)
                if snapshot_id == record_id and report.origin_seq is None:
                    report.origin_seq = event.seq
                    report.origin_actor = event.actor
                    snapshot = payload.get("record")
                    if isinstance(snapshot, dict):
                        source = snapshot.get("source")
                        if isinstance(source, dict):
                            report.origin_source = source
                # Consolidation provenance rides the WRITE payload too (P3.1).
                consolidation = payload.get("consolidation")
                if isinstance(consolidation, dict) and snapshot_id is not None:
                    members = consolidation.get("member_record_ids")
                    if (
                        isinstance(members, list)
                        and tainted & set(map(str, members))
                        and snapshot_id not in tainted
                    ):
                        tainted.add(snapshot_id)
                        report.descendants[snapshot_id] = f"consolidated@{event.seq}"
                        changed = True
            elif event.kind is EventKind.MERGE:
                kept = str(payload.get("kept_record_id", ""))
                dropped_snapshot = payload.get("dropped_record")
                dropped = (
                    str(dropped_snapshot.get("record_id", ""))
                    if isinstance(dropped_snapshot, dict)
                    else ""
                )
                if dropped in tainted and kept and kept not in tainted:
                    tainted.add(kept)
                    report.descendants[kept] = f"merged@{event.seq}"
                    changed = True
                # Taint flows both ways: merging INTO a tainted record taints
                # nothing new, but the merged row absorbed tainted content.
                if kept in tainted and dropped and dropped not in tainted:
                    tainted.add(dropped)
                    report.descendants[dropped] = f"absorbed_by@{event.seq}"
                    changed = True
            elif event.kind is EventKind.CONSOLIDATE:
                members = payload.get("member_record_ids")
                summary_id = str(payload.get("summary_record_id", ""))
                if (
                    isinstance(members, list)
                    and tainted & set(map(str, members))
                    and summary_id
                    and summary_id not in tainted
                ):
                    tainted.add(summary_id)
                    report.descendants[summary_id] = f"consolidated@{event.seq}"
                    changed = True
            elif event.kind is EventKind.CONFLICT:
                # A supersession seeded by a tainted incoming record.
                incoming = payload.get("incoming_record")
                if isinstance(incoming, dict):
                    incoming_id = str(incoming.get("record_id", ""))
                    existing_id = str(payload.get("existing_record_id", ""))
                    if (
                        incoming_id in tainted
                        and existing_id
                        and payload.get("verdict") in ("update", "invalidate")
                        and existing_id not in tainted
                    ):
                        tainted.add(existing_id)
                        report.descendants[existing_id] = f"superseded_by_taint@{event.seq}"
                        changed = True

    mention_ids = tainted
    for event in events:
        assert event.seq is not None
        text = str(event.payload)
        if any(marked in text for marked in mention_ids):
            report.event_seqs.append(event.seq)
    return report
