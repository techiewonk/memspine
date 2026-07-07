"""Prompt-as-memory binding (D-43 §4): references only, idempotent by version."""

from __future__ import annotations

from memspine.core.records import RecordStatus
from memspine.memories.procedural.lifecycle import SkillStage
from memspine.memories.procedural.prompt_registry import (
    prompt_reference,
    prompt_version_records,
)
from memspine.prompts.registry import PromptRegistry


def test_records_reference_prompts_without_duplicating_bodies() -> None:
    registry = PromptRegistry()
    records = prompt_version_records("ns", registry, existing=[])
    assert records, "shipped default pack should produce at least one prompt record"
    by_id = {record.entity: record for record in records}
    for prompt in registry.list():
        record = by_id[prompt.id]
        assert record.content == prompt_reference(prompt.id, prompt.version)
        assert prompt.body not in record.content  # definition lives in prompts/
        assert record.attribute == "prompt"
        assert record.memory_type == "procedural"
        assert record.version == prompt.version
        # Resolved prompts ARE the active version (D-43) — no ladder climb.
        assert record.skill_stage == SkillStage.ACTIVE.value
        assert record.status is RecordStatus.ACTIVATED
        assert record.source.channel == "prompts"


def test_sync_is_idempotent_by_id_and_version() -> None:
    registry = PromptRegistry()
    first = prompt_version_records("ns", registry, existing=[])
    second = prompt_version_records("ns", registry, existing=first)
    assert second == []


def test_override_bumps_version_and_produces_a_new_record() -> None:
    plain = PromptRegistry()
    baseline = prompt_version_records("ns", plain, existing=[])
    some_id = plain.list()[0].id
    overridden = PromptRegistry(overrides={some_id: {"body": "customized body"}})
    fresh = prompt_version_records("ns", overridden, existing=baseline)
    assert [record.entity for record in fresh] == [some_id]
    assert fresh[0].version == plain.get(some_id).version + 1
    assert fresh[0].source.message_id == "override"
