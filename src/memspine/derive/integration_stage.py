# ruff: noqa: E501
"""W2-10 (``write.integration_v2``): the integration ladder as an envelope stage, order 400 (W-21, W-22, M-12).

Built on LIB-02 (``derive/integrate.py``), which stays pure. This module adds the three W2-10 parts:

* :class:`DefaultVerdict`: what an attribute without a declared cardinality does (W-22 / M-12). Undeclared
  means ``many`` (coexist); with no hint that a value *changed*, a conflicting live value is not decided
  here but deferred to K_C (``offline.reconcile_v2`` re-folds the group in event-time order).
* :class:`ConflictLadder`: cardinality, then trust tier, then time (event interval closed or open), then
  negation binding, seeded by the extraction hints (``change_hint``, ``extras["transition"]`` /
  ``["replaces"]``, kind). It names the rung and the hint on every verdict.
* :func:`integration_stage`: the :class:`~memspine.derive.envelope_stages.EnvelopeStage`. For each state
  envelope it finds the live assertions on the same (subject, attribute) key, decides the operator and
  annotates the envelope; the deposit applies it (:func:`deposit_tags`).

Envelope fields
---------------
Read (all optional): ``subject.entity_id`` (W2-09 resolved id; else the normalised surface name),
``attribute``, ``kind``, ``change_hint``, ``extras["transition"|"replaces"]``, ``assessment`` (trust,
modality, binding), ``time.event`` (W2-14: closed or open interval).
Written: ``verdict`` (:class:`IntegrationVerdict`: ``op`` append / supersede / merge / coexist / contest /
defer_kc, ``rung``, ``hint``) and ``extras["integrate_target"]`` (record id the verdict acts on, if any),
``extras["integrate_outcome"]`` (the library outcome, e.g. ``retire``).

Deposit (``workers/windows.py:_store_envelopes`` -> :func:`deposit_tags`): the verdict becomes record tags.
``integ:<op>``; ``integ:coexist`` / ``integ:contest`` make the conflict policy ADD instead of superseding
(rung ``W2-10_integration``); ``disputed`` marks a contest; ``retract`` makes a bound negation retire the
value (the existing R2' rung); supersede and defer_kc leave the old behaviour (a newer value supersedes
through the door, an older late arrival is stored as closed history, R4).

Taps ``write.integrate.decision`` and ``write.integrate.stage``. Counters in ``ctx.notes``:
``integrate_<op>``, ``integrate_defer_kc``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from memspine.config.constants import TRUST_DEFAULT
from memspine.derive.envelope_compiler import storage_kind
from memspine.derive.envelope_stages import EnvelopeStage, EnvelopeStageContext
from memspine.derive.integrate import (
    Cardinality,
    CardinalityRegistry,
    IntegrationDecision,
    integrate,
    normalise_name,
    record_id,
)
from memspine.derive.pipeline.stages.envelope_adapter import OWNER, is_fact, key_of, to_envelope
from memspine.observability import tap
from memspine.schemas.durable.assertion import AssertionEnvelope, IntegrationVerdict, Mention
from memspine.schemas.durable.common import OwnerScope
from memspine.store.records import MemoryRecord

__all__ = [
    "STAGE_NAME",
    "STAGE_ORDER",
    "ConflictLadder",
    "DefaultVerdict",
    "LadderVerdict",
    "deposit_attribute",
    "deposit_tags",
    "hints_of",
    "integration_stage",
]

#: tag that keeps the attribute of a coexisting value (the semantic store holds one active fact per key,
#: so a value that coexists is stored without the key, like an event)
ATTR_TAG = "attr:"
STAGE_NAME = "integration"
STAGE_ORDER = 400

_OPS = {
    "append": "append",
    "supersede": "supersede",
    "retire": "supersede",
    "merge": "merge",
    "coexist": "coexist",
    "contest": "contest",
    "tentative": "coexist",
}

#: library reason -> the ladder rung that decided (cardinality, trust tier, time, negation)
_RUNG = {
    "first_value": "cardinality",
    "cardinality_many": "cardinality",
    "undeclared_cardinality": "cardinality",
    "newer_trust_ok": "trust_tier",
    "lower_trust": "trust_tier",
    "undeclared_transition": "hint",
    "past_interval": "time",
    "late_arrival_older": "time",
    "negation_retires": "negation",
    "restated_same_family": "merge",
    "support_other_family": "merge",
    "earlier_occurrence": "merge",
    "modality": "tentative",
    "binding": "tentative",
}


@dataclass(frozen=True)
class DefaultVerdict:
    """The W-22 / M-12 fallback for an attribute without a declared cardinality.

    ``undeclared`` is the cardinality it gets (M-12: ``many``, so a second employer or pet no longer
    silently retires the first). ``defer_without_hints``: a conflicting live value on an undeclared key,
    with no hint that the value changed, is left to K_C (verdict ``defer_kc``) instead of being decided."""

    undeclared: Cardinality = "many"
    defer_without_hints: bool = True


@dataclass(frozen=True)
class LadderVerdict:
    op: str  # append | supersede | merge | coexist | contest | defer_kc
    rung: str
    hint: str | None = None
    target: str | None = None
    outcome: str = "append"  # the library outcome (retire / tentative keep their own name)
    reason: str = ""
    affected: tuple[str, ...] = ()

    def as_verdict(self) -> IntegrationVerdict:
        return IntegrationVerdict(op=self.op, rung=self.rung, hint=self.hint)  # type: ignore[arg-type]


def hints_of(env: AssertionEnvelope) -> list[str]:
    """The extraction hints that seed the ladder (W-21): a changed-value mark, a transition, a
    ``replaces`` alias, and a state kind. Empty = no hint."""
    out: list[str] = []
    if env.change_hint:
        out.append("change_hint")
    if env.extras.get("transition"):
        out.append("transition")
    if env.extras.get("replaces"):
        out.append("replaces")
    return out


class ConflictLadder:
    """Decide ⊕ for one envelope against the live assertions of its key (pure; wraps LIB-02)."""

    def __init__(self, registry: CardinalityRegistry, default: DefaultVerdict | None = None) -> None:
        self.registry = registry
        self.default = default or DefaultVerdict()

    def decide(self, incoming: AssertionEnvelope, current: Sequence[AssertionEnvelope]) -> LadderVerdict:
        hints = hints_of(incoming)
        hint = hints[0] if hints else None
        work = incoming
        if "change_hint" in hints and not incoming.extras.get("transition"):
            work = incoming.model_copy(update={"extras": {**incoming.extras, "transition": True}})
        decision = integrate(work, current, self.registry)
        verdict = self._verdict(decision, incoming, hint)
        peers = [e for e in current if record_id(e) != record_id(incoming) and e.status != "hidden"]
        if self._defer(decision, incoming, hints, peers):
            return LadderVerdict(
                "defer_kc",
                "no_hint",
                None,
                None,
                decision.outcome,
                "undeclared_cardinality_without_hint",
                decision.affected,
            )
        return verdict

    def _defer(
        self,
        decision: IntegrationDecision,
        incoming: AssertionEnvelope,
        hints: Sequence[str],
        peers: Sequence[AssertionEnvelope],
    ) -> bool:
        """An undeclared key with a live peer and nothing saying the value changed: K_C decides."""
        if not self.default.defer_without_hints or decision.declared or hints or not peers:
            return False
        a = incoming.assessment
        if a.modality == "negated" or a.binding != "bound" or a.modality != "asserted":
            return False  # negation and tentative content are decided by their own rungs
        if incoming.time.event is not None and incoming.time.event.end is not None:
            return False  # a closed interval is history, decided by the time rung
        return decision.outcome in ("coexist", "supersede", "contest")

    @staticmethod
    def _verdict(decision: IntegrationDecision, incoming: AssertionEnvelope, hint: str | None) -> LadderVerdict:
        op = _OPS[decision.outcome]
        rung = _RUNG.get(decision.reason, decision.reason)
        target = decision.merge_into or (decision.affected[0] if decision.affected else None)
        if decision.outcome == "supersede" and decision.reason == "newer_trust_ok" and hint is None:
            hint = "cardinality_one" if decision.declared else None
        return LadderVerdict(op, rung, hint, target, decision.outcome, decision.reason, decision.affected)


# --------------------------------------------------------------------------- the stage


ListLive = Callable[[str], Awaitable[Sequence[MemoryRecord]]]


def _subject_cf(env: AssertionEnvelope) -> str | None:
    s = env.subject
    if s is None:
        return None
    return (s.entity_id or s.surface).strip().casefold()


def _live_index(records: Sequence[MemoryRecord], namespace: str) -> dict[tuple[str, str], list[AssertionEnvelope]]:
    """(casefolded entity, normalised attribute) -> projected live facts, entity id set to the key."""
    idx: dict[tuple[str, str], list[AssertionEnvelope]] = defaultdict(list)
    for r in records:
        if not is_fact(r) or r.valid_to is not None:
            continue  # a closed interval is history and never competes
        if "integ_rung:time" in r.tags or "integ_rung:tentative" in r.tags:
            continue  # a coexisting past or tentative value never competes either
        if r.attribute is None:  # a coexisting value stored without a key keeps it in an ``attr:`` tag
            tagged = next((t[5:] for t in r.tags if t.startswith(ATTR_TAG)), None)
            if tagged is not None:
                r = r.model_copy(update={"attribute": tagged})
        key = key_of(r)
        if key is None:
            continue
        ent = key[0].strip().casefold()
        env = to_envelope(r, namespace)
        if env.subject is not None:
            env = env.model_copy(
                update={"subject": Mention(surface=env.subject.surface, mention_id=env.subject.mention_id, entity_id=ent)}
            )
        idx[(ent, normalise_name(key[1]))].append(env)
    return idx


def _trust_cap(env: AssertionEnvelope, ctx: EnvelopeStageContext) -> float:
    """The trust the deposit will store: the envelope's source trust, capped at the least-trusted cited
    turn (derived content is never more trusted than its parents) and at the default trust the mined-fact
    door gives a derived record, so it compares with the trust of the stored facts it is weighed against."""
    by_id = {getattr(r, "record_id", None): r for r in ctx.records}
    cited = [by_id[e.turn.source_id].trust for e in env.evidence if e.turn.source_id in by_id]
    return min([TRUST_DEFAULT, env.assessment.source_trust, *cited])


def _working_copy(env: AssertionEnvelope, ctx: EnvelopeStageContext, key: str) -> AssertionEnvelope:
    """Align the incoming envelope with the projected stored ones: same tenant and owner, entity key,
    and the trust the deposit will store."""
    assert env.subject is not None
    subject = Mention(surface=env.subject.surface, mention_id=env.subject.mention_id, entity_id=key)
    assessment = env.assessment.model_copy(update={"source_trust": _trust_cap(env, ctx)})
    return env.model_copy(
        update={
            "scope": OwnerScope(tenant=ctx.namespace, owner=OWNER),
            "subject": subject,
            "assessment": assessment,
        }
    )


def integration_stage(
    list_live: ListLive,
    *,
    cardinality: Mapping[str, str] | None = None,
    default: DefaultVerdict | None = None,
) -> EnvelopeStage:
    """The order-400 stage. ``list_live(namespace)`` returns the namespace's semantic records (the stage
    keeps the live facts); ``cardinality`` maps attribute -> ``one`` / ``many`` (operator-declared)."""
    declared = {normalise_name(k): v for k, v in (cardinality or {}).items() if v in ("one", "many")}
    dflt = default or DefaultVerdict()

    async def run(envs: list[AssertionEnvelope], ctx: EnvelopeStageContext) -> list[AssertionEnvelope]:
        if not envs:
            return envs
        ns = ctx.namespace
        registry = CardinalityRegistry()
        for attr, card in declared.items():
            registry.declare(ns, attr, card)  # type: ignore[arg-type]
        ladder = ConflictLadder(registry, dflt)
        index: dict[tuple[str, str], list[AssertionEnvelope]] | None = None
        seen: dict[tuple[str, str], list[AssertionEnvelope]] = defaultdict(list)
        out: list[AssertionEnvelope] = []
        for env in envs:
            key_s = _subject_cf(env)
            if env.attribute is None or key_s is None or storage_kind(env.kind) != "state":
                # events, unkeyed facts and subject-less items have no key to conflict on: they coexist
                out.append(_annotate(env, LadderVerdict("append", "no_key", None), ctx, keyed=False))
                continue
            if index is None:
                index = _live_index(await list_live(ns), ns)
            gkey = (key_s, normalise_name(env.attribute))
            surface = env.subject.surface.strip().casefold() if env.subject is not None else key_s
            stored = [*index.get(gkey, []), *(index.get((surface, gkey[1]), []) if surface != key_s else [])]
            work = _working_copy(env, ctx, key_s)
            current = [*stored, *seen[gkey]]
            verdict = ladder.decide(work, current)
            seen[gkey].append(work)
            out.append(_annotate(env, verdict, ctx, keyed=True))
        if tap.enabled():
            tap.event("write.integrate.stage", window=ctx.window_id, items=len(envs), kept=len(out))
        return out

    return EnvelopeStage(STAGE_NAME, STAGE_ORDER, run)


def _annotate(
    env: AssertionEnvelope, v: LadderVerdict, ctx: EnvelopeStageContext, *, keyed: bool
) -> AssertionEnvelope:
    extras = {**env.extras, "integrate_outcome": v.outcome}
    if v.target is not None:
        extras["integrate_target"] = v.target
    ctx.notes[f"integrate_{v.op}"] += 1
    tap.event(
        "write.integrate.decision",
        id=env.occurrence_id,
        op=v.op,
        rung=v.rung,
        hint=v.hint,
        target=v.target,
        keyed=keyed,
        window=ctx.window_id,
    )
    return env.model_copy(update={"verdict": v.as_verdict(), "extras": extras})


def deposit_tags(env: AssertionEnvelope) -> list[str]:
    """The record tags that carry the verdict through the mined-fact door (empty without a verdict)."""
    v = env.verdict
    if v is None:
        return []
    tags = [f"integ:{v.op}"]
    if v.rung:
        tags.append(f"integ_rung:{v.rung}")
    outcome = env.extras.get("integrate_outcome")
    if deposit_attribute(env) is None and env.attribute:
        tags.append(f"{ATTR_TAG}{env.attribute}")
    if v.op == "contest":
        tags.append("disputed")
    if outcome == "retire":
        tags.append("retract")
    if outcome == "tentative":
        tags.append("integ:coexist")
    return tags


def deposit_attribute(env: AssertionEnvelope) -> str | None:
    """The attribute the deposit stores: dropped for a value that coexists (or is tentative), which the
    store would otherwise hold back as a zero-length contender; kept for everything else."""
    v = env.verdict
    if v is not None and v.op == "coexist" and env.attribute:
        return None
    return env.attribute or None
