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
    #: O4-02 (O-07..O-09, M-14; LIB-40): the real call 2 at session close. ``True``: when a session
    #: closes, ONE structured call (role ``session_close``, else ``summarize``, else ``extract``)
    #: reads the session's turns and returns a summary, episode narratives and proposed profile
    #: ops; code validates every item (citations, reserved fields, derived time), stitches episodes
    #: onto adjacent stubs (deterministic) and writes the survivors through the derived-write door
    #: (parents = the cited turns). ``False`` (default): the stub (a marker only).
    session_close_v2: bool = False
    #: Prompt budget (tokens, 4 chars each) of the call-2 plan; turns beyond it are deferred.
    session_close_budget: int = Field(default=6000, ge=256)
    #: O4-07 (O-18..O-20, M-20; LIB-39): the profile card (L4/L5), opt-in. ``True``: the profile
    #: ops call 2 proposes go through the guarded ``apply_ops`` (shape, evidence, stance, trust,
    #: staleness, kappa_P bound, EMA drift) and the card is kept in the event log (marker
    #: ``profile_card_v2``); the sleep stage ``reflect_profile`` then invalidates entries whose
    #: support was forgotten and reports the reflection plan. Needs ``session_close_v2`` to
    #: receive ops. ``False`` (default): no card, call-2 profile ops are only counted.
    profile_card_v2: bool = False
    #: kappa_P: the L5 profile bound (maximum card entries) and the card's character budget.
    profile_kappa_p: int = Field(default=24, ge=1)
    profile_max_chars: int = Field(default=2400, ge=64)
    #: O-19: ``inferred`` ops are rejected unless this is on (stated-only card by default).
    profile_allow_inferred: bool = False
    #: EMA gain of the preference drift ``P_{t+1} = alpha p_t + (1 - alpha) P_t``.
    profile_alpha: float = Field(default=0.3, gt=0.0, le=1.0)
