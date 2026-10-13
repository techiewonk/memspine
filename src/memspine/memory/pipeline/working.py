"""Memory: working moved out of the engine (F-12)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memspine.memories.working.persona import make_persona_record
from memspine.observability.logging import EVENT_WRITE, get_logger
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import ArchivedVersion, MemoryRecord, RecordStatus
from memspine.store.scope import validate_namespace

_log = get_logger(__name__)


async def set_persona(eng: Any, namespace: str, text: str) -> MemoryRecord:
    """Pin the persona block (M13.1): first token of the E2 stable prefix.

    Updating supersedes in place: the persona keeps ONE stable record_id
    per namespace (version bumped, prior text archived to history) so the
    stable prefix stays stable instead of accumulating contradictory
    personas that paging can never evict.
    """
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    existing = [
        record
        for record in await storage.list_records(ns, "working")
        if record.source.channel == "persona" and record.status is not RecordStatus.DELETED
    ]
    if existing:
        existing.sort(key=lambda record: record.recorded_at)
        prior = existing[0]
        record = prior.model_copy(
            update={
                "content": text,
                "content_fingerprint": fingerprint_payload({"content": text}),
                "version": prior.version + 1,
                "history": [
                    *prior.history,
                    ArchivedVersion(
                        version=prior.version,
                        content=prior.content,
                        archived_at=datetime.now(UTC),
                        reason="persona_update",
                    ),
                ],
            }
        )
    else:
        record = make_persona_record(ns, text)
    event = MemoryEvent(
        kind=EventKind.WRITE,
        namespace=ns,
        actor="system",
        payload={"record": record.model_dump(mode="json")},
    )
    await eng._append_and_project(event)
    _log.info(EVENT_WRITE, namespace=ns, record_id=record.record_id, persona=True)
    return record
