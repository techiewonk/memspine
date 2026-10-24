"""Entity summaries: one implementation with model calls outside namespace locks."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from memspine.compose.compression import CompressionPolicy
from memspine.config import constants
from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    _int_option,
    _parents_gone,
    live_record,
    live_records,
    sweep_namespaces,
)
from memspine.derive.pipeline.stages.extract_graph import _record_names
from memspine.derive.pipeline.stages.index import ENTITY_SUMMARIZED_MARKER
from memspine.derive.pipeline.stages.offline_door import is_live, write_summary_record
from memspine.memories.associative.entities import (
    MENTIONS_REL,
    canonical_entity,
    entity_node_id,
    parse_entity_node,
)
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo, chrono_key
from memspine.trust.firewall import instruction_shaped


def _entity_summary_options(ctx: PipelineContext) -> dict[str, object] | None:
    """``memories.associative.policies.entity_summaries``: None when off, else
    its options (``true`` = defaults)."""
    mem = ctx.config.memories.get("associative")
    raw = mem.policies.get("entity_summaries") if mem is not None else None
    if not raw:
        return None
    return dict(raw) if isinstance(raw, dict) else {}


def entity_summary_node(record: MemoryRecord) -> str | None:
    """The entity node an entity summary record is about (its ``about:`` tag)."""
    prefix = constants.ENTITY_SUMMARY_ABOUT_PREFIX
    for tag in record.tags:
        if tag.startswith(prefix) and tag[len(prefix) :].strip():
            return entity_node_id(record.namespace, canonical_entity(tag[len(prefix) :]))
    return None


def entity_summary_name(record: MemoryRecord) -> str:
    """The display name an entity summary record is about."""
    prefix = constants.ENTITY_SUMMARY_ABOUT_PREFIX
    return next((t[len(prefix) :] for t in record.tags if t.startswith(prefix)), "")


def _display_name(canonical: str, members: list[MemoryRecord]) -> str:
    """The most frequent spelling the members give the entity (ties: lowest)."""
    counts: Counter[str] = Counter()
    for member in members:
        for name in _record_names(member):
            if canonical_entity(name) == canonical:
                counts[" ".join(name.split())] += 1
    return min(counts, key=lambda n: (-counts[n], n)) if counts else canonical


def _fact_lines(members: list[MemoryRecord]) -> list[str]:
    ordered = sorted(members, key=chrono_key)
    return [f"[{m.valid_from:%Y-%m-%d}] {' '.join(m.content.split())}" for m in ordered]


def _newest_lines(lines: list[str], max_chars: int) -> str:
    """The newest lines that fit ``max_chars``, in date order (no-LLM fallback)."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if size + len(line) + (1 if kept else 0) > max_chars:
            break
        kept.append(line)
        size += len(line) + (1 if len(kept) > 1 else 0)
    if not kept and lines:
        return lines[-1][:max_chars].rstrip()
    return "\n".join(reversed(kept))


@dataclass
class _EntityWork:
    node: str
    display: str
    members: list[MemoryRecord]
    fingerprint: str
    old: list[MemoryRecord]
    lines: list[str]
    text: str = ""


async def _archive_summaries(
    ctx: PipelineContext, namespace: str, old: list[MemoryRecord], reason: str
) -> int:
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
                    "transition": "entity_summary->superseded",
                    "reason": reason,
                },
            )
        )
    return len(old)


_log = get_logger(__name__)


async def summarize_entities(ctx: PipelineContext) -> dict[str, object]:
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    opts = _entity_summary_options(ctx)
    if opts is None:
        return {"status": "skipped", "reason": "associative.policies.entity_summaries is off"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "no graph store (associative memory disabled)"}
    free_chars = _int_option(opts, "free_chars", constants.ENTITY_SUMMARY_FREE_CHARS)
    batch = _int_option(opts, "batch_size", constants.ENTITY_SUMMARY_BATCH)
    inflate = CompressionPolicy.bind()
    index = ctx.session_index
    await index.refresh(ctx.storage)
    counts: Counter[str] = Counter()
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        try:
            # 1. Read under the lock: which entities changed, and what they say.
            async with ctx.lock(namespace):
                work, gone = await _gather(ctx, namespace, inflate, counts)
            # 2. The model call, with no lock held.
            costly = _fill_free(work, free_chars, counts)
            for start in range(0, len(costly), batch):
                chunk = costly[start : start + batch]
                answers: dict[int, str] = {}
                if ctx.summarize_entities is not None:
                    counts["llm_calls"] += 1
                    counts["llm_items"] += len(chunk)
                    try:
                        answers = await ctx.summarize_entities(
                            [
                                (
                                    item.display,
                                    item.lines[-constants.ENTITY_SUMMARY_MAX_INPUT_LINES :],
                                )
                                for item in chunk
                            ]
                        )
                    except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                        errors.append(f"{namespace}: summarize_entity failed: {exc}")
                        _log.warning(
                            "summarize_entities.llm_failed", namespace=namespace, error=str(exc)
                        )
                for offset, item in enumerate(chunk, start=1):
                    text = " ".join(str(answers.get(offset, "")).split())
                    item.text = text if text else _newest_lines(item.lines, free_chars)
            # 3. Re-check and write under the lock.
            async with ctx.lock(namespace):
                written: list[dict[str, object]] = []
                for item in work:
                    member_ids = sorted(m.record_id for m in item.members)
                    if await _parents_gone(ctx, namespace, member_ids, "summarize_entities"):
                        counts["skipped_parents_gone"] += 1  # redone next sweep (no watermark)
                        continue
                    summary = _summary_record(namespace, item, member_ids)
                    stored, quarantined = await write_summary_record(
                        ctx, namespace, summary, member_ids, "L3", "entity_summary"
                    )
                    counts["summarized"] += 1
                    counts["quarantined"] += int(quarantined)
                    counts["superseded"] += await _archive_summaries(
                        ctx,
                        namespace,
                        [r for r in item.old if r.record_id != stored.record_id],
                        "membership_drift",
                    )
                    written.append(
                        {
                            "record_id": stored.record_id,
                            "namespace": namespace,
                            "entity": item.node,
                            "content_fingerprint": item.fingerprint,
                        }
                    )
                for old in gone:
                    counts["superseded"] += await _archive_summaries(
                        ctx, namespace, old, "entity_gone"
                    )
                if written:
                    event = MemoryEvent(
                        kind=EventKind.MARKER,
                        namespace=namespace,
                        actor="system",
                        payload={
                            "marker": ENTITY_SUMMARIZED_MARKER,
                            "stage": "summarize_entities",
                            "entities": written,
                        },
                    )
                    await ctx.append_event(event)
                    index.observe(event)
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{namespace}: {exc}")
            _log.warning("summarize_entities.namespace_failed", namespace=namespace, error=str(exc))
    if tap.enabled():
        tap.event(
            "job.communities.entities",
            entities_changed=counts["changed"],
            unchanged=counts["unchanged"],
            free=counts["free"],
            llm_calls=counts["llm_calls"],
            llm_items=counts["llm_items"],
            summarized=counts["summarized"],
            superseded=counts["superseded"],
            skipped_parents_gone=counts["skipped_parents_gone"],
            quarantined=counts["quarantined"],
        )
    stats: dict[str, object] = {
        "status": "ok" if not errors else "partial",
        "summarized": counts["summarized"],
        "free": counts["free"],
        "llm_calls": counts["llm_calls"],
        "unchanged": counts["unchanged"],
        "superseded": counts["superseded"],
        "quarantined": counts["quarantined"],
    }
    if counts["skipped_parents_gone"]:
        stats["skipped_parents_gone"] = counts["skipped_parents_gone"]
    if errors:
        stats["errors"] = errors
    return stats


