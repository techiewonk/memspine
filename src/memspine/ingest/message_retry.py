"""Compare supplied source identities before canonical ingestion; never merge by text."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any

from memspine.store.events import canonical_payload
from memspine.store.records import MemoryRecord


def input_fingerprint(value: Any) -> str:
    """Compare source payloads without retaining another copy of private raw text."""
    try:
        return sha256(canonical_payload({"value": value})).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError("message values must be JSON serializable") from exc


def _equal_time(left: object, right: object) -> bool:
    try:
        a = datetime.fromisoformat(left) if isinstance(left, str) else left
        b = datetime.fromisoformat(right) if isinstance(right, str) else right
        return bool(a == b)
    except ValueError:
        return False


def prepare_retries(
    messages: Sequence[Mapping[str, Any]],
    stored: Sequence[MemoryRecord],
    session_id: str | None,
    conversation_id: str | None,
    identities: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, MemoryRecord | None]]:
    """Expand identical retries with their saved defaults; reject changes to source data."""
    by_id = {r.source.message["message_id"]: r for r in stored if r.source.message}
    inputs: list[dict[str, Any]] = []
    reused: dict[str, MemoryRecord | None] = {}
    for raw in messages:
        if not isinstance(raw, Mapping):
            raise ValueError("messages must be mappings")
        item = dict(raw)
        if any(str(k).startswith("_") for k in item):
            raise ValueError("private ingestion fields cannot be supplied")
        mid = item.get("message_id")
        if mid is not None and not isinstance(mid, str):
            raise ValueError("message_id must be a string")
        old = by_id.get(mid)
        identity = (identities or {}).get(mid) if mid is not None else None
        if old is None and identity is None:
            inputs.append(item)
            continue
        meta = old.source.message if old is not None else identity
        assert meta is not None
        if meta.get("erased"):
            raise ValueError("message_id belongs to an erased message and cannot be reused")
        expected = {
            **meta,
            "role": old.source.role if old else item.get("role"),
            "content": old.content if old else item.get("content"),
        }
        sender = meta.get("sender") or {}
        expected["speaker"] = (
            sender.get("id") or sender.get("name") or (old.axes.speaker if old else None)
        )
        expected["name"] = sender.get("name")
        expected["timestamp"] = meta["created_at"]
        for key, value in (("session_id", session_id), ("conversation_id", conversation_id)):
            if value is not None and value != meta.get(key):
                raise ValueError(f"retry {key} conflict for {item['message_id']!r}")
        for key, value in item.items():
            if key.startswith("_"):
                raise ValueError("private ingestion fields cannot be supplied")
            same = (
                _equal_time(value, expected.get(key))
                if key in {"created_at", "timestamp", "updated_at"}
                else value == expected.get(key)
            )
            original = meta.get("input_fingerprints", {})
            if key in original:
                # Raw records may be redacted or absent (excluded roles). The
                # accepted input hash, not that projection, defines a retry.
                same = input_fingerprint(value) == original[key] or (
                    key in {"created_at", "timestamp", "updated_at"} and same
                )
            if not same:
                raise ValueError(
                    f"message_id {item['message_id']!r} has changed {key}; explicit revision required"
                )
        inputs.append({**expected, **item})
        reused[str(item["message_id"])] = old
    return inputs, reused
