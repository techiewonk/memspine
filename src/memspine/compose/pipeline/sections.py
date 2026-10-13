"""Compose: the sections part of context assembly, moved out of the engine (F-11)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from memspine.compose.pack import AssembledContext, estimate_tokens
from memspine.compose.pack import ranked as rank_pairs
from memspine.compose.pipeline.render import _unrendered
from memspine.config import constants
from memspine.core.escaping import escape_markers
from memspine.core.event_date import happened_of
from memspine.core.profile_pack import pack_profile, render_packed_profile
from memspine.core.read_filters import active_date_filter
from memspine.exceptions import StorageError
from memspine.models.encoders.query.perspective import Subject, record_view
from memspine.models.encoders.query.shape import (
    content_words,
    is_aggregation,
    is_count,
    is_duration,
    is_novelty,
    is_temporal,
)
from memspine.models.encoders.query.temporal_query import sentences
from memspine.observability.logging import get_logger
from memspine.read.lead import (
    card_line,
    count_terms,
    distinct_occurrences,
    event_day,
    graph_fact_line,
    is_standing_instruction,
    mentions_any,
    mentions_event,
    query_names,
    render_graph_facts,
    render_occurrences,
    render_profile,
    render_standing,
    render_timeline,
    timeline_line,
)
from memspine.read.pipeline.state import _TAINT_ARCHIVED_TAG, CUE_TAG
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo, chrono_key
from memspine.store.scope import grant_allows
from memspine.trust.privacy import inherited_consent, inherited_pii
from memspine.workers.pipelines import entity_summary_name, entity_summary_node

_log = get_logger(__name__)


def _without_entity(text: str, entity: str) -> str:
    """``text`` without a leading ``entity`` name (as timelines render a fact)."""
    normal = " ".join(text.split())
    if normal[: len(entity)].casefold() == entity.casefold():
        return normal[len(entity) :].lstrip(" :")
    return normal


async def _graph_facts_section(
    eng: Any, ns: str, query: str, allowance: int
) -> MemoryRecord | None:
    """GP-5 (``read.cards_include_edges``): the graph facts block, or None.

    The edge facts (records tagged ``rel:``) the graph walk reaches from the
    entities the query names, live and superseded alike, one line each:
    ``[2023-05-01 → present] Melanie read "X" (sources: 2)``, a superseded one
    with its end date. ``sources`` counts the live episodes stating the fact
    (its parents plus the ``edge_source:`` provenance of duplicates, GR-9). A
    fact's text goes through the per-record wrappers (marker escaping, the
    instruction and untrusted-note wrappers); a fact repeated verbatim is shown
    once. Lines are kept nearest first while the block fits ``allowance``, then
    shown oldest first. The block's parents are the facts it shows, so the read
    below leaves them out (no record twice)."""
    if allowance <= estimate_tokens(constants.GRAPH_FACTS_MARKER):
        return None
    try:
        reached = await eng._graph_walk(ns, query, history=True)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.graph_facts_failed", namespace=ns, error=str(exc))
        return None
    storage = eng._require_started()
    lines: dict[str, tuple[MemoryRecord, str]] = {}
    kept: list[MemoryRecord] = []
    integrity = eng._integrity()
    live = integrity.enabled and integrity.live_reevaluation
    for record, _hops in reached:
        if not any(tag.startswith("rel:") for tag in record.tags):
            continue
        if live:
            # B4': the block is not a search, so it re-judges admission itself.
            effective = await eng.effective_trust(record.record_id)
            if not integrity.admits(effective):
                continue
            record = record.model_copy(update={"trust": effective})
        shown = eng._wrap_for_context(record)
        text = " ".join(shown.content.split())
        if any(text == other for _, other in lines.values()):
            continue  # one line per fact, however many records restate it
        line = graph_fact_line(shown, await eng._edge_sources(storage, record))
        trial = {**lines, record.record_id: (shown, line)}
        if estimate_tokens(render_graph_facts([v[1] for v in eng._by_time(trial)])) <= allowance:
            lines = trial
            kept.append(shown)
    if not kept:
        return None
    block = eng._lead_record(ns, render_graph_facts([v[1] for v in eng._by_time(lines)]), kept)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.GRAPH_FACTS_TAG]})


async def _current_state_view(
    eng: Any, ns: str, scored: list[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """C4': annotate keyed facts with CURRENT / HISTORY from the bi-temporal chain.

    Only an open fact (``valid_to is None``) is labelled CURRENT; a closed one that
    is still activated (a backfill, a contested statement) renders as history. A
    HISTORY entry is a fact that was superseded or ended: it has a ``valid_to``, is
    not quarantined, was not archived by a taint rollback or repair, and passes the
    admission threshold. Each entry is inflated, dated against its own event time
    and wrapped like any other context record.
    """
    storage = eng._require_started()
    keyed = [r for r, _ in scored if r.entity is not None and r.attribute is not None]
    if not keyed:
        return scored
    tainted = await eng._taint_archived_ids()
    by_key: dict[tuple[str, str], list[MemoryRecord]] = {}
    for record in await storage.list_records(ns, "semantic"):
        if record.entity is None or record.attribute is None or record.valid_to is None:
            continue
        if record.status not in (RecordStatus.ARCHIVED, RecordStatus.ACTIVATED):
            continue
        if record.quarantined or record.record_id in tainted or "retract" in record.tags:
            continue
        by_key.setdefault((record.entity, record.attribute), []).append(record)
    out: list[tuple[MemoryRecord, float]] = []
    for record, score in scored:
        if record.entity is None or record.attribute is None:
            out.append((record, score))
            continue
        if record.valid_to is not None:
            text = (
                f"{constants.HISTORY_MARKER} {record.valid_from:%Y-%m-%d} to "
                f"{record.valid_to:%Y-%m-%d}: {record.content}"
            )
            if "disputed" in record.tags:
                text += " [DISPUTED: another source of equal standing states a different value]"
            out.append((record.model_copy(update={"content": text}), score))
            continue
        candidates = sorted(
            (
                h
                for h in by_key.get((record.entity, record.attribute), [])
                if h.record_id != record.record_id
            ),
            key=lambda h: h.valid_from,
            reverse=True,
        )
        history: list[MemoryRecord] = []
        for past_fact in candidates:
            if len(history) >= 3:
                break
            shown = await eng._history_view(past_fact)
            if shown is not None:
                history.append(shown)
        text = f"{constants.CURRENT_STATE_MARKER}{record.valid_from:%Y-%m-%d}): {record.content}"
        if "disputed" in record.tags:
            text += " [DISPUTED: another source of equal standing states a different value]"
        if history:
            past = "; ".join(
                f"{h.valid_from:%Y-%m-%d} to {h.valid_to:%Y-%m-%d}: {h.content}"
                for h in history
                if h.valid_to is not None
            )
            text += f"\n{constants.HISTORY_MARKER} {past}"
        out.append((record.model_copy(update={"content": text}), score))
    return out


def _lead_clean(eng: Any, record: MemoryRecord) -> bool:
    """H22: an entry may enter the lead section only unwrapped: not instruction
    flagged and not below the untrusted-note threshold (those records still
    reach the context through retrieval, wrapped)."""
    integrity = eng._integrity()
    wrap_below = integrity.untrusted_wrap_below if integrity.enabled else 0.0
    return not record.instruction_flag and record.trust >= wrap_below


def _lead_record(
    eng: Any,
    ns: str,
    content: str,
    parts: list[MemoryRecord],
    extra_tags: Sequence[str] = (),
) -> MemoryRecord:
    """A synthetic, never-stored context record for one lead block.

    Its trust is the least of its entries' (a block is never more trusted than
    what it shows) and its parents are the entries, so the provenance of every
    line stays visible to the caller. Its purposes are the intersection of the
    entries' and its PII tier their highest (#50), so the purpose gate and the
    remote-LLM gate judge the block as strictly as its strictest entry.
    """
    return MemoryRecord(
        namespace=ns,
        memory_type="semantic",
        content=content,
        tags=[constants.LEAD_TAG, *extra_tags],
        valid_from=max(p.valid_from for p in parts),
        trust=min(p.trust for p in parts),
        source=SourceInfo(role="system", channel="lead", parents=[p.record_id for p in parts]),
        consent_tags=inherited_consent(
            (p.consent_tags for p in parts), eng._config().consent.untagged
        ),
        pii_tier=inherited_pii(p.pii_tier for p in parts),
    ).as_engine_block()


async def _standing_block(eng: Any, ns: str) -> tuple[MemoryRecord, list[MemoryRecord]] | None:
    """H22: the user's stated preferences and standing requests, newest wins a slot.

    Candidates are user-role episodic turns and semantic facts that match the
    standing cue rules, at or above ``read.standing_min_trust``, and pass the
    context gates (status, quarantine, admission, live re-evaluation). An
    external document or tool output never qualifies, whatever it says. The
    E1 instruction flag does not exclude a user's own request (it is the point
    of the block), but a quarantined one never shows.
    """
    read_cfg = eng._config().read
    storage = eng._require_started()
    found: list[MemoryRecord] = []
    for memory_type in ("episodic", "semantic"):
        for record in await storage.list_records(ns, memory_type):
            if record.source.role != "user" or "atomic_fact" in record.tags:
                continue
            # Cheap test first: the cue rules on the (inflated) text, then the
            # gates, which can cost an effective-trust walk under live re-eval.
            inflated = eng._inflate_all([record], ns)
            if not inflated or not is_standing_instruction(
                inflated[0].content, wide=read_cfg.standing_patterns == "wide"
            ):
                continue
            view = await eng._live_view(record)
            if view is None or view.trust < read_cfg.standing_min_trust:
                continue
            shown = inflated[0].model_copy(update={"trust": view.trust})
            # A standing request is imperative by nature, so the E1 flag is
            # expected here; the block labels it as the user's words, not as
            # system instructions. The untrusted-note threshold still applies.
            if eng._lead_clean(shown.model_copy(update={"instruction_flag": False})):
                found.append(shown)
    if not found:
        return None
    found.sort(key=chrono_key)
    kept = found[-constants.LEAD_STANDING_MAX :]
    return eng._lead_record(ns, render_standing(kept), kept), kept


async def _timeline_blocks(
    eng: Any, ns: str, scored: list[tuple[MemoryRecord, float]]
) -> list[MemoryRecord]:
    """H22: one dated timeline per topic entity of the retrieved keyed facts.

    Entities come from the best-scored keyed semantic candidates. Each timeline
    lists every live fact on the entity (any attribute) and its superseded
    history, oldest first, keeping the newest ``read.timeline_items``. An entry
    passes the same gates as a context record (live view for current facts, the
    C4' history view for superseded ones; taint rollbacks and retractions never
    show) and must be clean (:meth:`_lead_clean`). An entity with fewer than two
    entries gets no timeline: retrieval already shows a single fact.
    """
    read_cfg = eng._config().read
    entities: list[str] = []
    for record, _ in rank_pairs(scored):
        if record.memory_type != "semantic" or not record.entity:
            continue
        if record.entity.lower() not in (e.lower() for e in entities):
            entities.append(record.entity)
        if len(entities) >= read_cfg.timeline_entities:
            break
    if not entities:
        return []
    storage = eng._require_started()
    tainted = await eng._taint_archived_ids()
    by_entity: dict[str, list[MemoryRecord]] = {}
    for record in await storage.list_records(ns, "semantic"):
        if record.entity and CUE_TAG not in record.tags:
            by_entity.setdefault(record.entity.lower(), []).append(record)
    blocks: list[MemoryRecord] = []
    for entity in entities:
        entries: list[tuple[MemoryRecord, datetime | None]] = []
        for record in by_entity.get(entity.lower(), []):
            if record.quarantined or record.record_id in tainted or "retract" in record.tags:
                continue
            if record.status is RecordStatus.ACTIVATED:
                view = await eng._live_view(record)
                inflated = eng._inflate_all([view], ns) if view is not None else []
                shown = inflated[0] if inflated else None
            elif record.status is RecordStatus.ARCHIVED and record.valid_to is not None:
                shown = await eng._history_view(record)
            else:
                continue
            if shown is None or not eng._lead_clean(shown):
                continue
            entries.append((shown, record.valid_to))
        if len(entries) < 2:
            continue
        entries.sort(key=lambda e: chrono_key(e[0]))
        entries = entries[-read_cfg.timeline_items :]
        lines = [timeline_line(r, entity, until) for r, until in entries]
        blocks.append(eng._lead_record(ns, render_timeline(entity, lines), [r for r, _ in entries]))
    return blocks


async def _lead_section(
    eng: Any, ns: str, scored: list[tuple[MemoryRecord, float]], budget_tokens: int
) -> tuple[list[MemoryRecord], list[MemoryRecord]]:
    """H22: (standing preferences, timelines) within the lead sub-budget.

    Standing preferences are filled first, then timelines in entity order; a
    block that does not fit is skipped. Each block passes the untrusted-note
    wrapper (its trust is its weakest entry's), like any context record.
    """
    read_cfg = eng._config().read
    allowance = min(read_cfg.lead_budget_tokens, budget_tokens // 2)
    standing: list[MemoryRecord] = []
    timelines: list[MemoryRecord] = []
    used = 0
    candidates: list[tuple[list[MemoryRecord], MemoryRecord]] = []
    if read_cfg.standing_instructions:
        block = await eng._standing_block(ns)
        if block is not None:
            candidates.append((standing, block[0]))
    if read_cfg.topic_timelines:
        candidates.extend((timelines, b) for b in await eng._timeline_blocks(ns, scored))
    for target, record in candidates:
        wrapped = eng._wrap_untrusted(record)
        cost = estimate_tokens(wrapped.content)
        if used + cost > allowance:
            continue
        target.append(wrapped)
        used += cost
    return standing, timelines


async def _history_view(eng: Any, record: MemoryRecord) -> MemoryRecord | None:
    """C4': one HISTORY entry as the model sees it, or None when it may not be shown.

    A record archived by a taint rollback or repair is never shown: the
    durable :data:`_TAINT_ARCHIVED_TAG` covers logs that no longer hold the
    event (:meth:`_taint_archived_ids` covers records archived before the tag).

    Admission uses the stored trust, capped under live re-evaluation by the
    current view of each parent (an archived record's own effective trust is 0 by
    definition, so it cannot be used). The entry is inflated, dated against its
    own event time and wrapped (instruction flag, untrusted note).
    """
    if record.memory_type == "shared" or CUE_TAG in record.tags:
        return None
    if not eng._consent_ok(record):
        return None  # #50: the read's purpose may not see it
    if record.status is RecordStatus.ARCHIVED and _TAINT_ARCHIVED_TAG in record.tags:
        return None  # N3: rolled back, even when the log no longer says so
    integrity = eng._integrity()
    if integrity.enabled:
        trust = record.trust
        if integrity.live_reevaluation:
            storage = eng._require_started()
            grants_cache: dict[str, Mapping[str, frozenset[str] | None]] = {}
            for parent_id in record.source.parents:
                parent = await storage.get_record(parent_id)
                parent_ns = parent.namespace if parent is not None else record.namespace
                view = integrity.view_trust(
                    await eng.effective_trust(parent_id), parent_ns, record.namespace
                )
                if parent is not None and not grant_allows(
                    record.namespace,
                    parent.namespace,
                    parent.memory_type,
                    await eng._grants_cached(record.namespace, grants_cache),
                ):
                    view = 0.0  # N6: the grant that carried this parent is gone
                trust = min(trust, view * integrity.derivation_decay)
        if not integrity.admits(trust):
            return None
        record = record.model_copy(update={"trust": trust})
    try:
        record = eng._inflate.inflate(record)
    except StorageError:
        _log.warning(
            "memory.inflate_failed", namespace=record.namespace, record_id=record.record_id
        )
        return None
    if eng._config().read.resolve_relative_dates:
        record = eng._annotate_dates(record)
    return eng._wrap_untrusted(eng._wrap_instruction(record))


async def _replay_neighbour(eng: Any, record_id: str, ns: str) -> MemoryRecord | None:
    """C7': a replayed neighbour turn, gated, inflated and decorated; None if not shown.

    #37: a neighbour outside the active date filter is not shown."""
    raw = await eng._require_started().get_record(record_id)
    date_filter = active_date_filter()
    if raw is not None and date_filter is not None and not date_filter.matches(raw):
        return None
    view = await eng._live_view(raw) if raw is not None else None
    inflated = eng._inflate_all([view], ns) if view is not None else []
    if not inflated:
        return None
    decorated = await eng._decorate(ns, [(inflated[0], 0.0)], expand_claims=False)
    return decorated[0][0] if decorated else None


async def _cards_section(
    eng: Any,
    ns: str,
    query: str,
    budget_tokens: int,
    session_id: str | None = None,
    *,
    strong: bool = False,
) -> MemoryRecord | None:
    """G1b: the cards header, or None (``read.cards: off``, or no fact to show).

    The namespace's mined facts relevant to ``query`` come from the same hybrid
    search, restricted to ``atomic_fact`` records, so they pass every search
    gate (status, quarantine, admission, live re-evaluation). Each gets the
    per-record wrappers, then one ``[said YYYY-MM-DD] Entity: fact`` line (the
    date it was said: its earliest source turn; no date without one), best
    first while the block, rendered exactly so, fits ``read.cards_budget_share x
    budget_tokens``; the kept lines are shown in said order. Under B9 a card
    whose source turns sit below ``integrity.claims_only_below`` carries
    :data:`constants.CLAIM_MARKER`, as its claim would in the routed read.
    """
    read_cfg = eng._config().read
    if read_cfg.cards != "header" or eng._cards_gated(query, strong=strong):
        return None
    temporal = is_temporal(query)
    # B3 (ADR-055): ``cards_temporal: event_dates`` shows a date question only the
    # cards with a happened date, happened date first, instead of no cards.
    event_only = temporal and read_cfg.cards_temporal == "event_dates"
    if temporal and not event_only and read_cfg.cards_skip_temporal:
        return None
    allowance = int(budget_tokens * read_cfg.cards_budget_share)
    if allowance <= estimate_tokens(constants.CARDS_MARKER):
        return None
    # A2 (ADR-055): list cards only for list and count questions.
    drop_lists = read_cfg.list_cards_only_aggregate and not (
        is_aggregation(query) or is_count(query)
    )
    card_hide: Callable[[MemoryRecord], bool] | None = None
    if event_only or drop_lists:

        def card_hide(r: MemoryRecord) -> bool:
            return (event_only and happened_of(r) is None) or (
                drop_lists and constants.LIST_CARD_TAG in r.tags
            )

    k = read_cfg.cards_top_k
    hits = await eng._search(
        query, ns, k, tags=["atomic_fact"], keep_k=k, session_id=session_id, hide=card_hide
    )
    if eng._header_abstains(hits):
        return None
    # Smoke 2026-10-05: the miner's event date is often the session date, and the
    # reader trusted it over the H1-resolved raw turn. Label each card with the
    # date it was SAID (its earliest source turn); the raw turns carry event dates.
    # A-7: the labels are known before the fit, so the fit measures the real lines.
    storage = eng._require_started()
    integrity = eng._integrity()
    claim_below = integrity.claims_only_below if integrity.enabled else 0.0
    said: dict[str, datetime] = {}
    claims: set[str] = set()
    for record, _ in hits:
        parents = [await storage.get_record(pid) for pid in record.source.parents]
        found = [p for p in parents if p is not None]
        if found:
            said[record.record_id] = min(p.valid_from for p in found)
        if claim_below > 0.0 and await eng._from_low_trust(found, claim_below):
            claims.add(record.record_id)
    event_dates = read_cfg.cards_event_date or event_only
    kept: list[MemoryRecord] = []
    for record, _ in hits:
        trial = [*kept, eng._wrap_for_context(record)]
        text = eng._cards_text(trial, said, claims, event_dates, happened_first=event_only)
        if estimate_tokens(text) <= allowance:
            kept = trial
    if not kept:
        return None
    kept.sort(
        key=lambda r: (r.record_id in said, said.get(r.record_id, r.valid_from), chrono_key(r))
    )
    block = eng._lead_record(
        ns, eng._cards_text(kept, said, claims, event_dates, happened_first=event_only), kept
    )
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.CARDS_TAG]})