async def _gather(
    ctx: PipelineContext, namespace: str, inflate: CompressionPolicy, counts: Counter[str]
) -> tuple[list[_EntityWork], list[list[MemoryRecord]]]:
    """The entities whose membership changed (with their fact lines), and the summaries of entities
    with no live member left. Call with the namespace lock held."""
    assert ctx.graph is not None
    index = ctx.session_index
    members_of: dict[str, set[str]] = {}
    for edge in await ctx.graph.edge_list(namespace):
        if edge.rel_type != MENTIONS_REL or edge.weight <= 0:
            continue
        parsed = parse_entity_node(edge.dst)
        if parsed is not None and parsed[0] == namespace:
            members_of.setdefault(edge.dst, set()).add(edge.src)
    live: dict[str, list[MemoryRecord]] = {}
    for record in await live_records(ctx, namespace, "semantic"):
        if record.source.channel != constants.ENTITY_SUMMARY_CHANNEL or record.status not in (
            RecordStatus.ACTIVATED,
            RecordStatus.QUARANTINED,
        ):
            continue
        node = entity_summary_node(record)
        if node is not None:
            live.setdefault(node, []).append(record)
    work: list[_EntityWork] = []
    for node in sorted(members_of):
        members: list[MemoryRecord] = []
        for record_id in sorted(members_of[node]):
            member = await live_record(ctx, record_id)
            if (
                member is not None
                and member.namespace == namespace
                and is_live(member)
                and member.source.channel != constants.ENTITY_SUMMARY_CHANNEL
            ):
                members.append(inflate.inflate(member))
        if not members:
            continue
        fp = fingerprint_payload(
            {"entity_members": sorted(f"{m.record_id}:{m.content_fingerprint}" for m in members)}
        )
        old = live.pop(node, [])
        key = fingerprint_payload({"entity_summary": [node, fp]})
        if old and (
            index.entity_summaries.get((namespace, node)) == fp
            or any(r.source.message_id == key for r in old)
        ):
            counts["unchanged"] += 1
            continue
        parsed = parse_entity_node(node)
        assert parsed is not None
        work.append(
            _EntityWork(
                node, _display_name(parsed[1], members), members, fp, old, _fact_lines(members)
            )
        )
        counts["changed"] += 1
    return work, list(live.values())


def _fill_free(work: list[_EntityWork], free_chars: int, counts: Counter[str]) -> list[_EntityWork]:
    """Entities whose lines fit are summarised for free; the rest need the model."""
    costly: list[_EntityWork] = []
    for item in work:
        joined = "\n".join(item.lines)
        if len(joined) <= free_chars:
            item.text = joined
            counts["free"] += 1
        else:
            costly.append(item)
    return costly


def _summary_record(namespace: str, item: _EntityWork, member_ids: list[str]) -> MemoryRecord:
    return MemoryRecord(
        namespace=namespace,
        memory_type="semantic",
        content=item.text,
        tags=[
            constants.ENTITY_SUMMARY_TAG,
            f"{constants.ENTITY_SUMMARY_ABOUT_PREFIX}{item.display}",
        ],
        valid_from=max(m.valid_from for m in item.members),
        source=SourceInfo(
            role=constants.DERIVED_ROLE,
            channel=constants.ENTITY_SUMMARY_CHANNEL,
            message_id=fingerprint_payload({"entity_summary": [item.node, item.fingerprint]}),
            parents=member_ids,
        ),
        trust=min(m.trust for m in item.members),
        instruction_flag=instruction_shaped(item.text)
        or any(m.instruction_flag for m in item.members),
    )


__all__ = ["summarize_entities"]
