"""Prompt-as-memory binding (D-43 §4): store prompt *versions* as procedural records.

The prompt *definition* lives once, in ``prompts/`` (registry + YAML pack) —
this module never duplicates a body. Each resolved prompt version becomes one
procedural record (``attribute="prompt"``, ``entity=<prompt id>``) whose
content is a reference string, entering the ladder already ``active``: the
registry's resolved prompt IS the active version by construction (overrides
bump the version, D-43). The record trail is what lets ``audit taint`` (E1)
and the E3 cache key reason about which prompt version was live when.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.memories.procedural.lifecycle import SkillStage

__all__ = ["prompt_reference", "prompt_version_records"]


class _PromptLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def version(self) -> int: ...


class _RegistryLike(Protocol):
    def list(self) -> Sequence[_PromptLike]: ...

    def source_of(self, prompt_id: str) -> str: ...


def prompt_reference(prompt_id: str, version: int) -> str:
    """The reference-only content string (definition stays in prompts/)."""
    return f"prompt:{prompt_id}@v{version}"


def prompt_version_records(
    namespace: str,
    registry: _RegistryLike,
    existing: list[MemoryRecord],
) -> list[MemoryRecord]:
    """Records for prompt versions not yet stored — idempotent by (id, version).

    Pure function: the caller writes the returned records through the door.
    """
    seen = {
        (record.entity, record.version)
        for record in existing
        if record.attribute == "prompt"
    }
    fresh: list[MemoryRecord] = []
    for prompt in registry.list():
        if (prompt.id, prompt.version) in seen:
            continue
        fresh.append(
            MemoryRecord(
                namespace=namespace,
                memory_type="procedural",
                content=prompt_reference(prompt.id, prompt.version),
                entity=prompt.id,
                attribute="prompt",
                version=prompt.version,
                skill_stage=SkillStage.ACTIVE.value,
                status=RecordStatus.ACTIVATED,
                source=SourceInfo(
                    role="system",
                    channel="prompts",
                    prompt_version=str(prompt.version),
                    message_id=registry.source_of(prompt.id),
                ),
            )
        )
    return fresh