async def _from_low_trust(
    eng: Any, parents: list[MemoryRecord], threshold: float, _depth: int = 0
) -> bool:
    """B9 for a card: a source turn below ``threshold`` (view trust) makes it a claim.

    A parent that is itself a mined fact (a #30 list card's parents) is judged on
    its own source turns: the card is a claim when any of its facts would be."""
    live = eng._integrity().live_reevaluation
    storage = eng._require_started()
    for parent in parents:
        if parent.source.channel == "persona":
            continue
        if "atomic_fact" in parent.tags:
            if _depth >= constants.CLAIM_PARENT_DEPTH:
                continue
            grand = [await storage.get_record(pid) for pid in parent.source.parents]
            found = [g for g in grand if g is not None]
            if await eng._from_low_trust(found, threshold, _depth + 1):
                return True
            continue
        trust = await eng.effective_trust(parent.record_id) if live else parent.trust
        if trust < threshold:
            return True
    return False


def _cards_text(
    cards: list[MemoryRecord],
    said: dict[str, datetime] | None = None,
    claims: set[str] | None = None,
    event_dates: bool = False,
    *,
    happened_first: bool = False,
) -> str:
    dates = said or {}
    flagged = claims or set()
    lines = (
        card_line(
            r,
            dates.get(r.record_id),
            claim=r.record_id in flagged,
            # #29 (read.cards_event_date): the fact's happened date, when tagged.
            happened=happened_of(r) if event_dates else None,
            happened_first=happened_first,
        )
        for r in cards
    )
    return "\n".join([constants.CARDS_MARKER, *lines])


