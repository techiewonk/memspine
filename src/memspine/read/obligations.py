"""Evidence obligations: what a question type needs before the reader may answer (LIB-15, ER-19).

Pure and unwired (wired later by R3-16). Stdlib and pydantic only; no I/O, no model call,
deterministic: the same inputs give the same report whatever the order of facts or bundles.

Flow::

    QueryDescription --derive_obligations--> [Obligation]
    [Obligation] + [EvidenceBundle] --check--> ObligationReport

Obligation kinds and what "met" needs (``entity`` and ``attribute`` compare after NFKC + casefold, so
names in any script match; accents are kept):

* ``single``: one fact for the entity (and attribute, when named) with a value.
* ``join``: the *link* fact (subject, ``relation``, linked entity) AND an attribute fact for every
  distinct linked entity. Link only: partly met (hole ``missing_attribute`` for the linked entity).
  No link: unmet (hole ``missing_entity_link``).
* ``duration``: a stated duration, or both a start and an end anchor. One anchor: partly met (hole
  ``missing_end_anchor`` / ``missing_start_anchor``); none: unmet.
* ``list`` / ``count``: coverage of all members. Complete when an in-scope bundle is ``exhaustive`` or
  a fact is flagged ``list_complete``; a ``count`` is also met by an explicit ``stated_count``.
  Otherwise the distinct values seen make it partly met (hole ``possible_unseen_members``, caveat
  "at least N"); no values: unmet.
* ``comparison`` / ``order``: a time anchor for every operand (``entities``; at least two). Missing
  operands are holes ``missing_anchor``.
* ``latest``: a head value (a fact nothing in the evidence supersedes) and proof that nothing newer
  exists: the head fact or an in-scope bundle says ``recency_checked``. Without it: partly met (hole
  ``unverified_recency``). A fact superseded by an id that is not in hand: hole ``missing_successor``.
  Differing heads that cannot be ordered: hole ``conflicting_values``.

Verdict: every obligation met -> ``answerable``; any unmet, or a partly met one whose holes cannot be
phrased as a caveat -> ``abstain``; otherwise ``answer_with_caveat`` with caveat templates. Each hole
carries a :class:`FollowUp` retrieval suggestion (entity / attribute / time window) for the deep tier.

``also`` kinds on a query apply to the same entities and attribute (not to a join's linked entity).
Time anchors and ``valid_from`` are ISO-8601 strings, compared lexicographically (coarser precision
sorts before finer on a shared prefix).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EvidenceBundle",
    "EvidenceFact",
    "FollowUp",
    "Hole",
    "HoleKind",
    "Obligation",
    "ObligationKind",
    "ObligationReport",
    "ObligationResult",
    "QueryDescription",
    "Verdict",
    "check",
    "derive_obligations",
]

ObligationKind = Literal[
    "single", "join", "duration", "list", "count", "comparison", "order", "latest"
]
HoleKind = Literal[
    "missing_fact",
    "missing_entity_link",
    "missing_attribute",
    "missing_start_anchor",
    "missing_end_anchor",
    "possible_unseen_members",
    "stated_count_below_members",
    "missing_anchor",
    "unverified_recency",
    "missing_successor",
    "conflicting_values",
    "too_few_operands",
]
Verdict = Literal["answerable", "answer_with_caveat", "abstain"]
Status = Literal["met", "partly_met", "unmet"]

_CAVEATABLE: frozenset[str] = frozenset(
    {"possible_unseen_members", "unverified_recency", "stated_count_below_members"}
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _norm(text: str | None) -> str:
    """NFKC + casefold + collapsed whitespace; ``None`` -> empty string. Accents are kept."""
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


# ----------------------------------------------------------------------------- inputs


class QueryDescription(_Model):
    """Typed description of the question (R3-16 maps the query contract onto this)."""

    kind: ObligationKind = "single"
    entities: tuple[str, ...] = ()
    attribute: str | None = None
    #: ``join``: the relation from ``entities[0]`` to the entity whose ``attribute`` is asked.
    relation: str | None = None
    time_view: Literal["current", "as_of", "history", "any"] = "any"
    as_of: str | None = None
    #: Additional obligations over the same entities/attribute (for example a ``list`` that also needs ``latest``).
    also: tuple[ObligationKind, ...] = ()
    #: A ``single`` question about an attribute that changes over time becomes ``latest`` under ``current``.
    attribute_mutable: bool = False


class EvidenceFact(_Model):
    fact_id: str
    entity: str
    attribute: str
    value: str | None = None
    #: Relation facts: the entity this fact links ``entity`` to (for example sister -> "Mara").
    related_entity: str | None = None
    time_anchor: str | None = None
    anchor_role: Literal["start", "end", "point"] | None = None
    #: A stated duration ("three years"); satisfies a duration obligation on its own.
    duration: str | None = None
    session_id: str | None = None
    superseded_by: str | None = None
    valid_from: str | None = None
    stated_count: int | None = Field(default=None, ge=0)
    list_complete: bool = False
    #: The fact asserts that no newer value exists (checked against newer evidence).
    recency_checked: bool = False


class EvidenceBundle(_Model):
    bundle_id: str
    facts: tuple[EvidenceFact, ...] = ()
    #: Retrieval enumerated every member / checked for newer values, within the scope below.
    exhaustive: bool = False
    recency_checked: bool = False
    #: Scope of the two flags; ``None`` means any entity / attribute.
    scope_entity: str | None = None
    scope_attribute: str | None = None


# ----------------------------------------------------------------------------- outputs


class Obligation(_Model):
    obligation_id: str
    kind: ObligationKind
    entities: tuple[str, ...] = ()
    attribute: str | None = None
    relation: str | None = None
    time_view: Literal["current", "as_of", "history", "any"] = "any"
    as_of: str | None = None


class FollowUp(_Model):
    """A retrieval to run for the deep tier: fetch ``attribute`` of ``entity`` in ``time_window``."""

    entity: str | None
    attribute: str | None
    time_window: str | None = None
    reason: HoleKind


class Hole(_Model):
    kind: HoleKind
    entity: str | None = None
    attribute: str | None = None
    detail: str = ""
    follow_up: FollowUp


class ObligationResult(_Model):
    obligation_id: str
    kind: ObligationKind
    status: Status
    holes: tuple[Hole, ...] = ()
    supporting_fact_ids: tuple[str, ...] = ()
    caveat: str | None = None


class ObligationReport(_Model):
    verdict: Verdict
    results: tuple[ObligationResult, ...]
    caveats: tuple[str, ...] = ()
    follow_ups: tuple[FollowUp, ...] = ()

    @property
    def answerable(self) -> bool:
        return self.verdict == "answerable"


# ----------------------------------------------------------------------------- derive


def derive_obligations(contract: QueryDescription) -> list[Obligation]:
    """Obligations for a typed query: the primary kind, then ``also`` kinds, deduplicated in order."""
    primary: ObligationKind = contract.kind
    if primary == "single" and contract.time_view == "current" and contract.attribute_mutable:
        primary = "latest"
    kinds: list[ObligationKind] = []
    for k in (primary, *contract.also):
        if k not in kinds:
            kinds.append(k)
    return [
        Obligation(
            obligation_id=f"ob{i}:{k}",
            kind=k,
            entities=contract.entities,
            attribute=contract.attribute,
            relation=contract.relation,
            time_view=contract.time_view,
            as_of=contract.as_of,
        )
        for i, k in enumerate(kinds)
    ]


# ----------------------------------------------------------------------------- check


class _Ev:
    """Merged, order-independent view over the bundles."""

    def __init__(self, bundles: Iterable[EvidenceBundle]) -> None:
        by_id: dict[str, EvidenceFact] = {}
        self.bundles = sorted(bundles, key=lambda b: b.bundle_id)
        for b in self.bundles:
            for f in b.facts:
                by_id.setdefault(f.fact_id, f)
        self.facts = sorted(by_id.values(), key=lambda f: f.fact_id)
        self.by_id = by_id

    def select(self, entity: str | None, attribute: str | None) -> list[EvidenceFact]:
        e, a = _norm(entity), _norm(attribute)
        return [
            f
            for f in self.facts
            if (not e or _norm(f.entity) == e) and (not a or _norm(f.attribute) == a)
        ]

    def flag(
        self,
        name: Literal["exhaustive", "recency_checked"],
        entity: str | None,
        attribute: str | None,
    ) -> bool:
        e, a = _norm(entity), _norm(attribute)
        for b in self.bundles:
            if not getattr(b, name):
                continue
            if (not b.scope_entity or not e or _norm(b.scope_entity) == e) and (
                not b.scope_attribute or not a or _norm(b.scope_attribute) == a
            ):
                return True
        return False


_Outcome = tuple[Status, list[Hole], list[str], str | None]


def _hole(
    kind: HoleKind,
    entity: str | None,
    attribute: str | None,
    detail: str = "",
    window: str | None = None,
) -> Hole:
    return Hole(
        kind=kind,
        entity=entity,
        attribute=attribute,
        detail=detail,
        follow_up=FollowUp(entity=entity, attribute=attribute, time_window=window, reason=kind),
    )


def _status(holes: list[Hole], supporting: list[str]) -> Status:
    if not holes:
        return "met"
    return "partly_met" if supporting else "unmet"


def _ids(facts: Iterable[EvidenceFact]) -> list[str]:
    return sorted({f.fact_id for f in facts})


def _check_single(ob: Obligation, ev: _Ev) -> _Outcome:
    ent = ob.entities[0] if ob.entities else None
    hits = [f for f in ev.select(ent, ob.attribute) if f.value is not None]
    if hits:
        return "met", [], _ids(hits[:1]), None
    return "unmet", [_hole("missing_fact", ent, ob.attribute)], [], None


def _check_join(ob: Obligation, ev: _Ev) -> _Outcome:
    subject = ob.entities[0] if ob.entities else None
    links = [f for f in ev.select(subject, ob.relation) if f.related_entity]
    if not links:
        return (
            "unmet",
            [
                _hole(
                    "missing_entity_link",
                    subject,
                    ob.relation,
                    "no fact links the subject through the relation",
                )
            ],
            [],
            None,
        )
    targets: dict[str, str] = {}
    for f in links:
        if f.related_entity:
            targets.setdefault(_norm(f.related_entity), f.related_entity)
    holes: list[Hole] = []
    supporting = _ids(links)
    for key in sorted(targets):
        hits = [f for f in ev.select(targets[key], ob.attribute) if f.value is not None]
        if hits:
            supporting += _ids(hits[:1])
        else:
            holes.append(
                _hole(
                    "missing_attribute",
                    targets[key],
                    ob.attribute,
                    f"linked via {ob.relation or 'relation'}",
                )
            )
    return _status(holes, supporting), holes, sorted(set(supporting)), None


def _check_duration(ob: Obligation, ev: _Ev) -> _Outcome:
    ent = ob.entities[0] if ob.entities else None
    facts = ev.select(ent, ob.attribute)
    stated = [f for f in facts if f.duration]
    if stated:
        return "met", [], _ids(stated[:1]), None
    starts = [f for f in facts if f.anchor_role == "start" and f.time_anchor]
    ends = [f for f in facts if f.anchor_role == "end" and f.time_anchor]
    if starts and ends:
        return "met", [], _ids([starts[0], ends[0]]), None
    if starts:
        return (
            "partly_met",
            [
                _hole(
                    "missing_end_anchor", ent, ob.attribute, window=f"after:{starts[0].time_anchor}"
                )
            ],
            _ids(starts[:1]),
            None,
        )
    if ends:
        return (
            "partly_met",
            [
                _hole(
                    "missing_start_anchor",
                    ent,
                    ob.attribute,
                    window=f"before:{ends[0].time_anchor}",
                )
            ],
            _ids(ends[:1]),
            None,
        )
    return (
        "unmet",
        [
            _hole("missing_start_anchor", ent, ob.attribute),
            _hole("missing_end_anchor", ent, ob.attribute),
        ],
        [],
        None,
    )


def _check_members(ob: Obligation, ev: _Ev) -> _Outcome:
    ent = ob.entities[0] if ob.entities else None
    facts = [
        f for f in ev.select(ent, ob.attribute) if f.value is not None or f.stated_count is not None
    ]
    members = {_norm(f.value) for f in facts if f.value is not None}
    n = len(members)
    complete = any(f.list_complete for f in facts) or ev.flag("exhaustive", ent, ob.attribute)
    stated = sorted({f.stated_count for f in facts if f.stated_count is not None})
    supporting = _ids(facts)
    if ob.kind == "count" and stated:
        if n > stated[-1]:
            return (
                "partly_met",
                [
                    _hole(
                        "stated_count_below_members",
                        ent,
                        ob.attribute,
                        f"stated {stated[-1]}, seen {n}",
                    )
                ],
                supporting,
                f"at least {n}",
            )
        return "met", [], supporting, None
    if complete and n:
        return "met", [], supporting, None
    if n:
        hole = _hole(
            "possible_unseen_members",
            ent,
            ob.attribute,
            f"{n} distinct value(s) seen; coverage not confirmed",
        )
        return "partly_met", [hole], supporting, f"at least {n}"
    return (
        "unmet",
        [_hole("possible_unseen_members", ent, ob.attribute, "no members seen")],
        [],
        None,
    )


def _check_comparison(ob: Obligation, ev: _Ev) -> _Outcome:
    holes: list[Hole] = []
    supporting: list[str] = []
    if len(ob.entities) < 2:
        holes.append(
            _hole(
                "too_few_operands",
                ob.entities[0] if ob.entities else None,
                ob.attribute,
                "needs at least two operands",
            )
        )
    for e in ob.entities:
        hits = [f for f in ev.select(e, ob.attribute) if f.time_anchor]
        if hits:
            supporting += _ids(hits[:1])
        else:
            holes.append(_hole("missing_anchor", e, ob.attribute))
    return _status(holes, supporting), holes, sorted(set(supporting)), None


def _check_latest(ob: Obligation, ev: _Ev) -> _Outcome:
    ent = ob.entities[0] if ob.entities else None
    facts = [f for f in ev.select(ent, ob.attribute) if f.value is not None]
    if not facts:
        return "unmet", [_hole("missing_fact", ent, ob.attribute)], [], None
    heads = [f for f in facts if not f.superseded_by]
    dangling = [f for f in facts if f.superseded_by and f.superseded_by not in ev.by_id]
    holes: list[Hole] = []
    if not heads:
        for f in dangling:
            stamp = f.valid_from or f.time_anchor
            holes.append(
                _hole(
                    "missing_successor",
                    ent,
                    ob.attribute,
                    f"{f.fact_id} superseded by {f.superseded_by}",
                    window=f"after:{stamp}" if stamp else None,
                )
            )
        return "unmet", holes, _ids(facts), None

    def key(f: EvidenceFact) -> str:
        return f.valid_from or f.time_anchor or ""

    best = max(key(f) for f in heads)
    top = [f for f in heads if key(f) == best]
    if len({_norm(f.value) for f in top}) > 1:
        return (
            "partly_met",
            [_hole("conflicting_values", ent, ob.attribute, "heads cannot be ordered")],
            _ids(top),
            None,
        )
    head = top[0]
    for f in dangling:
        holes.append(
            _hole(
                "missing_successor",
                ent,
                ob.attribute,
                f"{f.fact_id} superseded by {f.superseded_by}",
            )
        )
    if not (head.recency_checked or ev.flag("recency_checked", ent, ob.attribute)):
        stamp = head.valid_from or head.time_anchor
        holes.append(
            _hole(
                "unverified_recency",
                ent,
                ob.attribute,
                "no evidence that nothing newer exists",
                window=f"after:{stamp}" if stamp else None,
            )
        )
    if holes:
        caveat = (
            "latest value found; newer updates were not checked"
            if all(h.kind == "unverified_recency" for h in holes)
            else None
        )
        return "partly_met", holes, _ids([head]), caveat
    return "met", [], _ids([head]), None


_CHECKS = {
    "single": _check_single,
    "join": _check_join,
    "duration": _check_duration,
    "list": _check_members,
    "count": _check_members,
    "comparison": _check_comparison,
    "order": _check_comparison,
    "latest": _check_latest,
}


def check(obligations: Sequence[Obligation], bundles: Iterable[EvidenceBundle]) -> ObligationReport:
    """Check each obligation against the evidence in hand and derive the overall verdict."""
    ev = _Ev(bundles)
    results: list[ObligationResult] = []
    for ob in obligations:
        status, holes, supporting, caveat = _CHECKS[ob.kind](ob, ev)
        results.append(
            ObligationResult(
                obligation_id=ob.obligation_id,
                kind=ob.kind,
                status=status,
                holes=tuple(holes),
                supporting_fact_ids=tuple(supporting),
                caveat=caveat,
            )
        )
    if any(r.status == "unmet" for r in results):
        verdict: Verdict = "abstain"
    elif all(r.status == "met" for r in results):
        verdict = "answerable"
    elif all(h.kind in _CAVEATABLE for r in results for h in r.holes):
        verdict = "answer_with_caveat"
    else:
        verdict = "abstain"
    caveats: list[str] = []
    if verdict == "answer_with_caveat":
        for r in results:
            if r.status == "partly_met":
                text = r.caveat or "; ".join(h.detail or h.kind for h in r.holes)
                if text not in caveats:
                    caveats.append(text)
    seen: dict[tuple[str, str, str, str], FollowUp] = {}
    for r in results:
        for h in r.holes:
            f = h.follow_up
            seen.setdefault((_norm(f.entity), _norm(f.attribute), f.time_window or "", f.reason), f)
    follow_ups = tuple(seen[k] for k in sorted(seen))
    return ObligationReport(
        verdict=verdict, results=tuple(results), caveats=tuple(caveats), follow_ups=follow_ups
    )
