# ruff: noqa: E501
"""Explicit feedback events and multi-agent observer views (LIB-44; O-25, O-26, M-24).

Pure and deterministic: no I/O, no model calls, no clock reads, no engine imports. Not wired yet
(O4-11 wires it).

* **Explicit only (O-25).** A :class:`FeedbackEvent` comes from a channel the user or an operator
  deliberately used: a message that says so, a UI control, an operator edit, an API call. Signals
  inferred from silence, dwell, regeneration or engagement are refused at construction.
* **Feedback never edits records (S5-2).** A correction is an asserted, bound statement by the
  owner; a rejection is a bound negation of the recalled value; a confirmation is a user-confirmed
  restatement. :func:`to_assertions` only produces *draft* assertion envelopes; integration
  (``derive.integrate``) decides supersede, retire, contest or corroborate. A rating is a signal
  only and produces no assertion.
* **Who may give feedback.** Only the memory owner (all kinds) or a registered operator
  (corrections, rejections, ratings; never confirmations: a confirmation is the user's word).
  An agent may never give feedback, so no agent can confirm its own memory. See :func:`check_feedback`.
* **Observer views (M-24 C, G-07).** :func:`observer_view` projects records an agent may see:
  its own at full trust, another agent's only through a visible grant chain (LIB-21), with trust
  attenuated by the product of kappa. Items are frozen, read-only, and an excluded item carries a
  reason code and no text.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from memspine.schemas import InternalModel
from memspine.schemas.durable import (
    AssertionEnvelope,
    Assessment,
    Evidence,
    OwnerScope,
    Provenance,
    Support,
    TimeAxes,
    TurnRef,
    occurrence_id,
    write_id,
)
from memspine.schemas.durable.evidence import Span
from memspine.trust.horizon import (
    Grant,
    TrustedRecord,
    chain_attenuation,
    chain_visible,
    effective_trust,
)

__all__ = [
    "DEFAULT_OPERATOR_TRUST",
    "DEFAULT_OWNER_TRUST",
    "EXPLICIT_CHANNELS",
    "INFERRED_CHANNELS",
    "Actor",
    "ExposureRecord",
    "FeedbackError",
    "FeedbackEvent",
    "LatencyStats",
    "ObservableRecord",
    "ViewItem",
    "check_feedback",
    "correction_latency",
    "observer_view",
    "to_assertions",
]

#: Channels a person deliberately used. Anything else is not feedback (O-25).
EXPLICIT_CHANNELS: frozenset[str] = frozenset(
    {"user_message", "ui_control", "operator_edit", "api"}
)
#: Named only to give a precise refusal message; every non-explicit channel is refused anyway.
INFERRED_CHANNELS: frozenset[str] = frozenset(
    {
        "silence",
        "engagement",
        "dwell",
        "regeneration",
        "retry",
        "copy",
        "click",
        "abandonment",
        "implicit",
        "classifier",
        "inferred",
    }
)

DEFAULT_OWNER_TRUST = 0.9
DEFAULT_OPERATOR_TRUST = 0.8

FeedbackKind = Literal["correction", "rejection", "confirmation", "rating"]
ActorKind = Literal["user", "operator", "agent"]
Scalar = str | int | float | bool


class FeedbackError(ValueError):
    """Feedback that is not explicit, not permitted for its actor, or cannot be resolved."""


class _Frozen(InternalModel):
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())


class Actor(_Frozen):
    """Who gave the feedback. ``kind`` is a claim; :func:`check_feedback` verifies it."""

    principal: str = Field(min_length=1)
    kind: ActorKind


class FeedbackEvent(_Frozen):
    """One explicit feedback event about one or more recalled memories.

    ``turn`` is the conversation turn index at which it was given (used for correction latency).
    ``source`` cites the turn that carries the feedback (every assertion needs a citation, ER-32);
    it is required for every kind except ``rating``.
    """

    event_id: str = Field(min_length=1)
    kind: FeedbackKind
    channel: str
    target_ids: tuple[str, ...] = Field(min_length=1)
    actor: Actor
    owner: str = Field(min_length=1)  # owner of the memory the feedback is about
    at: AwareDatetime
    turn: int | None = Field(default=None, ge=0)
    source: TurnRef | None = None
    quote: str | None = None
    new_value: Scalar | None = None
    new_text: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def _shape(self) -> FeedbackEvent:
        if self.channel in INFERRED_CHANNELS:
            raise ValueError(
                f"channel {self.channel!r} is an inferred signal; only explicit feedback is accepted"
            )
        if self.channel not in EXPLICIT_CHANNELS:
            raise ValueError(
                f"unknown feedback channel {self.channel!r}; explicit channels are "
                f"{sorted(EXPLICIT_CHANNELS)}"
            )
        if len(set(self.target_ids)) != len(self.target_ids):
            raise ValueError("target_ids must be distinct")
        if self.kind == "correction":
            if self.new_value is None:
                raise ValueError("a correction needs new_value")
            if len(self.target_ids) != 1:
                raise ValueError("a correction targets exactly one record")
        elif self.new_value is not None or self.new_text is not None:
            raise ValueError(f"{self.kind} may not carry a new value")
        if self.kind == "rating":
            if self.rating is None:
                raise ValueError("a rating needs a score 1..5")
        else:
            if self.rating is not None:
                raise ValueError(f"{self.kind} may not carry a rating")
            if self.source is None:
                raise ValueError(f"{self.kind} must cite the turn that carries it (source)")
        if self.new_text is not None and not self.new_text.strip():
            raise ValueError("new_text may not be blank")
        return self


def check_feedback(
    event: FeedbackEvent, *, operators: Iterable[str] = (), owner: str | None = None
) -> None:
    """Raise :class:`FeedbackError` unless the actor may give this feedback on this owner's memory.

    * ``owner`` (default: the event's own ``owner`` field) is the memory owner as the caller knows it;
      the event may not name a different one.
    * An agent may never give feedback (so none can confirm its own or another agent's memory).
    * A ``user`` actor must be the owner. An ``operator`` actor must be in ``operators``.
    * Operators may correct, reject and rate; a confirmation is the user's own word.
    """
    expected = event.owner if owner is None else owner
    if event.owner != expected:
        raise FeedbackError(f"event is about {event.owner!r}'s memory, not {expected!r}'s")
    actor = event.actor
    if actor.kind == "agent":
        raise FeedbackError("an agent cannot give feedback on memory (no self-confirmation)")
    if actor.kind == "user":
        if actor.principal != expected:
            raise FeedbackError(
                f"user {actor.principal!r} is not the memory owner {expected!r}; "
                "only the owner may give user feedback"
            )
        return
    if actor.principal not in set(operators):
        raise FeedbackError(f"{actor.principal!r} is not a registered operator")
    if event.kind == "confirmation":
        raise FeedbackError("an operator cannot confirm on the user's behalf")


# --------------------------------------------------------------------------- assertions


def _trust(base: float, target: AssertionEnvelope) -> float:
    """An explicit say-so about a memory is at least as trusted as the memory (so it can retire it)."""
    return min(1.0, max(base, target.assessment.source_trust))


def _turn_id(turn: TurnRef) -> str:
    return f"{turn.source_id}@{turn.source_version}#{turn.seg_index}"


def _draft(
    event: FeedbackEvent,
    target_id: str,
    target: AssertionEnvelope,
    *,
    modality: Literal["asserted", "negated"],
    src_class: Literal["user_stated", "user_confirmed", "external"],
    value: Scalar | None,
    text: str,
    trust: float,
    family: str,
) -> AssertionEnvelope:
    assert event.source is not None  # guaranteed for non-rating kinds
    span = Span(start=0, end=len(event.quote), quote=event.quote) if event.quote else None
    support = Support(independent=1, families=[family])
    return AssertionEnvelope(
        write_id=write_id(
            target.scope,
            event.source.source_id,
            event.source.seg_index,
            f"feedback:{event.event_id}:{target_id}",
        ),
        assertion_key=target.assertion_key,
        occurrence_id=occurrence_id(event.event_id, [_turn_id(event.source)], target.assertion_key),
        scope=target.scope,
        provenance=Provenance(
            session_id=target.provenance.session_id,
            principal=event.actor.principal,
            source_id=event.source.source_id,
            source_version=event.source.source_version,
        ),
        text=text,
        kind=target.kind,
        subject=target.subject,
        object=target.object,
        attribute=target.attribute,
        value=value,
        speaker=event.actor.principal,
        time=TimeAxes(source_time=event.at),
        evidence=[
            Evidence(role="support", turn=event.source, span=span, source_family=family),
            Evidence(role="binding", turn=event.source, source_family=family),
        ],
        assessment=Assessment(
            source_trust=trust,
            support=support,
            binding="bound",
            modality=modality,
        ),
        src_class=src_class,
        extras={"feedback": {"event": event.event_id, "kind": event.kind, "target": target_id}},
    )


def to_assertions(
    event: FeedbackEvent,
    targets: Mapping[str, AssertionEnvelope],
    *,
    operators: Iterable[str] = (),
    owner_trust: float = DEFAULT_OWNER_TRUST,
    operator_trust: float = DEFAULT_OPERATOR_TRUST,
) -> list[AssertionEnvelope]:
    """Draft assertions that carry ``event`` into integration. Never edits a record (S5-2).

    ``targets`` maps each target record id to the envelope it names (the recalled memory). Drafts
    have ``record_id=None``; the write door assigns it.

    * correction -> one asserted, bound ``user_stated`` assertion with the new value on the target's key
      (an operator edit is ``external``, family ``operator:<id>``);
    * rejection -> one bound ``negated`` assertion of the recalled value on the target's key;
    * confirmation -> one asserted, bound ``user_confirmed`` restatement of the target's value; the
      owner's family adds support (independent when the target came from someone else);
    * rating -> nothing (a signal, not a statement).

    Trust is ``max(base, target trust)`` capped at 1, so the owner's explicit word can retire the
    memory it is about. Raises :class:`FeedbackError` on a permission failure, an unknown target, or a
    target owned by someone else.
    """
    check_feedback(event, operators=operators)
    if event.kind == "rating":
        return []
    operator = event.actor.kind == "operator"
    family = f"{'operator' if operator else 'person'}:{event.actor.principal}"
    base = operator_trust if operator else owner_trust
    out: list[AssertionEnvelope] = []
    for target_id in event.target_ids:
        target = targets.get(target_id)
        if target is None:
            raise FeedbackError(f"unknown target record {target_id!r}")
        scope: OwnerScope = target.scope
        if scope.owner != event.owner:
            raise FeedbackError(
                f"target {target_id!r} belongs to {scope.owner!r}, not {event.owner!r}"
            )
        trust = _trust(base, target)
        if event.kind == "correction":
            value = event.new_value
            label = target.attribute or target.kind
            text = event.new_text or f"{label}: {value}"
            out.append(
                _draft(
                    event,
                    target_id,
                    target,
                    modality="asserted",
                    src_class="external" if operator else "user_stated",
                    value=value,
                    text=text,
                    trust=trust,
                    family=family,
                )
            )
        elif event.kind == "rejection":
            out.append(
                _draft(
                    event,
                    target_id,
                    target,
                    modality="negated",
                    src_class="external" if operator else "user_stated",
                    value=target.value,
                    text=f"not: {target.text}",
                    trust=trust,
                    family=family,
                )
            )
        else:  # confirmation (operators were refused above)
            out.append(
                _draft(
                    event,
                    target_id,
                    target,
                    modality="asserted",
                    src_class="user_confirmed",
                    value=target.value,
                    text=target.text,
                    trust=trust,
                    family=family,
                )
            )
    return out


# --------------------------------------------------------------------------- correction latency


class ExposureRecord(_Frozen):
    """History entry: ``record_id`` was shown to the user (recalled into context) at ``turn``."""

    record_id: str = Field(min_length=1)
    turn: int = Field(ge=0)


class LatencyStats(_Frozen):
    """Correction latency in turns: first exposure of the wrong memory -> the correction.

    ``measured`` counts corrections and rejections with a turn and a prior exposure; ``unmatched``
    those with no exposure at or before their turn; ``unturned`` those with no turn. Statistics are
    ``None`` when nothing was measured. ``per_event`` is sorted by event id.
    """

    total: int
    measured: int
    unmatched: int
    unturned: int
    mean: float | None = None
    median: float | None = None
    p90: float | None = None
    minimum: int | None = None
    maximum: int | None = None
    per_event: tuple[tuple[str, int], ...] = ()


def _nearest_rank(sorted_values: Sequence[int], q: float) -> float:
    rank = max(1, math.ceil(q * len(sorted_values)))
    return float(sorted_values[rank - 1])


def correction_latency(
    events: Iterable[FeedbackEvent], history: Iterable[ExposureRecord]
) -> LatencyStats:
    """Turns between a wrong memory first being surfaced and the explicit correction of it.

    Only corrections and rejections count (confirmations and ratings are not corrections). For an
    event with several targets the latency is measured from the earliest exposure of any target at
    or before the event's turn.
    """
    first_by_record: dict[str, list[int]] = {}
    for item in history:
        first_by_record.setdefault(item.record_id, []).append(item.turn)
    considered = sorted(
        (e for e in events if e.kind in ("correction", "rejection")), key=lambda e: e.event_id
    )
    per_event: list[tuple[str, int]] = []
    unmatched = unturned = 0
    for event in considered:
        if event.turn is None:
            unturned += 1
            continue
        exposures = [
            t for rid in event.target_ids for t in first_by_record.get(rid, ()) if t <= event.turn
        ]
        if not exposures:
            unmatched += 1
            continue
        per_event.append((event.event_id, event.turn - min(exposures)))
    values = sorted(v for _, v in per_event)
    if not values:
        return LatencyStats(
            total=len(considered), measured=0, unmatched=unmatched, unturned=unturned
        )
    return LatencyStats(
        total=len(considered),
        measured=len(values),
        unmatched=unmatched,
        unturned=unturned,
        mean=statistics.fmean(values),
        median=float(statistics.median(values)),
        p90=_nearest_rank(values, 0.9),
        minimum=values[0],
        maximum=values[-1],
        per_event=tuple(per_event),
    )


# --------------------------------------------------------------------------- observer views

ExclusionReason = Literal["no_grant", "scope_not_granted", "horizon_exceeded", "zero_trust"]


class ObservableRecord(_Frozen):
    """What a view needs from a record: its id, owning agent, scope, trust and text."""

    record_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)  # the agent whose memory it is
    scope: str = Field(min_length=1)
    trust: float = Field(ge=0.0, le=1.0)
    text: str


class ViewItem(_Frozen):
    """One record as an observer sees it. Read-only projection: frozen, never written back.

    An excluded item has ``included=False``, ``trust=0.0``, no ``text`` and a ``reason``.
    """

    record_id: str
    owner: str
    observer: str
    included: bool
    trust: float
    text: str | None = None
    reason: ExclusionReason | None = None
    via: tuple[str, ...] = ()  # principals along the grant chain, owner first
    hops: int = 0
    read_only: Literal[True] = True


def _chains(grants: Sequence[Grant], source: str, target: str) -> list[tuple[Grant, ...]]:
    """All simple connected grant paths ``source -> target`` (within one scope/purpose group)."""
    by_grantor: dict[str, list[Grant]] = {}
    for grant in grants:
        by_grantor.setdefault(grant.grantor, []).append(grant)
    found: list[tuple[Grant, ...]] = []

    def walk(at: str, path: tuple[Grant, ...], seen: frozenset[str]) -> None:
        for grant in by_grantor.get(at, ()):
            if grant.grantee in seen:
                continue
            nxt = (*path, grant)
            if grant.grantee == target:
                found.append(nxt)
            else:
                walk(grant.grantee, nxt, seen | {grant.grantee})

    walk(source, (), frozenset({source}))
    return found


def observer_view(
    agent: str,
    records: Iterable[ObservableRecord],
    grants: Iterable[Grant],
    *,
    purpose: str | None = None,
) -> list[ViewItem]:
    """Read-only view of ``records`` as ``agent`` may see them, sorted by record id.

    * the agent's own records: included at their own trust;
    * another agent's record: included only through a visible grant chain (LIB-21) on the record's
      scope (and ``purpose`` when given), at trust ``trust * prod(kappa)`` of the best chain;
    * otherwise excluded with a reason: ``no_grant`` (the owner granted nobody on a path to this
      agent), ``scope_not_granted`` (the owner grants other scopes only), ``horizon_exceeded`` (a
      chain connects but over-claims its horizon) or ``zero_trust``.
    """
    pool = sorted(
        set(grants), key=lambda g: (g.scope, g.purpose, g.grantor, g.grantee, g.horizon, g.kappa)
    )
    if purpose is not None:
        pool = [g for g in pool if g.purpose == purpose]
    items: list[ViewItem] = []
    for rec in sorted(records, key=lambda r: r.record_id):
        if rec.owner == agent:
            items.append(
                ViewItem(
                    record_id=rec.record_id,
                    owner=rec.owner,
                    observer=agent,
                    included=rec.trust > 0.0,
                    trust=rec.trust,
                    text=rec.text if rec.trust > 0.0 else None,
                    reason=None if rec.trust > 0.0 else "zero_trust",
                    via=(agent,),
                )
            )
            continue
        items.append(_foreign_item(agent, rec, pool))
    return items


def _foreign_item(agent: str, rec: ObservableRecord, pool: Sequence[Grant]) -> ViewItem:
    trusted = TrustedRecord(owner=rec.owner, scope=rec.scope, trust=rec.trust)
    in_scope = [g for g in pool if g.scope == rec.scope]
    groups: dict[str, list[Grant]] = {}
    for grant in in_scope:
        groups.setdefault(grant.purpose, []).append(grant)
    best: tuple[float, int, tuple[Grant, ...]] | None = None
    connected = False
    for _, group in sorted(groups.items()):
        for chain in _chains(group, rec.owner, agent):
            connected = True
            if not chain_visible(chain):
                continue
            score = (chain_attenuation(chain), -len(chain), chain)
            if best is None or score[:2] > best[:2]:
                best = score
    if best is not None:
        chain = best[2]
        trust = effective_trust(trusted, chain, viewer=agent)
        if trust > 0.0:
            return ViewItem(
                record_id=rec.record_id,
                owner=rec.owner,
                observer=agent,
                included=True,
                trust=trust,
                text=rec.text,
                via=(rec.owner, *(g.grantee for g in chain)),
                hops=len(chain),
            )
        return _excluded(agent, rec, "zero_trust")
    if connected:
        return _excluded(agent, rec, "horizon_exceeded")
    if any(g.grantor == rec.owner for g in pool) and not any(
        g.grantor == rec.owner for g in in_scope
    ):
        return _excluded(agent, rec, "scope_not_granted")
    return _excluded(agent, rec, "no_grant")


def _excluded(agent: str, rec: ObservableRecord, reason: ExclusionReason) -> ViewItem:
    return ViewItem(
        record_id=rec.record_id,
        owner=rec.owner,
        observer=agent,
        included=False,
        trust=0.0,
        reason=reason,
    )