async def _slots_section(eng: Any, ns: str, query: str, budget_tokens: int) -> MemoryRecord | None:
    """W5 (plan v3.2, ``read.profile_slots_header``): the Λ-profile block: for each
    person the question names (else the asker, ``user``), their current keyed
    STATE facts (home, origin, job, favourites, attitudes …; one per key, the live
    one) as one line each. Slots tagged ``sensitive:*`` stay out unless
    ``read.profile_sensitive``. Under ``read.profile_scope_gate`` only for
    questions it applies to. None when nothing is known."""
    read_cfg = eng._config().read
    if not read_cfg.profile_slots_header:
        return None
    if read_cfg.profile_scope_gate and not await eng._applies_to_person(ns, query):
        return None
    if read_cfg.profile_subject_card:
        return await eng._subject_card(ns, query, budget_tokens)  # I50
    names = [n.lower() for n in query_names(query)]
    if not names:
        names = ["user"]
        if read_cfg.perspective_mode != "off":
            # I39: the asker's own profile, and none for a question about someone else
            qp = await eng._question_perspective(ns, query)
            if qp.about == "third":
                return None
            names = [qp.asker or "user"]
    slots: dict[str, list[MemoryRecord]] = {}
    for record in await eng._records(ns, "semantic"):
        if (
            record.status is not RecordStatus.ACTIVATED
            or record.quarantined
            or record.valid_to is not None
            or not record.entity
            or not record.attribute
            or record.entity.lower() not in names
            or "kind:state" not in record.tags
        ):
            continue
        if not read_cfg.profile_sensitive and any(t.startswith("sensitive:") for t in record.tags):
            continue
        if not eng._profile_line_relevant(query, record.content):
            continue  # I33: a slot the question does not bear on stays out
        slots.setdefault(record.entity, []).append(record)
    if not slots:
        return None
    allowance = max(1, int(budget_tokens * read_cfg.profile_budget_share))
    lines: list[str] = []
    kept: list[MemoryRecord] = []
    for entity in sorted(slots):
        for record in sorted(slots[entity], key=lambda r: r.attribute or ""):
            line = f"- {' '.join(record.content.split())}"
            if estimate_tokens("\n".join([constants.SLOTS_MARKER, *lines, line])) > allowance:
                break
            lines.append(line)
            kept.append(record)
    if not kept:
        return None
    block = eng._lead_record(ns, "\n".join([constants.SLOTS_MARKER, *lines]), kept)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.SLOTS_TAG]})


