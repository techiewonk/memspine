"""Canonical adapters from accepted messages/tool records to semantic provenance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from memspine.semantics.origins import SOURCE_ORIGINS, SourceProvenance

__all__ = ["message_provenance", "structured_provenance", "tool_provenance"]


def _sender_id(message: Mapping[str, Any]) -> str | None:
    sender = message.get("sender")
    value = sender.get("id") if isinstance(sender, Mapping) else None
    return value if isinstance(value, str) and value else None


def message_provenance(message: Mapping[str, Any], *, source_id: str) -> SourceProvenance:
    """Adapt one canonical message without inferring trust or assertion ownership.

    ADR-090: the axes come from the typed ``provenance`` envelope field (origin, assertion
    owner, asset, explicit fetch), never from the caller's ``metadata`` bag, which is stored
    verbatim and never interpreted. The trust tier is the firewall's assessment, never declared
    by the caller, so it is always None here."""
    declared = message.get("provenance")
    axes = declared if isinstance(declared, Mapping) else {}
    origin: Any = axes.get("origin") or "raw_message"
    if origin not in SOURCE_ORIGINS:
        raise ValueError(f"unknown source_origin {origin!r}")
    return SourceProvenance(
        origin=origin,
        source_id=source_id,
        sender_id=_sender_id(message),
        assertion_owner_id=axes.get("assertion_owner_id"),
        asset_id=axes.get("asset_id"),
        explicit_fetch=bool(axes.get("explicit_fetch", False)),
    )


def tool_provenance(
    *, source_id: str, sender_id: str | None = None, assertion_owner_id: str | None = None
) -> SourceProvenance:
    """Tool output is attributable system evidence, never independent corroboration."""
    return SourceProvenance(
        origin="tool_output",
        source_id=source_id,
        sender_id=sender_id,
        assertion_owner_id=assertion_owner_id,
    )


def structured_provenance(
    *, source_id: str, sender_id: str | None = None, assertion_owner_id: str | None = None
) -> SourceProvenance:
    """Structured records retain their source and caller-supplied ownership axes."""
    return SourceProvenance(
        origin="structured_record",
        source_id=source_id,
        sender_id=sender_id,
        assertion_owner_id=assertion_owner_id,
    )
