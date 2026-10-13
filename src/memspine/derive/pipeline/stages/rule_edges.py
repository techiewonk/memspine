"""Derive stage: rule_edges (rule-derived edges) (F-15)."""

from __future__ import annotations

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.rule_edges import BECAUSE_REL, causal_links, kinship_relations
from memspine.derive.pipeline.stages.context import PipelineContext, _int_option, _local_screen
from memspine.exceptions import ConflictError
from memspine.memories.associative.entities import canonical_entity
from memspine.memories.associative.links import assert_within_budget, link_event
from memspine.memories.semantic.write_pipeline import edge_fact_key
from memspine.prompts.models import ExtractedEdge
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo, chrono_key


def rule_edges_options(config: MemspineConfig) -> dict[str, object] | None:
    """``memories.associative.policies.rule_edges`` (W12): None when off, else its
    options (``true`` = defaults: ``causal`` and ``kinship`` on, ``lookback``
    ``RULE_EDGES_LOOKBACK``, ``min_overlap`` ``RULE_EDGES_MIN_OVERLAP``)."""
    mem = config.memories.get("associative")
    if mem is None or not mem.enabled:
        return None
    raw = mem.policies.get("rule_edges")
    if not raw:
        return None
    return dict(raw) if isinstance(raw, dict) else {}


def _rule_turns(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """The turns rule edges read: live, unheld, not instruction-flagged, not cues;
    in time order."""
    return sorted(
        (
            r
            for r in records
            if r.status is RecordStatus.ACTIVATED
            and not r.quarantined
            and not r.instruction_flag
            and constants.CUE_TAG not in r.tags
        ),
        key=chrono_key,
    )


async def rule_edges(ctx: PipelineContext) -> dict[str, object]:
    """W12 / G08 (plan v3.2, ADR-061): deterministic graph edges, no model.

    Over each namespace's live episodic turns (time order):

    - **causal** (:func:`memspine.core.rule_edges.causal_links`): a ``because`` LINK
      from the effect turn to the cause turn, weight ``RULE_EDGE_WEIGHT`` capped by
      both endpoints' trust (GP-10), reason ``rule:<cue>``. An existing live edge
      is not re-sent; an endpoint at its link budget (M13.6) skips the edge.
    - **kinship** (:func:`memspine.core.rule_edges.kinship_relations`): "my sister
      Ana" -> one semantic fact "Ana is Caroline's sister" (entity ``Ana``, tags
      ``kind:event``, ``rel:sister_of``, ``dst:caroline``), parent = the turn, written
      through the semantic door (``write_fact``; else screened and appended) with
      the turn's trust as cap. Keyed by ``(person, relation, owner)``, so a re-run
      writes nothing new.

    Skipped unless the policy is on and associative memory projects the graph."""
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    opts = rule_edges_options(ctx.config)
    if opts is None:
        return {"status": "skipped", "reason": "memories.associative.policies.rule_edges is off"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "associative memory is off (no graph)"}
    graph = ctx.graph
    append = ctx.append_event
    causal = bool(opts.get("causal", True))
    kinship = bool(opts.get("kinship", True))
    lookback = _int_option(opts, "lookback", constants.RULE_EDGES_LOOKBACK)
    min_overlap = _int_option(opts, "min_overlap", constants.RULE_EDGES_MIN_OVERLAP)
    screen = ctx.screen or _local_screen(ctx)
    protected = ctx.config.firewall.protected_keys
    linked = facts = existing_links = budget_full = quarantined = 0
    for namespace in await ctx.storage.list_namespaces():
        async with ctx.lock(namespace):
            turns = _rule_turns(await ctx.storage.list_records(namespace, "episodic"))
            by_id = {t.record_id: t for t in turns}
            if causal:
                pairs = [(t.record_id, t.content) for t in turns]
                for link in causal_links(pairs, lookback=lookback, min_overlap=min_overlap):
                    src, dst = by_id[link.src], by_id[link.dst]
                    if any(
                        e.rel_type == BECAUSE_REL and e.src == src.record_id and e.weight > 0
                        for e in await graph.edges_of(src.record_id)
                        if e.dst == dst.record_id
                    ):
                        existing_links += 1
                        continue
                    weight = min(constants.RULE_EDGE_WEIGHT, src.trust, dst.trust)
                    if weight <= 0:
                        continue
                    try:
                        await assert_within_budget(graph, src.record_id)
                        await assert_within_budget(graph, dst.record_id)
                    except ConflictError:
                        budget_full += 1
                        continue
                    await append(
                        link_event(
                            namespace,
                            src.record_id,
                            dst.record_id,
                            BECAUSE_REL,
                            weight=weight,
                            reason=f"rule:{link.cue}",
                            actor="system",
                        )
                    )
                    linked += 1
            if not kinship:
                continue
            done = {
                r.source.message_id
                for r in await ctx.storage.list_records(namespace, "semantic")
                if r.source.channel == RULE_EDGES_CHANNEL and r.source.message_id
            }
            for turn in turns:
                for found in kinship_relations(turn.content):
                    key = fingerprint_payload(
                        {
                            RULE_EDGES_CHANNEL: [
                                namespace,
                                canonical_entity(found.person),
                                found.relation,
                                found.owner,
                            ]
                        }
                    )
                    if key in done:
                        continue
                    done.add(key)
                    owner = found.owner if found.owner != "user" else "the user"
                    edge = ExtractedEdge(
                        src_entity=found.person,
                        rel=f"{found.relation}_of",
                        dst_entity=found.owner,
                        fact=f"{found.person} is {owner.title()}'s {found.relation}.",
                        kind="event",
                    )
                    attribute, tags = edge_fact_key(edge, found.person, protected)
                    fact = MemoryRecord(
                        namespace=namespace,
                        memory_type="semantic",
                        content=edge.fact,
                        entity=found.person,
                        attribute=attribute,
                        tags=tags,
                        valid_from=turn.valid_from,
                        source=SourceInfo(
                            role=constants.DERIVED_ROLE,
                            channel=RULE_EDGES_CHANNEL,
                            message_id=key,
                            parents=[turn.record_id],
                        ),
                    )
                    if ctx.write_fact is not None:
                        stored = await ctx.write_fact(fact, [turn.trust])
                        quarantined += stored.quarantined
                        facts += not stored.quarantined
                        continue
                    fact, reasons = await screen(fact, [turn.trust])
                    payload: dict[str, object] = {"record": fact.model_dump(mode="json")}
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
                    quarantined += bool(reasons)
                    facts += not reasons
    return {
        "status": "ok",
        "links": linked,
        "facts": facts,
        "existing_links": existing_links,
        "budget_full": budget_full,
        "quarantined": quarantined,
    }


#: W12: the source channel of a rule kinship fact (its idempotency key rides
#: ``source.message_id``).
RULE_EDGES_CHANNEL = "rule_edges"
