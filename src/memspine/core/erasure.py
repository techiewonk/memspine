"""M7 erasure primitives: find and scrub a record's content anywhere in an
event payload — not just the top-level ``{"record": ...}`` snapshot.

The conflict ladder embeds full snapshots under ``incoming_record``, dedup
merges under ``dropped_record``, and future event kinds may nest them deeper.
Erasure (``redact_record``) and its proof (``payload_retains_content``) MUST
walk the same structure, or a hard delete can report success while the content
survives under a key the redactor never looked at. One walker, used by both.
"""

from __future__ import annotations

from typing import Any

__all__ = ["payload_retains_content", "redact_record"]

#: Fields on a record snapshot that carry its recoverable content.
_CONTENT_FIELDS = ("content", "content_zstd")


def _content_carriers(node: dict[str, Any], record_id: str) -> list[dict[str, Any]]:
    """The dicts that hold ``record_id``'s recoverable content, if this node is
    one of the two carrier shapes:

    - a full snapshot: ``{record_id, content/content_fingerprint/namespace, ...}``
      (WRITE ``record``, CONFLICT ``incoming_record``, MERGE ``dropped_record``),
    - a lifecycle delta: ``{record_id, "set": {content/content_zstd}}`` (the
      cold-tier compress DECAY_TRANSITION — plaintext lives zstd'd in ``set``).
    """
    if node.get("record_id") != record_id:
        return []
    carriers: list[dict[str, Any]] = []
    if any(field in node for field in ("content", "content_fingerprint", "namespace")):
        carriers.append(node)
    delta = node.get("set")
    if isinstance(delta, dict) and any(field in delta for field in _CONTENT_FIELDS):
        carriers.append(delta)
    return carriers


def _scrub(carrier: dict[str, Any]) -> bool:
    changed = False
    for field in _CONTENT_FIELDS:
        if carrier.get(field):
            carrier[field] = "" if field == "content" else None
            changed = True
    return changed


def redact_record(node: Any, record_id: str) -> bool:
    """Recursively empty the content of every snapshot/delta of ``record_id``.
    Returns True if anything was redacted. Mutates ``node`` in place.

    Also scrubs a MERGE event's ``dropped_record`` when ``record_id`` is the
    surviving ``kept_record_id``: a dedup merge means the dropped duplicate's
    content *is* the survivor's, so erasing the survivor must erase the
    absorbed copy too (it has a different record_id but the same content)."""
    changed = False
    if isinstance(node, dict):
        for carrier in _content_carriers(node, record_id):
            changed |= _scrub(carrier)
        if node.get("kept_record_id") == record_id and isinstance(
            node.get("dropped_record"), dict
        ):
            changed |= _scrub(node["dropped_record"])
        for value in node.values():
            changed |= redact_record(value, record_id)
    elif isinstance(node, list):
        for item in node:
            changed |= redact_record(item, record_id)
    return changed


def payload_retains_content(node: Any, record_id: str) -> bool:
    """True if any snapshot/delta of ``record_id`` anywhere in ``node`` still
    carries recoverable content — the erasure-proof predicate. Mirrors
    ``redact_record`` exactly, including the MERGE-absorbed-duplicate case."""
    if isinstance(node, dict):
        if any(
            carrier.get(field)
            for carrier in _content_carriers(node, record_id)
            for field in _CONTENT_FIELDS
        ):
            return True
        dropped = node.get("dropped_record")
        if (
            node.get("kept_record_id") == record_id
            and isinstance(dropped, dict)
            and any(dropped.get(field) for field in _CONTENT_FIELDS)
        ):
            return True
        return any(payload_retains_content(value, record_id) for value in node.values())
    if isinstance(node, list):
        return any(payload_retains_content(item, record_id) for item in node)
    return False
