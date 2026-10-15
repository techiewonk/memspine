"""Procedural memory mechanism (O-24, package O4-10): procedures from repeated how-to assertions.

Pure and deterministic: no I/O, no clock, no model calls. Opt-in and unwired (wiring is O4-10).

Decisions encoded:

* A :class:`Procedure` is *advisory*: it is ranked and suggested, never executed (ADR-060). ``stage`` is
  always ``"ADVISORY"``.
* A procedure exists only once at least ``min_repeats`` distinct how-to assertions agree on the name; the
  step list is the most common normalised sequence (ties go to the earliest, then lexicographic, so the
  result never depends on input order).
* Steps and preconditions are fixed slots read from ``extras`` (``procedure``, ``steps``,
  ``preconditions``, ``tags``); free text outside the slots is ignored.
* A change of the consensus steps opens a new *version*; the counts of the old version move to history and
  the new version starts at zero (an outcome belongs to the steps that were followed).
* O4-11 / O-25: only explicit feedback updates counts. Anything else raises
  :class:`ImplicitFeedbackError`. Applying the same outcome id twice is a no-op.
* Trust is bounded by the minimum trust of the source assertions (G1); lineage is the source id set.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import ConfigDict, Field

from memspine.schemas.base import InternalModel
from memspine.schemas.durable.assertion import AssertionEnvelope, inherit_trust

__all__ = [
    "HOWTO_KINDS",
    "HowTo",
    "ImplicitFeedbackError",
    "Outcome",
    "Procedure",
    "ProcedureVersion",
    "Suggestion",
    "build_procedures",
    "howto_of",
    "suggest",
    "update",
]

HOWTO_KINDS: frozenset[str] = frozenset({"how_to", "howto", "procedure"})
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
#: Cap on remembered outcome ids (idempotency window); oldest dropped first.
_OUTCOME_WINDOW = 256


class ImplicitFeedbackError(ValueError):
    """Raised when a procedure update is not backed by explicit feedback (O-25, O4-11)."""


def _norm(text: str) -> str:
    return " ".join(m.group(0).casefold() for m in _WORD.finditer(text))


def _digest(prefix: str, parts: list[object]) -> str:
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{prefix}_{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]}"


class HowTo(InternalModel):
    """One how-to statement: a named procedure with its ordered steps, from one source assertion."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    scope_key: str
    name: str
    steps: tuple[str, ...]
    preconditions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    trust: float = Field(ge=0.0, le=1.0)
    stamp: str = ""  # sortable source-time string (ISO); only used for tie-breaks


class ProcedureVersion(InternalModel):
    """A closed (or current) version: the steps and how they performed while current."""

    model_config = ConfigDict(frozen=True)

    version: int = Field(ge=1)
    steps: tuple[str, ...]
    preconditions: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    success: int = Field(default=0, ge=0)
    failure: int = Field(default=0, ge=0)


class Procedure(InternalModel):
    """An advisory procedure with lineage, bounded trust, counts for the current version, and history."""

    model_config = ConfigDict(frozen=True)

    procedure_id: str
    scope_key: str
    name: str
    steps: tuple[str, ...]
    preconditions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    success: int = Field(default=0, ge=0)
    failure: int = Field(default=0, ge=0)
    version: int = Field(default=1, ge=1)
    history: tuple[ProcedureVersion, ...] = ()  # closed versions, oldest first
    source_ids: tuple[str, ...]  # lineage: assertions behind the current version
    trust: float = Field(ge=0.0, le=1.0)
    stage: Literal["ADVISORY"] = "ADVISORY"
    applied_outcomes: tuple[str, ...] = ()

    @property
    def reliability(self) -> float:
        """Laplace-smoothed success rate of the current version; 0.5 with no outcomes."""
        return (self.success + 1) / (self.success + self.failure + 2)


class Outcome(InternalModel):
    """Feedback about one use of a procedure. Only ``explicit=True`` feedback may update counts."""

    model_config = ConfigDict(frozen=True)

    outcome_id: str
    procedure_id: str
    result: Literal["success", "failure"]
    explicit: bool = False  # set by the caller's like/dislike/note feedback path only
    version: int | None = None  # the version followed, when known


