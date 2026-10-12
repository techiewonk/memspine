"""Deep tier: one sufficiency check, at most one rewrite, one extra retrieval (LIB-43, R-22 B).

Pure and unwired (R3-12 wires it). Stdlib, pydantic and the merged read libraries only. The model is
injected as callables, so the module makes no call of its own and is deterministic for deterministic
callables.

Flow::

    first pass + QueryDescription + ObligationReport
      1. rules: every obligation met            -> answer, zero calls
      2. budget (or router plan) forbids a call -> answer_with_caveat, zero calls
      3. ONE sufficiency call (strict output, validated; invalid -> rule holes)
            sufficient            -> answer_with_caveat from the first pass, no retrieval
            insufficient          -> typed holes (model holes if valid, else the rule holes)
      4. at most ONE rewrite call (only when the budget and the plan allow it; invalid -> none)
      5. queries = typed follow-ups from the holes (LIB-15 follow-ups preferred) + model rewrites
      6. ONE retrieve call; results merged into the first pass with de-duplication
      7. stop. There is no second round, ever.

Decisions encoded:

* R-22 B / R3-12: one sufficiency round and one rewrite; two model calls, one retrieval at most.
* LIB-23: sufficiency and rewrite are deep-tier, worker-model features; :meth:`DeepBudget.from_plan`
  reads the router's ``RewritePlan`` so a plan that forbids them yields zero calls.
* ER-15 / ER-39: every call is recorded with a cost class (``model_generative`` for the two judge
  calls, ``retrieval`` for the extra search), bounded by :class:`DeepBudget`, and counted apart.
* ER-32: model output is validated against a strict schema and never trusted for ids or scope. The
  model can only name a hole kind and an entity already in the question or in the rule holes, plus a
  short attribute; it cannot name item ids, sessions or time windows. Anything else makes the whole
  output invalid and the rule holes are used instead. Retrieved items come from ``retrieve_fn``, not
  from the model.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from memspine.read.obligations import (
    FollowUp,
    Hole,
    HoleKind,
    ObligationReport,
    QueryDescription,
    _norm,
)
from memspine.read.router_rules import RewritePlan

__all__ = [
    "CallRecord",
    "Candidate",
    "DeepBudget",
    "DeepQuery",
    "DeepResult",
    "RewriteRequest",
    "SufficiencyJudgement",
    "SufficiencyRequest",
    "TraceStep",
    "TypedHole",
    "deep_read",
    "parse_rewrite",
    "parse_sufficiency",
]

#: Hard ceilings, whatever the budget asks for: one judge call and one rewrite call.
MAX_MODEL_CALLS = 2
_MAX_HOLES = 8
_MAX_REWRITES = 3
_MAX_TEXT = 300
_MAX_NAME = 120

CostClass = Literal["model_generative", "retrieval"]
Action = Literal["answer", "answer_with_caveat"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ----------------------------------------------------------------------------- inputs / outputs


class Candidate(_Frozen):
    """One retrieved item. ``item_id`` identifies it for de-duplication (never model-supplied)."""

    item_id: str = Field(min_length=1)
    text: str = ""
    score: float = 0.0
    session_id: str | None = None


class DeepBudget(_Frozen):
    """Bounds on the deep step. ``max_model_calls`` is clamped to 2 (one judge, one rewrite)."""

    max_model_calls: int = Field(default=2, ge=0)
    allow_sufficiency: bool = True
    allow_rewrite: bool = True
    max_queries: int = Field(default=4, ge=1)
    max_new_items: int = Field(default=30, ge=0)
    #: Output token cap handed to each model call (judge thinking cap, P-15).
    max_output_tokens: int = Field(default=512, ge=1)
    #: How many first-pass items the judge is shown (deterministic prefix).
    max_items_shown: int = Field(default=12, ge=0)

    @classmethod
    def from_plan(cls, plan: RewritePlan, **overrides: Any) -> DeepBudget:
        """Budget that follows the router's rewrite plan (LIB-23): no plan permission, no calls."""
        allowed = plan.sufficiency_check_allowed
        return cls(
            max_model_calls=overrides.pop("max_model_calls", 2 if allowed else 0),
            allow_sufficiency=allowed,
            allow_rewrite=plan.rewrite_allowed and allowed,
            **overrides,
        )

    @property
    def calls_available(self) -> int:
        if not self.allow_sufficiency:
            return 0
        return min(self.max_model_calls, MAX_MODEL_CALLS)


class TypedHole(_Frozen):
    """A missing piece named by the judge. Kind, entity and attribute only (ER-32)."""

    kind: HoleKind
    entity: str | None = Field(default=None, max_length=_MAX_NAME)
    attribute: str | None = Field(default=None, max_length=_MAX_NAME)


