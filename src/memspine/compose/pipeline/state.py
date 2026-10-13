"""Compose: the state part of context assembly, moved out of the engine (F-11)."""

from __future__ import annotations

from typing import TypedDict


class AnswerVerification(TypedDict):
    """#39: :meth:`Engine.verify_answer`'s verdict on one answer."""

    supported: bool
    #: Record ids of the supporting context lines (``L<n>`` line numbers for a text context).
    evidence_ids: list[str]
    #: A corrected answer the context supports, when the given one is not supported.
    revised_answer: str | None
