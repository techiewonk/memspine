"""Read path: request planning and query-contract helpers moved out of the engine (F-10a)."""

from __future__ import annotations

from collections.abc import (
    Sequence,
)
from typing import Any

from memspine.models.encoders.query.shape import (
    is_aggregation,
    is_count,
    is_intent_list,
    is_set_question,
    is_set_question_wide,
    rule_read_mode,
)
from memspine.models.providers.llm.structured import structured_call
from memspine.observability.logging import get_logger
from memspine.prompts.models import (
    QueryContractOut,
    ReadPlan,
)
from memspine.read.contract import (
    QueryContract,
    build_contract,
    is_ambiguous,
    merge_contract,
    parse_contract,
)
from memspine.read.pipeline.state import (
    _FORENSICS,
)
from memspine.store.records import (
    MemoryRecord,
)

_log = get_logger(__name__)


def _list_mode_fires(eng: Any, query: str) -> bool:
    """B1: ``read.list_mode`` is on and the question matches ``read.list_trigger``."""
    read = eng._config().read
    if not read.list_mode:
        return False
    if read.list_trigger == "aggregation":
        return is_aggregation(query) or is_count(query)
    if read.list_trigger == "intent":
        return is_intent_list(query)
    if read.list_trigger == "set_question_wide":
        return is_set_question_wide(query)
    return is_set_question(query)


async def _decided(eng: Any, task: str, question: str, context: str | None, heuristic: str) -> str:
    """The label for ``task``: the decider's when it is configured for the task and sure
    enough, else ``heuristic`` (the existing rule's label, computed by the caller). Never
    raises. Every decision is recorded in the forensics as ``decisions``."""
    if not eng._decider_active(task):
        return heuristic
    read = eng._config().read
    entry: dict[str, Any] = {"task": task, "adapter": read.decider, "heuristic": heuristic}
    label = heuristic
    try:
        decision = await eng._decider_adapter().decide(task, question, context)
        entry.update(decision.as_meta())
        sure = (
            decision.confidence is not None and decision.confidence >= read.decider_min_confidence
        )
        if sure:
            label = decision.label
        entry["used"] = sure
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.decider_failed", task=task, error=str(exc))
        entry.update({"label": None, "confidence": None, "used": False, "error": str(exc)})
    entry["final"] = label
    if (fx := _FORENSICS.get()) is not None:
        fx.setdefault("decisions", []).append(entry)
    return label


async def _list_fires(eng: Any, query: str) -> bool:
    """B1 list-mode trigger; ``task=list_mode`` of the decider replaces the regex."""
    fired = eng._list_mode_fires(query)
    if not eng._config().read.list_mode or not eng._decider_active("list_mode"):
        return fired
    return await eng._decided("list_mode", query, None, "set" if fired else "single") == "set"


async def _bridge_fires(
    eng: Any, query: str, read: Any, scored: Sequence[tuple[MemoryRecord, float]]
) -> bool:
    """R2-1b gate; ``task=bridge_hop`` of the decider replaces ``read.bridge_hop_gate``."""
    fired = eng._bridge_gate(query, read)
    if not eng._decider_active("bridge_hop"):
        return fired
    snippets = chr(10).join(f"- {r.content}" for r, _ in scored[:5])
    label = await eng._decided("bridge_hop", query, snippets, "hop" if fired else "no_hop")
    if (fx := _FORENSICS.get()) is not None:
        fx["bridge_gate"] = "decider" if label == "hop" else "skipped"
    return label == "hop"


