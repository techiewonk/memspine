"""Pure record identities touched by a canonical event, for append and projection guards."""

from __future__ import annotations

from collections.abc import Mapping

from memspine.store.events import EventKind, MemoryEvent


def _valid_ids(value: object) -> bool:
    return isinstance(value, (list, tuple)) and all(
        isinstance(rid, str) and bool(rid.strip()) for rid in value
    )


def validate_record_event_shape(event: MemoryEvent) -> None:
    """Reject malformed record mutation identities before canonical append.

    Audit-only feedback and unrelated markers retain their existing payload
    contract. Native stores call this before extracting affected IDs, while the
    projector still validates the full WRITE snapshot when it folds the log.
    """
    payload = event.payload
    if event.kind is EventKind.WRITE:
        snapshot = payload.get("record")
        if (
            not isinstance(snapshot, Mapping)
            or not isinstance(snapshot.get("record_id"), str)
            or not str(snapshot["record_id"]).strip()
            or snapshot.get("namespace") != event.namespace
        ):
            raise ValueError("WRITE requires a record ID in its event namespace")
    elif event.kind in {EventKind.FORGET, EventKind.DECAY_TRANSITION}:
        rid = payload.get("record_id")
        if not isinstance(rid, str) or not rid.strip():
            raise ValueError("record transition requires a nonempty record_id")
    elif event.kind is EventKind.FEEDBACK:
        receipt = payload.get("feedback")
        if (
            event.actor == "system"
            and isinstance(receipt, Mapping)
            and receipt.get("channel") == "outcome_receipt"
        ):
            rid = payload.get("record_id")
            if not isinstance(rid, str) or not rid.strip():
                raise ValueError("outcome receipt requires a nonempty record_id")
    elif event.kind in {EventKind.RETRIEVE, EventKind.SESSION}:
        if "record_ids" in payload and not _valid_ids(payload["record_ids"]):
            raise ValueError("record_ids must be an array of nonempty IDs")
    elif event.kind is EventKind.MARKER:
        marker = payload.get("marker")
        if marker == "erasure_manifest":
            manifest = payload.get("manifest")
            if manifest is not None and not isinstance(manifest, Mapping):
                raise ValueError("erasure manifest must be an object")
            ids = manifest.get("ids") if isinstance(manifest, Mapping) else None
            if ids is not None and not isinstance(ids, Mapping):
                raise ValueError("erasure manifest IDs must be an object")
            if (
                isinstance(ids, Mapping)
                and "record_ids" in ids
                and not _valid_ids(ids["record_ids"])
            ):
                raise ValueError("erasure manifest record_ids must be an array")
            if "hidden" in payload and not _valid_ids(payload["hidden"]):
                raise ValueError("erasure manifest hidden IDs must be an array")
        elif (
            marker == "erasure_release"
            and "record_ids" in payload
            and not _valid_ids(payload["record_ids"])
        ):
            raise ValueError("erasure release requires a record_ids array")


def affected_record_ids(event: MemoryEvent) -> tuple[str, ...]:
    payload = event.payload
    if event.kind is EventKind.WRITE:
        snapshot = payload.get("record")
        if isinstance(snapshot, Mapping) and isinstance(snapshot.get("record_id"), str):
            return (str(snapshot["record_id"]),)
    if event.kind in {EventKind.DECAY_TRANSITION, EventKind.FORGET, EventKind.FEEDBACK}:
        rid = payload.get("record_id")
        return (str(rid),) if rid is not None else ()
    if event.kind in {EventKind.RETRIEVE, EventKind.SESSION}:
        return tuple(str(rid) for rid in payload.get("record_ids", ()))
    if event.kind is EventKind.MARKER:
        marker = payload.get("marker")
        if marker == "erasure_manifest":
            manifest = payload.get("manifest") or {}
            ids = manifest.get("ids") or {}
            return tuple(
                dict.fromkeys(
                    [
                        *(str(rid) for rid in ids.get("record_ids") or ()),
                        *(str(rid) for rid in payload.get("hidden") or ()),
                    ]
                )
            )
        if marker == "erasure_release":
            return tuple(str(rid) for rid in payload.get("record_ids") or ())
    return ()
