"""Derive stage: extract_graph (edges, entity resolution, retraction) (F-15)."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime

from memspine.config import constants
from memspine.derive.pipeline.stages.context import PipelineContext, _local_screen, _policy_options
from memspine.derive.pipeline.stages.index import (
    ENTITY_RESOLVED_MARKER,
    GRAPH_EXTRACTED_MARKER,
    _members_fp,
    stage_marker,
)
from memspine.derive.pipeline.stages.session_stage import _live_members
from memspine.exceptions import ConfigError, ConflictError
from memspine.memories.associative.entities import EntityPolicy, canonical_entity
from memspine.memories.associative.links import assert_within_budget, link_event
from memspine.memories.associative.resolution import MERGE_METHODS, EntityResolver, KnownEntity
from memspine.memories.semantic.write_pipeline import (
    MAX_PREVIOUS_EPISODES,
    EdgeContext,
    cardinality_map,
    edge_fact_key,
)
from memspine.models.encoders.query.perspective import inherit_perspective_tags
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.prompts.models import ExtractedEdge
from memspine.scope import Scope
from memspine.store import keys as graph_keys
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo, chrono_key

_log = get_logger(__name__)


_RESOLVE_MODES = ("off", "rules", "llm")


def resolve_mode(opts: dict[str, object]) -> str:
    mode = str(opts.get("resolve", "off") or "off")
    if mode not in _RESOLVE_MODES:
        raise ConfigError(
            f"memories.semantic.policies.extract_graph.resolve must be one of "
            f"{list(_RESOLVE_MODES)}, got {mode!r}"
        )
    return mode


def _record_names(record: MemoryRecord) -> list[str]:
    """The raw entity names a record carries: its ``entity`` and ``dst:`` tags."""
    names = [record.entity] if record.entity else []
    names.extend(tag[4:] for tag in record.tags if tag.startswith("dst:"))
    return [name for name in names if name.strip()]


def _known_entities(records: list[MemoryRecord]) -> dict[str, KnownEntity]:
    """GP-7: the entities live, unquarantined records name: canonical -> its most
    frequent spelling and the trust of the most trusted record naming it."""
    policy = EntityPolicy(blocklist=constants.ENTITY_NODE_BLOCKLIST)
    spellings: dict[str, Counter[str]] = {}
    trust: dict[str, float] = {}
    for record in records:
        if record.status is not RecordStatus.ACTIVATED or record.quarantined:
            continue
        for name in _record_names(record):
            canonical = canonical_entity(name)
            if not policy.accepts(canonical):
                continue
            spellings.setdefault(canonical, Counter())[name.strip()] += 1
            trust[canonical] = max(trust.get(canonical, 0.0), record.trust)
    return {
        c: KnownEntity(c, min(counts, key=lambda n: (-counts[n], n)), trust[c])
        for c, counts in spellings.items()
    }


async def _resolve_edges(
    ctx: PipelineContext,
    namespace: str,
    mode: str,
    known: list[MemoryRecord],
    extracted: list[tuple[MemoryRecord, list[ExtractedEdge]]],
    parent_sets: list[list[MemoryRecord]] | None = None,
) -> tuple[list[tuple[MemoryRecord, list[ExtractedEdge]]], dict[str, int]]:
    """GP-7: resolve every edge's names, rewrite merged ones to the known entity's
    spelling, and append the decisions as one ``entity_resolved`` MARKER.

    ``parent_sets`` (parallel to ``extracted``) are the records each entry's
    facts cite; every decision is keyed by all of them (erasure of any one
    erases it), not only by the owner the trust guard judges."""
    assert ctx.append_event is not None
    requests: list[tuple[str, float]] = []
    owners: list[str] = []
    keyed_by: list[tuple[str, ...]] = []
    for i, (record, edges) in enumerate(extracted):
        cited = parent_sets[i] if parent_sets is not None else [record]
        parent_ids = tuple(sorted({p.record_id for p in cited} | {record.record_id}))
        for edge in edges:
            for name in (edge.src_entity, edge.dst_entity):
                requests.append((name, record.trust))
                owners.append(record.record_id)
                keyed_by.append(parent_ids)
    resolver = EntityResolver(
        _known_entities(known),
        dict(ctx.session_index.entity_aliases.get(namespace, {})),
        embed=ctx.embed,
        llm=ctx.resolve_entities if mode == "llm" else None,
    )
    try:
        outcomes = await resolver.resolve(requests)
    except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
        _log.warning("extract_graph.resolve_failed", namespace=namespace, error=str(exc))
        return extracted, {"resolve_errors": 1}
    rewrite: dict[tuple[str, str], str] = {}
    decisions: list[dict[str, object]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    counts: dict[str, int] = {"resolved": 0, "contested": 0}
    group = 0
    for (name, _trust), owner, parent_ids, outcome in zip(
        requests, owners, keyed_by, outcomes, strict=True
    ):
        if outcome.target is not None:
            rewrite[(owner, name)] = outcome.target
        if outcome.method not in (*MERGE_METHODS, "contested"):
            continue
        other = outcome.target if outcome.target is not None else outcome.candidate
        key = (canonical_entity(name), outcome.method, parent_ids)
        if key in seen:
            continue
        seen.add(key)
        counts["resolved" if outcome.target is not None else "contested"] += 1
        for parent_id in parent_ids:
            decisions.append(
                {
                    "record_id": parent_id,
                    "namespace": namespace,
                    "entity": name,
                    "attribute": other,
                    "method": outcome.method,
                    "group": group,
                }
            )
        group += 1
    counts["resolve_llm_calls"] = resolver.llm_calls
    if decisions:
        event = MemoryEvent(
            kind=EventKind.MARKER,
            namespace=namespace,
            actor="system",
            payload={
                "marker": ENTITY_RESOLVED_MARKER,
                "stage": "extract_graph",
                "decisions": decisions,
            },
        )
        await ctx.append_event(event)
        ctx.session_index.observe(event)
    resolved: list[tuple[MemoryRecord, list[ExtractedEdge]]] = []
    for record, edges in extracted:
        out = []
        for edge in edges:
            src = rewrite.get((record.record_id, edge.src_entity), edge.src_entity)
            dst = rewrite.get((record.record_id, edge.dst_entity), edge.dst_entity)
            if (src, dst) != (edge.src_entity, edge.dst_entity):
                edge = edge.model_copy(update={"src_entity": src, "dst_entity": dst})
            out.append(edge)
        resolved.append((record, out))
    return resolved, counts


#: GR-4: at most this many known entity names ride along as extraction context.
EDGE_CONTEXT_MAX_ENTITIES = 50


def _one_line(text: str, limit: int = 400) -> str:
    """An episode squeezed onto one prompt line (GR-4 context, never a source)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


