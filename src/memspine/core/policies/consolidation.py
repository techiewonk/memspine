"""Consolidation policy (M2): triggers + session-summary decisions.

Pure decision logic for the consolidate pipeline (workers/pipelines.py):
which trigger fires, which detected sessions deserve a summary, and how the
deterministic extractive fallback summarizes when no LLM role is bound —
deterministic-first (N6): same episodic log ⇒ same summaries, with the LLM
only ever *improving* wording, never deciding structure.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import ClassVar

from pydantic import Field

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["ConsolidationPolicy", "ConsolidationTrigger", "extractive_summary"]


class ConsolidationTrigger(StrEnum):
    SESSION_END = "session_end"
    HEAT = "heat"
    SLEEP_CYCLE = "sleep_cycle"


class ConsolidationOptions(PolicyOptions):
    triggers: list[ConsolidationTrigger] = Field(
        default_factory=lambda: [
            ConsolidationTrigger.SESSION_END,
            ConsolidationTrigger.SLEEP_CYCLE,
        ]
    )
    heat_threshold: int = constants.CONSOLIDATION_HEAT_THRESHOLD
    session_gap_minutes: int = constants.SESSION_GAP_MINUTES
    min_session_records: int = constants.CONSOLIDATION_MIN_SESSION_RECORDS
    summary_max_chars: int = constants.CONSOLIDATION_SUMMARY_MAX_CHARS


def extractive_summary(contents: list[str], max_chars: int) -> str:
    """Deterministic fallback summarizer (N6): first sentence of each member,
    in temporal order, truncated to the budget. No LLM, no randomness."""
    sentences: list[str] = []
    for content in contents:
        text = content.strip()
        if not text:
            continue
        for terminator in (". ", "! ", "? ", "\n"):
            cut = text.find(terminator)
            if cut != -1:
                text = text[: cut + 1]
                break
        sentences.append(text.strip())
    summary = " ".join(sentences)
    return summary[:max_chars].rstrip()


class ConsolidationPolicy(BindablePolicy):
    name: ClassVar[str] = "consolidation"
    Options: ClassVar[type[PolicyOptions]] = ConsolidationOptions

    def _options(self) -> ConsolidationOptions:
        options = self.options
        assert isinstance(options, ConsolidationOptions)
        return options

    @property
    def session_gap(self) -> timedelta:
        return timedelta(minutes=self._options().session_gap_minutes)

    def should_trigger(self, trigger: ConsolidationTrigger, heat: int = 0) -> bool:
        options = self._options()
        if trigger not in options.triggers:
            return False
        if trigger is ConsolidationTrigger.HEAT:
            return heat >= options.heat_threshold
        return True

    def worth_summarizing(self, members: list[MemoryRecord]) -> bool:
        """Sessions below the member floor cost more as a summary than as raw
        records; summaries of summaries are never re-consolidated."""
        real = [record for record in members if record.source.channel != "consolidation"]
        return len(real) >= self._options().min_session_records

    def fallback_summary(self, members: list[MemoryRecord]) -> str:
        ordered = sorted(members, key=lambda record: record.valid_from)
        return extractive_summary(
            [record.content for record in ordered], self._options().summary_max_chars
        )