async def _subject_card(eng: Any, ns: str, query: str, budget_tokens: int) -> MemoryRecord | None:
    """I50 (``read.profile_subject_card``): the per-subject profile card. The live
    ``kind:state`` facts of ``ns`` are rolled up per subject (the record's ``sub:`` tags,
    else its entity); only the subjects the question's resolved persons name get a block
    (a person, the asker for "I / my", the assistant for "you", ``mother@caroline`` for
    "Caroline's mother"). One attribute holding several live values (cardinality many)
    lists them in time order; a single-valued one is its live (latest) value. The same
    sensitivity and I33 relevance gates as the slots block. None when no subject matches."""
    read_cfg = eng._config().read
    qp = await eng._question_perspective(ns, query)
    targets = list(qp.targets)
    for name in query_names(query):  # a name no turn has spoken under yet
        low = name.lower()
        anchor = any(k.endswith(f"@{low}") for t in targets for k in t.keys)
        if not anchor and all(low not in t.keys for t in targets):  # "Caroline's mother"
            targets.append(Subject(name.lower(), "named"))
    if not targets:
        return None
    cards: dict[str, dict[str, list[MemoryRecord]]] = {}
    for record in await eng._records(ns, "semantic"):
        if (
            record.status is not RecordStatus.ACTIVATED
            or record.quarantined
            or record.valid_to is not None
            or not record.entity
            or not record.attribute
            or "kind:state" not in record.tags
        ):
            continue
        if not read_cfg.profile_sensitive and any(t.startswith("sensitive:") for t in record.tags):
            continue
        rv = record_view(record)
        entity = record.entity.lower()
        for target in targets:
            if any(k in rv.subs or k == entity for k in target.keys):
                if eng._profile_line_relevant(query, record.content):  # I33
                    label = target.rel or target.id
                    cards.setdefault(label, {}).setdefault(record.attribute, []).append(record)
                break
    if not cards:
        return None
    allowance = max(1, int(budget_tokens * read_cfg.profile_budget_share))
    lines: list[str] = []
    kept: list[MemoryRecord] = []
    for label in sorted(cards):
        head = f"[{label}]"
        for attribute in sorted(cards[label]):
            for record in sorted(cards[label][attribute], key=lambda r: r.valid_from):
                line = f"- {' '.join(record.content.split())}"
                trial = [
                    constants.SLOTS_MARKER,
                    *lines,
                    *([] if head in lines else [head]),
                    line,
                ]
                if estimate_tokens("\n".join(trial)) > allowance:
                    break
                if head not in lines:
                    lines.append(head)
                lines.append(line)
                kept.append(record)
    if not kept:
        return None
    block = eng._lead_record(ns, "\n".join([constants.SLOTS_MARKER, *lines]), kept)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.SLOTS_TAG]})


