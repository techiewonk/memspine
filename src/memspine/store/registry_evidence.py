"""Pure source-scoped redaction of resolver registry markers."""

from __future__ import annotations

from typing import Any

from memspine.store.events import EventKind

ENTITY_MARKERS = frozenset(
    {"entity.create", "entity.alias", "entity.pending", "entity.merge", "entity.split"}
)


def registry_control_marker(kind: EventKind, payload: dict[str, Any]) -> bool:
    """Registry mutations must use the guarded door, including text-free ones."""
    return kind is EventKind.MARKER and payload.get("marker") in ENTITY_MARKERS


def registry_marker_has_text(payload: dict[str, Any]) -> bool:
    """Fields whose copied words require pinned source evidence."""
    return any(payload.get(field) for field in ("name", "alias", "surface", "context"))


def registry_marker_has_unattributed_text(payload: dict[str, Any]) -> bool:
    """Known text fields that the marker kind cannot source or safely persist."""
    if payload.get("marker") in {"entity.create", "entity.alias"}:
        return bool(payload.get("reason") or payload.get("added_aliases"))
    return any(
        payload.get(field)
        for field in ("name", "alias", "surface", "context", "reason", "added_aliases")
    )


def valid_source_refs(payload: dict[str, Any]) -> bool:
    """Whether a marker carries nonempty, well-formed pinned raw source references."""
    source_ids = payload.get("source_ids")
    source_versions = payload.get("source_versions")
    return (
        isinstance(source_ids, list)
        and bool(source_ids)
        and all(isinstance(rid, str) and rid for rid in source_ids)
        and len(set(source_ids)) == len(source_ids)
        and isinstance(source_versions, list)
        and len(source_versions) == len(source_ids)
        and all(
            isinstance(version, int) and not isinstance(version, bool) and version >= 1
            for version in source_versions
        )
    )


def scrub_registry_marker(payload: dict[str, Any], erased: set[str]) -> bool:
    """Remove one conjunctively sourced mention from a logged registry marker."""
    marker = payload.get("marker")
    changed = False
    source_ids = payload.get("source_ids")
    attributed = valid_source_refs(payload)
    if marker not in ENTITY_MARKERS:
        return False
    if marker in {"entity.create", "entity.alias"} and (
        not attributed
        or bool(erased.intersection(source_ids if isinstance(source_ids, list) else ()))
    ):
        for field_name in ("name", "alias", "surface", "context"):
            if payload.get(field_name):
                payload[field_name] = ""
                changed = True
    # Older/crafted markers can carry text in another marker kind's fields. No
    # supported create/alias sources reason or added_aliases; control markers
    # source no free text. Merge aliases are derived from surviving entities.
    fields = (
        ("added_aliases", "reason")
        if marker in {"entity.create", "entity.alias"}
        else ("name", "alias", "surface", "context", "added_aliases", "reason")
    )
    for field_name in fields:
        if payload.get(field_name):
            payload[field_name] = [] if field_name == "added_aliases" else ""
            changed = True
    return changed
