"""Read path: deep moved out of the engine (F-10b)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from time import perf_counter
from typing import Any

from memspine.compose.pack import AssembledContext, estimate_tokens
from memspine.config import constants
from memspine.core.agentic import clean_query, evidence_view, fuse_new, merge_budget
from memspine.core.event_ledger import (
    is_event_question,
    link_mentions,
    render_ledger,
    validate_events,
)
from memspine.core.reference_edges import reference_kinds
from memspine.models.encoders.query.shape import is_multi_hop
from memspine.models.encoders.query.temporal_query import LegHit
from memspine.models.providers.llm.structured import structured_call
from memspine.observability.logging import get_logger
from memspine.prompts.models import (
    AgenticStepOut,
    EventsOut,
    MissingInfoOut,
    ReadPlan,
    SufficiencyOut,
)
from memspine.read.pipeline.router import _reply_targets
from memspine.read.pipeline.state import _FORENSICS, ReadResult
from memspine.store.records import MemoryRecord

_log = get_logger(__name__)


async def _agentic_fires(eng: Any, query: str, read: Any, records: Sequence[MemoryRecord]) -> bool:
    """I67 ``read.agentic_trigger``: ``always``; ``multi_hop`` (the surface heuristic);
    ``decider`` (task ``needs_more_evidence``; the heuristic when the decider is
    inactive, unsure or failing)."""
    heuristic = "needs_more" if is_multi_hop(query) else "enough"
    if read.agentic_trigger == "always":
        return True
    if read.agentic_trigger == "multi_hop":
        return heuristic == "needs_more"
    snippets = chr(10).join(f"- {r.content}" for r in records[:5])
    label = await eng._decided("needs_more_evidence", query, snippets, heuristic)
    return label == "needs_more"


async def _agentic_extend(
    eng: Any,
    query: str,
    ns: str,
    result: ReadResult,
    budget_tokens: int,
    *,
    hide: Callable[[MemoryRecord], bool] | None,
    session_id: str | None,
) -> ReadResult:
    """I67 (``read.agentic``): step 0 is ``result`` (the normal read, untouched). Up to
    ``read.agentic_max_steps`` times the action role sees the question and a compact
    view of the evidence and returns one structured action (``answer_ready``,
    ``search``, ``search_person_time``; JSON/YAML validated by ``structured_call`` with
    its repair and retry, not native tool calling). A search runs through the same
    gated ``_search`` of the SAME namespace (the model supplies only text, never a
    namespace); the new hits of all steps are RRF-fused, de-duplicated by record id and
    appended after the step-0 records under ``budget_tokens``: step-0 records inside
    ``read.agentic_first_share`` of the budget are never displaced. Stops on
    ``answer_ready``, ``max_steps``, ``no_new`` (a step found nothing new),
    ``budget`` (nothing more fits), ``repeat`` (a query already run), ``error`` or
    ``unbound`` (no action role); any of the last two keeps ``result``. Every step goes
    to ``search_forensics()["agentic_steps"]`` and the totals to ``["agentic"]``."""
    read = eng._config().read
    steps: list[dict[str, Any]] = []
    info: dict[str, Any] = {
        "trigger": read.agentic_trigger,
        "fired": False,
        "stop": None,
        "llm_calls": 0,
        "llm_s": 0.0,
        "search_s": 0.0,
        "new_ids": [],
        "displaced_ids": [],
    }
    if (fx := _FORENSICS.get()) is not None:
        fx["agentic"] = info
        fx["agentic_steps"] = steps
    context = result.context
    if result.mode == "full" or context.abstained or not context.records:
        info["stop"] = "full_context" if result.mode == "full" else "abstained"
        return result
    if not await eng._agentic_fires(query, read, context.records):
        info["stop"] = "not_triggered"
        return result
    llm_router = eng._llm
    role = next((r for r in ("sufficiency", "plan") if llm_router and r in llm_router.roles), None)
    if llm_router is None or role is None or eng._prompts is None:
        _log.warning("read.agentic_unbound", role="sufficiency")
        info["stop"] = "unbound"
        return result
    info["fired"] = True
    llm = llm_router.for_role(role)
    prompt = eng._prompts.select("sufficiency", condition="agentic")
    step0 = list(context.records)
    step0_ids = {r.record_id for r in step0}
    rrf_k = read.rrf_k or constants.RRF_K
    evidence = list(step0)
    rankings: list[list[MemoryRecord]] = []
    searched = {query.strip().lower()}
    added: list[MemoryRecord] = []
    displaced: list[MemoryRecord] = []
    stop = "max_steps"
    for number in range(1, read.agentic_max_steps + 1):
        row: dict[str, Any] = {"step": number, "action": None, "query": None, "why": ""}
        steps.append(row)
        shown = eng._remote_view(role, evidence)
        calls_before = eng.model_calls().get(role, 0)
        started = perf_counter()
        try:
            act = await structured_call(
                llm,
                prompt,
                {"question": query, "context": evidence_view(shown)},
                AgenticStepOut,
            )
        except Exception as exc:  # an enhancer, never a gate
            _log.warning("read.agentic_failed", error=str(exc))
            row.update(action="error", error=str(exc)[:200])
            stop = "error"
            break
        finally:
            spent = perf_counter() - started
            row["llm_s"] = round(spent, 4)
            row["llm_calls"] = eng.model_calls().get(role, 0) - calls_before
            info["llm_s"] += spent
            info["llm_calls"] += row["llm_calls"]
        row.update(action=act.action, why=act.why[:200])
        if act.action == "answer_ready":
            stop = "answer_ready"
            break
        legs: list[list[LegHit]] = []
        if act.action == "search":
            text = clean_query(act.query)
        else:
            text = clean_query(f"{act.person} {act.time}")
            plan = ReadPlan(
                mode="lookup",
                persons=[act.person] if act.person else [],
                time_expr=act.time or None,
            )
            legs = [await eng._person_time_leg(ns, plan)]
            row.update(person=act.person[:80], time=act.time[:80])
        row["query"] = text
        if not text or text.lower() in searched:
            stop = "repeat"
            break
        searched.add(text.lower())
        started = perf_counter()
        sink = _FORENSICS.set(None)  # the step-0 forensics stay the first pass's
        try:
            hits = await eng._search(
                text,
                ns,
                read.agentic_top_k,
                session_id=session_id,
                keep_k=read.agentic_top_k,
                hide=hide,
                probes=[],
                fused_legs=legs,
            )
            if read.facts_to_sources and hits:
                hits = await eng._facts_to_sources(ns, hits)
        except Exception as exc:  # an enhancer, never a gate
            _log.warning("read.agentic_search_failed", error=str(exc))
            row["error"] = str(exc)[:200]
            stop = "error"
            break
        finally:
            _FORENSICS.reset(sink)
            row["search_s"] = round(perf_counter() - started, 4)
            info["search_s"] += row["search_s"]
        rankings.append([r for r, _ in hits if r.namespace == ns and not r.quarantined])
        prior = {r.record_id for r in added}
        ranked = fuse_new(rankings, step0_ids, rrf_k)
        row["new_ids"] = [r.record_id for r in ranked if r.record_id not in prior]
        if not row["new_ids"]:
            stop = "no_new"
            break
        decorated = await eng._decorate(
            ns, [(r, 0.0) for r in ranked[: read.agentic_max_new * 2]], hide=hide
        )
        kept, added, displaced = merge_budget(
            step0,
            [r for r, _ in decorated],
            budget_tokens,
            share=read.agentic_first_share,
            max_new=read.agentic_max_new,
        )
        evidence = [*kept, *added]
        if {r.record_id for r in added} <= prior:
            stop = "budget"  # nothing of this step's hits fits
            break
    info["stop"] = stop
    return eng._agentic_assemble(query, result, added, displaced, info)


def _agentic_assemble(
    eng: Any,
    query: str,
    result: ReadResult,
    added: Sequence[MemoryRecord],
    displaced: Sequence[MemoryRecord],
    info: dict[str, Any],
    notes: Sequence[MemoryRecord] = (),
) -> ReadResult:
    """The agentic read's result: the step-0 records (less the displaced ones), the
    added records rendered, then the computed-note blocks. Nothing added => ``result``."""
    context = result.context
    if not added and not notes:
        return result
    info["new_ids"] = [r.record_id for r in added]
    info["displaced_ids"] = [r.record_id for r in displaced]
    gone = {r.record_id for r in displaced}
    kept_records = [r for r in context.records if r.record_id not in gone]
    extra = eng._render(query, AssembledContext(records=list(added))).records if added else []
    records = [*kept_records, *extra, *notes]
    return ReadResult(
        result.mode,
        AssembledContext(
            records=records,
            boundary_index=min(context.boundary_index, len(kept_records)),
            abstained=False,
            tokens_used=sum(estimate_tokens(r.content) for r in records),
            evidence=context.evidence,
        ),
    )


async def _event_ledger_extend(
    eng: Any, query: str, ns: str, result: ReadResult, budget_tokens: int
) -> ReadResult:
    """A02 (``read.event_ledger: on``): a source-backed event ledger over the evidence
    already in context, for a question about events or state.

    ONE bounded ``extract@events`` call over the numbered evidence lines; code validates
    every span, resolves the event time from the quoted phrase, links repeated mentions
    only on identity evidence and renders a compact block (a synthetic, never-stored
    record, trust = the least of its sources) appended after the raw turns under the same
    budget rules as the fact chain. Any failure, no valid mention or an unbound role keeps
    ``result``. Forensics: ``search_forensics()["event_ledger"]``."""
    read = eng._config().read
    info: dict[str, Any] = {
        "trigger": read.event_ledger_trigger,
        "fired": False,
        "stop": None,
        "llm_calls": 0,
        "llm_s": 0.0,
        "mentions": 0,
        "valid": 0,
        "events": 0,
    }
    if (fx := _FORENSICS.get()) is not None:
        fx["event_ledger"] = info
    context = result.context
    evidence = [
        r
        for r in context.records
        if constants.LEAD_TAG not in r.tags and r.namespace == ns and not r.quarantined
    ][: constants.EVENT_LEDGER_MAX_LINES]
    if result.mode == "full" or context.abstained or not evidence:
        info["stop"] = "full_context" if result.mode == "full" else "no_evidence"
        return result
    if read.event_ledger_trigger == "event_questions":
        contract = await eng._query_contract(query)
        if not is_event_question(query, contract.answer_type):
            info["stop"] = "not_triggered"
            return result
    llm_router = eng._llm
    if llm_router is None or eng._prompts is None or "extract" not in llm_router.roles:
        _log.warning("read.event_ledger_unbound", role="extract")
        info["stop"] = "unbound"
        return result
    info["fired"] = True
    shown = eng._remote_view("extract", evidence)
    lines = [" ".join(r.content.split())[: constants.EVENT_LEDGER_LINE_CHARS] for r in shown]
    numbered = "\n".join(f"[{i}] {text}" for i, text in enumerate(lines, 1))
    calls_before = eng.model_calls().get("extract", 0)
    started = perf_counter()
    try:
        out = await structured_call(
            llm_router.for_role("extract"),
            eng._prompts.select("extract", condition="events"),
            {"question": query, "content": numbered},
            EventsOut,
        )
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.event_ledger_failed", error=str(exc))
        info["stop"] = "error"
        return result
    finally:
        info["llm_s"] = round(perf_counter() - started, 4)
        info["llm_calls"] = eng.model_calls().get("extract", 0) - calls_before
    info["mentions"] = len(out.events)
    mentions, dropped = validate_events(out.events, lines, [r.valid_from for r in shown])
    info["valid"] = len(mentions)
    info["dropped"] = dropped
    if not mentions:
        info["stop"] = "no_valid_mention"
        return result
    events, refused = link_mentions(mentions, lines)
    info["refused_links"] = refused
    text, events = render_ledger(
        events,
        heading=constants.EVENT_LEDGER_MARKER,
        max_events=constants.EVENT_LEDGER_MAX_EVENTS,
        fits=lambda t: (
            estimate_tokens(t) <= max(1, int(constants.EVENT_LEDGER_BLOCK_SHARE * budget_tokens))
        ),
    )
    info["events"] = len(events)
    if not events:
        info["stop"] = "budget"
        return result
    cited = sorted({m.line for ev in events for m in ev.mentions})
    block = eng._lead_record(
        ns, text, [shown[n - 1] for n in cited], extra_tags=[constants.EVENT_LEDGER_TAG]
    )
    info["ledger_lines"] = cited
    info["stop"] = "built"
    kept, added, displaced = merge_budget(
        list(context.records),
        [block],
        budget_tokens,
        share=read.agentic_first_share,
        max_new=1,
    )
    if not added:
        info["stop"] = "budget"
        return result
    info["displaced_ids"] = [r.record_id for r in displaced]
    records = [*kept, *added]
    return ReadResult(
        result.mode,
        AssembledContext(
            records=records,
            boundary_index=min(context.boundary_index, len(kept)),
            abstained=False,
            tokens_used=sum(estimate_tokens(r.content) for r in records),
            evidence=context.evidence,
        ),
    )


async def _attach_antecedents(
    eng: Any,
    ns: str,
    hits: Sequence[MemoryRecord],
    chosen: list[MemoryRecord],
    seen: set[str],
    best_window: set[str],
    used: int,
    budget_tokens: int,
    read_cfg: Any,
) -> int:
    """A07 (``read.reference_edges``): the antecedent of each retrieved turn that carries a
    ``ref:`` tag joins the evidence, best hit first, at most ``reference_max_per_read``,
    same namespace, within the budget. Each decision is logged (``reference_edges`` in the
    forensics): attached, already present, over budget, over the cap, or missing. Never
    raises: a failure leaves the evidence as it was. Returns the updated token use."""
    fx = _FORENSICS.get()
    attached = 0
    try:
        for rank, hit in enumerate(hits):
            kinds = reference_kinds(hit.tags)
            if not kinds:
                continue
            for answered in _reply_targets(hit):
                entry = {"hit": hit.record_id, "antecedent": answered, "kind": kinds[0]}
                if answered in seen:
                    outcome = "present"
                elif attached >= read_cfg.reference_max_per_read:
                    outcome = "over_cap"
                else:
                    turn = await eng._replay_neighbour(answered, ns)
                    if turn is None or turn.namespace != ns:
                        outcome = "missing"
                    elif used + len(turn.content) // 4 + 1 > budget_tokens:
                        outcome = "over_budget"
                    else:
                        outcome = "attached"
                        chosen.append(turn)
                        seen.add(answered)
                        used += len(turn.content) // 4 + 1
                        attached += 1
                        if rank == 0:
                            best_window.add(answered)
                if fx is not None:
                    fx.setdefault("reference_edges", []).append({**entry, "outcome": outcome})
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.reference_edges_failed", namespace=ns, error=str(exc))
    return used


async def _missing_info_queries(eng: Any, query: str, context: AssembledContext) -> list[str]:
    """#38: the completeness verdict on ``context``, then (only when incomplete) the
    missing-information queries; [] when complete, unbound or on any failure.

    The ``sufficiency`` role answers, else the ``plan`` role. The notes are the
    context's records one line each, as the read rendered them (escaped, wrapped)."""
    llm_router = eng._llm
    role = next(
        (r for r in ("sufficiency", "plan") if llm_router and r in llm_router.roles),
        None,
    )
    if llm_router is None or role is None or eng._prompts is None:
        _log.warning("read.completeness_unbound", role="sufficiency")
        return []
    shown = eng._remote_view(role, context.records)  # #50: withheld by id
    notes = "\n".join(f"- {' '.join(r.content.split())}" for r in shown)
    values: dict[str, object] = {"question": query, "context": notes}
    llm = llm_router.for_role(role)
    try:
        verdict = await structured_call(
            llm, eng._prompts.select("sufficiency"), values, SufficiencyOut
        )
        if verdict.complete:
            return []
        wanted = await structured_call(
            llm,
            eng._prompts.select("sufficiency", condition="missing"),
            values,
            MissingInfoOut,
        )
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.completeness_failed", error=str(exc))
        return []
    seen = {query.strip().lower()}
    queries: list[str] = []
    for text in wanted.queries:
        if text.strip().lower() not in seen:
            seen.add(text.strip().lower())
            queries.append(text.strip())
    return queries[: constants.COMPLETENESS_MAX_QUERIES]
