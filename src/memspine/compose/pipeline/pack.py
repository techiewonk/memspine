"""Compose: the pack part of context assembly, moved out of the engine (F-11)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from memspine.compose.budget import Budget, EstimatingCounter
from memspine.compose.bundles import TurnKey, TurnText, build_bundles, pack
from memspine.compose.pack import AssembledContext
from memspine.compose.pipeline.render import BUNDLE_TAG, current_counter, record_envelope, tok
from memspine.config import constants
from memspine.core import trace_sink
from memspine.core.dedupe import dedupe_scored
from memspine.core.read_filters import active_record_scope
from memspine.memories.procedural.lessons import render_lesson
from memspine.memories.procedural.task_state import TaskState, render_task_state
from memspine.models.encoders.query.shape import (
    content_words,
    is_aggregation,
    is_count,
    is_temporal,
    is_verbatim,
)
from memspine.observability import inspect as insp
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.read.lead import count_terms, query_names
from memspine.read.pipeline.state import _FORENSICS, CUE_TAG, _passive_hidden
from memspine.read.pipeline.time_guard import blocks_guarded
from memspine.store.records import MemoryRecord, RecordStatus

_log = get_logger(__name__)

#: R3-09b: source turns shown with a fact bundle (the best-overlapping ones).
_BUNDLE_TURNS = 2


def _reply_budget(eng: Any, budget_tokens: int) -> int:
    """ContextPipe: the budget left once ``read.reply_reserve_tokens`` is kept free."""
    reserve = eng._config().read.reply_reserve_tokens
    return max(1, budget_tokens - reserve) if reserve else budget_tokens


async def _procedural_blocks(
    eng: Any, ns: str, query: str, session_id: str | None
) -> tuple[list[MemoryRecord], list[MemoryRecord]]:
    """W17e / W17a read blocks: the task state pinned for ``session_id`` (with
    ``memories.procedural.policies.task_state``) and the advisory lessons for the
    query (with ``lessons: {inject: true}``). Both empty by default."""
    policy = eng._procedural_policy()
    pinned: list[MemoryRecord] = []
    if policy.get("task_state") and session_id is not None:
        record = await eng._task_state_record(ns, session_id)
        if record is not None and record.status is RecordStatus.ACTIVATED:
            state = TaskState.model_validate_json(record.content)
            if not state.closed:
                pinned.append(record.model_copy(update={"content": render_task_state(state)}))
    lessons: list[MemoryRecord] = []
    option = policy.get("lessons")
    if isinstance(option, Mapping) and option.get("inject") and eng._procedural is not None:
        k = int(option.get("max", constants.LESSON_BLOCK_MAX))
        floor = float(option.get("min_similarity", constants.LESSON_MIN_SIMILARITY))
        hits = await eng.recall_lessons(query, namespace=ns, k=k, min_similarity=floor)
        lessons = [render_lesson(record) for record, _ in hits]
    return pinned, lessons


def _place_procedural(
    assembled: AssembledContext, pinned: list[MemoryRecord], lessons: list[MemoryRecord]
) -> AssembledContext:
    """W17e: the task state right after the persona (stable prefix); W17a: the
    lessons last, after every piece of evidence, never in place of it."""
    records = list(assembled.records)
    personas = 0
    while personas < len(records) and records[personas].source.channel == "persona":
        personas += 1
    records[personas:personas] = pinned
    records.extend(lessons)
    assembled.records = records
    assembled.boundary_index += len(pinned)
    assembled.tokens_used += sum(tok(r.content) for r in [*pinned, *lessons])
    return assembled


async def _expand_trajectories(
    eng: Any, ns: str, assembled: AssembledContext, budget_tokens: int
) -> AssembledContext:
    """N27 (``memories.procedural.policies.trajectory: {expand: true}``): a hit
    on a trajectory step or head brings its neighbouring steps (``radius``,
    default 1) right after it, inside its group, at most ``cap`` (default 20)
    added states, while the budget allows."""
    option = eng._procedural_policy().get("trajectory")
    if not (isinstance(option, Mapping) and option.get("expand")):
        return assembled
    radius = int(option.get("radius", constants.TRAJECTORY_WINDOW_RADIUS))
    cap = int(option.get("cap", constants.TRAJECTORY_WINDOW_CAP))
    present = {r.record_id for r in assembled.records}
    out: list[MemoryRecord] = []
    added = 0
    used = assembled.tokens_used
    for index, record in enumerate(assembled.records):
        out.append(record)
        if (
            index < assembled.boundary_index
            or added >= cap
            or not (
                {constants.TRAJECTORY_STEP_TAG, constants.TRAJECTORY_HEAD_TAG} & set(record.tags)
            )
        ):
            continue
        for step in await eng.trajectory_window(
            record.record_id, namespace=ns, radius=radius, cap=cap
        ):
            cost = tok(step.content)
            if step.record_id in present or added >= cap or used + cost > budget_tokens:
                continue
            present.add(step.record_id)
            out.append(step)
            added += 1
            used += cost
    assembled.records = out
    assembled.tokens_used = used
    return assembled


def _place_lead(
    assembled: AssembledContext,
    standing: list[MemoryRecord],
    timelines: list[MemoryRecord],
) -> AssembledContext:
    """H22: standing preferences right after the pinned persona (stable prefix,
    they change only when the user states a new one); timelines open the
    volatile part (they depend on the query). An abstained assembly keeps the
    standing block but gets no timelines (they came from weak candidates)."""
    records = list(assembled.records)
    boundary = assembled.boundary_index
    personas = 0
    while personas < boundary and records[personas].source.channel == "persona":
        personas += 1
    if assembled.abstained:
        timelines = []
    records[personas:personas] = standing
    boundary += len(standing)
    records[boundary:boundary] = timelines
    assembled.records = records
    assembled.boundary_index = boundary
    assembled.tokens_used += sum(tok(r.content) for r in [*standing, *timelines])
    return assembled


async def _claims_only(
    eng: Any,
    ns: str,
    scored: list[tuple[MemoryRecord, float]],
    *,
    expand: bool,
    hide: Callable[[MemoryRecord], bool] | None = None,
) -> list[tuple[MemoryRecord, float]]:
    """B9 facts-only: low-trust raw records leave; their mined facts may stand in.

    A record below ``integrity.claims_only_below`` (view trust) that is not
    itself a mined fact, the pinned persona or a lead block is removed. With
    ``expand``, each live, unflagged atomic fact mined from it takes its place
    once, prefixed :data:`constants.CLAIM_MARKER` and scored like the record it
    replaces. Facts already in the list are not repeated, and facts ``hide``
    leaves out (a read header shows them, labelled there) do not stand in.
    """
    threshold = eng._integrity().claims_only_below
    keep_as_is = ("atomic_fact", constants.LEAD_TAG)

    def exempt(record: MemoryRecord) -> bool:
        return (
            record.trust >= threshold
            or record.source.channel == "persona"
            or any(tag in record.tags for tag in keep_as_is)
        )

    if all(exempt(record) for record, _ in scored):
        return scored
    mined: dict[str, list[MemoryRecord]] = {}
    if expand:
        for fact in await eng._records(ns, "semantic"):
            if "atomic_fact" in fact.tags:
                for parent in fact.source.parents:
                    mined.setdefault(parent, []).append(fact)
    seen = {record.record_id for record, _ in scored}
    out: list[tuple[MemoryRecord, float]] = []
    for record, score in scored:
        if exempt(record):
            out.append((record, score))
            continue
        for fact in mined.get(record.record_id, []):
            if fact.record_id in seen or (hide is not None and hide(fact)):
                continue
            view = await eng._live_view(fact)
            inflated = eng._inflate_all([view], ns) if view is not None else []
            if not inflated or inflated[0].instruction_flag:
                continue
            seen.add(fact.record_id)
            claim = inflated[0].model_copy(
                update={"content": f"{constants.CLAIM_MARKER} {inflated[0].content}"}
            )
            out.append((claim, score))
    return out


async def _decorate(
    eng: Any,
    ns: str,
    scored: list[tuple[MemoryRecord, float]],
    *,
    expand_claims: bool = True,
    hide: Callable[[MemoryRecord], bool] | None = None,
) -> list[tuple[MemoryRecord, float]]:
    """The context-entry transforms shared by every read mode (projection only).

    B9 facts-only (low-trust raw records replaced by their mined claims, or
    dropped when ``expand_claims`` is off), H1 relative dates (each record
    against its own event time), the E1 instruction-flag wrapper, the C4'
    current-state view, then the B6 untrusted-note wrapper. ``hide`` keeps
    B9 from putting back a fact a read header already shows.
    """
    read_cfg = eng._config().read
    integrity = eng._integrity()
    if integrity.enabled and integrity.claims_only_below > 0.0:
        scored = await eng._claims_only(ns, scored, expand=expand_claims, hide=hide)
    if read_cfg.resolve_relative_dates:
        scored = [(eng._annotate_dates(record), score) for record, score in scored]
    # E1: instruction-shaped content enters a context window WRAPPED — the
    # flag was stored inert at write time precisely so assembly could do
    # this; unwrapped, a flagged-but-unquarantined record (e.g. a benign
    # user imperative) would read as instructions to the model.
    scored = [(eng._wrap_instruction(record), score) for record, score in scored]
    if read_cfg.current_state_view:
        scored = await eng._current_state_view(ns, scored)
    # B6: low-trust records reach the model as labelled DATA with their
    # view trust, never as instructions (spotlighting, trust-graded).
    return [(eng._wrap_untrusted(record), score) for record, score in scored]


async def _read_headers(
    eng: Any,
    ns: str,
    query: str,
    budget_tokens: int,
    session_id: str | None = None,
    *,
    strong: bool = False,
) -> list[MemoryRecord]:
    """G1b/G3b: the cards header, then the profile header (each optional).

    ``session_id`` keys their searches in the B0 read ledger, like the read's own.
    F5 (``read.verbatim_raw_only``): a verbatim question gets none."""
    if eng._config().read.verbatim_raw_only and is_verbatim(query):
        return []
    headers = []
    for header in (
        await eng._cards_section(ns, query, budget_tokens, session_id, strong=strong),
        await eng._profile_section(ns, query, budget_tokens, session_id),
        await eng._novelty_section(ns, query, budget_tokens),
        await eng._slots_section(ns, query, budget_tokens),
        await eng._session_digest_section(ns, query, budget_tokens, session_id),
        await eng._recent_section(ns, query, budget_tokens),
    ):
        if header is not None:
            headers.append(header)
    read_cfg = eng._config().read
    skip_derived = blocks_guarded(read_cfg, query)  # GTIME: "when" question, no derived block
    if read_cfg.cards_include_edges and not skip_derived:
        # GP-5: the graph facts block shares the cards allowance, after the cards.
        cards = [h for h in headers if constants.CARDS_TAG in h.tags]
        allowance = int(budget_tokens * read_cfg.cards_budget_share)
        facts = await eng._graph_facts_section(ns, query, allowance - eng._headers_cost(cards))
        if facts is not None:
            headers.insert(len(cards), facts)
    if read_cfg.entity_summaries and not skip_derived:
        # GP-6: the "About" block shares the cards allowance, after cards and facts.
        shared = [
            h
            for h in headers
            if constants.CARDS_TAG in h.tags or constants.GRAPH_FACTS_TAG in h.tags
        ]
        allowance = int(budget_tokens * read_cfg.cards_budget_share)
        about = await eng._entity_summary_section(ns, query, allowance - eng._headers_cost(shared))
        if about is not None:
            headers.insert(len(shared), about)
    if insp.inspect_enabled():  # TI-1: which header blocks the reader sees, from which records
        insp.headers_event(headers)
    return headers


def _count_allowance(eng: Any, query: str, budget_tokens: int) -> int:
    """E3: the tokens kept for the occurrences block; 0 when ``read.count_timeline``
    is off, the query is not a count question, or the share fits no line."""
    read_cfg = eng._config().read
    if not read_cfg.count_timeline or not is_count(query) or not count_terms(query):
        return 0
    allowance = int(budget_tokens * read_cfg.count_budget_share)
    return allowance if allowance > tok(constants.COUNT_MARKER) else 0


def _cap_lead_blocks(
    eng: Any, headers: list[MemoryRecord], count_share: int, budget_tokens: int
) -> tuple[list[MemoryRecord], int]:
    """H12 (ADR-055, ``read.lead_budget_share``): the lead blocks and the count
    reserve, capped together at that share of ``budget_tokens``.

    The occurrences reserve (count questions only) is kept first, as far as it
    fits; then whole header blocks leave in :data:`constants.LEAD_BLOCK_DROP_ORDER`
    (profile, entity summaries, graph facts, cards; the later of two same-kind
    blocks first) until the rest fits. Unset: both returned unchanged."""
    share = eng._config().read.lead_budget_share
    if share is None:
        return headers, count_share
    cap = int(budget_tokens * share)
    if count_share > cap:
        count_share = cap if cap > tok(constants.COUNT_MARKER) else 0
    room = cap - count_share
    kept = list(headers)
    for tag in constants.LEAD_BLOCK_DROP_ORDER:
        for block in [h for h in reversed(kept) if tag in h.tags]:
            if eng._headers_cost(kept) <= room:
                return kept, count_share
            kept.remove(block)
    return kept, count_share


def _headers_cost(headers: list[MemoryRecord]) -> int:
    return sum(tok(h.content) for h in headers)


def _cards_gated(eng: Any, query: str, *, strong: bool = False) -> bool:
    """ADR-055 addendum (``read.cards_only_aggregate``): True when ``query`` gets
    no cards header and no mined fact in its routed read.

    Only with ``read.cards: header`` and the key on, and only for a question that
    is neither a list/count question (``is_aggregation`` / ``is_count``) nor a
    date question (``is_temporal``): date questions keep the ``cards_skip_temporal``
    / ``cards_temporal`` behaviour unchanged, except under
    ``read.cards_skip_hides_facts``: a date question that skips the cards header
    (``cards_skip_temporal`` with ``cards_temporal: skip``) then reads with every
    mined fact hidden too."""
    read_cfg = eng._config().read
    if read_cfg.cards != "header":
        return False
    if read_cfg.verbatim_raw_only and is_verbatim(query):
        return True
    if read_cfg.cards_when_weak and strong:
        # F4 (plan v3.2): the raw turns already answer it (see _raw_evidence_strong).
        return True
    if (
        read_cfg.cards_skip_hides_facts
        and read_cfg.cards_skip_temporal
        and read_cfg.cards_temporal == "skip"
        and is_temporal(query)
    ):
        # A date question that skips the cards header reads as with mining off:
        # otherwise the hidden-by-header mined facts flood its raw read.
        return True
    return read_cfg.cards_only_aggregate and not (
        is_aggregation(query) or is_count(query) or is_temporal(query)
    )


def _header_hide(
    headers: list[MemoryRecord], *, all_facts: bool = True, hide_facts: bool = False
) -> Callable[[MemoryRecord], bool] | None:
    """What the routed read leaves out: every record a header shows, and with
    the cards header every mined fact (facts reach the context through it).
    ``all_facts=False`` (a ``full`` read) leaves out only the shown records.
    ``hide_facts`` (``read.cards_only_aggregate`` gating the cards header off)
    leaves out every mined fact even without a cards header, in a ``full`` read
    too, so the question reads as with mining off."""
    if not headers and not hide_facts:
        return None
    # N31: a session digest shows two sentences of a session; its turns stay readable.
    shown = {
        pid
        for h in headers
        if constants.SESSION_DIGEST_TAG not in h.tags and constants.RECENT_TAG not in h.tags
        for pid in h.source.parents
    }
    facts = hide_facts or (all_facts and any(constants.CARDS_TAG in h.tags for h in headers))
    return lambda r: r.record_id in shown or (facts and "atomic_fact" in r.tags)


def _header_abstains(eng: Any, hits: list[tuple[MemoryRecord, float]]) -> bool:
    """A header is evidence: it is shown only when its own hits pass M12 abstention."""
    return eng._assembly is not None and eng._assembly.abstains(hits)


def _attach_headers(
    eng: Any, assembled: AssembledContext, headers: list[MemoryRecord]
) -> AssembledContext:
    """G1b/G3b: the headers open the volatile part (after the stable prefix),
    cards first.

    Each header passed M12 abstention on its own hits, so it is evidence: when
    the routed read abstained because the headers carry what it would have
    shown (A-1/A-3: mined facts hidden, or B9 dropping their low-trust source
    turns), the headers still go in and the read no longer abstains."""
    if not headers:
        return assembled
    assembled.abstained = False
    records = list(assembled.records)
    at = min(assembled.boundary_index, len(records))
    if eng._config().read.section_captions and len(records) > at:
        # C7: the retrieved part gets its own caption after the headers' markers.
        caption = eng._lead_record(
            records[at].namespace, constants.RETRIEVED_CAPTION, list(records[at:])
        ).model_copy(update={"tags": [constants.LEAD_TAG, constants.RETRIEVED_CAPTION_TAG]})
        headers = [*headers, caption]
    records[at:at] = headers
    assembled.records = records
    assembled.tokens_used += eng._headers_cost(headers)
    return assembled


async def _best_source_turn(eng: Any, fact: MemoryRecord) -> MemoryRecord | None:
    """H6: the eligible parent turn sharing the most words with ``fact``."""
    storage = eng._require_started()
    words = set(fact.content.lower().split())
    best: tuple[float, MemoryRecord] | None = None
    for parent_id in fact.source.parents:
        parent = await storage.get_record(parent_id)
        if parent is None or parent.memory_type != "episodic":
            continue
        view = await eng._live_view(parent)
        if view is None:
            continue
        other = set(view.content.lower().split())
        overlap = len(words & other) / (len(words | other) or 1)
        if best is None or overlap > best[0]:
            best = (overlap, view)
    if best is None:
        return None
    inflated = eng._inflate_all([best[1]], fact.namespace)
    if not inflated:
        return None
    decorated = await eng._decorate(fact.namespace, [(inflated[0], 0.0)], expand_claims=False)
    return decorated[0][0] if decorated else None


async def _dedupe_candidates(
    eng: Any, scored: list[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """I31 (``read.dedupe``): the candidates without near-duplicates; what was dropped
    goes to ``search_forensics()["dedupe_dropped"]`` as ``(dropped, kept, similarity)``."""
    read_cfg = eng._config().read
    vectors: dict[str, list[float]] | None = None
    if read_cfg.dedupe == "embedding" and eng._embedder is not None:
        try:
            rows = await eng._embedder.embed([r.content for r, _ in scored])
            vectors = {
                r.record_id: [float(x) for x in row]
                for (r, _), row in zip(scored, rows, strict=False)
            }
        except Exception as exc:  # an enhancer, never a gate
            _log.warning("read.dedupe_embedding_failed", error=str(exc))
    kept, dropped = dedupe_scored(
        scored,
        read_cfg.dedupe,
        threshold=read_cfg.dedupe_threshold,
        keep=read_cfg.dedupe_keep,
        vectors=vectors,
    )
    sink = _FORENSICS.get()
    if sink is not None:
        sink["dedupe_dropped"] = [(d.dropped, d.kept, d.similarity) for d in dropped]
        for d in dropped:
            trace_sink.cut("dedupe", d.dropped, kept=d.kept, similarity=d.similarity)
    return kept


def _profile_line_relevant(eng: Any, query: str, text: str) -> bool:
    """I33 (``read.profile_relevance_gate``): whether one profile / preference line
    bears on ``query``. ``off``: always. ``overlap``: they share a content word.
    This is the seam for the decider's relevance check (not merged): replace the
    body, keep the signature. A question-less call (session start) is never gated."""
    if eng._config().read.profile_relevance_gate == "off" or not query.strip():
        return True
    named = {n.lower() for n in query_names(query)}  # a name alone is not relevance
    return bool((content_words(query) - named) & content_words(text))


def _budget_factor(eng: Any, budget_tokens: int) -> float:
    """I20 (``read.replay_budget_scaling``): how much of the reference budget the
    routed read has left, in [0.25, 1]; 1.0 when scaling is off or the budget is
    at least the reference."""
    read_cfg = eng._config().read
    if not read_cfg.replay_budget_scaling:
        return 1.0
    return max(0.25, min(1.0, budget_tokens / read_cfg.replay_budget_reference))  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _context_eligible(eng: Any, record: MemoryRecord) -> bool:
    """C7': the search-time gates, for records reached without a search (stored trust)."""
    if record.status is not RecordStatus.ACTIVATED or record.quarantined:
        return False
    if record.memory_type == "shared" or CUE_TAG in record.tags:
        return False
    scope = active_record_scope()
    if scope is not None and not scope.matches(record):
        return False  # I7 / I8: outside the read's sessions or roles
    if _passive_hidden(record):
        return False  # #53
    if not eng._consent_ok(record):
        return False  # #50: the read's purpose may not see it
    integrity = eng._integrity()
    return not integrity.enabled or integrity.admits(record.trust)