class Suggestion(InternalModel):
    """An advisory ranking entry; ``advisory`` is always True."""

    model_config = ConfigDict(frozen=True)

    procedure_id: str
    name: str
    score: float
    matched_tags: tuple[str, ...]
    unmet_preconditions: tuple[str, ...]
    reliability: float
    steps: tuple[str, ...]
    reason: str
    advisory: Literal[True] = True


# -------------------------------------------------------------------------------------------- build


def _strs(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(s.strip() for s in value if isinstance(s, str) and s.strip())


def howto_of(a: AssertionEnvelope, kinds: frozenset[str] = HOWTO_KINDS) -> HowTo | None:
    """Read a how-to from an assertion's fixed slots; ``None`` if it is not a usable how-to.

    Needs ``kind`` in ``kinds`` and a non-empty ``extras["steps"]`` list; the name is
    ``extras["procedure"]`` or the assertion ``attribute``. Negated, hidden and superseded assertions and
    non-asserted modalities (hypothetical, question, ...) are not how-to evidence.
    """
    if a.kind.casefold() not in kinds or a.status in ("hidden", "superseded"):
        return None
    if a.assessment.modality != "asserted":
        return None
    raw_name = a.extras.get("procedure") or a.attribute
    steps = _strs(a.extras.get("steps"))
    if not isinstance(raw_name, str) or not _norm(raw_name) or not steps:
        return None
    stamp = a.time.source_time or a.time.recorded_at
    return HowTo(
        source_id=a.record_id or a.occurrence_id,
        scope_key=f"{a.scope.tenant}/{a.scope.owner}",
        name=raw_name.strip(),
        steps=steps,
        preconditions=_strs(a.extras.get("preconditions")),
        tags=_strs(a.extras.get("tags")),
        trust=a.assessment.source_trust,
        stamp=stamp.isoformat() if stamp is not None else "",
    )


def _step_key(steps: Sequence[str]) -> tuple[str, ...]:
    return tuple(_norm(s) for s in steps)


def _consensus(group: list[HowTo]) -> tuple[list[HowTo], tuple[str, ...]]:
    """Members that agree on the winning step sequence, and that sequence (original wording)."""
    by_key: dict[tuple[str, ...], list[HowTo]] = {}
    for h in group:
        by_key.setdefault(_step_key(h.steps), []).append(h)
    best = min(
        by_key,
        key=lambda k: (-len(by_key[k]), min(m.stamp for m in by_key[k]), k),
    )
    members = sorted(by_key[best], key=lambda m: (m.stamp, m.source_id))
    return members, members[0].steps


def _procedure_id(scope_key: str, name: str) -> str:
    return _digest("proc", [scope_key, _norm(name)])


def build_procedures(
    howtos: Iterable[HowTo | AssertionEnvelope],
    *,
    existing: Iterable[Procedure] = (),
    min_repeats: int = 2,
) -> list[Procedure]:
    """Build or refresh procedures from repeated how-to statements.

    A name qualifies at ``min_repeats`` distinct sources agreeing on the same steps. If a procedure for
    that (scope, name) already exists: unchanged steps keep their counts (sources are refreshed); changed
    steps open a new version with zero counts, the old version moving to ``history``. Existing procedures
    with no new evidence are returned unchanged. Output is sorted by id.
    """
    if min_repeats < 1:
        raise ValueError("min_repeats must be >= 1")
    items: list[HowTo] = []
    for h in howtos:
        if isinstance(h, AssertionEnvelope):
            h = howto_of(h)
        if h is not None:
            items.append(h)
    groups: dict[str, list[HowTo]] = {}
    for h in items:
        groups.setdefault(_procedure_id(h.scope_key, h.name), []).append(h)

    prior = {p.procedure_id: p for p in existing}
    built: dict[str, Procedure] = dict(prior)
    for pid, group in groups.items():
        # One vote per source: a source restating itself does not count as repetition.
        uniq = {h.source_id: h for h in sorted(group, key=lambda m: (m.stamp, m.source_id))}
        members, steps = _consensus(list(uniq.values()))
        if len(members) < min_repeats:
            continue
        old = prior.get(pid)
        pre = _first_nonempty(m.preconditions for m in members)
        tags = tuple(sorted({t for m in members for t in m.tags}, key=lambda t: (_norm(t), t)))
        trust = inherit_trust([m.trust for m in members], max(m.trust for m in members))
        src = tuple(sorted({m.source_id for m in members}))
        common = {
            "procedure_id": pid,
            "scope_key": members[0].scope_key,
            "name": members[0].name,
            "preconditions": pre,
            "tags": tags,
            "source_ids": src,
            "trust": trust,
        }
        if old is None:
            built[pid] = Procedure(steps=steps, **common)
        elif _step_key(old.steps) == _step_key(steps):
            built[pid] = old.model_copy(update={**common, "steps": old.steps})
        else:
            closed = ProcedureVersion(
                version=old.version,
                steps=old.steps,
                preconditions=old.preconditions,
                source_ids=old.source_ids,
                success=old.success,
                failure=old.failure,
            )
            built[pid] = Procedure(
                steps=steps,
                version=old.version + 1,
                history=(*old.history, closed),
                applied_outcomes=old.applied_outcomes,
                **common,
            )
    return [built[k] for k in sorted(built)]


def _first_nonempty(options: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    """Most common non-empty precondition tuple; ties by tuple order (deterministic)."""
    counts = Counter(o for o in options if o)
    if not counts:
        return ()
    return min(counts, key=lambda o: (-counts[o], o))


# ------------------------------------------------------------------------------------------ update


def update(procedure: Procedure, outcome: Outcome) -> Procedure:
    """Apply one outcome, explicit feedback only (O4-11). Returns a new procedure; input is untouched.

    Raises :class:`ImplicitFeedbackError` unless ``outcome.explicit`` is true, and ``ValueError`` if the
    outcome targets another procedure or a version other than the current one. A repeated
    ``outcome_id`` is a no-op.
    """
    if not outcome.explicit:
        raise ImplicitFeedbackError(
            "procedure counts change only on explicit feedback (like, dislike, note)"
        )
    if outcome.procedure_id != procedure.procedure_id:
        raise ValueError("outcome targets a different procedure")
    if outcome.version is not None and outcome.version != procedure.version:
        raise ValueError(
            f"outcome is for version {outcome.version}, current version is {procedure.version}"
        )
    if outcome.outcome_id in procedure.applied_outcomes:
        return procedure
    applied = (*procedure.applied_outcomes, outcome.outcome_id)[-_OUTCOME_WINDOW:]
    if outcome.result == "success":
        return procedure.model_copy(
            update={"success": procedure.success + 1, "applied_outcomes": applied}
        )
    return procedure.model_copy(
        update={"failure": procedure.failure + 1, "applied_outcomes": applied}
    )


# ----------------------------------------------------------------------------------------- suggest


def suggest(
    procedures: Iterable[Procedure],
    context_tags: Iterable[str],
    *,
    limit: int | None = None,
) -> list[Suggestion]:
    """Advisory ranking of procedures for a context. Never executes anything.

    Score = tag overlap (Jaccard of procedure tags and context tags) x reliability x trust, with a
    precondition factor: each precondition not present among the context tags halves the score and is
    listed as unmet. Procedures sharing no tag with the context are not suggested. Ties break by name,
    then id.
    """
    ctx = {_norm(t) for t in context_tags if _norm(t)}
    out: list[Suggestion] = []
    for p in procedures:
        ptags = {_norm(t): t for t in p.tags if _norm(t)}
        matched = sorted(set(ptags) & ctx)
        if not matched:
            continue
        jaccard = len(matched) / len(set(ptags) | ctx)
        unmet = tuple(c for c in p.preconditions if _norm(c) not in ctx)
        score = jaccard * p.reliability * p.trust * (0.5 ** len(unmet))
        reason = f"matched tags {matched}; reliability {p.reliability:.2f} (v{p.version})"
        if unmet:
            reason += f"; unmet preconditions {list(unmet)}"
        out.append(
            Suggestion(
                procedure_id=p.procedure_id,
                name=p.name,
                score=round(score, 12),
                matched_tags=tuple(matched),
                unmet_preconditions=unmet,
                reliability=p.reliability,
                steps=p.steps,
                reason=reason,
            )
        )
    out.sort(key=lambda s: (-s.score, _norm(s.name), s.procedure_id))
    return out if limit is None else out[: max(limit, 0)]
