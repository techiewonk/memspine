"""Rebuildable lookup fields projected from a canonical claim envelope (RM-45)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def claim_subject_token(raw: Mapping[str, Any] | None) -> str | None:
    """Keep opaque ids separate from surface names in the records read model."""
    if not isinstance(raw, Mapping):
        return None
    subject = raw.get("subject")
    if not isinstance(subject, Mapping):
        return None
    entity_id = subject.get("entity_id")
    if isinstance(entity_id, str) and entity_id:
        return f"id:{entity_id}"
    surface = subject.get("surface")
    if not isinstance(surface, str):
        return None
    token = surface.strip().casefold()
    return f"surface:{token}" if token else None


def claim_predicate(raw: Mapping[str, Any] | None) -> str | None:
    """A claim's indexed predicate is its pinned registry predicate, if present."""
    if not isinstance(raw, Mapping):
        return None
    registry = raw.get("registry")
    if not isinstance(registry, Mapping):
        return None
    predicate = registry.get("predicate")
    return predicate if isinstance(predicate, str) and predicate else None


def claim_cardinality_flag(raw: Mapping[str, Any] | None) -> bool:
    return bool(raw.get("cardinality_conflict") is True) if isinstance(raw, Mapping) else False