def _context_entities(known: list[MemoryRecord]) -> list[str]:
    """GR-4: the entity names already known in the namespace, most recent first."""
    entities: list[str] = []
    for record in sorted(known, key=lambda r: r.recorded_at, reverse=True):
        if record.entity and record.entity not in entities:
            entities.append(record.entity)
    return entities[:EDGE_CONTEXT_MAX_ENTITIES]


def _session_transcript(members: list[MemoryRecord]) -> str:
    """#20: the session as ``[n] [YYYY-MM-DD] turn`` lines (1-based), the numbers
    the extractor cites in ``episode_indices``."""
    return "\n".join(
        f"[{n}] [{m.valid_from:%Y-%m-%d}] {_one_line(m.content, 2000)}"
        for n, m in enumerate(members, 1)
    )


def _cited(members: list[MemoryRecord], indices: list[int]) -> list[MemoryRecord]:
    """#20: the session turns an edge cites (valid 1-based indices, transcript
    order); the whole session when it cites none, so a fact always has parents."""
    cited = [members[i - 1] for i in sorted(set(indices)) if 1 <= i <= len(members)]
    return cited or list(members)


#: #20: the stage name of the per-session ``stage_done`` markers extract_graph
#: appends under ``granularity: session`` (the session watermark).
EXTRACT_GRAPH_SESSION_STAGE = "extract_graph"


