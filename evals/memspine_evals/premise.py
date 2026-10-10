"""I61: premise-tolerant answering (opt-in ``--premise-tolerant``).

A question can carry one detail that differs from memory: a date that is a day or a year off,
the wrong person for an event that did happen, a slightly different item. A reader shown the
event tends either to confirm the wrong detail or to refuse. This wrapper adds a short clause
to the context so the reader answers the supported part and corrects the premise, while an
event or person memory does not hold at all is still reported as absent.

No extra model call, no gold, no category, no benchmark wording. It trades against
abstention (a mismatched-subject question wants a refusal), so it must be screened with the
cat-5 guard slice before it is adopted. Off (the default) builds no wrapper.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from .contracts import ReaderAnswer

__all__ = ["PREMISE_NOTE", "PremiseTolerantReader"]

#: Told to the reader (prepended to the context) when there is context to compare against.
PREMISE_NOTE = (
    "[Note: the question may state a detail (a date, a person, a place, an item, a number) that "
    "differs slightly from what the memories say. If the memories clearly record the event the "
    "question is about, answer from them and give the corrected detail in one clause (for "
    "example: it was X, not Y); do not simply confirm the detail and do not refuse because one "
    "detail differs. If no memory records such an event, or the person or thing asked about is "
    "not the one in the memories, say that the memories do not contain it.]"
)


class PremiseTolerantReader:
    """Wraps a reader: prepends :data:`PREMISE_NOTE` to every non-empty context. Same number
    of reader calls as the inner reader."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.guard = getattr(inner, "guard", None)
        self.reader_id = f"{inner.reader_id}+premise"
        self.model = inner.model
        self.makes_model_calls = getattr(inner, "makes_model_calls", True)
        #: Questions that got the clause.
        self.applied = 0

    def describe(self) -> Mapping[str, Any]:
        return {**self.inner.describe(), "premise_tolerant": True}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        applied = bool(context.strip())
        if applied:
            self.applied += 1
            context = f"{PREMISE_NOTE}\n{context}"
        result: ReaderAnswer = await self.inner.answer(question, context, question_date)
        if applied:
            result = dataclasses.replace(
                result, extra_meta={**(result.extra_meta or {}), "premise_tolerant": True}
            )
        return result