async def _source_turns(eng: Any, fact: MemoryRecord, cap: int) -> list[MemoryRecord]:
    """R3-09b: the eligible parent turns of ``fact`` (decorated like any context record), the
    ``cap`` sharing most words with it first. Today's records carry no evidence lists: the fact's
    source turns (``source.parents``) are its support evidence."""
    storage = eng._require_started()
    words = set(fact.content.lower().split())
    found: list[tuple[float, int, MemoryRecord]] = []
    for index, parent_id in enumerate(fact.source.parents):
        parent = await storage.get_record(parent_id)
        if parent is None or parent.memory_type != "episodic":
            continue
        view = await eng._live_view(parent)
        if view is None:
            continue
        inflated = eng._inflate_all([view], fact.namespace)
        if not inflated:
            continue
        decorated = await eng._decorate(fact.namespace, [(inflated[0], 0.0)], expand_claims=False)
        if not decorated:
            continue
        other = set(decorated[0][0].content.lower().split())
        overlap = len(words & other) / (len(words | other) or 1)
        found.append((-overlap, index, decorated[0][0]))
    found.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in found[:cap]]


def _turn_text(turn: MemoryRecord) -> TurnText:
    undated = constants.UNDATED_TAG in turn.tags
    return TurnText(text=turn.content, date=None if undated else f"{turn.valid_from:%Y-%m-%d}")


