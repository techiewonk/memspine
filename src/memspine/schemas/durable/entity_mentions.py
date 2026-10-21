"""Shared strict representation of source-grounded GLiNER mentions."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EntityMention(BaseModel):
    """An unasserted entity span bound to its authoritative source snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str = Field(min_length=1)
    label: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    asserted: Literal[False] = False
    source_namespace: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    source_content_fingerprint: str = Field(min_length=16, max_length=16)
    source_binding_hash: str = Field(min_length=16, max_length=16)
    source_role: str = Field(min_length=1)
    source_channel: str = Field(min_length=1)
    source_message_id: str | None = None
    sender_id: str | None = None
    sender_role: str | None = None
    created_at: str | None = None
    parents: tuple[str, ...] = ()

    @field_validator("parents", mode="before")
    @classmethod
    def _accept_json_parent_array(cls, value: Any) -> Any:
        # JSON arrays decode to Python lists; convert just this representation
        # boundary while retaining strict element validation.
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _finite_confidence(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("mention confidence must be numeric")
        score = float(value)
        if not math.isfinite(score):
            raise ValueError("mention confidence must be finite")
        return score

    @model_validator(mode="after")
    def _validate_annotation(self) -> EntityMention:
        if self.end <= self.start:
            raise ValueError("mention end must be greater than start")
        for value, name in (
            (self.sender_id, "sender_id"),
            (self.sender_role, "sender_role"),
            (self.source_message_id, "source_message_id"),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be non-empty when supplied")
        return self


__all__ = ["EntityMention"]