class SufficiencyJudgement(_Frozen):
    """Strict structured output of the sufficiency call."""

    sufficient: StrictBool
    missing: tuple[TypedHole, ...] = Field(default=(), max_length=_MAX_HOLES)

    @field_validator("missing", mode="after")
    @classmethod
    def _consistent(cls, v: tuple[TypedHole, ...], info: Any) -> tuple[TypedHole, ...]:
        if info.data.get("sufficient") is True and v:
            raise ValueError("sufficient answer cannot list missing pieces")
        return v


class _RewriteOutput(_Frozen):
    queries: tuple[str, ...] = Field(min_length=1, max_length=_MAX_REWRITES)


class SufficiencyRequest(_Frozen):
    """What the injected judge sees. ``schema_hint`` documents the only accepted output shape."""

    contract: QueryDescription
    rule_holes: tuple[Hole, ...]
    items: tuple[Candidate, ...]
    max_output_tokens: int
    schema_hint: str = (
        '{"sufficient": bool, "missing": [{"kind": <hole kind>, "entity": str|null, '
        '"attribute": str|null}]}'
    )


class RewriteRequest(_Frozen):
    contract: QueryDescription
    holes: tuple[Hole, ...]
    max_output_tokens: int
    schema_hint: str = '{"queries": [str, ...]}  (1 to 3 short search queries)'


class DeepQuery(_Frozen):
    """One query for ``retrieve_fn``: a typed follow-up, a model rewrite, or both."""

    origin: Literal["follow_up", "rewrite"]
    text: str | None = None
    entity: str | None = None
    attribute: str | None = None
    time_window: str | None = None
    reason: HoleKind | None = None


class CallRecord(_Frozen):
    purpose: Literal["sufficiency", "rewrite", "retrieve"]
    cost_class: CostClass
    valid: bool = True
    detail: str = ""


class TraceStep(_Frozen):
    step: str
    detail: str


class DeepResult(_Frozen):
    action: Action
    #: Stable reason code, see the ``R_*`` constants.
    reason: str
    items: tuple[Candidate, ...]
    new_item_ids: tuple[str, ...] = ()
    holes: tuple[Hole, ...] = ()
    queries: tuple[DeepQuery, ...] = ()
    caveats: tuple[str, ...] = ()
    sufficient: bool | None = None
    calls: tuple[CallRecord, ...] = ()
    trace: tuple[TraceStep, ...] = ()

    @property
    def model_calls(self) -> int:
        return sum(1 for c in self.calls if c.cost_class == "model_generative")

    @property
    def retrieve_calls(self) -> int:
        return sum(1 for c in self.calls if c.cost_class == "retrieval")


R_MET = "rules_met"
R_NO_BUDGET = "budget_exhausted"
R_SUFFICIENT = "judge_sufficient"
R_EXTENDED = "extended"
R_NO_NEW = "extended_nothing_new"
R_NO_QUERIES = "no_queries"
R_RETRIEVE_FAILED = "retrieve_failed"


# ----------------------------------------------------------------------------- validation


def _loads(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):  # tolerate a fenced block, nothing else
            text = text.strip("`").removeprefix("json").strip()
        return json.loads(text)
    return raw


def parse_sufficiency(raw: Any, *, allowed_entities: Iterable[str]) -> SufficiencyJudgement | None:
    """Validate a judge output; ``None`` when invalid. Entities must be known (ER-32)."""
    try:
        data = _loads(raw)
        if isinstance(data, SufficiencyJudgement):
            judgement = data
        elif isinstance(data, Mapping):
            judgement = SufficiencyJudgement.model_validate(dict(data))
        else:
            return None
    except (ValueError, TypeError, ValidationError):
        return None
    allowed = {_norm(e) for e in allowed_entities}
    for h in judgement.missing:
        if h.entity is not None and _norm(h.entity) not in allowed:
            return None
    return judgement


def _clean_query(text: str) -> str | None:
    t = " ".join(text.split())
    if not t or len(t) > _MAX_TEXT or any(ord(c) < 32 for c in text if c not in "\t\n\r"):
        return None
    return t


def parse_rewrite(raw: Any) -> tuple[str, ...] | None:
    """Validate a rewrite output (list of strings or a queries object); ``None`` if invalid."""
    try:
        data = _loads(raw)
        if isinstance(data, (list, tuple)):
            data = {"queries": tuple(data)}
        if not isinstance(data, Mapping):
            return None
        out = _RewriteOutput.model_validate(dict(data))
    except (ValueError, TypeError, ValidationError):
        return None
    cleaned: list[str] = []
    for q in out.queries:
        c = _clean_query(q)
        if c is None:
            return None
        if _norm(c) not in {_norm(x) for x in cleaned}:
            cleaned.append(c)
    return tuple(cleaned)