def _edge_contexts(sources: list[MemoryRecord], known: list[MemoryRecord]) -> list[EdgeContext]:
    """GR-4: per source, its event time, the episodes just before it (same
    group, at most ``MAX_PREVIOUS_EPISODES``) and the entity names already
    known in the namespace (most recent first)."""
    entities = _context_entities(known)
    episodes = sorted(
        (r for r in sources if r.memory_type == "episodic"),
        key=chrono_key,
    )
    contexts: list[EdgeContext] = []
    for record in sources:
        previous = [
            e
            for e in episodes
            if e.group_id == record.group_id and chrono_key(e) < chrono_key(record)
        ][-MAX_PREVIOUS_EPISODES:]
        hint = next(
            (
                t[len(constants.EXTRACTION_HINT_PREFIX) :]
                for t in record.tags
                if t.startswith(constants.EXTRACTION_HINT_PREFIX)
            ),
            "",
        )
        contexts.append(
            EdgeContext(
                reference_time=record.valid_from,
                previous=[_one_line(e.content) for e in previous],
                entities=entities,
                hint=hint,
            )
        )
    return contexts


def _edge_key(
    namespace: str, edge: ExtractedEdge, *, dated: bool = False, scoped: bool = False
) -> str:
    """Idempotency key for an extracted edge: same (src, rel, dst) in a
    namespace fingerprints to the same record, so a re-run never duplicates.

    ``dated`` (G-3, ``extract_graph.event_identity: dated``): an EVENT edge also keys
    on its event day and the numbers its fact states, so "visited Paris in 2022" and
    "visited Paris in 2023" (or "ran 5 km" / "ran 10 km") stay two events instead of
    one. State edges keep (src, rel, dst): a newer state supersedes, it does not add."""
    parts: list[object] = [namespace, edge.src_entity, edge.rel, edge.dst_entity]
    if scoped:  # F-16c / M-05 ``isolation.scope_keys_v2``: tenant + owner, never the session
        parts[0] = list(Scope.from_namespace(namespace).identity)
    if dated and edge.kind == "event":
        day = (edge.valid_from or "")[:10]
        numbers = sorted(set(_NUMERAL.findall(edge.fact)))
        parts += [day, numbers]
    return fingerprint_payload({"extract_graph": parts})


def _live(record: MemoryRecord) -> bool:
    return (
        record.status is RecordStatus.ACTIVATED
        and record.valid_to is None
        and not record.quarantined
    )


#: G-3: the numbers a fact states ("5", "10.5", "2,000") for event-edge identity.
_NUMERAL = re.compile(r"\d+(?:[.,]\d+)*")


async def _retract_contradicted(
    ctx: PipelineContext, namespace: str, fact: MemoryRecord, trusts: list[float]
) -> int:
    """G-2 (Graphiti cross-key contradiction): the live ``extract_graph`` facts about the
    same subject on OTHER keys, most words shared with ``fact`` first (at most
    ``CONTRADICTION_CANDIDATES``), are each adjudicated by ``invalidate_edge``. An
    ``update`` / ``invalidate`` verdict retracts the old key through the semantic door,
    so the M4 ladder closes it (``valid_to``) and its trust gate still applies.
    Returns how many facts were retracted. Failures skip the candidate."""
    assert ctx.adjudicate_edge is not None and ctx.write_fact is not None
    subject = (fact.entity or "").casefold()
    if not subject:
        return 0
    words = set(fact.content.lower().split())
    candidates = [
        r
        for r in await ctx.storage.list_records(namespace, "semantic")
        if r.source.channel == "extract_graph"
        and r.record_id != fact.record_id
        and r.status is RecordStatus.ACTIVATED
        and r.valid_to is None
        and (r.entity or "").casefold() == subject
        and r.attribute
        and r.attribute != fact.attribute
        and "retract" not in r.tags
    ]
    candidates.sort(key=lambda r: (-len(words & set(r.content.lower().split())), r.record_id))
    retracted = 0
    for old in candidates[: constants.CONTRADICTION_CANDIDATES]:
        try:
            verdict = await ctx.adjudicate_edge(
                old.content,
                old.valid_from.date().isoformat(),
                fact.content,
                fact.valid_from.date().isoformat(),
            )
        except Exception as exc:
            _log.warning("extract_graph.adjudicate_failed", namespace=namespace, error=str(exc))
            continue
        if verdict.strip().lower() not in ("update", "invalidate"):
            continue
        retraction = MemoryRecord(
            namespace=namespace,
            memory_type="semantic",
            content=f"RETRACTED {old.entity}.{old.attribute}: contradicted by {fact.content}",
            entity=old.entity,
            attribute=old.attribute,
            tags=["retract"],
            valid_from=fact.valid_from,
            source=SourceInfo(
                role=constants.DERIVED_ROLE, channel="extract_graph", parents=[fact.record_id]
            ),
        )
        await ctx.write_fact(retraction, trusts)
        retracted += 1
    return retracted


