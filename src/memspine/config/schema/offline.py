# ruff: noqa: E501
"""Config area: offline (O4 wiring of the K_C consolidate libraries; every flag default OFF)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    #: O4-06 (LIB-36): entity communities and summaries on the derived-write door. ``True``: the
    #: ``reorganize`` stage clusters the entity graph (deterministic label propagation, hub exclusion,
    #: stable ids, incremental change detection) and plans summary jobs inside a token budget; the
    #: ``summarize_entities`` stage makes its model call outside the namespace lock and writes through
    #: ``derive/write.py``. ``False`` (default): today's record-level reorganizer and summariser.
    communities_v2: bool = False
    #: Token budget of the summary jobs one sleep may plan; jobs over it stay dirty (counted, not dropped).
    community_summary_budget_tokens: int = Field(default=20000, ge=1)
    #: Communities smaller than this get no summary.
    community_min_size: int = Field(default=3, ge=2)
    #: A larger community is flagged oversized and gets no summary (the label-propagation collapse guard).
    community_max_size: int = Field(default=200, ge=2)
    #: Canonical entity names kept out of clustering (the owner self-entity), besides the automatic hubs.
    community_hubs: list[str] = Field(default_factory=list)
    #: An entity linked to at least this share of the other entities is a hub (0 turns the rule off).
    community_hub_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    #: A record naming more entities than this adds no entity-pair edges (it would link everything).
    community_max_entities_per_record: int = Field(default=8, ge=2)

    #: O4-09 (LIB-33): activation-based decay tiers (hysteresis, protected kinds) and compression plans.
    #: ``True``: ``decay_sweep`` plans tier moves with ``derive/decay.py`` and ``compress`` also plans
    #: multi-resolution text jobs for cold groups. Nothing is deleted; tiers do not change ranking (O-22).
    #: ``False`` (default): today's absolute idle-day tiers and the zstd-only compress.
    decay_v2: bool = False
    #: Activation boundaries hot|warm, warm|cold, cold|dormant (strictly decreasing).
    decay_boundaries: tuple[float, float, float] = (0.50, 0.30, 0.15)
    #: Dead band above a boundary before a record moves back up (the hysteresis).
    decay_margin: float = Field(default=0.05, ge=0.0)
    #: Memory types never moved to a colder tier (a cold one is restored to hot).
    decay_protected_kinds: list[str] = Field(default_factory=list)
    #: Multi-resolution jobs one sleep may run (each is one summarize call). 0 = plan and report only.
    decay_multires_jobs: int = Field(default=0, ge=0)
    #: A cold group needs at least this many eligible members to be compressed.
    decay_group_min_size: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def _decay_bands(self) -> OfflineConfig:
        b = self.decay_boundaries
        if not (b[0] > b[1] > b[2] >= 0.0):
            raise ValueError("offline.decay_boundaries must be strictly decreasing and >= 0")
        if b[0] - b[1] <= self.decay_margin or b[1] - b[2] <= self.decay_margin:
            raise ValueError("offline.decay_margin must be smaller than the gap between boundaries")
        return self
