# ruff: noqa: E501
"""Config area: offline (K_C consolidate / K_P personalise wiring, OW-C). Every key is off by default."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OfflineConfig(BaseModel):
    """Opt-in sleep-cycle stages built on the offline libraries. With every flag off, the sleep cycle,
    its order and the stored records are exactly what they were before."""

    model_config = ConfigDict(extra="forbid")

    #: O4-05 (LIB-31, opt-in L4): the ``pattern_observations`` sleep stage mines recurrence, frequency,
    #: trend, co-occurrence and preference-stability observations from the stored facts and writes each as a
    #: derived semantic record through the derived-write door (parents = the supporting facts, trust capped at
    #: the least-trusted parent). A changed supporting fact retires the observations it supports. No model call.
    patterns_v2: bool = False
    #: O4-05: which observation kinds the stage mines (a subset of recurrence, frequency, trend,
    #: co_occurrence, preference_stability). Empty = all five.
    patterns_kinds: list[str] = Field(default_factory=list)
    #: O4-05: at most this many NEW observation records per namespace per sleep (the rest wait a cycle).
    patterns_max_new: int = Field(default=50, ge=1)
    #: O4-08 (LIB-32, opt-in L5): the ``standing_answers`` sleep stage builds a precomputed answer for the
    #: current value of each keyed state fact (from value histories), rebuilds only the answers whose
    #: group changed, and evicts the idle or surplus ones. Written as derived semantic records. No model
    #: call. The read side is ``read.standing_v2``.
    standing_v2: bool = False
    #: O4-08: at most this many standing answers kept per namespace.
    standing_max_answers: int = Field(default=50, ge=1)
    #: O4-08: sleep cycles without a read hit after which an answer is an eviction candidate (only applied
    #: once this process has logged reads; a cold start never evicts by idleness).
    standing_idle_after: int = Field(default=5, ge=1)
