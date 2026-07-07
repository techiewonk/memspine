"""Retention policy (M2/M7): legal hold prefixes + regulated-PII protection."""

from __future__ import annotations

from memspine.core.policies.retention import RetentionPolicy
from memspine.core.records import MemoryRecord, PiiTier


def rec(namespace: str = "agent/a", pii: PiiTier = PiiTier.NONE) -> MemoryRecord:
    return MemoryRecord(namespace=namespace, memory_type="semantic", content="x", pii_tier=pii)


def test_legal_hold_is_hierarchical_prefix_match() -> None:
    policy = RetentionPolicy.bind({"legal_hold_namespaces": ["org/finance"]})
    assert policy.on_legal_hold(rec("org/finance"))
    assert policy.on_legal_hold(rec("org/finance/q3"))
    assert not policy.on_legal_hold(rec("org/financex"))  # sibling, not child
    assert not policy.on_legal_hold(rec("org/hr"))


def test_may_delete_blocks_hold_and_regulated_pii() -> None:
    policy = RetentionPolicy.bind({"legal_hold_namespaces": ["org/finance"]})
    assert not policy.may_delete(rec("org/finance/q3"))
    assert not policy.may_delete(rec("agent/a", pii=PiiTier.REGULATED))
    assert policy.may_delete(rec("agent/a", pii=PiiTier.HIGH))
    assert policy.may_delete(rec("agent/a"))