def _as_candidate(x: Candidate | Mapping[str, Any]) -> Candidate | None:
    try:
        return x if isinstance(x, Candidate) else Candidate.model_validate(dict(x))
    except (ValueError, TypeError, ValidationError):
        return None


def _dedupe(items: Iterable[Candidate | Mapping[str, Any]]) -> list[Candidate]:
    """Keep the first position of an id and the best score seen for it."""
    order: list[str] = []
    best: dict[str, Candidate] = {}
    for raw in items:
        c = _as_candidate(raw)
        if c is None:
            continue
        cur = best.get(c.item_id)
        if cur is None:
            order.append(c.item_id)
            best[c.item_id] = c
        elif c.score > cur.score:
            best[c.item_id] = c
    return [best[i] for i in order]


# ----------------------------------------------------------------------------- queries


def _queries_from_holes(holes: Sequence[Hole], report: ObligationReport) -> list[DeepQuery]:
    """LIB-15 follow-ups first (they carry a time window), then follow-ups of any extra holes."""
    seen: set[tuple[str, str, str, str]] = set()
    out: list[DeepQuery] = []

    def add(f: FollowUp) -> None:
        key = (_norm(f.entity), _norm(f.attribute), f.time_window or "", f.reason)
        if key in seen:
            return
        seen.add(key)
        out.append(
            DeepQuery(
                origin="follow_up",
                entity=f.entity,
                attribute=f.attribute,
                time_window=f.time_window,
                reason=f.reason,
            )
        )

    wanted = {(_norm(h.entity), _norm(h.attribute), h.kind) for h in holes}
    for f in report.follow_ups:
        if (_norm(f.entity), _norm(f.attribute), f.reason) in wanted:
            add(f)
    for h in holes:
        add(h.follow_up)
    return out


def _rule_holes(report: ObligationReport) -> tuple[Hole, ...]:
    return tuple(h for r in report.results if r.status != "met" for h in r.holes)


def _model_holes(
    judgement: SufficiencyJudgement, rule_holes: Sequence[Hole], contract: QueryDescription
) -> tuple[Hole, ...]:
    """Typed model holes as :class:`Hole`; a missing entity defaults to the question's."""
    default_entity = contract.entities[0] if contract.entities else None
    holes: list[Hole] = []
    for m in judgement.missing:
        entity = m.entity if m.entity is not None else default_entity
        attribute = m.attribute if m.attribute is not None else contract.attribute
        holes.append(
            Hole(
                kind=m.kind,
                entity=entity,
                attribute=attribute,
                detail="judge",
                follow_up=FollowUp(
                    entity=entity, attribute=attribute, time_window=None, reason=m.kind
                ),
            )
        )
    return tuple(holes)


# ----------------------------------------------------------------------------- the step