async def _facts_to_sources(
    eng: Any, ns: str, scored: list[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """N54: each mined fact (``atomic_fact``) replaced, at its rank and score, by
    its live source turns (its parents); a turn already listed is not repeated.
    A fact whose sources are all gone or gated keeps its own slot."""
    storage = eng._require_started()
    out: list[tuple[MemoryRecord, float]] = []
    listed: set[str] = set()
    for record, score in scored:
        if "atomic_fact" not in record.tags:
            if record.record_id not in listed:
                out.append((record, score))
                listed.add(record.record_id)
            continue
        sources: list[MemoryRecord] = []
        for pid in record.source.parents:
            parent = await storage.get_record(pid)
            if parent is not None and parent.namespace == ns:
                view = await eng._live_view(parent)
                if view is not None:
                    sources.append(view)
        if not sources:
            out.append((record, score))
            continue
        for src in sources:
            if src.record_id not in listed:
                out.append((src, score))
                listed.add(src.record_id)
    return out


async def _recent_section(eng: Any, ns: str, query: str, budget_tokens: int) -> MemoryRecord | None:
    """C2 (``read.recent_exchanges``): the namespace's last N live episodic turns,
    oldest first, within ``RECENT_SHARE`` of the budget, leaving out a turn that is
    the question itself (the in-flight message). The turns stay readable below."""
    n = eng._config().read.recent_exchanges
    if n <= 0:
        return None
    try:
        turns = [
            r
            for r in await eng._records(ns, "episodic")
            if eng._context_eligible(r) and r.content.strip() != query.strip()
        ]
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.recent_section_failed", namespace=ns, error=str(exc))
        return None
    latest = sorted(turns, key=chrono_key)[-n:]
    allowance = int(budget_tokens * constants.RECENT_SHARE)
    lines: list[str] = []
    kept: list[MemoryRecord] = []
    for record in reversed(latest):  # newest first while it fits, shown oldest first
        line = f"- {escape_markers(' '.join(record.content.split()))}"
        if estimate_tokens("\n".join([constants.RECENT_MARKER, line, *lines])) > allowance:
            break
        lines.insert(0, line)
        kept.insert(0, record)
    if not kept:
        return None
    block = eng._lead_record(ns, "\n".join([constants.RECENT_MARKER, *lines]), kept)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.RECENT_TAG]})


