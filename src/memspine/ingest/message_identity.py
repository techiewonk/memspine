"""Pure identity and timestamp resolution for message batches."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "MESSAGE_FIELDS",
    "PROVENANCE_FIELDS",
    "SENDER_FIELDS",
    "normalise_messages",
    "source_clock",
    "validate_message_fields",
]

_ROLES = {"user", "assistant", "system", "tool", "operator", "document", "external"}
_SENDER_KINDS = {"human", "agent", "service"}
_VISIBILITIES = {"private", "participants", "owner_shared"}

#: ADR-090 (DS-09): the strict message envelope. One spelling per field (ADR-068/086); any
#: other key is refused by name. Caller data that the engine must not interpret goes in
#: ``metadata``, stored verbatim and never read for identity, time or role.
MESSAGE_FIELDS: frozenset[str] = frozenset(
    {
        "message_id",
        "conversation_id",
        "session_id",
        "reply_to",
        "sender",
        "role",
        "content",
        "created_at",
        "updated_at",
        "tz",
        "addressee",
        "participants",
        "visibility",
        "caption",
        "attachments",
        "memory_type",
        "entity",
        "attribute",
        "value",
        "provenance",
        "metadata",
    }
)
SENDER_FIELDS: frozenset[str] = frozenset({"id", "kind", "name"})
#: ADR-090: the typed source-provenance axes a caller may declare (``source_provenance``
#: adapter). The firewall's trust tier is never caller input, and nothing here is read from
#: ``metadata``.
PROVENANCE_FIELDS: frozenset[str] = frozenset(
    {"origin", "assertion_owner_id", "asset_id", "explicit_fetch"}
)
#: Alternate spellings that are refused with the canonical field they duplicate.
_CANONICAL = {
    "timestamp": "created_at",
    "speaker": "sender.id or sender.name",
    "name": "sender.name",
    "timezone": "tz",
    "turn_id": "message_id",
}
_STRING_FIELDS = ("tz", "addressee", "caption", "memory_type", "entity", "attribute", "value")
_OFFSET = r"(?:[+-]\d{2}(?::?\d{2})?|Z)?"
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_HOUR = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}" + _OFFSET)
_MINUTE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}" + _OFFSET)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _time(value: object, field: str) -> datetime | str:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from None
        return value
    raise ValueError(f"{field} must be a datetime or ISO-8601 string")


def _text_precision(text: str) -> str:
    """What the source text states (TemporalRef ``text_precision``), not what was resolved."""
    t = text.strip()
    if _DATE_ONLY.fullmatch(t):
        return "day"
    if _HOUR.fullmatch(t):
        return "hour"
    if _MINUTE.fullmatch(t):
        return "minute"
    return "instant"


def source_clock(
    value: datetime | str, tz: str | None = None
) -> tuple[datetime | None, dict[str, Any]]:
    """ADR-090 (DS-08, D-100 Q-S2-B): resolve a source time only when it is aware or the message
    declares a zone (``tz``). A naive or date-only value without a declared zone keeps its source
    text with ``precision="unknown"`` and no resolved instant; no zone is ever guessed.

    Returns ``(resolved instant or None, SourceClock provenance)``."""
    if isinstance(value, datetime):
        text, parsed, text_precision = value.isoformat(), value, "instant"
    else:
        text, parsed = value, datetime.fromisoformat(value)
        text_precision = _text_precision(value)
    resolved: datetime | None = None
    origin = "unknown"
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        resolved, origin = parsed, "source"
    elif tz is not None:
        try:
            resolved, origin = parsed.replace(tzinfo=ZoneInfo(tz)), "declared"
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"tz {tz!r} is not a known IANA zone") from None
    clock = {
        "original_text": text,
        "text_precision": text_precision,
        "precision": text_precision if resolved is not None else "unknown",
        "timezone": tz if origin == "declared" else None,
        "timezone_origin": origin,
    }
    return resolved, clock


def validate_message_fields(raw: Mapping[str, Any], index: int) -> None:
    """ADR-090 (DS-09): refuse any key outside the strict envelope, naming it."""
    for key in raw:
        if not isinstance(key, str):
            raise ValueError(f"message {index} has a non-string field name {key!r}")
        if key in MESSAGE_FIELDS:
            continue
        if key in _CANONICAL:
            raise ValueError(
                f"message {index} field {key!r} is not accepted: use {_CANONICAL[key]!r}"
            )
        raise ValueError(
            f"message {index} has unknown field {key!r}; put caller data in 'metadata'"
        )
    for key in _STRING_FIELDS:
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"message {index} field {key!r} must be a non-empty string")
    visibility = raw.get("visibility")
    if visibility is not None and visibility not in _VISIBILITIES:
        raise ValueError(f"message {index} visibility must be one of {sorted(_VISIBILITIES)}")
    participants = raw.get("participants")
    if participants is not None and (
        not isinstance(participants, list | tuple)
        or any(not isinstance(p, str) or not p.strip() for p in participants)
    ):
        raise ValueError(f"message {index} participants must be a list of non-empty strings")
    sender = raw.get("sender")
    if isinstance(sender, Mapping):
        unknown = sorted(str(k) for k in sender if k not in SENDER_FIELDS)
        if unknown:
            raise ValueError(
                f"message {index} sender has unknown field {unknown[0]!r}; "
                "put caller data in 'metadata'"
            )
    _validate_provenance(raw.get("provenance"), index)


def _validate_provenance(provenance: object, index: int) -> None:
    """The typed ``provenance`` envelope field: known keys only, each of its type."""
    if provenance is None:
        return
    if not isinstance(provenance, Mapping):
        raise ValueError(f"message {index} provenance must be a mapping")
    unknown = sorted(str(k) for k in provenance if k not in PROVENANCE_FIELDS)
    if unknown:
        hint = (
            " (the trust tier is assessed by the firewall, never declared)"
            if unknown[0] == "trust_tier"
            else ""
        )
        raise ValueError(f"message {index} provenance has unknown field {unknown[0]!r}{hint}")
    for key in ("origin", "assertion_owner_id", "asset_id"):
        value = provenance.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"message {index} provenance.{key} must be a non-empty string")
    fetch = provenance.get("explicit_fetch")
    if fetch is not None and not isinstance(fetch, bool):
        raise ValueError(f"message {index} provenance.explicit_fetch must be a boolean")


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def normalise_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    namespace: str,
    session_id: str | None = None,
    conversation_id: str | None = None,
    existing: Iterable[Mapping[str, Any]] = (),
    existing_conversations: Mapping[str, str] | None = None,
    retry_ids: Collection[str] = (),
    now: datetime,
) -> list[dict[str, Any]]:
    """Resolve identity and timestamps without mutating inputs or touching storage.

    A missing session is allocated once per call. Every new root without an explicit conversation
    starts a new conversation; replies inherit the earlier message's session and conversation.
    """
    _identifier(namespace, "namespace")
    trusted_retry_ids = {_identifier(mid, "retry_id") for mid in retry_ids}
    if session_id is not None:
        _identifier(session_id, "session_id")
    if conversation_id is not None:
        _identifier(conversation_id, "conversation_id")
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")

    prior: dict[str, tuple[str | None, str | None]] = {}
    conversation_sessions: dict[str, str | None] = {}
    for known_conversation, known_session in (existing_conversations or {}).items():
        _identifier(known_conversation, "existing conversation_id")
        _identifier(known_session, "existing conversation session_id")
        conversation_sessions[known_conversation] = known_session
    for item in existing:
        if not isinstance(item, Mapping):
            raise ValueError("existing records must be mappings")
        mid = _identifier(item.get("message_id"), "existing.message_id")
        if mid in prior:
            raise ValueError(f"duplicate existing message_id {mid!r}")
        old_session_value, old_conversation_value = (
            item.get("session_id"),
            item.get("conversation_id"),
        )
        old_session = (
            _identifier(old_session_value, "existing.session_id")
            if old_session_value is not None
            else None
        )
        old_conversation = (
            _identifier(old_conversation_value, "existing.conversation_id")
            if old_conversation_value is not None
            else None
        )
        prior[mid] = (old_session, old_conversation)
        if old_conversation is not None:
            _identifier(old_conversation, "existing.conversation_id")
            _identifier(old_session, "existing.session_id")
            if (
                old_conversation in conversation_sessions
                and conversation_sessions[old_conversation] != old_session
            ):
                raise ValueError("existing conversation spans multiple sessions")
            conversation_sessions[old_conversation] = old_session

    generated_call_session = session_id or uuid4().hex
    staged: list[dict[str, Any]] = []
    batch_ids: set[str] = set()
    supplied_batch_ids = {
        _identifier(raw["message_id"], "message_id")
        for raw in messages
        if isinstance(raw, Mapping) and raw.get("message_id") is not None
    }
    for index, raw in enumerate(messages):
        if not isinstance(raw, Mapping):
            raise ValueError(f"message {index} must be a mapping")
        validate_message_fields(raw, index)
        item = dict(raw)
        if not isinstance(item.get("role"), str):
            raise ValueError(f"message {index} must have a string role")
        if item["role"] not in _ROLES:
            raise ValueError(f"unsupported role {item['role']!r}")
        if not isinstance(item.get("content"), str):
            raise ValueError(f"message {index} must have string content")
        metadata = item.get("metadata")
        if metadata is not None:
            if not isinstance(metadata, Mapping) or any(not isinstance(k, str) for k in metadata):
                raise ValueError("metadata must be a mapping with string keys")
            try:
                json.dumps(metadata, default=_json_default, allow_nan=False)
            except (TypeError, ValueError):
                raise ValueError("metadata must contain JSON-serializable values") from None
        raw_mid = item.get("message_id")
        new_id = uuid4().hex if raw_mid is None else _identifier(raw_mid, "message_id")
        if new_id in batch_ids or (new_id in prior and new_id not in trusted_retry_ids):
            raise ValueError(f"duplicate message_id {new_id!r}")
        batch_ids.add(new_id)

        reply_to = item.get("reply_to")
        if reply_to is not None:
            reply_to = _identifier(reply_to, "reply_to")
            if reply_to == new_id:
                raise ValueError(f"message {new_id!r} cannot reply to itself")
            parent = next((m for m in staged if m["message_id"] == reply_to), None)
            if parent is None and reply_to in supplied_batch_ids and reply_to not in prior:
                raise ValueError(f"message {new_id!r} has a forward reply_to reference")
            parent_session, parent_conversation = (
                (parent["session_id"], parent["conversation_id"])
                if parent is not None
                else prior.get(reply_to, (None, None))
            )
            if parent is None and reply_to not in prior:
                raise ValueError(f"message {new_id!r} replies to unresolved message {reply_to!r}")
        else:
            parent_session = parent_conversation = None

        msg_session = item.get("session_id")
        msg_conversation = item.get("conversation_id")
        for field, value in (("session_id", msg_session), ("conversation_id", msg_conversation)):
            if value is not None:
                _identifier(value, field)
        if session_id is not None and msg_session is not None and session_id != msg_session:
            raise ValueError("call and message session_id conflict")
        if (
            conversation_id is not None
            and msg_conversation is not None
            and conversation_id != msg_conversation
        ):
            raise ValueError("call and message conversation_id conflict")

        if reply_to is not None:
            # A missing session inherits the authorised stored parent.  An
            # explicitly different session is a cross-session reply: retain the
            # link but start a new conversation in that session.
            explicit_session = msg_session or session_id
            resolved_session = explicit_session or parent_session or generated_call_session
            cross_session = parent_session is not None and resolved_session != parent_session
            if cross_session:
                resolved_conversation = msg_conversation or conversation_id or uuid4().hex
                if parent_conversation is not None and resolved_conversation == parent_conversation:
                    raise ValueError("cross-session reply must start a new conversation_id")
            else:
                resolved_conversation = msg_conversation or conversation_id or parent_conversation
                if resolved_conversation is None:
                    raise ValueError("reply parent has no conversation_id")
                if parent_conversation is not None and resolved_conversation != parent_conversation:
                    raise ValueError("reply conversation_id differs from parent conversation")
        else:
            resolved_session = msg_session or generated_call_session
            resolved_conversation = msg_conversation or conversation_id or uuid4().hex

        # A supplied sender is additive metadata; sender fields are individually optional.
        sender = item.get("sender")
        if sender is not None:
            if not isinstance(sender, Mapping):
                raise ValueError("sender must be a mapping")
            sid = sender.get("id")
            kind = sender.get("kind")
            name = sender.get("name")
            if sid is not None:
                sid = _identifier(sid, "sender.id")
            if kind is not None:
                kind = _identifier(kind, "sender.kind")
                if kind not in _SENDER_KINDS:
                    raise ValueError(f"unsupported sender.kind {kind!r}")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValueError("sender.name must be a non-empty string")
            # Internal apply keys derived from the canonical sender (never caller input).
            speaker = sid or name
            if speaker is not None:
                item["speaker"] = speaker
            if name is not None:
                item["name"] = name

        if item.get("updated_at") is not None:
            item["updated_at"] = _time(item["updated_at"], "updated_at")

        if item.get("created_at") is not None:
            created = _time(item["created_at"], "created_at")
            resolved, clock = source_clock(created, item.get("tz"))
            generated = False
        else:
            created = now
            resolved, clock = source_clock(now)
            generated = True
        if resolved_conversation in conversation_sessions:
            if conversation_sessions[resolved_conversation] != resolved_session:
                raise ValueError("conversation_id is already associated with another session")
        else:
            conversation_sessions[resolved_conversation] = resolved_session
        item.update(
            message_id=new_id, session_id=resolved_session, conversation_id=resolved_conversation
        )
        item["created_at"] = created
        # Internal: the resolved instant, or None for civil source text (ADR-090).
        item["timestamp"] = resolved
        item["_source_clock"] = clock
        item["_created_at_generated"] = generated
        staged.append(item)
    return staged
