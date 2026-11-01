"""Durable tier: persisted kinds with an integer ``schema_version``, current-format validation, frozen fixtures."""

from memspine.schemas.durable.assertion import AssertionEnvelope  # transitional, next commit
from memspine.schemas.durable.assessment import (
    Assessment,
    Support,
    can_supersede,
    integration_outcome,
    is_tentative,
)
from memspine.schemas.durable.common import Fingerprint, OwnerScope, Provenance, SourceRef
from memspine.schemas.durable.entity_mentions import EntityMention
from memspine.schemas.durable.erasure import ErasedIds, ErasureManifest, RetentionBoundary
from memspine.schemas.durable.events import EVENT_KINDS, MemoryEvent
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.identity import AssertionKey, occurrence_id, write_id
from memspine.schemas.durable.ingestion import (
    AnnotationBatch,
    DerivationFailure,
    EffectCommitState,
    EffectIdentity,
    IngestionSourceSnapshot,
    RequiredDerivation,
    effect_commit_allowed,
)
from memspine.schemas.durable.jobs import (
    Hole,
    JobLease,
    OutboxRow,
    Progress,
    commit_allowed,
)
from memspine.schemas.durable.lineage import LineageEdge
from memspine.schemas.durable.semantic import (
    ClaimProducer,
    GroundingChecks,
    IntegrationVerdict,
    Mention,
    PredicateTyping,
    RoleBinding,
    SemanticEnvelope,
    inherit_trust,
)
from memspine.schemas.durable.temporal import (
    Duration,
    Interval,
    TemporalRef,
    TemporalRelation,
    TimeAxes,
)

__all__ = [
    "EVENT_KINDS",
    "AnnotationBatch",
    "AssertionEnvelope",
    "AssertionKey",
    "Assessment",
    "ClaimProducer",
    "DerivationFailure",
    "Duration",
    "EffectCommitState",
    "EffectIdentity",
    "EntityMention",
    "ErasedIds",
    "ErasureManifest",
    "Evidence",
    "Fingerprint",
    "GroundingChecks",
    "Hole",
    "IngestionSourceSnapshot",
    "IntegrationVerdict",
    "Interval",
    "JobLease",
    "LineageEdge",
    "MemoryEvent",
    "Mention",
    "OutboxRow",
    "OwnerScope",
    "PredicateTyping",
    "Progress",
    "Provenance",
    "RequiredDerivation",
    "RetentionBoundary",
    "RoleBinding",
    "SemanticEnvelope",
    "SourceRef",
    "Span",
    "Support",
    "TemporalRef",
    "TemporalRelation",
    "TimeAxes",
    "TurnRef",
    "can_supersede",
    "commit_allowed",
    "effect_commit_allowed",
    "inherit_trust",
    "integration_outcome",
    "is_tentative",
    "occurrence_id",
    "write_id",
]
