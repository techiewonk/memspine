# ruff: noqa: E501
"""LIB-47: read depth tiers, ReadRequest normalisation and the evidence unit (R-01, R-02, ER-29, ER-39).

Pure and unwired (wired by R3-01). No I/O, no clock read, no model call. Imports only the standard
library, pydantic, ``memspine.schemas`` and the merged read libraries ``read.router_rules``,
``read.contract_rules`` and ``read.cache``.

* Tiers (R-01 A+B). :class:`ReadConfig` holds the deployment default and the allowed set; the per-call
  ``depth`` overrides the default only inside the allowed set (no silent downgrade). Each tier is a
  :class:`Tier`: a p95 latency target (R-25), a cap on generative calls (ER-39: counted apart from
  embedding and rerank calls; ``fast`` 0, ``standard`` 0, ``deep`` at most 2 per Q-R2 A), a cap on the
  rerank pool (at most 64, Q-R4 A) and the share of the request's token budget that evidence may use.
  All numbers are configurable defaults, "to be confirmed" by the R-25 CI measurements.
* :func:`normalise_request` turns a raw payload into the public :class:`ReadRequest`: the viewer is bound
  to the authenticated principal (ER-29 C, session 10), a multi-party scope without a viewer is rejected,
  defaults are filled, as-of fields are validated. :func:`prepare_read` adds the ER-31 cache-key inputs.
* :func:`choose_tier`: an explicit depth wins; with no depth and ``default_depth="auto"`` the deep tier is
  chosen only when allowed, a worker model exists and the contract shape needs it.
* The evidence unit (R-02, ER-24): one :class:`EvidenceBundle` = assertion + support + binding + time
  anchors, entering the context whole or not at all. :func:`unit_tokens`, :func:`count_units` and
  :func:`fit_units` count and budget by whole units.

Interpretations (flagged in the LIB-47 report): ``owner_kind == "group"`` counts as multi-party; a
single-party read without a viewer keeps ``viewer=None``; ``as_of_known`` later than ``reference_time`` is
rejected; ``auto`` falls back to ``standard`` (not ``fast``) for a deep-shaped contract when deep is
unavailable and ``standard`` is allowed; token shares are placeholders pending calibration.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from memspine.read.cache import ReadCacheKey, read_key
from memspine.read.contract_rules import TypedContract
from memspine.read.router_rules import LayerState
from memspine.schemas.public.read import EvidenceBundle, ReadRequest

__all__ = [
    "DEEP_SHAPES",
    "DEFAULT_ALLOWED_DEPTHS",
    "DEFAULT_BUDGET_TOKENS",
    "DEFAULT_TIER_TABLE",
    "EvidenceUnit",
    "PreparedRead",
    "ReadConfig",
    "ReadConfigError",
    "ReadRequestError",
    "Tier",
    "TierName",
    "choose_tier",
    "count_units",
    "estimate_tokens",
    "fit_units",
    "normalise_request",
    "prepare_read",
    "unit_of",
    "unit_tokens",
]

TierName = Literal["fast", "standard", "deep"]
DEFAULT_BUDGET_TOKENS = 2048  # ASSEMBLE_BUDGET_TOKENS in v1; request.budget_tokens None -> this
MAX_RERANK_POOL = 64  # Q-R4 A: the listwise reranker scores at most 64 documents per call
DEFAULT_ALLOWED_DEPTHS: tuple[TierName, ...] = ("fast", "standard", "deep")
# Contract shapes that need more than one hop or a whole value history (join / list / temporal history).
DEEP_SHAPES: frozenset[str] = frozenset({"join", "list_count", "history"})


class ReadRequestError(ValueError):
    """The raw request cannot become a valid ReadRequest (caller error)."""


class ReadConfigError(ValueError):
    """The requested depth is not allowed, or is unavailable and the config says to raise (R-01, P-03 D)."""


# ------------------------------------------------------------------ tiers


class Tier(BaseModel):
    """One depth tier with its budgets (R-01, R-25, ER-39, Q-R2, Q-R4).

    ``max_generative_calls`` counts generative model calls only; embedding and rerank calls are accounted
    separately (ER-39) and are not limited here. ``degraded_from`` is set on a tier returned in place of
    a requested ``deep`` that could not run (``on_missing_model="rules"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: TierName
    slo_p95_ms: int = Field(gt=0)  # p95 target, no reranker (R-25)
    slo_p95_ms_rerank: int = Field(gt=0)  # p95 target with the reranker in the path
    max_generative_calls: int = Field(ge=0)
    max_rerank_pool: int = Field(ge=0, le=MAX_RERANK_POOL)
    token_share: float = Field(gt=0.0, le=1.0)  # share of budget_tokens evidence units may use
    degraded_from: TierName | None = None

    @model_validator(mode="after")
    def _slo(self) -> Tier:
        if self.slo_p95_ms_rerank < self.slo_p95_ms:
            raise ValueError("the reranked SLO cannot be tighter than the unreranked one")
        return self

    def evidence_budget(self, budget_tokens: int | None) -> int:
        """Tokens evidence units may use under this tier for a request budget (None -> the default)."""
        total = DEFAULT_BUDGET_TOKENS if budget_tokens is None else budget_tokens
        return int(total * self.token_share)


DEFAULT_TIER_TABLE: dict[TierName, Tier] = {
    "fast": Tier(
        name="fast",
        slo_p95_ms=150,
        slo_p95_ms_rerank=350,
        max_generative_calls=0,
        max_rerank_pool=32,
        token_share=0.85,
    ),
    "standard": Tier(
        name="standard",
        slo_p95_ms=500,
        slo_p95_ms_rerank=500,
        max_generative_calls=0,
        max_rerank_pool=48,
        token_share=0.85,
    ),
    "deep": Tier(
        name="deep",
        slo_p95_ms=3000,
        slo_p95_ms_rerank=3000,
        max_generative_calls=2,
        max_rerank_pool=64,
        token_share=0.85,
    ),
}


class ReadConfig(BaseModel):
    """The ``read`` config slice this module needs (``read.default_depth``, ``read.allowed_depths``,
    ``read.tiers``). ``default_depth="auto"`` leaves an unset depth to :func:`choose_tier`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_depth: TierName | Literal["auto"] = "fast"
    allowed_depths: tuple[TierName, ...] = DEFAULT_ALLOWED_DEPTHS
    default_budget_tokens: int = Field(default=DEFAULT_BUDGET_TOKENS, gt=0)
    on_missing_model: Literal["error", "rules"] = "error"  # P-03 D, for an explicit deep request
    tiers: Mapping[TierName, Tier] = Field(default_factory=lambda: dict(DEFAULT_TIER_TABLE))

    @model_validator(mode="after")
    def _consistent(self) -> ReadConfig:
        if not self.allowed_depths:
            raise ValueError("allowed_depths is empty")
        if set(self.tiers) != {"fast", "standard", "deep"}:
            raise ValueError("tiers must define fast, standard and deep")
        for key, tier in self.tiers.items():
            if tier.name != key:
                raise ValueError(f"tier {key!r} is named {tier.name!r}")
        if self.default_depth != "auto" and self.default_depth not in self.allowed_depths:
            raise ValueError("default_depth is not in allowed_depths")
        return self

    def digest(self) -> str:
        """Stable config hash for cache keys (ER-31)."""
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "cfg_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ------------------------------------------------------------------ request normalisation

_FIELDS = frozenset(ReadRequest.model_fields)


def _clean_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def _raw_dict(raw: ReadRequest | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, ReadRequest):
        return {k: getattr(raw, k) for k in _FIELDS}
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise ReadRequestError(f"unknown request field(s): {', '.join(unknown)}")
    return dict(raw)


def normalise_request(
    raw: ReadRequest | Mapping[str, Any],
    principal: str | None,
    config: ReadConfig | None = None,
) -> ReadRequest:
    """Build the public :class:`ReadRequest` from a raw payload and the authenticated principal.

    * ``principal`` comes from authentication only; a payload ``principal`` must be absent or equal to it.
    * A viewer must equal the principal (bound, never trusted from the payload). A group-owned scope or an
      explicit ``multi_party`` is a multi-party read and needs a viewer: it is rejected without one.
    * The query is NFKC-normalised with whitespace collapsed (case kept); empty is rejected.
    * ``depth`` None takes the config default unless that is ``auto`` (then it stays None for
      :func:`choose_tier`); an explicit or default depth outside ``allowed_depths`` raises.
    * ``budget_tokens`` None takes the config default. As-of instants become UTC; ``as_of_known`` later
      than ``reference_time`` is rejected (nothing can be known in the future of the read).
    """
    cfg = config or ReadConfig()
    data = _raw_dict(raw)

    claimed = data.get("principal")
    if claimed is not None and claimed != principal:
        raise ReadRequestError("the payload principal does not match the authenticated principal")
    viewer = data.get("viewer")
    if isinstance(viewer, str):
        viewer = viewer.strip() or None
    if viewer is not None:
        if principal is None:
            raise ReadRequestError("a viewer needs an authenticated principal")
        if viewer != principal:
            raise ReadRequestError("the viewer must be the authenticated principal")

    try:
        probe_scope = data.get("scope")
        group = (
            probe_scope.get("owner_kind") == "group"
            if isinstance(probe_scope, Mapping)
            else getattr(probe_scope, "owner_kind", None) == "group"
        )
    except Exception:  # pragma: no cover - defensive; validation below reports the real problem
        group = False
    multi_party = bool(data.get("multi_party", False)) or group
    if multi_party and viewer is None:
        raise ReadRequestError("a multi-party read requires a viewer bound to the principal")

    query = data.get("query")
    if not isinstance(query, str) or not _clean_text(query):
        raise ReadRequestError("the query is empty")

    depth = data.get("depth")
    if depth is None and cfg.default_depth != "auto":
        depth = cfg.default_depth
    if depth is not None and depth not in cfg.allowed_depths:
        raise ReadConfigError(
            f"depth {depth!r} is not allowed (allowed: {list(cfg.allowed_depths)})"
        )

    lang = data.get("lang")
    if isinstance(lang, str):
        lang = lang.strip().lower() or None

    fields: dict[str, Any] = {
        "query": _clean_text(query),
        "scope": data.get("scope"),
        "multi_party": multi_party,
        "principal": principal,
        "viewer": viewer,
        "depth": depth,
        "budget_tokens": cfg.default_budget_tokens
        if data.get("budget_tokens") is None
        else data["budget_tokens"],
        "lang": lang,
    }
    for key in ("as_of_valid", "as_of_known", "reference_time"):
        fields[key] = data.get(key)
    try:
        req = ReadRequest(**fields)
    except ValidationError as exc:
        raise ReadRequestError(str(exc)) from exc

    update = {k: _utc(getattr(req, k)) for k in ("as_of_valid", "as_of_known", "reference_time")}
    known, ref = update["as_of_known"], update["reference_time"]
    if known is not None and ref is not None and known > ref:
        raise ReadRequestError("as_of_known is later than reference_time")
    return req.model_copy(update=update)


@dataclass(frozen=True)
class PreparedRead:
    """A normalised request plus the ER-31 cache-key inputs known before the store is touched.

    Generations are read from the store at lookup time, so they are arguments of :meth:`cache_key`.
    """

    request: ReadRequest
    config_hash: str
    model_ids: tuple[str, ...]

    def cache_key(
        self,
        *,
        store_generation: int,
        erasure_generation: int,
        consent_generation: int | None = None,
    ) -> ReadCacheKey:
        return read_key(
            scope=self.request.scope,
            viewer=self.request.viewer,
            query=self.request.query,
            config_hash=self.config_hash,
            model_ids=self.model_ids,
            store_generation=store_generation,
            erasure_generation=erasure_generation,
            consent_generation=consent_generation,
        )


def prepare_read(
    raw: ReadRequest | Mapping[str, Any],
    principal: str | None,
    *,
    config: ReadConfig | None = None,
    model_ids: Iterable[str] = (),
) -> PreparedRead:
    """:func:`normalise_request` plus cache-key inputs (scope, viewer, query, config hash, model ids)."""
    cfg = config or ReadConfig()
    return PreparedRead(
        request=normalise_request(raw, principal, cfg),
        config_hash=cfg.digest(),
        model_ids=tuple(sorted(set(model_ids))),
    )


# ------------------------------------------------------------------ tier choice


def choose_tier(
    request: ReadRequest,
    contract: TypedContract | None,
    layers: LayerState,
    config: ReadConfig | None = None,
) -> Tier:
    """Pick the tier for a read.

    Explicit ``request.depth`` wins: outside ``allowed_depths`` it raises :class:`ReadConfigError`; an
    explicit ``deep`` with no worker model raises (``on_missing_model="error"``) or runs as ``standard``
    with ``degraded_from="deep"`` (``"rules"``). With no depth (auto) the result is ``deep`` only when deep
    is allowed, a worker model exists and the contract shape is in :data:`DEEP_SHAPES` (or its time view
    is a history); otherwise a deep-shaped contract gets ``standard`` when allowed, and everything else
    the first allowed tier of fast, standard, deep.
    """
    cfg = config or ReadConfig()
    worker = layers.services.worker_model

    if request.depth is not None:
        if request.depth not in cfg.allowed_depths:
            raise ReadConfigError(f"depth {request.depth!r} is not allowed")
        if request.depth == "deep" and not worker:
            if cfg.on_missing_model == "error":
                raise ReadConfigError("deep needs a worker model and none is bound")
            fallback = "standard" if "standard" in cfg.allowed_depths else "fast"
            if fallback not in cfg.allowed_depths:
                raise ReadConfigError("deep is unavailable and no fallback tier is allowed")
            return cfg.tiers[fallback].model_copy(update={"degraded_from": "deep"})
        return cfg.tiers[request.depth]

    needs_deep = contract is not None and (
        contract.shape in DEEP_SHAPES or contract.time_view.kind == "history"
    )
    if needs_deep:
        if "deep" in cfg.allowed_depths and worker:
            return cfg.tiers["deep"]
        if "standard" in cfg.allowed_depths:
            return cfg.tiers["standard"]
    base: TierName
    if cfg.default_depth != "auto":
        base = cfg.default_depth
    else:
        base = next(d for d in ("fast", "standard", "deep") if d in cfg.allowed_depths)  # type: ignore[assignment]
    if (
        base == "deep" and not worker
    ):  # a configured deep default with no model: same rule as explicit
        base = "standard" if "standard" in cfg.allowed_depths else "fast"
        if base not in cfg.allowed_depths:  # pragma: no cover - guarded by config validation
            raise ReadConfigError("no usable tier")
        return cfg.tiers[base].model_copy(update={"degraded_from": "deep"})
    return cfg.tiers[base]


# ------------------------------------------------------------------ the evidence unit (R-02, ER-24)


@dataclass(frozen=True)
class EvidenceUnit:
    """What a unit is: one assertion with its support, binding evidence and time anchors, all or none.

    ``tokens`` is the unit's cost for budgeting (see :func:`unit_tokens`).
    """

    record_id: str
    text: str
    support: int
    binding: int
    anchors: int
    tokens: int


def estimate_tokens(text: str) -> int:
    """Tokenizer-free estimate: ceil(characters / 4), at least 1 for non-empty text."""
    return 0 if not text else max(1, -(-len(text) // 4))


def unit_tokens(bundle: EvidenceBundle, counter: Callable[[str], int] | None = None) -> int:
    """Token cost of a bundle. An exact ``counter`` (the reader tokenizer) takes precedence and counts the
    assertion text plus every cited quote and anchor phrase; otherwise ``bundle.tokens`` when set, else an
    estimate over the same texts. Always the whole unit, never a part."""
    if counter is None and bundle.tokens > 0:
        return bundle.tokens
    count = counter or estimate_tokens
    parts = [bundle.assertion.text]
    for e in (*bundle.support, *bundle.binding):
        if e.span is not None and e.span.quote:
            parts.append(e.span.quote)
    parts.extend(a.text for a in bundle.time_anchors if a.text)
    return sum(count(p) for p in parts)


def unit_of(bundle: EvidenceBundle, counter: Callable[[str], int] | None = None) -> EvidenceUnit:
    return EvidenceUnit(
        record_id=bundle.record_id,
        text=bundle.assertion.text,
        support=len(bundle.support),
        binding=len(bundle.binding),
        anchors=len(bundle.time_anchors),
        tokens=unit_tokens(bundle, counter),
    )


def count_units(bundles: Iterable[EvidenceBundle]) -> int:
    """Number of units: bundles counted once per ``record_id`` (a repeated record is one unit)."""
    return len({b.record_id for b in bundles})


def fit_units(
    bundles: Sequence[EvidenceBundle],
    budget_tokens: int,
    *,
    counter: Callable[[str], int] | None = None,
    stop_at_first_overflow: bool = False,
) -> tuple[list[EvidenceBundle], list[EvidenceBundle]]:
    """Greedy whole-unit fit in rank order: returns ``(kept, dropped)``.

    A bundle that does not fit is dropped whole, never truncated (ER-24). By default later, smaller bundles
    may still fit; with ``stop_at_first_overflow`` the first overflow ends the fit (strict rank order). A
    repeated ``record_id`` after the first is dropped. Deterministic, order-preserving.
    """
    kept: list[EvidenceBundle] = []
    dropped: list[EvidenceBundle] = []
    seen: set[str] = set()
    used = 0
    stopped = False
    for b in bundles:
        if stopped or b.record_id in seen:
            dropped.append(b)
            continue
        cost = unit_tokens(b, counter)
        if used + cost <= budget_tokens:
            kept.append(b)
            seen.add(b.record_id)
            used += cost
        else:
            dropped.append(b)
            stopped = stop_at_first_overflow
    return kept, dropped
