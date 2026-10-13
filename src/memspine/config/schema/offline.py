# ruff: noqa: E501
"""Config area: offline (O4 wiring of the K_C consolidate libraries; every flag default OFF)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OfflineConfig(BaseModel):
    """Opt-in switches for the offline (sleep-cycle) library wiring. All default ``False``: with every
    flag off the sleep cycle, the golden and the record ids are exactly as before."""

    model_config = ConfigDict(extra="forbid")

    #: O4-03 (LIB-37, O-10..O-13): the ``reconcile`` stage re-folds every (entity, attribute) group of
    #: live semantic facts event-time ordered (LIB-02 ``integrate``), so a reverse-order ingest ends
    #: with the same current value as a forward one: the older value is archived (``evolve_to`` the
    #: current one, validity closed), true ties are tagged ``disputed``. Runs in the sleep cycle after
    #: ``extract_graph``/``rule_edges``. Tap: ``job.reconcile.sweep``.
    reconcile_v2: bool = False
    #: O4-04 (LIB-20, O-13, O-14): the ``histories`` stage merges near-duplicate facts (shingle
    #: similarity plus the numbers/negation/modality guard; the dropped copy is archived with
    #: ``evolve_to`` the canonical) and rebuilds the disposable value histories. Runs right after
    #: ``reconcile``. Tap: ``job.histories``.
    histories_v2: bool = False
    #: O4-04: shingle similarity at or above which two facts of one key are near-duplicates.
    near_duplicate_text: float = Field(default=0.80, ge=0.0, le=1.0)
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
    #: O4-08: answer-set revisions without a read hit after which an answer is an eviction candidate (only
    #: applied once this process has logged reads; a cold start never evicts by idleness).
    standing_idle_after: int = Field(default=5, ge=1)
