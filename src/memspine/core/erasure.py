"""M7 erasure primitives: find and scrub a record's identifying data anywhere in
an event payload — not just the top-level ``{"record": ...}`` snapshot.

Identifying data is more than the content: the fact key (entity, attribute),
the tags, the archived versions in ``history``, the content fingerprint and the
dedup sketches all point back at the subject (#2).

The conflict ladder embeds full snapshots under ``incoming_record``, dedup
merges under ``dropped_record``, and future event kinds may nest them deeper.
Erasure (``redact_record``) and its proof (``retained_fields``) MUST
walk the same structure, or a hard delete can report success while the content
survives under a key the redactor never looked at. One walker, used by both.
"""

from __future__ import annotations

from typing import Any

__all__ = ["redact_record", "retained_fields"]

#: Fields on a record snapshot that identify the subject, with their erased value:
#: the content (plain and cold-tier), its fingerprint (an xxhash of short content
#: is reversible by guessing), the fact key, the tags (cues and labels are derived
#: from the content), the archived versions, and the dedup sketches of the content.
_SNAPSHOT_SCRUB: dict[str, object] = {
    "content": "",
    "content_zstd": None,
    "content_fingerprint": "",
    "entity": None,
    "attribute": None,
    "tags": [],
    "history": [],
    "simhash": None,
    "minhash_sig": None,
}
#: Fields a lifecycle delta (``{"record_id", "set": {...}}``) may carry that
#: identify the subject: the cold-tier compress pair and add-only tag patches.
_DELTA_SCRUB: dict[str, object] = {
    "content": "",
    "content_zstd": None,
    "tags_add": [],
    "tags": [],
}
_Carrier = tuple[dict[str, Any], dict[str, object]]


def _carriers(node: dict[str, Any], record_id: str) -> list[_Carrier]:
    """The dicts in ``node`` that hold ``record_id``'s identifying data, each with
    the scrub map that applies to it, if ``node`` is one of the two carrier shapes:

    - a full snapshot: ``{record_id, content/content_fingerprint/namespace, ...}``
      (WRITE ``record``, CONFLICT ``incoming_record``, MERGE ``dropped_record``),
    - a lifecycle delta: ``{record_id, "set": {...}}`` (the cold-tier compress
      DECAY_TRANSITION puts the plaintext zstd'd in ``set``; tag patches add tags).
    """
    if node.get("record_id") != record_id:
        return []
    found: list[_Carrier] = []
    if any(field in node for field in ("content", "content_fingerprint", "namespace")):
        found.append((node, _SNAPSHOT_SCRUB))
    delta = node.get("set")
    if isinstance(delta, dict) and any(field in delta for field in _DELTA_SCRUB):
        found.append((delta, _DELTA_SCRUB))
    return found


def _identifying(carrier: dict[str, Any], scrub: dict[str, object]) -> list[str]:
    """The fields of ``carrier`` that still hold identifying data."""
    return [field for field in scrub if carrier.get(field)]


def _scrub(carrier: dict[str, Any], scrub: dict[str, object]) -> bool:
    fields = _identifying(carrier, scrub)
    for field in fields:
        carrier[field] = scrub[field]
    return bool(fields)


def _merge_carriers(node: dict[str, Any], record_id: str) -> list[_Carrier]:
    """A MERGE event's ``dropped_record`` when ``record_id`` is the kept record: a
    dedup merge means the dropped duplicate's content *is* the survivor's, so
    erasing the survivor must erase the absorbed copy too."""
    dropped = node.get("dropped_record")
    if node.get("kept_record_id") == record_id and isinstance(dropped, dict):
        return [(dropped, _SNAPSHOT_SCRUB)]
    return []


def redact_record(node: Any, record_id: str) -> bool:
    """Recursively erase every identifying field of every snapshot/delta of
    ``record_id`` (see ``_SNAPSHOT_SCRUB``). Returns True if anything was
    redacted. Mutates ``node`` in place. A MERGE event's absorbed duplicate of
    ``record_id`` is erased with it."""
    changed = False
    if isinstance(node, dict):
        for carrier, scrub in [*_carriers(node, record_id), *_merge_carriers(node, record_id)]:
            changed |= _scrub(carrier, scrub)
        for value in node.values():
            changed |= redact_record(value, record_id)
    elif isinstance(node, list):
        for item in node:
            changed |= redact_record(item, record_id)
    return changed


def retained_fields(node: Any, record_id: str) -> set[str]:
    """The identifying fields of ``record_id`` still present anywhere in ``node``.

    Mirrors ``redact_record`` exactly, so the erasure proof cannot share a blind
    spot with the erasure; empty means erased."""
    found: set[str] = set()
    if isinstance(node, dict):
        for carrier, scrub in [*_carriers(node, record_id), *_merge_carriers(node, record_id)]:
            found.update(_identifying(carrier, scrub))
        for value in node.values():
            found |= retained_fields(value, record_id)
    elif isinstance(node, list):
        for item in node:
            found |= retained_fields(item, record_id)
    return found
