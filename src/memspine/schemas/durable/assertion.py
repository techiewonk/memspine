"""Transitional module for the schema-only RM-129 commit; deleted by the consumer migration commit.

Not an alias contract (ADR-088 removes ``AssertionEnvelope``): it only keeps this one intermediate
commit importable so other streams can rebase onto the schema change.
"""

from memspine.schemas.durable.semantic import (
    AssertionStatus,
    GroundingChecks,
    IntegrationVerdict,
    Mention,
    SemanticEnvelope,
    inherit_trust,
)

AssertionEnvelope = SemanticEnvelope

__all__ = [
    "AssertionEnvelope",
    "AssertionStatus",
    "GroundingChecks",
    "IntegrationVerdict",
    "Mention",
    "inherit_trust",
]
