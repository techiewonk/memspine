"""Conflict ladder (M4): deterministic verdicts over fact keys."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.policies.conflict import ConflictPolicy, ConflictVerdict
from memspine.core.records import MemoryRecord

NOW = datetime(2026, 7, 7, tzinfo=UTC)


def fact(
    content: str,
    entity: str | None = "alice",
    attribute: str | None = "city",
    days_ago: float = 0.0,
    trust: float = 0.5,
) -> MemoryRecord:
    return MemoryRecord(
        namespace="n",
        memory_type="semantic",
        content=content,
        entity=entity,
        attribute=attribute,
        valid_from=NOW - timedelta(days=days_ago),
        trust=trust,
    )


def test_r0_identical_content_is_noop() -> None:
    policy = ConflictPolicy.bind()
    assert policy.resolve(fact("Berlin"), fact("Berlin")) is ConflictVerdict.NOOP


def test_different_fact_keys_coexist() -> None:
    policy = ConflictPolicy.bind()
    incoming = fact("Berlin", attribute="city")
    existing = fact("42", attribute="age")
    assert policy.resolve(incoming, existing) is ConflictVerdict.ADD
    # unkeyed records never enter the ladder as conflicts
    assert policy.resolve(fact("x", entity=None), fact("y")) is ConflictVerdict.ADD


def test_r1_trust_gate_rejects_untrusted_displacement() -> None:
    policy = ConflictPolicy.bind({"trust_margin": 0.3})
    incoming = fact("Paris", trust=0.1)  # e.g. retrieved-content cap (E1)
    existing = fact("Berlin", days_ago=30, trust=0.8)
    assert policy.resolve(incoming, existing) is ConflictVerdict.NOOP


def test_r3_newer_fact_updates() -> None:
    policy = ConflictPolicy.bind()
    incoming = fact("Paris", days_ago=0)
    existing = fact("Berlin", days_ago=30)
    assert policy.resolve(incoming, existing) is ConflictVerdict.UPDATE


def test_r4_older_fact_is_historical_backfill() -> None:
    policy = ConflictPolicy.bind()
    incoming = fact("Munich", days_ago=365)
    existing = fact("Berlin", days_ago=30)
    assert policy.resolve(incoming, existing) is ConflictVerdict.ADD


def test_bias_oldest_inverts_r3() -> None:
    policy = ConflictPolicy.bind({"bias": "oldest"})
    incoming = fact("Paris", days_ago=0)
    existing = fact("Berlin", days_ago=30)
    assert policy.resolve(incoming, existing) is ConflictVerdict.ADD
