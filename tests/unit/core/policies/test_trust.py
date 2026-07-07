"""TrustPolicy (E1/M17): the source-class x channel matrix + quarantine rules."""

from __future__ import annotations

from memspine.core.policies.trust import TrustPolicy
from memspine.core.records import MemoryRecord, SourceInfo


def rec(role: str = "user", channel: str = "internal", trust: float = 0.5) -> MemoryRecord:
    return MemoryRecord(
        namespace="agent/a",
        memory_type="semantic",
        content="x",
        trust=trust,
        source=SourceInfo(role=role, channel=channel),
    )


def test_trust_matrix_role_ordering() -> None:
    policy = TrustPolicy.bind()
    trusts = {
        role: policy.trust_at_write(SourceInfo(role=role))
        for role in ("operator", "system", "user", "assistant", "tool", "unknown")
    }
    assert trusts["operator"] == trusts["system"] > trusts["user"]
    assert trusts["user"] > trusts["assistant"] > trusts["tool"]
    assert trusts["unknown"] == 0.5  # default midfield


def test_external_channels_cap_regardless_of_claimed_role() -> None:
    """A web page that claims to be the operator is still a web page (E1)."""
    policy = TrustPolicy.bind()
    for channel in ("retrieved", "web", "external", "email", "mcp"):
        assert policy.trust_at_write(SourceInfo(role="operator", channel=channel)) <= 0.3


def test_quarantine_on_rock_bottom_trust() -> None:
    policy = TrustPolicy.bind({"role_trust": {"scraper": 0.1}})
    scraped = rec(role="scraper", trust=policy.trust_at_write(SourceInfo(role="scraper")))
    assert policy.should_quarantine(scraped)


def test_instruction_shape_quarantines_untrusted_origins_only() -> None:
    policy = TrustPolicy.bind()
    assert policy.should_quarantine(rec(role="tool"), instruction_shaped=True)
    assert policy.should_quarantine(rec(role="assistant"), instruction_shaped=True)
    assert policy.should_quarantine(rec(role="user", channel="web"), instruction_shaped=True)
    # Operators and users legitimately store imperatives — flag stays inert.
    assert not policy.should_quarantine(rec(role="operator"), instruction_shaped=True)
    assert not policy.should_quarantine(rec(role="user"), instruction_shaped=True)


def test_anomaly_quarantines_unless_privileged() -> None:
    policy = TrustPolicy.bind()
    assert policy.should_quarantine(rec(role="user"), anomalous=True)
    assert not policy.should_quarantine(rec(role="system"), anomalous=True)


def test_promotion_needs_enough_corroborations() -> None:
    policy = TrustPolicy.bind()
    held = rec().model_copy(update={"corroborations": 1})
    assert not policy.may_promote(held)
    assert policy.may_promote(held.model_copy(update={"corroborations": 2}))
