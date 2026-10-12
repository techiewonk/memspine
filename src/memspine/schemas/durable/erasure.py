# ruff: noqa: E501
"""Erasure and retention (schema session 11): manifest without content, retained-history boundary."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import AwareDatetime, Field, model_validator

from memspine.schemas.base import DurableModel, DurablePart
from memspine.schemas.durable.common import OwnerScope

__all__ = ["CONTENT_KEYS", "ErasedIds", "ErasureManifest", "RetentionBoundary"]

# A manifest holds ids and the generation only. These keys are refused anywhere in it (extras included).
CONTENT_KEYS: frozenset[str] = frozenset(
    {"content", "text", "payload", "value", "snippet", "quote", "body", "summary", "note"}
)


class ErasedIds(DurablePart):
    record_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.record_ids or self.event_ids or self.source_ids or self.entity_ids)


def _content_keys(node: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in CONTENT_KEYS:
                hits.append(child)
            hits.extend(_content_keys(value, child))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits.extend(_content_keys(value, f"{path}[{i}]"))
    return hits


class ErasureManifest(DurableModel):
    """What was erased: ids and the erasure generation, never content (ER-30).

    The erased data is logically invisible at once. A tracked purge job removes projections, vectors,
    graph and caches physically; restoring a backup re-applies this manifest.
    """

    __kind__: ClassVar[str | None] = "erasure_manifest"

    manifest_id: str = Field(min_length=1)
    scope: OwnerScope
    erasure_generation: int = Field(ge=1)
    erased_at: AwareDatetime
    requested_by: str | None = None
    reason_code: Literal["user_request", "retention", "admin", "legal"] = "user_request"
    ids: ErasedIds
    purge_job_id: str | None = None
    purge_state: Literal["pending", "running", "done", "failed"] = "pending"

    @model_validator(mode="after")
    def _no_content(self) -> ErasureManifest:
        if self.ids.is_empty():
            raise ValueError("an erasure manifest must name at least one id")
        extra = self.model_extra or {}
        hits = _content_keys(extra) + [
            f"ids.{h}" for h in _content_keys(self.ids.model_extra or {})
        ]
        if hits:
            raise ValueError(f"an erasure manifest holds no content; found {hits}")
        return self


class RetentionBoundary(DurableModel):
    """The retained-history boundary (ER-27): what a rebuild can still do under the event-log mode.

    Under rolling retention, lineage and tombstones are kept, and the store reports a limited rebuild.
    """

    __kind__: ClassVar[str | None] = "retention_boundary"

    scope: OwnerScope
    mode: Literal["full", "rolling", "ephemeral"]
    boundary_seq: int | None = Field(default=None, ge=0)  # events at or before this seq were pruned
    boundary_ts: AwareDatetime | None = None
    rebuild: Literal["full", "limited", "unavailable"]
    lineage_kept: bool = True
    tombstones_kept: bool = True

    @model_validator(mode="after")
    def _mode(self) -> RetentionBoundary:
        expected = {"full": "full", "rolling": "limited", "ephemeral": "unavailable"}[self.mode]
        if self.rebuild != expected:
            raise ValueError(
                f"mode {self.mode!r} implies rebuild {expected!r}, got {self.rebuild!r}"
            )
        if self.mode == "full" and (self.boundary_seq is not None or self.boundary_ts is not None):
            raise ValueError("full retention has no boundary")
        if self.mode == "rolling" and self.boundary_seq is None and self.boundary_ts is None:
            raise ValueError("rolling retention needs a boundary")
        if self.mode == "rolling" and not (self.lineage_kept and self.tombstones_kept):
            raise ValueError("rolling retention keeps lineage and tombstones")
        return self
