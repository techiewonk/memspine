# ruff: noqa: E501
"""Shared helpers of the offline-wired stages O4-06 / O4-09: the summary write door, archive, tokens."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from memspine.derive.pipeline.stages.context import PipelineContext, _local_screen
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus

_log = get_logger(__name__)

#: Channels of derived summaries; none of them is evidence for another summary (no summary of a summary).
SUMMARY_CHANNELS = frozenset({"entity_summary", "reorganize", "community_summary", "multires"})
COMMUNITY_CHANNEL = "community_summary"
COMMUNITY_TAG = "community_summary"
MULTIRES_CHANNEL = "multires"
MULTIRES_TAG = "multires"


def est_tokens(text: str) -> int:
    """A cheap, deterministic token estimate (four characters a token, at least one)."""
    return max(1, len(text) // 4)


def is_live(record: MemoryRecord | None) -> bool:
    return record is not None and record.status is RecordStatus.ACTIVATED and not record.quarantined


async def write_summary_record(
    ctx: PipelineContext,
    namespace: str,
    record: MemoryRecord,
    parents: Sequence[str],
    layer: str,
    site: str,
) -> tuple[MemoryRecord, bool]:
    """Write one derived summary and say whether the firewall quarantined it. The caller holds the
    namespace lock. Through the engine's derived-write door when the context has one (lineage and
    trust cap with ``write.lineage_table``); a bare context screens and appends it raw."""
    assert ctx.append_event is not None
    cap = [record.trust]
    if ctx.write_summary is not None:
        stored = await ctx.write_summary(namespace, record, list(parents), layer, site, cap)
        return stored, stored.quarantined
    screen = ctx.screen or _local_screen(ctx)
    screened, reasons = await screen(record, cap)
    payload: dict[str, object] = {"record": screened.model_dump(mode="json")}
    if reasons:
        payload["firewall"] = {"reasons": reasons}
        _log.warning(
            "memory.quarantined", namespace=namespace, record_id=screened.record_id, reasons=reasons
        )
    await ctx.append_event(
        MemoryEvent(kind=EventKind.WRITE, namespace=namespace, actor="system", payload=payload)
    )
    return screened, bool(reasons)


def stamp_summary(ctx: PipelineContext, record: MemoryRecord, site: str) -> MemoryRecord:
    """FU-SAFE: the door's envelope stamp (layer, derivation) on a summary a legacy stage appends
    raw; the identity unless ``write.lineage_table`` is on (``ctx.link_derived`` set)."""
    if ctx.link_derived is None:
        return record
    from memspine.derive.write import stamp_derived

    return stamp_derived(record, route="summary", site=site)


async def link_summary(
    ctx: PipelineContext, namespace: str, record: MemoryRecord, parents: Sequence[str], site: str
) -> None:
    """FU-SAFE: write the lineage edges of a summary appended raw, so erasure finds it as a
    dependant of its parents. A no-op unless ``write.lineage_table`` is on."""
    if ctx.link_derived is not None:
        await ctx.link_derived(namespace, record, list(parents), site)


async def archive_records(
    ctx: PipelineContext, namespace: str, old: Sequence[MemoryRecord], transition: str, reason: str
) -> int:
    """Archive superseded derived records (a delta event each; nothing is deleted)."""
    assert ctx.append_event is not None
    for record in old:
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": record.record_id,
                    "set": {
                        "status": RecordStatus.ARCHIVED.value,
                        "superseded_at": datetime.now(UTC).isoformat(),
                    },
                    "transition": transition,
                    "reason": reason,
                },
            )
        )
    return len(old)
