"""Config area: integrity (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memspine.exceptions import ConfigError


class IntegrityConfig(BaseModel):
    """Trust-horizon invariant (THI, formerly "MTI") for shared memory — opt-in, default OFF.

    Off, every path is byte-identical to the pre-MTI engine: shared reads keep
    the flat ``TRUST_RETRIEVED_CAP`` min-cap, ranking stays trust-blind, and
    ``derived_from`` parents are recorded but never lower trust.

    On:

    - ``write(..., derived_from=[ids])`` sets trust to
      ``min(base_trust, view_trust(parent) for each parent) * derivation_decay``
      (MTI-D). An unreadable parent counts as view trust 0.0 (fail closed).
    - A foreign record's view trust is ``trust ⊗ kappa`` for its grant edge, with
      ``⊗`` = ``attenuation`` (``product`` or ``min``). ``edge_kappa`` overrides
      per edge, keyed ``"grantor->grantee"``.
    - ``trust_weighted_ranking`` ranks by ``composite_score * view_trust``;
      ``admission_threshold`` drops candidates whose view trust is below it.
    - ``principal_bound_corroboration`` requires every corroborator to name a
      ``source.principal`` that differs from the held record's and from every
      earlier corroborator's.
    - ``merge_reinforcement_gate`` skips dedup-merge reinforcement when the
      incoming write is less trusted than the kept record.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    attenuation: str = "product"  # product | min
    kappa: float = Field(default=0.5, gt=0.0, le=1.0)
    edge_kappa: dict[str, float] = Field(default_factory=dict)
    derivation_decay: float = Field(default=1.0, gt=0.0, le=1.0)
    #: Baseline emulation ONLY (MAP-Graph-style verification bonus): multiplies
    #: derived trust and so can exceed the parents' minimum — it breaks the
    #: invariant on purpose, to measure what that costs. Never raise in production.
    verification_bonus: float = Field(default=1.0, ge=1.0, le=2.0)
    admission_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    trust_weighted_ranking: bool = True
    principal_bound_corroboration: bool = True
    #: W13 (plan v3.2): corroboration counts independent LINEAGE ROOTS, not records:
    #: a corroborating write whose ``source.parents`` lineage shares a root with the
    #: quarantined record, or with an earlier corroborator, does not count (a summary
    #: or a re-statement of the poison's own source is not a second opinion). Applies
    #: whether or not ``integrity.enabled``. Off: unchanged.
    corroboration_roots: bool = False
    merge_reinforcement_gate: bool = True
    #: B0: the engine records what each session READS and treats it as parents
    #: of the session's next writes, so omitting ``derived_from`` cannot launder
    #: (Paper A's A1/A2 become enforced, not assumed). ``turn``: the read ledger
    #: is consumed by each write; ``session``: kept until ``end_session``; ``off``.
    implicit_parents: str = "off"
    #: B6: assembled records whose view trust is below this are rendered inside
    #: an untrusted-data wrapper (data, not instructions). 0.0 = off.
    untrusted_wrap_below: float = Field(default=0.0, ge=0.0, le=1.0)
    #: B9 facts-only: a context record whose view trust is below this never enters
    #: a context window as raw text. It is replaced by the live atomic facts mined
    #: from it (H2), shown as unverified claims; with none, it is left out. Mined
    #: facts are themselves kept (they are claims already). Raw low-trust text is
    #: where instruction framing survives, so this trades recall for containment.
    #: 0.0 = off.
    claims_only_below: float = Field(default=0.0, ge=0.0, le=1.0)
    #: B4': re-check each candidate's trust against its CURRENT parents at read
    #: time. Ancestor quarantine or rollback propagates, and so does grant revocation:
    #: a parent behind a revoked grant counts 0 (N6, ADR-029 edge cases). Radii can
    #: only shrink. Costs extra storage reads per candidate.
    live_reevaluation: bool = False
    #: B7: per-principal reputation. A principal whose records were quarantined,
    #: or whose record was the SEED of a taint rollback, has its later writes'
    #: trust multiplied by min(1, 2 * Beta-mean(good, bad)) with a uniform prior:
    #: a new or clean principal keeps full trust, a bad history lowers it. It can
    #: only LOWER trust, so the per-hop gain stays below 1 and the horizon holds.
    principal_reputation: bool = False

    @field_validator("attenuation")
    @classmethod
    def _known_attenuation(cls, value: str) -> str:
        if value not in {"product", "min"}:
            raise ConfigError(f"integrity.attenuation must be 'product' or 'min', got {value!r}")
        return value

    @field_validator("implicit_parents")
    @classmethod
    def _known_implicit_mode(cls, value: str) -> str:
        if value not in {"off", "turn", "session"}:
            raise ConfigError(f"integrity.implicit_parents must be off|turn|session, got {value!r}")
        return value

    @field_validator("edge_kappa")
    @classmethod
    def _edge_kappa_in_range(cls, value: dict[str, float]) -> dict[str, float]:
        for edge, kappa in value.items():
            if "->" not in edge or not 0.0 < kappa <= 1.0:
                raise ConfigError(f"integrity.edge_kappa[{edge!r}] must be 'a->b' with 0<kappa<=1")
        return value
