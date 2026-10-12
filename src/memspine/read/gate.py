# ruff: noqa: E501
"""Store-calibrated relevance gate with explicit missing / conflicting evidence states (LIB-12).

Pure and unwired (wired later by R3-11, decisions R-21 and ER-23). Stdlib and pydantic only; no I/O,
no model call, deterministic.

Calibration. :class:`StoreCalibration` summarises, per *scorer* (for example a reranker's raw score or
the fused score), the top scores that off-topic probe queries reach in this store. Scores are never
assumed to be on a fixed scale: the level is the 95th percentile of the probe tops, the spread is a
robust sigma (``1.4826 * MAD``). Observations with no score (``None`` or non-finite) are counted but
never enter the statistics, and an unscored candidate is never treated as score 0.

Thresholds, per scorer (``p95``, ``scale`` from the calibration)::

    weak_threshold   = p95 + weak_margin_sd * scale             (default margin 0.0)
    strong_threshold = p95 + margin_sd * scale                  (default margin 1.0, as today's gate)
    personal_strict  = p95 + personal_strict_margin_sd * scale  (default margin 3.0)

States of :class:`GateDecision`:

* ``answerable``: at least one admitted item scores above its strong threshold.
* ``conflicting``: two or more strong items for the same ``key`` carry different values. Every item of a
  conflicting key is kept and labelled; nothing is resolved here.
* ``weak``: only items between the weak and strong thresholds, or evidence whose relevance cannot be
  judged (unscored, or the scorer is not calibrated yet; the gate fails open). Returned with
  ``low_confidence=True``.
* ``missing``: nothing relevant (including an empty candidate list). The caller abstains or says
  "not found". Empty retrieval is not proof of absence (ER-23), so the reason is recorded.
* ``bypass``: the gate was skipped for a named reason (explicit enumeration, as-of / time-view request,
  "show everything", named reference); all items pass through and the reason is recorded.

Generic vs personal. ``GateContract.personal`` is ``True`` (the question concerns the user or a named
person), ``False`` (a generic / world-knowledge question) or ``None`` (unknown). Only ``False`` changes
behaviour: items flagged ``personal`` are admitted only when above the stricter ``personal_strict``
threshold, otherwise they are dropped (reason ``personal_on_generic_query``). ``None`` is treated like
``True`` so that an unclassified query never loses recall.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BypassReason",
    "Conflict",
    "GateConfig",
    "GateContract",
    "GateDecision",
    "GateItem",
    "GateState",
    "ItemAssessment",
    "ProbeObservation",
    "ScorerStats",
    "StoreCalibration",
    "assess",
]

GateState = Literal["answerable", "missing", "conflicting", "weak", "bypass"]
BypassReason = Literal["enumeration", "as_of", "show_all", "named_reference"]
Band = Literal["strong", "weak", "irrelevant", "unscored", "uncalibrated", "dropped"]
_MAD_TO_SIGMA = 1.4826


def _finite(x: float | None) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    f = float(x)
    return f if math.isfinite(f) else None


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = (pct / 100.0) * (n - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ProbeObservation(_Frozen):
    """One observation: a query's top score under a scorer, or unscored (``top_score=None``)."""

    query: str = ""
    scorer: str
    top_score: float | None = None


class ScorerStats(_Frozen):
    """Robust statistics of one scorer's probe tops (most recent ``max_samples`` kept)."""

    scorer: str
    samples: tuple[float, ...] = ()
    n_unscored: int = 0

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def median(self) -> float:
        s = sorted(self.samples)
        return _percentile(s, 50.0) if s else 0.0

    @property
    def mad(self) -> float:
        if not self.samples:
            return 0.0
        m = self.median
        return _percentile(sorted(abs(x - m) for x in self.samples), 50.0)

    @property
    def scale(self) -> float:
        """Robust sigma (``1.4826 * MAD``)."""
        return _MAD_TO_SIGMA * self.mad

    @property
    def p95(self) -> float:
        s = sorted(self.samples)
        return _percentile(s, 95.0) if s else 0.0

    def threshold(self, margin_sd: float) -> float:
        return self.p95 + margin_sd * self.scale