async def _session_digest_section(
    eng: Any, ns: str, query: str, budget_tokens: int, session_id: str | None = None
) -> MemoryRecord | None:
    """N31 (``read.session_digest``; Memori session summaries, made extractive):
    for the episodic sessions of the first ``SESSION_DIGEST_HITS`` hits, one line
    each of the ``SESSION_DIGEST_SENTENCES`` sentences sharing most content words
    with the question, within ``SESSION_DIGEST_SHARE`` of the budget. The block's
    parents are the turns quoted; they are not hidden from the read."""
    if not eng._config().read.session_digest or eng._episodic is None:
        return None
    try:
        hits = await eng._search(
            query,
            ns,
            constants.SESSION_DIGEST_HITS,
            session_id=session_id,
            keep_k=constants.SESSION_DIGEST_HITS,
        )
        sessions = await eng._episodic.sessions(ns, constants.SESSION_GAP_MINUTES)
        session_of_id = {rid: s for s in sessions for rid in s.record_ids}
        chosen: list[Any] = []
        for record, _ in hits:
            s = session_of_id.get(record.record_id)
            if s is not None and all(s.session_key != c.session_key for c in chosen):
                chosen.append(s)
        chosen = chosen[: constants.SESSION_DIGEST_SESSIONS]
        if not chosen:
            return None
        live = {
            r.record_id: r for r in await eng._records(ns, "episodic") if eng._context_eligible(r)
        }
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.session_digest_failed", namespace=ns, error=str(exc))
        return None
    asked = content_words(query)
    allowance = int(budget_tokens * constants.SESSION_DIGEST_SHARE)
    lines: list[str] = []
    kept: list[MemoryRecord] = []
    for s in chosen:
        scored: list[tuple[int, int, str, MemoryRecord]] = []
        for order, rid in enumerate(s.record_ids):
            turn = live.get(rid)
            if turn is None:
                continue
            for sentence in sentences(turn.content):
                shared = len(asked & content_words(sentence))
                if shared:
                    scored.append((-shared, order, sentence, turn))
        if not scored:
            continue
        best = sorted(scored, key=lambda item: (item[0], item[1]))[
            : constants.SESSION_DIGEST_SENTENCES
        ]
        best.sort(key=lambda item: item[1])  # in the order they were said
        text = " … ".join(" ".join(sentence.split()) for _, _, sentence, _ in best)
        line = f"- {s.start:%Y-%m-%d}: {escape_markers(text)}"
        trial = [*lines, line]
        if estimate_tokens("\n".join([constants.SESSION_DIGEST_MARKER, *trial])) > allowance:
            break
        lines = trial
        kept += [turn for _, _, _, turn in best if turn not in kept]
    if not kept:
        return None
    block = eng._lead_record(ns, "\n".join([constants.SESSION_DIGEST_MARKER, *lines]), kept)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.SESSION_DIGEST_TAG]})


async def _novelty_section(
    eng: Any, ns: str, query: str, budget_tokens: int
) -> MemoryRecord | None:
    """G32 (plan v3.2, ``read.novelty_exclusions``): for a request for something new
    ("a book I haven't read"), a header listing what memory says the asker already
    likes, does or has had (keyed facts whose attribute is ``likes``,
    ``activities``, ``favourite_*``, ``pets`` or a list card), so the answer can
    avoid repeating it. None for any other question or when nothing is known."""
    if not (eng._config().read.novelty_exclusions and is_novelty(query)):
        return None
    known: list[MemoryRecord] = []
    for record in await eng._records(ns, "semantic"):
        if record.status is not RecordStatus.ACTIVATED or record.quarantined:
            continue
        attribute = (record.attribute or "").lower()
        if (
            attribute in {"likes", "activities", "pets", "dislikes"}
            or attribute.startswith("favourite_")
            or constants.LIST_CARD_TAG in record.tags
        ):
            known.append(record)
    if not known:
        return None
    allowance = max(1, budget_tokens // 10)
    lines: list[str] = []
    kept: list[MemoryRecord] = []
    for record in sorted(known, key=chrono_key, reverse=True):
        line = f"- {' '.join(record.content.split())}"
        if estimate_tokens("\n".join([constants.NOVELTY_MARKER, *lines, line])) > allowance:
            break
        lines.append(line)
        kept.append(record)
    if not kept:
        return None
    block = eng._lead_record(ns, "\n".join([constants.NOVELTY_MARKER, *lines]), kept)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.NOVELTY_TAG]})


