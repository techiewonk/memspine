"""The source binding of a claim or annotation: one hash over a record's source snapshot.

This is the binding hash GLiNER mentions already carry (D-98 / ADR-076,
``EntityMention.source_binding_hash``); ADR-088 puts the same hash on the claim
(``SemanticEnvelope.source_binding_hash``). It covers the record id, version, the
fingerprint of the *current* text and the canonical message envelope identity
(role, channel, message id, sender, creation time, parents), so a claim or mention
bound to one version never silently rebinds to edited text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from memspine.store.events import fingerprint_payload
from memspine.store.records import MemoryRecord

__all__ = ["SourceBinding", "source_binding"]


@dataclass(frozen=True)
class SourceBinding:
    """The authoritative source snapshot fields and their binding hash."""

    content_fingerprint: str
    message_id: str | None
    sender_id: str | None
    sender_role: str | None
    created_at: str | None
    binding_hash: str


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def source_binding(record: MemoryRecord) -> SourceBinding:
    """The binding of ``record`` as it is now (its text, version and message envelope)."""
    message = record.source.message if isinstance(record.source.message, dict) else {}
    sender = message.get("sender")
    sender_id = _text(sender.get("id")) if isinstance(sender, dict) else None
    sender_role = _text(message.get("role")) or record.source.role
    message_id = _text(message.get("message_id"))
    created_value = message.get("created_at", message.get("timestamp"))
    if isinstance(created_value, datetime):
        created_at: str | None = created_value.isoformat()
    else:
        created_at = _text(created_value)
    # Bind the actual current source text even if a caller used model_copy(update=)
    # and carried forward an older, non-empty record.content_fingerprint.
    content_fingerprint = fingerprint_payload({"content": record.content})
    identity = {
        "namespace": record.namespace,
        "record_id": record.record_id,
        "version": record.version,
        "content_fingerprint": content_fingerprint,
        "source_role": record.source.role,
        "source_channel": record.source.channel,
        "source_message_id": message_id,
        "sender_id": sender_id,
        "sender_role": sender_role,
        "created_at": created_at,
        "parents": list(record.source.parents),
    }
    return SourceBinding(
        content_fingerprint=content_fingerprint,
        message_id=message_id,
        sender_id=sender_id,
        sender_role=sender_role,
        created_at=created_at,
        binding_hash=fingerprint_payload(identity),
    )
