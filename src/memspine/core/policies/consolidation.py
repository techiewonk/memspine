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
    #: C6': after consolidating a session, mine ATOMIC facts from it once (needs
    #: an ``extract`` LLM role). Facts are dated with the session start, carry
    #: the session records as parents, and go through the engine's write door.
    mine_facts: bool = False
    #: H15: mine each topic segment of a session in its own call (lexical-cohesion
    #: boundaries, no model; ``sessions.topic_segments``), so the miner reads one
    #: topic at a time. A mined fact's parents are its segment's turns: the call
    #: saw nothing else, so the parent set still covers its whole context (A2).
    #: Costs one call per segment instead of one per session.
    mine_by_topic: bool = False
    #: #27: the ``extract`` prompt condition the miner selects: ``session``
    #: (``extract@session``, unchanged) or ``session3`` (``extract@session3``: a
    #: worked example, a complete-coverage rule and a larger output cap, sent as
    #: the call's ``max_tokens``).
    mine_prompt: str = "session"
    #: #29: number the transcript lines (``[n] [YYYY-MM-DD] ...``) so the miner can
    #: cite the lines a fact comes from (``turns``); a fact that cites valid lines
    #: gets those turns as parents instead of the whole session (segment).
    mine_evidence_turns: bool = False
    #: #29: give each mined fact a happened (event) date, tagged
    #: ``happened:<date>``: the H1 resolution of a relative phrase in the fact or in
    #: its cited turns (each against its own date), else the miner's ``date``. A
    #: deterministic resolution also becomes the fact's event time (``valid_from``).
    mine_event_dates: bool = False
    #: #29: with ``mine_event_dates``, one batched ``extract@dates`` call per mined
    #: batch fills the facts that still have no date (needs the ``extract`` role).
    mine_event_dates_llm: bool = False
    #: #28: store each mined fact's multi-view fields as tags (``person:<name>``,
    #: ``loc:<place>``, ``topic:<class>``, normalised) for read-leg prefilters. With
    #: the default ``mine_prompt`` the miner switches to ``extract@session4``, which
    #: asks for them; the statement itself is stored as the miner wrote it.
    mine_multiview: bool = False
    #: #30: after mining, derive one person-level list card per (person, class) of
    #: event facts ("Melanie - activities: pottery (2023-05), camping (2023-07)"):
    #: parents = the facts (erasure cascades), trust = their minimum, re-derived only
    #: when the membership or text changes. An ``extract@classes`` call per person
    #: classes the facts with no topic and a generic attribute.
    list_cards: bool = False
    #: H8: after consolidating a session, ask the ``anticipate`` role (falls back to
    #: ``extract``) for likely future questions and store them as firewall-governed
    #: retrieval cues on the turns that answer them (``Engine.add_cues``), once.
    anticipate: bool = False
    #: H14: after consolidating a session, derive profile insights (preferences,
    #: habits, goals) with the ``reflect`` role and store them as reflective memory
    #: through ``Engine.reflect`` (trust capped at the evidence, depth capped), once.
    reflect_profile: bool = False


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

    @property
    def triggers(self) -> list[ConsolidationTrigger]:
        return list(self._options().triggers)

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