def deep_read(
    first_pass: Sequence[Candidate | Mapping[str, Any]],
    contract: QueryDescription,
    obligations_report: ObligationReport,
    *,
    sufficiency_fn: Callable[[SufficiencyRequest], Any],
    rewrite_fn: Callable[[RewriteRequest], Any],
    retrieve_fn: Callable[[tuple[DeepQuery, ...]], Iterable[Candidate | Mapping[str, Any]]],
    budget: DeepBudget | None = None,
) -> DeepResult:
    """Run the deep tier once. See the module docstring; there is no loop."""
    budget = budget or DeepBudget()
    items = _dedupe(first_pass)
    trace: list[TraceStep] = []
    calls: list[CallRecord] = []

    def note(step: str, detail: str) -> None:
        trace.append(TraceStep(step=step, detail=detail))

    def done(action: Action, reason: str, **kw: Any) -> DeepResult:
        note("result", f"{action} ({reason})")
        return DeepResult(
            action=action,
            reason=reason,
            items=tuple(kw.pop("items", items)),
            calls=tuple(calls),
            trace=tuple(trace),
            **kw,
        )

    # 1. cheap rule check
    if obligations_report.verdict == "answerable":
        note("rules", "every obligation met: no model call")
        return done("answer", R_MET)
    rule_holes = _rule_holes(obligations_report)
    caveats = obligations_report.caveats
    note("rules", f"verdict={obligations_report.verdict}, {len(rule_holes)} open holes")

    # 2. budget
    available = budget.calls_available
    if available <= 0:
        note("budget", "no model call available: answer from the first pass with a caveat")
        return done(
            "answer_with_caveat",
            R_NO_BUDGET,
            holes=rule_holes,
            caveats=(*caveats, "deep tier skipped: call budget exhausted"),
        )

    # 3. one sufficiency call
    allowed_entities = [*contract.entities, *(h.entity for h in rule_holes if h.entity)]
    request = SufficiencyRequest(
        contract=contract,
        rule_holes=rule_holes,
        items=tuple(items[: budget.max_items_shown]),
        max_output_tokens=budget.max_output_tokens,
    )
    judgement: SufficiencyJudgement | None
    try:
        raw = sufficiency_fn(request)
        judgement = parse_sufficiency(raw, allowed_entities=allowed_entities)
    except Exception as exc:  # the injected model may fail; that is an invalid answer
        judgement = None
        note("sufficiency", f"call raised {type(exc).__name__}")
    available -= 1
    calls.append(
        CallRecord(
            purpose="sufficiency",
            cost_class="model_generative",
            valid=judgement is not None,
            detail="" if judgement is not None else "invalid output: rule holes used",
        )
    )
    holes = rule_holes
    if judgement is None:
        note("sufficiency", "invalid output: falling back to the rule holes")
    elif judgement.sufficient:
        note("sufficiency", "judge says sufficient: no rewrite, no retrieval")
        return done(
            "answer_with_caveat",
            R_SUFFICIENT,
            holes=rule_holes,
            caveats=caveats,
            sufficient=True,
        )
    else:
        model_holes = _model_holes(judgement, rule_holes, contract)
        if model_holes:
            holes = model_holes
        note("sufficiency", f"judge says insufficient, {len(holes)} typed holes")
    sufficient: bool | None = False if judgement is not None else None

    # 4. at most one rewrite
    rewrites: tuple[str, ...] = ()
    if budget.allow_rewrite and available >= 1:
        try:
            parsed = parse_rewrite(
                rewrite_fn(
                    RewriteRequest(
                        contract=contract, holes=holes, max_output_tokens=budget.max_output_tokens
                    )
                )
            )
        except Exception as exc:
            parsed = None
            note("rewrite", f"call raised {type(exc).__name__}")
        calls.append(
            CallRecord(
                purpose="rewrite",
                cost_class="model_generative",
                valid=parsed is not None,
                detail="" if parsed is not None else "invalid output: follow-ups only",
            )
        )
        if parsed is None:
            note("rewrite", "invalid output: using the typed follow-ups only")
        else:
            rewrites = parsed
            note("rewrite", f"{len(rewrites)} rewritten queries")
    else:
        note("rewrite", "not allowed or no budget: typed follow-ups only")

    # 5. queries: typed follow-ups first, then the model rewrites
    queries = _queries_from_holes(holes, obligations_report)
    queries.extend(DeepQuery(origin="rewrite", text=t) for t in rewrites)
    queries = queries[: budget.max_queries]
    if not queries:
        note("retrieve", "no query to run")
        return done(
            "answer_with_caveat",
            R_NO_QUERIES,
            holes=holes,
            caveats=caveats,
            sufficient=sufficient,
        )

    # 6. one retrieval, merge
    try:
        fetched = list(retrieve_fn(tuple(queries)))
        ok = True
    except Exception as exc:
        fetched = []
        ok = False
        note("retrieve", f"call raised {type(exc).__name__}")
    calls.append(
        CallRecord(
            purpose="retrieve",
            cost_class="retrieval",
            valid=ok,
            detail="" if ok else "retrieval failed: first pass kept",
        )
    )
    if not ok:
        return done(
            "answer_with_caveat",
            R_RETRIEVE_FAILED,
            holes=holes,
            queries=tuple(queries),
            caveats=caveats,
            sufficient=sufficient,
        )
    known = {c.item_id for c in items}
    extra = [c for c in _dedupe(fetched) if c.item_id not in known][: budget.max_new_items]
    # merged order: first pass (best score kept for ids retrieval also returned), then new items
    rescored = {c.item_id: c for c in _dedupe(fetched)}
    merged = [
        rescored[c.item_id] if c.item_id in rescored and rescored[c.item_id].score > c.score else c
        for c in items
    ] + extra
    note("retrieve", f"{len(extra)} new items merged, {len(fetched)} returned")
    return done(
        "answer" if extra else "answer_with_caveat",
        R_EXTENDED if extra else R_NO_NEW,
        items=merged,
        new_item_ids=tuple(c.item_id for c in extra),
        holes=holes,
        queries=tuple(queries),
        caveats=caveats,
        sufficient=sufficient,
    )