def _edge_valid_to(edge: ExtractedEdge) -> datetime | None:
    """G-1: the edge's stated ISO ``valid_to`` if parseable, else None (open)."""
    if not edge.valid_to:
        return None
    try:
        parsed = datetime.fromisoformat(edge.valid_to)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _edge_valid_from(edge: ExtractedEdge, fallback: datetime) -> datetime:
    """The edge's stated ISO ``valid_from`` if parseable, else the source
    record's time — a malformed date never fails the sweep."""
    if edge.valid_from:
        try:
            parsed = datetime.fromisoformat(edge.valid_from)
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


async def extract_graph(ctx: PipelineContext) -> dict[str, object]:
    """C2 graphiti-style write path: LLM edge extraction over recent records →
    semantic fact records + ``asserted`` association LINKs.

    For each source record (episodic/semantic, active, not itself extract_graph
    output) the ``extract_edges`` callable returns reflexion-merged relationship
    edges; each edge becomes a semantic record keyed ``(entity=src, attribute=
    rel)`` with the fact as content, and an ``asserted`` LINK from the source to
    the new fact so the D-42 reorganizer forms communities over the LLM edges.
    Slotted after ``consolidate`` and before ``reorganize`` for exactly that.

    No-op ("skipped") without a write door or an ``extract_edges`` callable
    (feature off / no LLM role). Idempotent: an edge's (src, rel, dst) keys its
    record, so a re-run skips already-written edges; LINK upserts are replay
    -deterministic. Derived trust never exceeds the source's (E1/D-47 §5), and
    the ``asserted`` rel is non-reserved so the M13.6 link budget applies — a
    saturated source keeps the fact record but skips the extra edge (logged).

    N2: a fact is LLM-authored, so it carries the non-privileged
    ``DERIVED_ROLE`` with its source as parent, and passes the write-door
    firewall (``ctx.screen``) before it is appended. A quarantined fact is
    recorded inert and gets no LINK; an instruction-flagged source is never
    sent to the extractor."""
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    if ctx.extract_edges is None and ctx.window_edges is None:
        return {"status": "skipped", "reason": "extract_graph disabled or no extract_edges role"}
    opts = _policy_options(ctx, "semantic", "extract_graph") or {}
    # G-3: event edges keyed on their day and numerals (off: (src, rel, dst) only).
    dated_events = opts.get("event_identity", "plain") == "dated"
    # G-1: an edge whose text says it ended is written already closed (valid_to).
    close_ended = bool(opts.get("close_ended", False))
    # I17: operator-declared cardinality per relation (one: supersede, many: coexist).
    cardinality = cardinality_map(opts.get("cardinality"))
    # G-6: a declared relation vocabulary drops edges outside it (after extraction).
    declared = opts.get("relation_types") or ()
    allowed_rels = {
        str(r).strip().lower() for r in (declared if isinstance(declared, list | tuple) else ())
    }
    # G-2: a new STATE fact is checked against the subject's facts on OTHER keys.
    # F-01: graph key v2 (kind + occurrence; old keys stay aliases). Off: the v1 key.
    key_v2 = bool(ctx.config.graph.key_v2)
    scoped_keys = bool(ctx.config.isolation.scope_keys_v2)
    contradictions = bool(opts.get("contradictions", False)) and ctx.adjudicate_edge is not None
    invalidated = 0
    raw_conf = opts.get("min_confidence", 0.0)
    min_conf = float(raw_conf) if isinstance(raw_conf, (int, float, str)) else 0.0
    # #20: ``granularity: session`` sends each consolidated session in one call
    # (needs the session extractor the engine builds for it); record = per source.
    session_extract = (
        ctx.extract_session_edges if opts.get("granularity", "record") == "session" else None
    )
    resolving = resolve_mode(opts)
    resolution: dict[str, int] = {}
    written = 0
    linked = 0
    skipped = 0
    quarantined = 0
    provenance = 0
    errors: list[str] = []
    screen = ctx.screen or _local_screen(ctx)
    protected = ctx.config.firewall.protected_keys
    append = ctx.append_event
    # GP-8a: a source already sent to the LLM (same content) is not sent again.
    index = ctx.session_index
    await index.refresh(ctx.storage)
    already = 0
    for namespace in await ctx.storage.list_namespaces():
        async with ctx.lock(namespace):
            # (src, rel, dst) key -> the fact record already written for it (GR-9).
            existing: dict[str, MemoryRecord] = {}
            sources: list[MemoryRecord] = []
            known: list[MemoryRecord] = []
            for mtype in ("episodic", "semantic"):
                for record in await ctx.storage.list_records(namespace, mtype):
                    if mtype == "semantic" and record.status is RecordStatus.ACTIVATED:
                        known.append(record)
                    if record.source.channel == "extract_graph":
                        key = record.source.message_id
                        if key and (
                            key not in existing
                            or record.status is RecordStatus.ACTIVATED
                            or existing[key].status is not RecordStatus.ACTIVATED
                        ):
                            existing[key] = record
                        continue  # never re-extract from our own output (no feedback loop)
                    if constants.CUE_TAG in record.tags:
                        continue  # a cue is a retrieval key, never a fact source (R2-1)
                    if record.source.channel == constants.ENTITY_SUMMARY_CHANNEL:
                        continue  # GP-6: a summary of facts is not a new fact source
                    if record.source.channel == constants.WINDOW_EPISODE_CHANNEL:
                        continue  # W1: an episode fragment restates turns; they are the source
                    if (
                        record.status is RecordStatus.ACTIVATED
                        and not record.quarantined
                        # N2: injection framing must not be laundered into "facts".
                        and not record.instruction_flag
                    ):
                        sources.append(record)
            kidx = graph_keys.EdgeKeyIndex()
            handles: dict[str, MemoryRecord] = {}
            if key_v2:
                for ekey, rec in existing.items():
                    handles[rec.record_id] = rec
                    ebase = next(
                        (
                            t[len(graph_keys.BASE_TAG_PREFIX) :]
                            for t in rec.tags
                            if t.startswith(graph_keys.BASE_TAG_PREFIX)
                        ),
                        None,
                    )
                    if ebase:
                        kidx.add(rec.record_id, _live(rec), key=ekey, base=ebase)
                    else:
                        kidx.add(rec.record_id, _live(rec), alias=ekey)
            done: dict[str, str] = {}
            # Extraction first, writes after (GP-7 resolves every name in one pass).
            # Each entry: (owner, edges, parents). The owner is the record the
            # resolver judges trust by and keys its decision on; the parents are
            # what the fact cites: the source record, or the session turns (#20).
            extracted: list[tuple[MemoryRecord, list[ExtractedEdge]]] = []
            parent_sets: list[list[MemoryRecord]] = []
            sessions_done: list[tuple[str, str]] = []
            if ctx.window_edges is not None:
                # W1 (call 3 wiring): the edges the window calls already returned join this
                # sweep's resolution, contradiction check and emit; the turns those calls
                # read leave the extraction below (no second LLM pass over them).
                covered_ids: set[str] = set()
                for batch in ctx.window_edges(namespace):
                    live = await _live_members(ctx, batch.covered)
                    live_by_id = {m.record_id: m for m in live}
                    for cited_ids, edge in batch.edges:
                        cited = [live_by_id[i] for i in cited_ids if i in live_by_id]
                        if not cited or len(cited) < len(set(cited_ids)):
                            continue  # a cited turn is gone (forgotten, held): no edge from it
                        if edge.confidence < min_conf:
                            continue
                        owner = min(cited, key=lambda m: (m.trust, chrono_key(m)))
                        extracted.append((owner, [edge]))
                        parent_sets.append(cited)
                    for member in live:
                        done[member.record_id] = member.content_fingerprint
                        covered_ids.add(member.record_id)
                if covered_ids:
                    sources = [r for r in sources if r.record_id not in covered_ids]
            if session_extract is not None:
                # #20: one call per consolidated session (numbered turns); the
                # session's members leave the per-record pass.
                member_of: dict[str, str] = {}
                for (session_ns, session_key), member_ids in index.sessions.items():
                    if session_ns == namespace:
                        for member_id in member_ids:
                            member_of.setdefault(member_id, session_key)
                grouped: dict[str, list[MemoryRecord]] = {}
                loose: list[MemoryRecord] = []
                for record in sources:
                    owner_key = member_of.get(record.record_id)
                    if owner_key is None:
                        loose.append(record)
                    else:
                        grouped.setdefault(owner_key, []).append(record)
                names = _context_entities(known)
                for session_key, members in grouped.items():
                    members.sort(key=chrono_key)
                    members_fp = _members_fp([m.record_id for m in members])
                    if index.membership_done(
                        EXTRACT_GRAPH_SESSION_STAGE, namespace, members_fp
                    ) or all(
                        index.graph_sources.get((namespace, m.record_id)) == m.content_fingerprint
                        for m in members
                    ):
                        already += len(members)
                        continue
                    allowed: list[str] = []
                    if ctx.find_entities is not None:
                        try:
                            allowed = list(
                                await ctx.find_entities("\n".join(m.content for m in members))
                            )
                        except Exception as exc:  # an enhancer, never a gate
                            _log.warning("extract_graph.entities_failed", error=str(exc))
                    context = EdgeContext(
                        reference_time=members[-1].valid_from,
                        entities=names,
                        allowed_entities=allowed,
                    )
                    try:
                        edges = await session_extract(_session_transcript(members), context)
                    except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                        errors.append(f"{namespace}:{session_key}: {exc}")
                        _log.warning(
                            "extract_graph.extract_failed", session_key=session_key, error=str(exc)
                        )
                        continue  # no watermark: retried next sweep
                    for member in members:
                        done[member.record_id] = member.content_fingerprint
                    sessions_done.append((session_key, members_fp))
                    for edge in edges:
                        if edge.confidence < min_conf:
                            continue
                        cited = _cited(members, edge.episode_indices)
                        # The resolver's trust guard judges the least trusted turn.
                        owner = min(cited, key=lambda m: (m.trust, chrono_key(m)))
                        extracted.append((owner, [edge]))
                        parent_sets.append(cited)
                sources = loose
            for record, edge_context in zip(sources, _edge_contexts(sources, known), strict=True):
                if (
                    index.graph_sources.get((namespace, record.record_id))
                    == record.content_fingerprint
                ):
                    already += 1
                    continue
                if ctx.extract_edges is None:
                    continue  # only the window calls extract here (no extract_edges role)
                try:
                    edges = await ctx.extract_edges(record.content, edge_context)
                except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                    errors.append(f"{record.record_id}: {exc}")
                    _log.warning(
                        "extract_graph.extract_failed", record_id=record.record_id, error=str(exc)
                    )
                    continue  # no watermark: retried next sweep
                done[record.record_id] = record.content_fingerprint
                extracted.append((record, [e for e in edges if e.confidence >= min_conf]))
                parent_sets.append([record])
            if resolving != "off" and any(edges for _, edges in extracted):
                # GP-7: one resolution pass (at most one batched LLM call) per sweep.
                extracted, counts = await _resolve_edges(
                    ctx, namespace, resolving, [*known, *sources], extracted, parent_sets
                )
                for name, count in counts.items():
                    resolution[name] = resolution.get(name, 0) + count

            async def emit(
                edge: ExtractedEdge,
                parents: list[MemoryRecord],
                namespace: str = namespace,
                existing: dict[str, MemoryRecord] = existing,
                kidx: graph_keys.EdgeKeyIndex = kidx,
                handles: dict[str, MemoryRecord] = handles,
            ) -> None:
                """One extracted edge -> a fact record (+ ``asserted`` LINKs) whose
                parents are ``parents``: the source record, or the session turns
                the edge cites (#20)."""
                nonlocal written, linked, skipped, quarantined, provenance, invalidated
                if edge.confidence < min_conf:
                    return
                if allowed_rels and edge.rel.strip().lower() not in allowed_rels:
                    skipped += 1  # G-6: outside the declared ontology
                    return
                key = _edge_key(namespace, edge, dated=dated_events, scoped=scoped_keys)
                dup: MemoryRecord | None = existing.get(key)
                ebase = ""
                if key_v2:
                    extras: list[object] = []
                    if dated_events and edge.kind == "event":
                        extras = [
                            (edge.valid_from or "")[:10],
                            sorted(set(_NUMERAL.findall(edge.fact))),
                        ]
                    ebase = graph_keys.base_key(
                        namespace, edge.kind, edge.src_entity, edge.rel, edge.dst_entity, extras
                    )
                    occ = graph_keys.occurrence_of(
                        f"{p.content_fingerprint}@{p.valid_from.isoformat()}" for p in parents
                    )
                    decision = kidx.decide(ebase, occ, alias_keys=(key,))
                    dup = handles.get(decision.existing_id) if decision.existing_id else None
                    outcome = decision.outcome
                    if outcome == "duplicate" and decision.via != "key" and dup is not None:
                        fresh = await ctx.storage.get_record(dup.record_id)
                        if fresh is None or not _live(fresh):
                            outcome = "new_occurrence"  # the index is stale: it was closed since
                            dup = None
                    tap.event(
                        "derive.graph.key",
                        key_v=graph_keys.KEY_V,
                        outcome=outcome,
                        via=decision.via,
                        kind=edge.kind,
                        key=decision.key,
                        base=ebase,
                        legacy_key=key,
                    )
                    key = decision.key
                else:
                    tap.event(
                        "derive.graph.key",
                        key_v=1,
                        outcome="duplicate" if dup is not None else "new",
                        kind=edge.kind,
                        key=key,
                    )
                if dup is not None:
                    skipped += 1
                    # GR-9: a verbatim duplicate (same src, rel, dst and kind) adds
                    # its source episode to the fact's provenance; no LLM call.
                    for parent in parents:
                        if await _add_edge_provenance(ctx, dup, edge, parent):
                            provenance += 1
                    return
                trusts = [p.trust for p in parents]
                # GP-1: an event edge drops its attribute (add-only, never
                # superseded); kind, rel and dst persist as tags.
                attribute, tags = edge_fact_key(edge, edge.src_entity, protected, cardinality)
                # I55: the edge is about what its source turn(s) are about (tags exist
                # only when the perspective layer is on)
                tags = [*tags, *(t for t in inherit_perspective_tags(parents) if t not in tags)]
                if key_v2:
                    tags.append(graph_keys.base_tag(ebase))
                fact = MemoryRecord(
                    namespace=namespace,
                    memory_type="semantic",
                    content=edge.fact,
                    entity=edge.src_entity,
                    attribute=attribute,
                    tags=tags,
                    valid_from=_edge_valid_from(edge, parents[-1].valid_from),
                    valid_to=_edge_valid_to(edge) if close_ended else None,
                    source=SourceInfo(
                        role=constants.DERIVED_ROLE,
                        channel="extract_graph",
                        message_id=key,
                        parents=[p.record_id for p in parents],
                    ),
                )
                if ctx.write_fact is not None:
                    # The semantic door: firewall (N2, cap = the sources' trust),
                    # M5 dedup and the M4 ladder, so a state edge supersedes the
                    # older value; event edges carry no attribute and are ADDed.
                    stored = await ctx.write_fact(fact, trusts)
                    existing[key] = stored
                    if key_v2:
                        handles[stored.record_id] = stored
                        kidx.add(stored.record_id, _live(stored), key=key, base=ebase)
                    if stored.quarantined:
                        quarantined += 1  # held content gains no graph reach (E1)
                        return
                    if stored.record_id != fact.record_id:
                        skipped += 1  # merged into / rejected by an existing fact
                        return
                    fact = stored
                else:
                    # Derived trust never exceeds the source (E1): the cap rides
                    # the same firewall screening as every other derived write.
                    fact, reasons = await screen(fact, trusts)
                    payload: dict[str, object] = {
                        "record": fact.model_dump(mode="json"),
                        "extract_graph": {"source_record_id": parents[0].record_id},
                    }
                    if reasons:
                        payload["firewall"] = {"reasons": reasons}
                    await append(
                        MemoryEvent(
                            kind=EventKind.WRITE,
                            namespace=namespace,
                            actor="system",
                            payload=payload,
                        )
                    )
                    existing[key] = fact
                    if key_v2:
                        handles[fact.record_id] = fact
                        kidx.add(fact.record_id, _live(fact), key=key, base=ebase)
                    if reasons:
                        quarantined += 1  # held content gains no graph reach (E1)
                        _log.warning(
                            "memory.quarantined",
                            namespace=namespace,
                            record_id=fact.record_id,
                            reasons=reasons,
                        )
                        return
                written += 1
                if contradictions and edge.kind == "state" and ctx.write_fact is not None:
                    invalidated += await _retract_contradicted(ctx, namespace, fact, trusts)
                for parent in parents:
                    # Associate the fact with its source (non-reserved rel: budget
                    # applies). A saturated source keeps the record, skips the link.
                    if ctx.graph is not None:
                        try:
                            await assert_within_budget(ctx.graph, parent.record_id)
                        except ConflictError:
                            _log.warning(
                                "extract_graph.link_budget_full", record_id=parent.record_id
                            )
                            continue
                    await append(
                        link_event(
                            namespace,
                            parent.record_id,
                            fact.record_id,
                            "asserted",
                            # GP-10: an edge is never stronger than its
                            # confidence or the trust of what it links.
                            weight=max(0.0, min(1.0, edge.confidence, parent.trust, fact.trust)),
                            reason="extract_graph",
                            actor="system",
                        )
                    )
                    linked += 1

            for (_owner, edges), parents in zip(extracted, parent_sets, strict=True):
                for edge in edges:
                    await emit(edge, parents)
            if done:
                # GP-8a: one watermark marker per namespace per sweep, appended
                # after the facts, so a crash mid-sweep re-extracts idempotently.
                await append(
                    MemoryEvent(
                        kind=EventKind.MARKER,
                        namespace=namespace,
                        actor="system",
                        payload={
                            "marker": GRAPH_EXTRACTED_MARKER,
                            "stage": "extract_graph",
                            "sources": [
                                {"record_id": rid, "content_fingerprint": fp}
                                for rid, fp in done.items()
                            ],
                        },
                    )
                )
                for record_id, fp in done.items():
                    index.graph_sources[(namespace, record_id)] = fp
            for session_key, members_fp in sessions_done:
                # #20: the session watermark (a re-consolidated session with the
                # same live turns is not sent again).
                await append(
                    stage_marker(
                        namespace, EXTRACT_GRAPH_SESSION_STAGE, session_key, members_fp=members_fp
                    )
                )
                index.mark(
                    EXTRACT_GRAPH_SESSION_STAGE,
                    namespace,
                    session_key,
                    done=True,
                    members_fp=members_fp,
                )
    stats: dict[str, object] = {
        "status": "ok" if not errors else "partial",
        "edges_written": written,
        "edges_invalidated": invalidated,
        "links": linked,
        "skipped_existing": skipped,
        "skipped_sources": already,
        "quarantined": quarantined,
        "provenance_added": provenance,
    }
    if resolving != "off":
        stats.update(resolution)
    if errors:
        stats["errors"] = errors
    return stats


async def _add_edge_provenance(
    ctx: PipelineContext, fact: MemoryRecord, edge: ExtractedEdge, source: MemoryRecord
) -> bool:
    """GR-9: record ``source`` as one more episode stating ``fact``'s edge.

    Only a verbatim match counts: the fact's ``kind:`` tag must equal the edge's
    kind (the (src, rel, dst) part is the key already matched). The episode is
    added as an ``edge_source:<id>`` tag through an add-only lifecycle delta, so
    the fact's trust, parents and erasure lineage are unchanged. Returns True when
    a tag was added (a source already counted adds nothing).
    """
    if ctx.append_event is None or f"kind:{edge.kind}" not in fact.tags:
        return False
    tag = f"{constants.EDGE_SOURCE_TAG_PREFIX}{source.record_id}"
    current = await ctx.storage.get_record(fact.record_id)
    if current is None or current.status is RecordStatus.DELETED:
        return False
    if source.record_id in current.source.parents or tag in current.tags:
        return False
    await ctx.append_event(
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace=current.namespace,
            actor="system",
            payload={
                "record_id": current.record_id,
                "set": {"tags_add": [tag]},
                "reason": "edge_provenance",
            },
        )
    )
    return True
