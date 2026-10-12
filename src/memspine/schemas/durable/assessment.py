# ruff: noqa: E501
"""Assessment (schema session 5): five separate fields, the supersede rule, tentative handling."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from memspine.schemas.base import DurablePart
from memspine.schemas.durable.temporal import TimePrecision

__all__ = [
    "MODALITIES",
    "Assessment",
    "Binding",
    "Modality",
    "Support",
    "can_supersede",
    "integration_outcome",
    "is_tentative",
]

Modality = Literal["asserted", "negated", "hypothetical", "planned", "reported", "question"]
Binding = Literal["bound", "partly", "unbound"]
MODALITIES: tuple[str, ...] = (
    "asserted",
    "negated",
    "hypothetical",
    "planned",
    "reported",
    "question",
)


class Support(DurablePart):
    """Independent source families, plus the ``restated`` count (S4-3).

    ``restated`` is a stability signal used for recency and conflict tie-breaks; it is never corroboration.
    """

    independent: int = Field(default=0, ge=0)
    restated: int = Field(default=0, ge=0)
    families: list[str] = Field(
        default_factory=list
    )  # optional detail; if given it must agree with ``independent``

    @model_validator(mode="after")
    def _agree(self) -> Support:
        if self.families and len(set(self.families)) != self.independent:
            raise ValueError("'independent' must equal the number of distinct families listed")
        return self


class Assessment(DurablePart):
    """What the system knows about how far an assertion can be believed (ER-14), as separate fields.

    ``model_confidence_hint`` is the model's self-confidence, a hint only (S2-1): it feeds nothing in
    supersede, graph or profile logic.
    """

    source_trust: float = Field(ge=0.0, le=1.0)
    support: Support = Field(default_factory=Support)
    binding: Binding
    time_precision: TimePrecision = "unknown"
    modality: Modality
    # The planned / hypothetical / reported / question content is negated ("won't move" = planned +
    # negated). An asserted negation stays modality "negated", so the integration rules are unchanged.
    negated: bool = False
    model_confidence_hint: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _negation(self) -> Assessment:
        if self.negated and self.modality in ("asserted", "negated"):
            raise ValueError(
                f"negated=True is invalid with modality {self.modality!r} "
                "(an asserted negation is modality 'negated')"
            )
        return self


def _assessment(obj: object) -> Assessment:
    if isinstance(obj, Assessment):
        return obj
    inner = getattr(obj, "assessment", None)
    if isinstance(inner, Assessment):
        return inner
    raise TypeError(
        f"expected an Assessment or an object with .assessment, got {type(obj).__name__}"
    )


def can_supersede(new: object, current: object) -> bool:
    """S5-2: ``new`` may supersede ``current`` only if all three hold.

    * ``new.modality == "asserted"``;
    * ``new.binding == "bound"``;
    * ``new.source_trust >= current.source_trust``.

    Accepts an :class:`Assessment` or anything with an ``assessment`` attribute (the envelope).
    """
    n, c = _assessment(new), _assessment(current)
    return n.modality == "asserted" and n.binding == "bound" and n.source_trust >= c.source_trust


def is_tentative(assessment: object) -> bool:
    """S5-2/S5-3: anything not (asserted and bound) is tentative.

    Hypothetical, planned, reported, questioned, negated and unbound content coexists as tentative and
    yields graph edges tagged ``tentative``. Only asserted and bound assertions are normal.
    """
    a = _assessment(assessment)
    return not (a.modality == "asserted" and a.binding == "bound")


def integration_outcome(
    new: object, current: object
) -> Literal["supersede", "contest", "coexist_tentative"]:
    """What a newer assertion does to the current value of a one-valued attribute (S5-2).

    Tentative newer content coexists as tentative; a normal (asserted, bound) one supersedes when
    :func:`can_supersede` holds and otherwise becomes ``contest`` (both kept, conflict shown).
    """
    if is_tentative(new):
        return "coexist_tentative"
    return "supersede" if can_supersede(new, current) else "contest"
