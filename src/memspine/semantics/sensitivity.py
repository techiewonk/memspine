"""Optional sensitivity labels (RM-52) that can only tighten handling policy.

Labels are hints attached to predicates or property keys. They strengthen the
policy derived from the source; they never weaken firewall, scope, erasure or
legal-hold obligations. Sensitive content can appear under any predicate, so an
absent label is not a classification of the content as non-sensitive.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

SensitivityLabel = Literal["health", "financial", "contact", "identity", "location"]
SENSITIVITY_LABELS: frozenset[str] = frozenset(get_args(SensitivityLabel))


class HandlingPolicy(BaseModel):
    """The policy dimensions a label may tighten.

    ``retention_days=None`` means no label-imposed limit. ``legal_hold`` is never
    cleared by a label; while it is set, a label's retention cap is recorded but
    deferred until the hold is released.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    min_trust_tier: int = Field(default=0, ge=0)
    quarantine_on_anomaly: bool = False
    require_consent: bool = False
    exclude_from_shared: bool = False
    redact_in_logs: bool = False
    retention_days: int | None = Field(default=None, ge=1)
    legal_hold: bool = False


_REQUIREMENTS: dict[str, HandlingPolicy] = {
    "health": HandlingPolicy(
        min_trust_tier=1,
        quarantine_on_anomaly=True,
        require_consent=True,
        exclude_from_shared=True,
        redact_in_logs=True,
    ),
    "financial": HandlingPolicy(
        min_trust_tier=1, quarantine_on_anomaly=True, exclude_from_shared=True, redact_in_logs=True
    ),
    "contact": HandlingPolicy(exclude_from_shared=True, redact_in_logs=True),
    "identity": HandlingPolicy(require_consent=True, exclude_from_shared=True, redact_in_logs=True),
    "location": HandlingPolicy(exclude_from_shared=True, redact_in_logs=True),
}
LABEL_REQUIREMENTS = MappingProxyType(_REQUIREMENTS)


def _min_retention(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def combine_strictest(a: HandlingPolicy, b: HandlingPolicy) -> HandlingPolicy:
    """Pointwise strictest combination of two policies (commutative, idempotent)."""
    return HandlingPolicy(
        min_trust_tier=max(a.min_trust_tier, b.min_trust_tier),
        quarantine_on_anomaly=a.quarantine_on_anomaly or b.quarantine_on_anomaly,
        require_consent=a.require_consent or b.require_consent,
        exclude_from_shared=a.exclude_from_shared or b.exclude_from_shared,
        redact_in_logs=a.redact_in_logs or b.redact_in_logs,
        retention_days=_min_retention(a.retention_days, b.retention_days),
        legal_hold=a.legal_hold or b.legal_hold,
    )


def tighten(base: HandlingPolicy, labels: Iterable[str]) -> HandlingPolicy:
    """Apply label requirements to a source-derived policy; result is never looser."""
    result = base
    for label in labels:
        if label not in SENSITIVITY_LABELS:
            raise ValueError(f"unknown sensitivity label: {label}")
        result = combine_strictest(result, LABEL_REQUIREMENTS[label])
    return result


def is_at_least_as_strict(candidate: HandlingPolicy, base: HandlingPolicy) -> bool:
    """True when ``candidate`` keeps every obligation of ``base``."""
    retention_ok = base.retention_days is None or (
        candidate.retention_days is not None and candidate.retention_days <= base.retention_days
    )
    return (
        candidate.min_trust_tier >= base.min_trust_tier
        and candidate.quarantine_on_anomaly >= base.quarantine_on_anomaly
        and candidate.require_consent >= base.require_consent
        and candidate.exclude_from_shared >= base.exclude_from_shared
        and candidate.redact_in_logs >= base.redact_in_logs
        and candidate.legal_hold >= base.legal_hold
        and retention_ok
    )


def effective_retention_days(policy: HandlingPolicy) -> int | None:
    """Retention cap actually enforceable now; a legal hold defers any cap."""
    return None if policy.legal_hold else policy.retention_days