async def _profile_section(
    eng: Any, ns: str, query: str, budget_tokens: int, session_id: str | None = None
) -> MemoryRecord | None:
    """G3b: the profile header, or None (``read.profile_header`` off, or nothing).

    Candidates are the H14 profile insights (``reflect_profile`` deposits:
    reflective records from the ``reflection`` channel) from the same search,
    restricted to reflective records, so every search gate applies. Insights
    that name a person in the query come first; when none does, the most
    relevant insights are used. Lines are kept best first while the block fits
    ``read.profile_budget_share x budget_tokens``.

    #40: under ``read.profile_header_packing`` the packed block replaces it
    (:meth:`_packed_profile_section`).
    """
    read_cfg = eng._config().read
    if read_cfg.profile_skip_temporal and is_temporal(query):
        return None  # D1 (ADR-055): no profile header, plain or packed, on date questions
    if read_cfg.profile_scope_gate and not await eng._applies_to_person(ns, query):
        return None  # W9: a general-knowledge question gets no profile
    if read_cfg.profile_header_packing:
        return await eng._packed_profile_section(ns, query, budget_tokens, session_id)
    if not read_cfg.profile_header:
        return None
    allowance = int(budget_tokens * read_cfg.profile_budget_share)
    names = query_names(query)
    if allowance <= estimate_tokens(render_profile(names, [])):
        return None
    k = constants.PROFILE_HEADER_TOP_K
    hits = await eng._search(
        query, ns, k, memory_type="reflective", keep_k=k, session_id=session_id
    )
    if eng._header_abstains(hits):
        return None
    insights = [
        r
        for r, _ in hits
        if r.source.channel == "reflection"
        and (r.source.message_id or "").startswith("reflected:")
        and eng._profile_line_relevant(query, r.content)  # I33
    ]
    about = [r for r in insights if mentions_any(r.content, names)]
    kept: list[MemoryRecord] = []
    for record in about or insights:
        trial = [*kept, eng._wrap_for_context(record)]
        if estimate_tokens(render_profile(names if about else [], trial)) <= allowance:
            kept = trial
    if not kept:
        return None
    block = eng._lead_record(ns, render_profile(names if about else [], kept), kept)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.PROFILE_TAG]})


async def _packed_profile_section(
    eng: Any, ns: str, query: str, budget_tokens: int, session_id: str | None = None
) -> MemoryRecord | None:
    """#40: the packed profile header, or None (nothing relevant fits).

    Three searches, each gated like any search and each shown only when its hits
    pass M12 abstention: the session summaries (semantic, ``consolidation``
    channel), the H14 profile observations (reflective ``reflected:`` insights) and
    the other hits (neither, and no mined fact while the cards header shows them).
    Each candidate gets the per-record wrappers (marker escaping, instruction and
    untrusted-note wrappers, H1 dates), then :func:`pack_profile` keeps summaries,
    then observations, then hits while the block fits ``read.profile_header_budget``
    tokens, never more than :data:`constants.PROFILE_PACK_MAX_SHARE` of the budget.
    The routed read then leaves the packed records out (they are the block's parents).
    """
    read_cfg = eng._config().read
    allowance = min(
        read_cfg.profile_header_budget,
        int(budget_tokens * constants.PROFILE_PACK_MAX_SHARE),
    )
    if allowance <= estimate_tokens(constants.PROFILE_PACK_MARKER):
        return None

    def is_summary(r: MemoryRecord) -> bool:
        return r.memory_type == "semantic" and r.source.channel == "consolidation"

    def is_insight(r: MemoryRecord) -> bool:
        return (
            r.memory_type == "reflective"
            and r.source.channel == "reflection"
            and (r.source.message_id or "").startswith("reflected:")
        )

    cards_on = read_cfg.cards == "header"

    def is_other(r: MemoryRecord) -> bool:
        return not (is_summary(r) or is_insight(r) or (cards_on and "atomic_fact" in r.tags))

    k = constants.PROFILE_PACK_SECTION_K
    candidates: list[list[MemoryRecord]] = []
    for memory_type, wanted in (
        ("semantic", is_summary),
        ("reflective", is_insight),
        (None, is_other),
    ):
        hits = await eng._search(
            query,
            ns,
            k,
            memory_type=memory_type,
            keep_k=k,
            session_id=session_id,
            hide=lambda r, wanted=wanted: not wanted(r),  # type: ignore[misc]
        )
        if eng._header_abstains(hits):
            hits = []
        candidates.append([eng._wrap_for_context(r) for r, _ in hits])
    kept = pack_profile(candidates, allowance)
    parts = [r for section in kept for r in section]
    if not parts:
        return None
    block = eng._lead_record(ns, render_packed_profile(kept), parts)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.PROFILE_TAG]})


