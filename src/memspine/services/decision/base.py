"""Decision capability port (H24), ``services/decision``.

A decision provider picks one of several *described* options for a text, with a confidence,
in one encoder forward pass and without generating tokens. memspine uses it where a rule is too
blunt but an LLM call is too expensive on the read path: routing ``read(mode="auto")``,
triage and segmentation. Providers are optional extras (``gliner2`` via ``[ner]``); the engine
never requires one, and every consumer falls back to its rules when the provider fails.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

__all__ = ["DecisionProvider"]


@runtime_checkable
class DecisionProvider(Protocol):
    """``choose`` returns ``(label, confidence)`` for the option that best fits ``text``.

    ``confidence`` is ``None`` when the provider cannot say how sure it is; consumers
    with a confidence gate treat that as below any positive gate.
    """

    provider_id: str

    async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float | None]: ...