class StoreCalibration(_Frozen):
    """Per-scorer calibration of one store. Immutable; refresh with :meth:`with_observations`."""

    scorers: dict[str, ScorerStats] = Field(default_factory=dict)
    max_samples: int = Field(default=200, ge=1)

    @classmethod
    def from_observations(
        cls, observations: Iterable[ProbeObservation], *, max_samples: int = 200
    ) -> StoreCalibration:
        return cls(max_samples=max_samples).with_observations(observations)

    def with_observations(self, observations: Iterable[ProbeObservation]) -> StoreCalibration:
        """A new calibration with the observations appended (oldest samples fall off the window)."""
        samples = {k: list(v.samples) for k, v in self.scorers.items()}
        unscored = {k: v.n_unscored for k, v in self.scorers.items()}
        for obs in observations:
            samples.setdefault(obs.scorer, [])
            unscored.setdefault(obs.scorer, 0)
            score = _finite(obs.top_score)
            if score is None:
                unscored[obs.scorer] += 1
            else:
                samples[obs.scorer].append(score)
        out = {
            k: ScorerStats(scorer=k, samples=tuple(v[-self.max_samples :]), n_unscored=unscored[k])
            for k, v in samples.items()
        }
        return StoreCalibration(scorers=out, max_samples=self.max_samples)

    def stats(self, scorer: str) -> ScorerStats | None:
        return self.scorers.get(scorer)


class GateConfig(_Frozen):
    """Thresholds, all configurable. Margins are in robust sigmas above the off-topic p95."""

    margin_sd: float = Field(default=1.0, ge=0.0)
    weak_margin_sd: float = Field(default=0.0, ge=0.0)
    personal_strict_margin_sd: float = Field(default=3.0, ge=0.0)
    min_samples: int = Field(default=5, ge=1)


class GateContract(_Frozen):
    """The slice of the query contract the gate needs.

    ``request_kind`` ``enumeration`` / ``as_of`` / ``show_all``, ``time_view`` as_of/history and
    ``named_reference`` bypass the gate.
    """

    personal: bool | None = None
    request_kind: Literal["lookup", "enumeration", "as_of", "show_all"] = "lookup"
    named_reference: bool = False
    time_view: Literal["current", "history", "as_of"] = "current"


class GateItem(_Frozen):
    """A candidate: its score under ``scorer`` (``None`` = unscored), plus optional claim key/value."""

    item_id: str
    score: float | None = None
    scorer: str = "default"
    personal: bool = False
    key: str | None = None
    value: str | None = None
    current: bool = True


class ItemAssessment(_Frozen):
    item_id: str
    scorer: str
    score: float | None
    weak_threshold: float | None
    strong_threshold: float | None
    band: Band
    reason: str = ""


class Conflict(_Frozen):
    key: str
    values: tuple[str, ...]
    item_ids: tuple[str, ...]