async def _entity_summary_section(
    eng: Any, ns: str, query: str, allowance: int
) -> MemoryRecord | None:
    """GP-6 (``read.entity_summaries``): the "About" block, or None.

    One ``About <Name>: …`` line per entity the query names (the graph leg's
    seeds) that has a live entity summary (``summarize_entities`` stage), in
    seed order, while the block fits ``allowance``. A summary passes the graph
    admission gate (status, quarantine, trust floor, integrity) and the
    per-record context wrappers; the block's parents are the summaries shown,
    so the read below leaves them out."""
    if eng._associative is None or allowance <= estimate_tokens(constants.ENTITY_SUMMARIES_MARKER):
        return None
    try:
        seeds = await eng._graph_seeds(ns, query)
        if not seeds:
            return None
        admit = eng._graph_admit(ns)
        found: dict[str, MemoryRecord] = {}
        for record in await eng._records(ns, "semantic"):
            if record.source.channel != constants.ENTITY_SUMMARY_CHANNEL or not admit(record):
                continue
            node = entity_summary_node(record)
            if node in seeds and (
                node not in found
                or (record.valid_from, record.record_id)
                > (found[node].valid_from, found[node].record_id)
            ):
                found[node] = record
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.entity_summaries_failed", namespace=ns, error=str(exc))
        return None
    integrity = eng._integrity()
    live = integrity.enabled and integrity.live_reevaluation
    lines: list[str] = []
    kept: list[MemoryRecord] = []
    for node in seeds:
        summary = found.get(node)
        if summary is None:
            continue
        record = summary
        if live:
            effective = await eng.effective_trust(record.record_id)
            if not integrity.admits(effective):
                continue
            record = record.model_copy(update={"trust": effective})
        shown = eng._wrap_for_context(record)
        name = escape_markers(entity_summary_name(record))
        line = f"About {name}: {' '.join(shown.content.split())}"
        trial = [*lines, line]
        if estimate_tokens("\n".join([constants.ENTITY_SUMMARIES_MARKER, *trial])) <= allowance:
            lines = trial
            kept.append(shown)
    if not kept:
        return None
    block = eng._lead_record(ns, "\n".join([constants.ENTITY_SUMMARIES_MARKER, *lines]), kept)
    return block.model_copy(update={"tags": [constants.LEAD_TAG, constants.ENTITY_SUMMARIES_TAG]})


def _duration_section(
    eng: Any,
    ns: str,
    query: str,
    assembled: AssembledContext,
    headers: list[MemoryRecord],
) -> list[MemoryRecord]:
    """N10 (plan v3.2, ``read.span_line``): for a duration question ("how long
    after …", "how many weeks between …"), one line computed by the engine from
    the two retrieved turns that best match the question and carry different
    days: ``A (date) -> B (date): N days (about W weeks / M months)``. The reader
    no longer does the date arithmetic. Unchanged when off, for other questions,
    or when two dated matches are not found."""
    if not eng._config().read.span_line or not is_duration(query):
        return headers
    wanted = content_words(query)
    turns = [
        r
        for r in assembled.records
        if r.memory_type == "episodic" and constants.LEAD_TAG not in r.tags
    ]
    ranked = sorted(turns, key=lambda r: (-len(wanted & content_words(r.content)), chrono_key(r)))
    picked: list[MemoryRecord] = []
    for record in ranked:
        if not wanted & content_words(record.content):
            break
        if all(record.valid_from.date() != p.valid_from.date() for p in picked):
            picked.append(record)
        if len(picked) == 2:
            break
    if len(picked) < 2:
        return headers
    first, second = sorted(picked, key=lambda r: r.valid_from)
    days = (second.valid_from.date() - first.valid_from.date()).days
    line = (
        f"{constants.SPAN_MARKER} "
        f'"{" ".join(first.content.split())[:60]}" ({first.valid_from:%Y-%m-%d}) -> '
        f'"{" ".join(second.content.split())[:60]}" ({second.valid_from:%Y-%m-%d}): '
        f"{days} days (about {days / 7:.1f} weeks, {days / 30.44:.1f} months)"
    )
    block = eng._lead_record(ns, line, [first, second])
    return [
        *headers,
        block.model_copy(update={"tags": [constants.LEAD_TAG, constants.SPAN_TAG]}),
    ]


def _count_section(
    eng: Any,
    ns: str,
    query: str,
    assembled: AssembledContext,
    allowance: int,
    headers: list[MemoryRecord],
) -> list[MemoryRecord]:
    """E3: ``headers`` led by the occurrences block when there is one.

    Its lines are the distinct dated mentions of the counted event
    (:func:`count_terms`, :func:`mentions_event`) among the episodic records the
    read put in the volatile context, so every read gate already applied; mined
    facts, lead blocks and wrapped records (instruction-flagged or untrusted, which
    stay in the context wrapped) are left out. Same-day mentions of one event count
    once (:func:`distinct_occurrences`). Lines are kept oldest first while the block
    fits ``allowance``. The mentions stay in the context: the block points at them.
    """
    if allowance <= 0:
        return headers
    terms = count_terms(query)
    mentions = [
        (record, _unrendered(record.content))
        for record in assembled.records[assembled.boundary_index :]
        if record.memory_type == "episodic"
        and constants.LEAD_TAG not in record.tags
        and "atomic_fact" not in record.tags
        and eng._lead_clean(record)
    ]
    found = [(r, text) for r, text in mentions if mentions_event(text, terms)]
    read_cfg = eng._config().read
    days = None
    if read_cfg.count_dedupe:  # #60: merge mentions of one event said on other days
        week = read_cfg.relative_week
        days = {r.record_id: event_day(text, r.valid_from, week, record=r) for r, text in found}
    kept: list[tuple[MemoryRecord, str]] = []
    for occurrence in distinct_occurrences(found, event_days=days):
        trial = [*kept, occurrence]
        if estimate_tokens(render_occurrences(trial)) <= allowance:
            kept = trial
    if not kept:
        return headers
    block = eng._lead_record(ns, render_occurrences(kept), [r for r, _ in kept])
    tags = [constants.LEAD_TAG, constants.COUNT_TAG]
    return [block.model_copy(update={"tags": tags}), *headers]
