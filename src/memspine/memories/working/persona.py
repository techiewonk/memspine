"""Persona block: pinned working memory, first in the E2 stable prefix.

A persona is an ordinary working record whose ``source.channel == "persona"``:
paging never evicts it, and the assembly policy places it before everything
else (the most stable token in the prompt prefix).
"""

from __future__ import annotations

from memspine.core.records import MemoryRecord, ScoringState, SourceInfo

__all__ = ["is_persona", "make_persona_record"]

PERSONA_CHANNEL = "persona"


def make_persona_record(namespace: str, text: str) -> MemoryRecord:
    return MemoryRecord(
        namespace=namespace,
        memory_type="working",
        content=text,
        source=SourceInfo(role="system", channel=PERSONA_CHANNEL),
        scoring=ScoringState(importance=1.0),  # persona always survives scoring
    )


def is_persona(record: MemoryRecord) -> bool:
    return record.memory_type == "working" and record.source.channel == PERSONA_CHANNEL