async def _plan_read_mode(eng: Any, query: str) -> str | None:
    """H24: the decision provider's read mode, or None (rules) on any failure.

    G24: the ``query_shape`` rules go first (counts and sets -> compose, ordering ->
    replay, not gated by confidence); the provider chooses only what they leave open.
    """
    provider = eng._decision_provider()
    if provider is None:
        return None
    ruled = rule_read_mode(query)
    if ruled is not None:
        return ruled
    options = {label: desc for label, (_mode, desc) in eng._READ_OPTIONS.items()}
    try:
        label, confidence = await provider.choose(query, options)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.planner_failed", error=str(exc))
        return None
    if label not in eng._READ_OPTIONS:
        return None
    gate = eng._config().read.planner_min_confidence
    # A bare label carries no confidence (None): below any positive gate.
    if (confidence is None and gate > 0) or (confidence is not None and confidence < gate):
        # G2b: an unsure choice does not route; keep the default replay read.
        _log.info("read.planner_unsure", label=label, confidence=confidence, gate=gate)
        return "replay"
    return eng._READ_OPTIONS[label][0]


async def _query_contract(eng: Any, query: str) -> QueryContract:
    """A03: the typed query contract of ``query`` under ``read.query_contract``.

    ``heuristic``: the rules (``core/query_contract``). ``llm``: the rules first; ONE
    ``plan@contract`` call only when the rules are unsure of the answer type
    (:func:`is_ambiguous`), and the reply may refine but never blank the contract; any
    failure keeps the rules' contract. Cached per question text (bounded), so the
    reranker hint, the reader header and the forensics share one build and one call."""
    cache: dict[str, QueryContract] = eng.__dict__.setdefault("_contract_cache", {})
    hit = cache.get(query)
    if hit is not None:
        return hit
    contract = build_contract(query)
    if eng._config().read.query_contract == "llm" and is_ambiguous(contract):
        contract = merge_contract(contract, await eng._llm_query_contract(query, contract))
    if len(cache) >= 512:
        cache.clear()
    cache[query] = contract
    return contract


async def _llm_query_contract(eng: Any, query: str, rules: QueryContract) -> QueryContract | None:
    """One ``plan`` role call (``plan@contract``), or None (the rules' contract stands)."""
    if eng._llm is None or eng._prompts is None or "plan" not in eng._llm.roles:
        _log.warning("read.query_contract_unbound", role="plan")
        return None
    try:
        out = await structured_call(
            eng._llm.for_role("plan"),
            eng._prompts.select("plan", condition="contract"),
            {"query": query},
            QueryContractOut,
        )
        return parse_contract(out.model_dump(), rules)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.query_contract_failed", error=str(exc))
        return None


async def _llm_read_plan(eng: Any, query: str) -> ReadPlan | None:
    """G2a: one ``plan`` role call (counted by the router), or None (rules).

    None when the role is not bound, the call fails, or the reply is not a
    valid :class:`ReadPlan`; each case logs a warning (an enhancer, never a gate).
    """
    if eng._llm is None or eng._prompts is None or "plan" not in eng._llm.roles:
        _log.warning("read.planner_unbound", planner="llm", role="plan")
        return None
    try:
        version = eng._config().read.planner_version
        return await structured_call(
            eng._llm.for_role("plan"),
            # #35: plan@v2 also writes evidence-seeking subqueries for lookups;
            # #36: plan@v3 also names the persons and the time expression.
            eng._prompts.select("plan", condition=None if version == "v1" else version),
            {"query": query},
            ReadPlan,
        )
    except Exception as exc:
        _log.warning("read.planner_failed", planner="llm", error=str(exc))
        return None


async def _query_rewrite_probes(eng: Any, query: str) -> list[str]:
    """P4 (JustMem COMPOSE): up to two answer-free rewrites from the ``query_rewrite``
    role (``@compose`` variant), when ``read.compose_rewrites`` is on and the role is
    bound. Any failure yields no extra probes (an enhancer, never a gate)."""
    if (
        not eng._config().read.compose_rewrites
        or eng._llm is None
        or eng._prompts is None
        or "query_rewrite" not in eng._llm.roles
    ):
        return []
    prompt = eng._prompts.select("query_rewrite", condition="compose")
    try:
        text = await eng._llm.for_role("query_rewrite").chat(prompt.render({"query": query}))
    except Exception as exc:
        _log.warning("read.query_rewrite_failed", error=str(exc))
        return []
    lines = [line.strip(" -*0123456789.\t") for line in text.splitlines()]
    return [line for line in lines if line and line.lower() != query.lower()][:2]
