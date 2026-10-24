"""Durable contracts for raw-first background derivation.

These are the canonical acceptance and source-snapshot shapes used by the
ingestion ledger.  They deliberately describe work; they do not introduce a
second writer or an alternative ingestion API.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from memspine.schemas.base import DurableModel, DurablePart
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.entity_mentions import EntityMention


def _fingerprint_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]

DerivationLayer = Literal[
    "raw_store",
    "vector",
    "lexical",
    "annotations",
    "facts",
    "graph_mentions",
    "summaries",
]


class RequiredDerivation(DurableModel):
    """The policy promise frozen in the same transaction as raw acceptance."""

    __kind__ = "required_derivation"

    intent_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    scope: OwnerScope
    source_id: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    source_write_ids: tuple[str, ...] = Field(min_length=1)
    source_record_ids: tuple[str, ...] = ()
    required_layers: tuple[DerivationLayer, ...] = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    registry_digest: str | None = None
    activated_at: AwareDatetime
    historical: Literal[False] = False

    @model_validator(mode="after")
    def _canonical(self) -> RequiredDerivation:
        if self.required_layers[0] != "raw_store":
            raise ValueError("required_layers must start with raw_store")
        if len(set(self.required_layers)) != len(self.required_layers):
            raise ValueError("required_layers contains duplicates")
        if len(set(self.source_write_ids)) != len(self.source_write_ids):
            raise ValueError("source_write_ids contains duplicates")
        if len(set(self.source_record_ids)) != len(self.source_record_ids):
            raise ValueError("source_record_ids contains duplicates")
        return self


class IngestionSourceSnapshot(DurableModel):
    """Pinned source input checked again at fenced effect commit."""

    __kind__ = "ingestion_source_snapshot"

    namespace: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    content_hash: str = Field(min_length=1)
    sender: str | None = None
    role: str | None = None
    source_at: datetime | None = None
    erasure_generation: int = Field(default=0, ge=0)


class AnnotationBatch(DurableModel):
    """One completed mention assessment, including an explicit empty result."""

    __kind__ = "annotation_batch"

    source: IngestionSourceSnapshot
    mentions: tuple[EntityMention, ...] = ()
    producer_revision: str = Field(min_length=1)

    @model_validator(mode="after")
    def _source_bound(self) -> AnnotationBatch:
        for mention in self.mentions:
            if mention.source_namespace != self.source.namespace:
                raise ValueError("mention namespace differs from its source snapshot")
            if mention.source_record_id != self.source.record_id:
                raise ValueError("mention record differs from its source snapshot")
            if mention.source_version != self.source.source_version:
                raise ValueError("mention version differs from its source snapshot")
            if mention.source_content_fingerprint != self.source.content_hash:
                raise ValueError("mention content hash differs from its source snapshot")
            if self.source.role is not None and mention.source_role != self.source.role:
                raise ValueError("mention role differs from its source snapshot")
            if self.source.sender is not None and mention.sender_id != self.source.sender:
                raise ValueError("mention sender differs from its source snapshot")
        return self


class EffectIdentity(DurableModel):
    """Stable idempotency identity for one fenced derivation output."""

    __kind__ = "derivation_effect_identity"

    effect_id: str = Field(min_length=16, max_length=16)
    job_id: str = Field(min_length=1)
    output_key: str = Field(min_length=1)
    sources: tuple[IngestionSourceSnapshot, ...] = Field(min_length=1)
    producer_revision: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)

    @classmethod
    def build(
        cls,
        *,
        job_id: str,
        output_key: str,
        sources: tuple[IngestionSourceSnapshot, ...],
        producer_revision: str,
        policy_revision: str,
    ) -> EffectIdentity:
        payload: dict[str, object] = {
            "job_id": job_id,
            "output_key": output_key,
            "sources": [source.model_dump(mode="json") for source in sources],
            "producer_revision": producer_revision,
            "policy_revision": policy_revision,
        }
        return cls(
            effect_id=_fingerprint_payload(payload),
            job_id=job_id,
            output_key=output_key,
            sources=sources,
            producer_revision=producer_revision,
            policy_revision=policy_revision,
        )

    @model_validator(mode="after")
    def _stable_id(self) -> EffectIdentity:
        source_keys = [(source.record_id, source.source_version) for source in self.sources]
        if len(set(source_keys)) != len(source_keys):
            raise ValueError("effect sources contain duplicate snapshots")
        expected = _fingerprint_payload(
            {
                "job_id": self.job_id,
                "output_key": self.output_key,
                "sources": [source.model_dump(mode="json") for source in self.sources],
                "producer_revision": self.producer_revision,
                "policy_revision": self.policy_revision,
            }
        )
        if self.effect_id != expected:
            raise ValueError("effect_id does not match the fenced output identity")
        return self


class EffectCommitState(DurablePart):
    """Authoritative state read in the transaction that appends an effect."""

    current_epoch: int = Field(ge=1)
    erasure_generation: int = Field(ge=0)
    source_versions: dict[str, int]
    source_content_hashes: dict[str, str]


def effect_commit_allowed(
    identity: EffectIdentity,
    *,
    lease_epoch: int,
    state: EffectCommitState,
) -> bool:
    """Refuse stale workers, changed sources, and forget-during-inference.

    The caller must obtain ``state`` and append/reuse ``identity.effect_id`` in
    one storage transaction.  This pure predicate is shared by storage
    implementations so those transactional doors enforce identical rules.
    """
    if lease_epoch != state.current_epoch:
        return False
    for source in identity.sources:
        if source.erasure_generation != state.erasure_generation:
            return False
        if state.source_versions.get(source.record_id) != source.source_version:
            return False
        if state.source_content_hashes.get(source.record_id) != source.content_hash:
            return False
    return True


class DerivationFailure(DurablePart):
    """Bounded, content-free failure metadata suitable for status surfaces."""

    stage: str = Field(min_length=1)
    error_class: str = Field(min_length=1)
    attempts: int = Field(ge=1)
    retry_eligible: bool
    source_ids: tuple[str, ...] = ()


__all__ = [
    "AnnotationBatch",
    "DerivationFailure",
    "DerivationLayer",
    "EffectCommitState",
    "EffectIdentity",
    "IngestionSourceSnapshot",
    "RequiredDerivation",
    "effect_commit_allowed",
]
