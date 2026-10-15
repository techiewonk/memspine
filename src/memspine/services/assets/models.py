"""Attachment identity: what a turn referenced, kept apart from what was learned about it."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["ASSET_TAG_PREFIX", "AssetEntry", "AssetRef", "asset_id_for", "parse_attachments"]

#: A record that carries an attachment is tagged ``asset:<asset_id>``.
ASSET_TAG_PREFIX = "asset:"


@dataclass(frozen=True, slots=True)
class AssetRef:
    """One attachment as the data source declared it. ``caption`` is the source's own
    machine caption; ``search_hint`` is a source-side search phrase and is NEVER evidence
    (it is stored for audit only and never reaches a reader)."""

    uri: str
    kind: str = "image"
    caption: str | None = None
    search_hint: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssetEntry:
    """Registry row: identity (immutable after ingest) plus what resolving it produced."""

    asset_id: str
    source_turn_id: str
    uri: str
    kind: str = "image"
    caption: str | None = None
    search_hint: str | None = None
    record_id: str | None = None
    #: unfetched | cached | unavailable (a failed download stays ``unavailable``, with ``error``)
    availability: str = "unfetched"
    content_hash: str | None = None
    mime: str | None = None
    size: int | None = None
    #: none | ok | skipped (no vision backend) | failed
    evidence_status: str = "none"
    evidence: str | None = None
    evidence_backend: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def provenance(self) -> str:
        """``asset:<hash>`` once the bytes are known, else ``asset:<asset_id>``."""
        return f"asset:{(self.content_hash or self.asset_id)[:16]}"


def asset_id_for(source_turn_id: str, uri: str) -> str:
    """Stable identity of one attachment of one turn (the same URI in two turns is two assets
    that share cached bytes through the content hash)."""
    return hashlib.sha256(f"{source_turn_id}\x00{uri}".encode()).hexdigest()[:16]


def parse_attachments(raw: Any) -> list[AssetRef]:
    """Attachments of a message: a list of mappings with ``uri`` (or ``url``). Anything else
    is skipped; a mapping without a usable ``uri`` is skipped, not repaired."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[AssetRef] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        uri = item.get("uri") or item.get("url")
        if not isinstance(uri, str) or not uri.strip():
            continue
        caption = item.get("caption")
        hint = item.get("search_hint")
        out.append(
            AssetRef(
                uri=uri.strip(),
                kind=str(item.get("kind") or "image"),
                caption=caption if isinstance(caption, str) and caption.strip() else None,
                search_hint=hint if isinstance(hint, str) and hint.strip() else None,
                meta={
                    k: v
                    for k, v in item.items()
                    if k not in ("uri", "url", "kind", "caption", "search_hint")
                },
            )
        )
    return out
