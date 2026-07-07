"""REST request/response models (D-06): thin pydantic mirrors of Engine verbs.

Records cross the wire as the universal :class:`MemoryRecord` shape (M1) —
the REST layer adds no second record schema.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from memspine.config import constants
from memspine.core.records import MemoryRecord, PiiTier, SourceInfo

__all__ = [
    "AssembleRequest",
    "AssembleResponse",
    "GrantRequest",
    "PlanRequest",
    "PromoteRequest",
    "ReflectRequest",
    "RetrieveRequest",
    "ScoredRecord",
    "SearchRequest",
    "SkillRequest",
    "WatchRequest",
    "WriteRequest",
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WriteRequest(_Request):
    content: str
    memory_type: str = "semantic"
    entity: str | None = None
    attribute: str | None = None
    actor: str = "user"
    pii_tier: PiiTier = PiiTier.NONE
    source: SourceInfo | None = None


class SearchRequest(_Request):
    query: str
    top_k: int = Field(default=constants.SEARCH_TOP_K, ge=1)


class AssembleRequest(_Request):
    query: str
    budget_tokens: int = Field(default=constants.ASSEMBLE_BUDGET_TOKENS, ge=1)
    top_k: int = Field(default=constants.ASSEMBLE_TOP_K, ge=1)


class RetrieveRequest(_Request):
    memory_type: str | None = None


class SkillRequest(_Request):
    content: str
    name: str
    actor: str = "user"


class PromoteRequest(_Request):
    dry_run_passed: bool = False


class PlanRequest(_Request):
    task: str
    content: str
    actor: str = "assistant"


class ReflectRequest(_Request):
    content: str
    source_record_ids: list[str]
    actor: str = "assistant"


class WatchRequest(_Request):
    content: str
    due_at: datetime | None = None
    entity: str | None = None
    attribute: str | None = None
    actor: str = "user"


class GrantRequest(_Request):
    to_namespace: str
    memory_types: list[str] | None = None
    actor: str = "user"


class ScoredRecord(BaseModel):
    record: MemoryRecord
    score: float


class AssembleResponse(BaseModel):
    """Wire mirror of :class:`~memspine.core.policies.assembly.AssembledContext`."""

    records: list[MemoryRecord]
    boundary_index: int
    abstained: bool
    tokens_used: int
