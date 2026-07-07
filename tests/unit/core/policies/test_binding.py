"""Policy contract tests: every skeleton binds from config and rejects typos."""

from __future__ import annotations

import pytest

from memspine.core.policies.assembly import AssemblyPolicy
from memspine.core.policies.base import BindablePolicy
from memspine.core.policies.compression import CompressionPolicy
from memspine.core.policies.conflict import ConflictPolicy
from memspine.core.policies.consolidation import ConsolidationPolicy
from memspine.core.policies.decay import DecayPolicy
from memspine.core.policies.dedup import DedupPolicy
from memspine.core.policies.retention import RetentionPolicy
from memspine.core.policies.scoring import ScoringPolicy
from memspine.core.policies.trust import TrustPolicy
from memspine.exceptions import ConfigError

ALL_POLICIES: list[type[BindablePolicy]] = [
    ScoringPolicy,
    ConsolidationPolicy,
    DecayPolicy,
    ConflictPolicy,
    DedupPolicy,
    CompressionPolicy,
    RetentionPolicy,
    AssemblyPolicy,
    TrustPolicy,
]


@pytest.mark.parametrize("policy_cls", ALL_POLICIES, ids=lambda c: c.name)
def test_binds_with_defaults(policy_cls: type[BindablePolicy]) -> None:
    policy = policy_cls.bind()
    assert policy.name == policy_cls.name
    assert policy.options is not None


@pytest.mark.parametrize("policy_cls", ALL_POLICIES, ids=lambda c: c.name)
def test_unknown_option_rejected_at_config_time(policy_cls: type[BindablePolicy]) -> None:
    with pytest.raises(ConfigError, match=policy_cls.name):
        policy_cls.bind({"definitely_not_an_option": 1})


def test_option_values_flow_from_config() -> None:
    dedup = DedupPolicy.bind({"cosine_threshold": 0.95})
    assert dedup.options.cosine_threshold == 0.95  # type: ignore[attr-defined]
