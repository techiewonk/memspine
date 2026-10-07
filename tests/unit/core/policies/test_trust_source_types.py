"""N26: document-type authority tier and the needs-evidence hold (ADR-060)."""

from __future__ import annotations

import pytest

from memspine.config import constants
from memspine.core.firewall import Firewall
from memspine.core.policies.trust import TrustPolicy
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.exceptions import ConfigError

TYPES = {"register": 3, "news": 2, "media": 1, "agent_report": 0}


def _record(doctype: str | None, role: str = "user", channel: str = "internal") -> MemoryRecord:
    tags = [f"doctype:{doctype}"] if doctype else []
    return MemoryRecord(
        namespace="ns",
        memory_type="semantic",
        content="the bridge closes on Friday",
        source=SourceInfo(role=role, channel=channel),
        tags=tags,
    )


def test_off_by_default_is_unchanged() -> None:
    policy = TrustPolicy.bind()
    record = _record("agent_report")
    assert policy.source_tier(record) is None
    assert policy.tier_capped(0.7, record) == 0.7
    assert not policy.needs_evidence(record)
    assert not Firewall(policy).assess(record).quarantine


def test_tier_caps_trust_by_document_type() -> None:
    policy = TrustPolicy.bind({"source_types": TYPES})
    assert policy.source_tier(_record("register")) == 3
    assert policy.source_tier(_record(None, channel="news")) == 2  # channel fallback
    capped = Firewall(policy).assess(_record("agent_report")).trust
    assert capped == constants.SOURCE_TIER_TRUST_CAP[0]


def test_hold_needs_evidence_quarantines_low_tier() -> None:
    policy = TrustPolicy.bind({"source_types": TYPES, "hold_needs_evidence": True})
    verdict = Firewall(policy).assess(_record("media"))
    assert verdict.quarantine
    assert any(reason.startswith("pending_evidence") for reason in verdict.reasons)
    assert not Firewall(policy).assess(_record("news")).quarantine
    assert not Firewall(policy).assess(_record("media", role="operator")).quarantine


def test_evidence_verdict() -> None:
    policy = TrustPolicy.bind({"source_types": TYPES})
    assert policy.evidence_verdict([3], contradicted=False) == "promote"
    assert policy.evidence_verdict([2, 0], contradicted=True) == "disputed"
    assert policy.evidence_verdict([1, None], contradicted=False) == "pending_evidence"


def test_bad_tier_is_a_config_error() -> None:
    with pytest.raises((ConfigError, ValueError)):
        TrustPolicy.bind({"source_types": {"blog": 7}})