class GateDecision(_Frozen):
    state: GateState
    reason: str
    bypass_reason: BypassReason | None = None
    low_confidence: bool = False
    admitted_ids: tuple[str, ...] = ()
    dropped_ids: tuple[str, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    assessments: tuple[ItemAssessment, ...] = ()
    config: GateConfig = Field(default_factory=GateConfig)
    personal: bool | None = None


def _norm(v: str | None) -> str:
    return " ".join((v or "").casefold().split())


def _bypass(contract: GateContract) -> BypassReason | None:
    if contract.request_kind == "enumeration":
        return "enumeration"
    if contract.request_kind == "show_all":
        return "show_all"
    if contract.request_kind == "as_of" or contract.time_view in ("as_of", "history"):
        return "as_of"
    if contract.named_reference:
        return "named_reference"
    return None


def assess(
    items: Sequence[GateItem],
    calibration: StoreCalibration,
    *,
    contract: GateContract | None = None,
    config: GateConfig | None = None,
) -> GateDecision:
    """Classify candidate evidence. Pure; ``items`` are expected best-first and order is preserved."""
    contract = contract or GateContract()
    config = config or GateConfig()
    bypass = _bypass(contract)

    if not items:
        return GateDecision(
            state="missing",
            reason="no candidates (absence of retrieval is not proof of absence)",
            bypass_reason=bypass,
            config=config,
            personal=contract.personal,
        )
    if bypass is not None:
        return GateDecision(
            state="bypass",
            reason=f"gate bypassed: {bypass}",
            bypass_reason=bypass,
            admitted_ids=tuple(i.item_id for i in items),
            config=config,
            personal=contract.personal,
        )

    assessments: list[ItemAssessment] = []
    strong: list[GateItem] = []
    weak: list[GateItem] = []
    unjudged: list[GateItem] = []
    dropped: list[str] = []
    generic = contract.personal is False

    for it in items:
        st = calibration.stats(it.scorer)
        score = _finite(it.score)
        w_thr: float | None = None
        s_thr: float | None = None
        if st is not None and st.n >= config.min_samples:
            w_thr = st.threshold(config.weak_margin_sd)
            s_thr = st.threshold(config.margin_sd)

        band: Band
        reason = ""
        if score is None:
            band, reason = "unscored", "no score; relevance unknown (never treated as 0)"
            unjudged.append(it)
        elif st is None or w_thr is None or s_thr is None:
            band = "uncalibrated"
            reason = f"scorer {it.scorer!r} has fewer than {config.min_samples} samples"
            unjudged.append(it)
        elif generic and it.personal and score <= st.threshold(config.personal_strict_margin_sd):
            band, reason = "dropped", "personal_on_generic_query"
            dropped.append(it.item_id)
        elif score > s_thr:
            band = "strong"
            strong.append(it)
        elif score > w_thr:
            band = "weak"
            weak.append(it)
        else:
            band = "irrelevant"
        assessments.append(
            ItemAssessment(
                item_id=it.item_id,
                scorer=it.scorer,
                score=score,
                weak_threshold=w_thr,
                strong_threshold=s_thr,
                band=band,
                reason=reason,
            )
        )

    common = {
        "assessments": tuple(assessments),
        "dropped_ids": tuple(dropped),
        "config": config,
        "personal": contract.personal,
    }

    if strong:
        by_key: dict[str, dict[str, list[str]]] = {}
        for it in strong:
            if it.key is not None and it.value is not None and it.current:
                by_key.setdefault(it.key, {}).setdefault(_norm(it.value), []).append(it.item_id)
        conflicts = tuple(
            Conflict(
                key=k,
                values=tuple(vals),
                item_ids=tuple(i for ids in vals.values() for i in ids),
            )
            for k, vals in by_key.items()
            if len(vals) > 1
        )
        if conflicts:
            return GateDecision(
                state="conflicting",
                reason="strong evidence asserts incompatible values for: "
                + ", ".join(c.key for c in conflicts),
                admitted_ids=tuple(i.item_id for i in strong),
                conflicts=conflicts,
                **common,  # type: ignore[arg-type]
            )
        return GateDecision(
            state="answerable",
            reason="evidence above the store-calibrated threshold",
            admitted_ids=tuple(i.item_id for i in strong),
            **common,  # type: ignore[arg-type]
        )
    if weak or unjudged:
        admitted = {i.item_id for i in weak + unjudged}
        return GateDecision(
            state="weak",
            reason="only low-relevance or unjudgeable evidence",
            low_confidence=True,
            admitted_ids=tuple(i.item_id for i in items if i.item_id in admitted),
            **common,  # type: ignore[arg-type]
        )
    return GateDecision(
        state="missing",
        reason="no candidate above the off-topic level",
        **common,  # type: ignore[arg-type]
    )