def _line_record(source: MemoryRecord, line: Any) -> MemoryRecord:
    return source.model_copy(
        update={"content": f"{line.alias} {line.text}", "tags": [*source.tags, BUNDLE_TAG]}
    )


async def bundle_selected(
    eng: Any, ns: str, assembled: AssembledContext, budget_tokens: int
) -> AssembledContext:
    """R3-09b (``read.evidence_bundles``): the volatile part of ``assembled`` packed under the TRUE
    ``budget_tokens`` (the selection ran on a wider pool), mined facts as complete bundles.

    A fact's bundle is the fact plus its source turns (cited ``[n]``, shared turns once); it enters
    whole or not at all. Other records are their own evidence and enter whole in rank order with
    what the bundles left. Fact bundles may take ``read.bundle_fact_share`` of the room while
    other records wait, all of it when none do. A turn cited by an included bundle is not repeated
    as a record of its own. The stable prefix is kept and charged first.
    """
    cfg = eng._config().read
    counter = current_counter() or EstimatingCounter()
    boundary = min(assembled.boundary_index, len(assembled.records))
    facts: dict[str, tuple[MemoryRecord, list[MemoryRecord]]] = {}
    for record in assembled.records:
        if "atomic_fact" in record.tags and record.source.parents and not record.is_engine_block:
            turns = await _source_turns(eng, record, _BUNDLE_TURNS)
            if turns:
                facts[record.record_id] = (record, turns)
    # Mined facts are semantic records, so placement puts them in the stable prefix; as bundles
    # they depend on the query, so they move to the front of the volatile part.
    stable = [r for r in assembled.records[:boundary] if r.record_id not in facts]
    volatile = [r for r in assembled.records if r.record_id in facts] + [
        r for r in assembled.records[boundary:] if r.record_id not in facts
    ]
    room = max(0, budget_tokens - sum(counter.count(r.content) for r in stable))
    others = [r for r in volatile if r.record_id not in facts]

    turn_text: dict[TurnKey, TurnText] = {}
    envelopes = []
    for fact, turns in facts.values():
        for t in turns:
            turn_text[(t.record_id, 1, 0)] = _turn_text(t)
        envelopes.append(record_envelope(fact, turns))
    bundles = build_bundles(envelopes)
    fact_room = room if not others else int(room * cfg.bundle_fact_share)
    dropped: list[dict[str, Any]] = []
    included_ids: set[str] = set()
    cited: set[str] = set()
    lines_for: dict[str, list[MemoryRecord]] = {}
    spent = 0
    if bundles and fact_room > 0:
        packed = pack(
            bundles,
            Budget(total=fact_room, counter=counter),  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
            order=[b.record_id for b in bundles],
            turns=turn_text,
            early_break_after_n_skips=cfg.exact_budget_max_skips,
        )
        spent = packed.tokens
        dropped.extend({"id": d.record_id, "reason": d.reason} for d in packed.dropped)
        included_ids = {b.record_id for b in packed.included}
        by_text: dict[str, MemoryRecord] = {}
        for _fact, turns in facts.values():
            for t in turns:
                tt = _turn_text(t)
                for role in ("support", "binding"):
                    by_text[f"{role} | {tt.text}" + (f" ({tt.date})" if tt.date else "")] = t
        current: list[MemoryRecord] | None = None
        for block in packed.blocks:
            previous = None
            for ln in block.lines:
                is_evidence = ln.record_id == previous and ln.text.startswith(
                    ("support | ", "binding | ")
                )
                if is_evidence and ln.text in by_text and current is not None:
                    source = by_text[ln.text]
                    cited.add(source.record_id)
                    current.append(_line_record(source, ln))
                else:
                    previous = ln.record_id
                    current = lines_for.setdefault(ln.record_id, [])
                    current.append(_line_record(facts[ln.record_id][0], ln))
    elif bundles:
        dropped.extend({"id": b.record_id, "reason": "over_budget"} for b in bundles)

    remaining = max(0, room - spent)
    kept_other: set[str] = set()
    skips = 0
    for record in others:
        if record.record_id in cited:
            dropped.append({"id": record.record_id, "reason": "cited_by_bundle"})
            continue
        if skips >= cfg.exact_budget_max_skips:
            dropped.append({"id": record.record_id, "reason": "early_break"})
            continue
        cost = counter.count(record.content)
        if cost <= remaining or (not kept_other and not included_ids and not stable):
            kept_other.add(record.record_id)  # never an empty context: the first one always enters
            remaining -= min(cost, remaining)
        else:
            skips += 1
            dropped.append({"id": record.record_id, "reason": "over_budget"})

    out: list[MemoryRecord] = []
    for record in volatile:
        if record.record_id in included_ids:
            out.extend(lines_for.get(record.record_id, []))
        elif record.record_id in kept_other:
            out.append(record)
    assembled.records = [*stable, *out]
    assembled.boundary_index = len(stable)
    assembled.tokens_used = sum(counter.count(r.content) for r in assembled.records)
    if tap.enabled():
        tap.event("compose.pack.dropped", dropped=dropped, n_dropped=len(dropped))
        tap.event(
            "compose.bundles",
            bundles=len(bundles),
            included=sorted(included_ids),
            others_kept=len(kept_other),
            tokens=assembled.tokens_used,
            room=room,
            accounting=counter.accounting,
        )
    return assembled
