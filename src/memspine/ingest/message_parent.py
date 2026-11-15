"""Live authorization of a stored source message used as a reply parent."""

from __future__ import annotations

from typing import Any

from memspine.store.records import RecordStatus
from memspine.trust.visibility import visible_to


class ReplyParentUnavailable(ValueError):
    """A source reply parent is no longer available to the caller."""


async def require_stored_parent(
    storage: Any,
    namespace: str,
    message_id: str,
    record_id: str | None,
    actor: str,
    *,
    session_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    parent = await storage.get_record(record_id) if record_id else None
    source = parent.source.message if parent is not None else None
    if (
        parent is None
        or parent.namespace != namespace
        or parent.status is not RecordStatus.ACTIVATED
        or parent.quarantined
        or not source
        or source.get("message_id") != message_id
        or (session_id is not None and source.get("session_id") != session_id)
        or (conversation_id is not None and source.get("conversation_id") != conversation_id)
        or not visible_to(parent, actor)
    ):
        raise ReplyParentUnavailable("reply parent is unavailable")
