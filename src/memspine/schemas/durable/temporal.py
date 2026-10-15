# ruff: noqa: E501
"""Time (schema session 6): three axes, precision with an approximate flag, basis, intervals, relations.

Rule (S6-2): never invent a date. A date-valued :class:`TemporalRef` always says how it was obtained
(``basis``); ``basis="none"`` means no date at all. The model proposes text plus a parsed candidate; the
system computes anchor, basis and precision (S2-1).
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from memspine.schemas.base import DurablePart

__all__ = [
    "DURATION_UNITS",
    "TIME_BASES",
    "TIME_PRECISIONS",
    "Duration",
    "Interval",
    "TemporalRef",
    "TemporalRelation",
    "TimeAxes",
    "TimeBasis",
    "TimePrecision",
]

TimePrecision = Literal[
    "instant", "minute", "hour", "day", "week", "month", "season", "year", "decade", "unknown"
]
TimeBasis = Literal["explicit", "source_time", "relation", "inferred", "none"]
DurationUnit = Literal["second", "minute", "hour", "day", "week", "month", "year"]
RelationKind = Literal["before", "after", "during"]

TIME_PRECISIONS: tuple[str, ...] = (
    "instant",
    "minute",
    "hour",
    "day",
    "week",
    "month",
    "season",
    "year",
    "decade",
    "unknown",
)
TIME_BASES: tuple[str, ...] = ("explicit", "source_time", "relation", "inferred", "none")
DURATION_UNITS: tuple[str, ...] = ("second", "minute", "hour", "day", "week", "month", "year")


class Duration(DurablePart):
    """A length of time: value plus unit, with an anchor when one is known (S6-4)."""

    value: float = Field(gt=0)
    unit: DurationUnit
    anchor: AwareDatetime | None = None


class TemporalRelation(DurablePart):
    """A phrase like "after X": a typed relation to another assertion, resolved only if an anchor exists."""

    relation: RelationKind
    target_assertion_id: str = Field(min_length=1)


class TemporalRef(DurablePart):
    """One temporal value. ``at`` is the resolved instant (start of the period at ``precision``)."""

    text: str | None = None  # the phrase as written, when there was one
    at: AwareDatetime | None = None
    precision: TimePrecision = "unknown"
    approximate: bool = False
    basis: TimeBasis = "none"
    anchor: AwareDatetime | None = None  # the source time a relative phrase was resolved against
    relation: TemporalRelation | None = None
    duration: Duration | None = None

    @model_validator(mode="after")
    def _consistent(self) -> TemporalRef:
        if self.at is None:
            if self.precision != "unknown":
                raise ValueError("precision must be 'unknown' when no date is resolved")
            if self.approximate:
                raise ValueError("approximate flag needs a resolved date")
        elif self.precision == "unknown":
            raise ValueError(
                "a resolved date needs a known precision (no exact date of unknown precision)"
            )
        if self.basis == "none":
            if self.at is not None or self.relation is not None or self.anchor is not None:
                raise ValueError("basis 'none' cannot carry a date, an anchor or a relation")
        elif self.basis in ("explicit", "inferred"):
            if self.at is None:
                raise ValueError(f"basis {self.basis!r} requires a resolved date")
        elif self.basis == "source_time":
            if self.at is None or self.anchor is None:
                raise ValueError(
                    "basis 'source_time' requires a resolved date and the anchor it came from"
                )
        elif self.basis == "relation":
            if self.relation is None:
                raise ValueError("basis 'relation' requires a relation")
            if self.at is not None and self.anchor is None:
                raise ValueError("a relation is resolved to a date only with an anchor")
        if self.relation is not None and self.basis != "relation":
            raise ValueError("a relation is only allowed with basis 'relation'")
        return self


class Interval(DurablePart):
    """When something was true or happened. Either end may be open or unresolved; not both absent."""

    start: TemporalRef | None = None
    end: TemporalRef | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Interval:
        if self.start is None and self.end is None:
            raise ValueError("an interval needs a start or an end (use None on the axis instead)")
        if (
            self.start is not None
            and self.end is not None
            and self.start.at is not None
            and self.end.at is not None
            and self.end.at < self.start.at
        ):
            raise ValueError("interval end is before its start")
        return self

    @property
    def precision(self) -> str:
        """Precision of the start, else of the end, else ``unknown``."""
        for ref in (self.start, self.end):
            if ref is not None and ref.at is not None:
                return ref.precision
        return "unknown"


class TimeAxes(DurablePart):
    """The three times every assertion carries (S6-3).

    * ``event``: when it was true or happened (as-of "what was true on X").
    * ``source_time``: when it was said.
    * ``recorded_at``: when it was stored (``None`` only before the write door stamps it).

    "What did we know on X" uses source and recorded time.
    """

    event: Interval | None = None
    source_time: AwareDatetime | None = None
    recorded_at: AwareDatetime | None = None

    @property
    def event_precision(self) -> str:
        return self.event.precision if self.event is not None else "unknown"
