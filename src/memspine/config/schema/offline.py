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
