"""BI-05/BI-07 durable annotation and effect identity contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memspine.schemas.durable import (
    AnnotationBatch,
    EffectCommitState,
    EffectIdentity,
    IngestionSourceSnapshot,
    effect_commit_allowed,
)
from memspine.schemas.durable.entity_mentions import EntityMention


def _source(**changes: object) -> IngestionSourceSnapshot:
    values: dict[str, object] = {
        "namespace": "tenant/alice",
        "record_id": "record-1",
        "source_id": "message-1",
        "source_version": 2,
        "content_hash": "a" * 16,
        "sender": "alice",
        "role": "user",
        "source_at": datetime(2026, 1, 1, tzinfo=UTC),
        "erasure_generation": 3,
    }
    values.update(changes)
    return IngestionSourceSnapshot(**values)


def _mention(**changes: object) -> EntityMention:
    values: dict[str, object] = {
        "text": "Mira",
        "label": "person",
        "start": 0,
        "end": 4,
        "asserted": False,
        "source_namespace": "tenant/alice",
        "source_record_id": "record-1",
        "source_version": 2,
        "source_content_fingerprint": "a" * 16,
        "source_binding_hash": "b" * 16,
        "source_role": "user",
        "source_channel": "messages",
        "sender_id": "alice",
    }
    values.update(changes)
    return EntityMention(**values)


def test_empty_annotation_batch_is_an_explicit_successful_result() -> None:
    batch = AnnotationBatch(source=_source(), mentions=(), producer_revision="gliner:test")

    restored = AnnotationBatch.model_validate_json(batch.model_dump_json())

    assert restored == batch
    assert restored.mentions == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_namespace", "tenant/bob"),
        ("source_record_id", "record-2"),
        ("source_version", 3),
        ("source_content_fingerprint", "c" * 16),
        ("source_role", "assistant"),
        ("sender_id", "mallory"),
    ],
)
def test_annotation_batch_rejects_mentions_from_another_snapshot(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="differs from its source snapshot"):
        AnnotationBatch(
            source=_source(),
            mentions=(_mention(**{field: value}),),
            producer_revision="gliner:test",
        )


def test_effect_identity_is_stable_and_changes_with_any_fence_input() -> None:
    source = _source()
    effect = EffectIdentity.build(
        job_id="job-1",
        output_key="fact-1",
        sources=(source,),
        producer_revision="rules:1",
        policy_revision="policy:7",
    )
    replay = EffectIdentity.build(
        job_id="job-1",
        output_key="fact-1",
        sources=(source,),
        producer_revision="rules:1",
        policy_revision="policy:7",
    )
    changed = EffectIdentity.build(
        job_id="job-1",
        output_key="fact-1",
        sources=(source.model_copy(update={"source_version": 3}),),
        producer_revision="rules:1",
        policy_revision="policy:7",
    )

    assert replay.effect_id == effect.effect_id
    assert changed.effect_id != effect.effect_id
    with pytest.raises(ValidationError, match="effect_id does not match"):
        effect.model_copy(update={"effect_id": "0" * 16}).__class__.model_validate(
            {**effect.model_dump(mode="json"), "effect_id": "0" * 16}
        )


def test_effect_identity_rejects_duplicate_source_snapshots() -> None:
    source = _source()
    with pytest.raises(ValidationError, match="duplicate snapshots"):
        EffectIdentity.build(
            job_id="job-1",
            output_key="fact-1",
            sources=(source, source),
            producer_revision="rules:1",
            policy_revision="policy:7",
        )


def test_effect_commit_requires_current_epoch_source_and_erasure_generation() -> None:
    source = _source()
    effect = EffectIdentity.build(
        job_id="job-1",
        output_key="annotation-1",
        sources=(source,),
        producer_revision="gliner:1",
        policy_revision="policy:7",
    )
    current = EffectCommitState(
        current_epoch=4,
        erasure_generation=3,
        source_versions={"record-1": 2},
        source_content_hashes={"record-1": "a" * 16},
    )

    assert effect_commit_allowed(effect, lease_epoch=4, state=current)
    assert not effect_commit_allowed(effect, lease_epoch=3, state=current)
    assert not effect_commit_allowed(
        effect,
        lease_epoch=4,
        state=current.model_copy(update={"erasure_generation": 4}),
    )
    assert not effect_commit_allowed(
        effect,
        lease_epoch=4,
        state=current.model_copy(update={"source_versions": {"record-1": 3}}),
    )
    assert not effect_commit_allowed(
        effect,
        lease_epoch=4,
        state=current.model_copy(update={"source_content_hashes": {"record-1": "c" * 16}}),
    )
