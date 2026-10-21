"""Parse GLiNER entity output into source-groundable, unasserted mentions.

This module has no GLiNER dependency. It validates the installed SDK's public
formatted span shape and binds each result to an exact character range in the
caller-supplied source text.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memspine.store.events import fingerprint_payload
from memspine.store.records import EntityMention, MemoryRecord

__all__ = [
    "GlinerMentionError",
    "MentionSpan",
    "bind_gliner_mentions",
    "parse_gliner_mentions",
]


class GlinerMentionError(ValueError):
    """GLiNER output cannot be safely grounded to the supplied source text."""


class MentionSpan(BaseModel):
    """One exact, typed source mention; it is never an asserted fact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str = Field(min_length=1)
    label: str = Field(min_length=1)
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    confidence: float | None = None
    asserted: Literal[False] = False

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a finite number or None")
        score = float(value)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("confidence must be finite and between zero and one")
        return score

    @field_validator("text", "label")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mention text and label must not be blank")
        return value

    @field_validator("end")
    @classmethod
    def _positive_end(cls, value: int, info: Any) -> int:
        start = info.data.get("start")
        if start is not None and value <= start:
            raise ValueError("mention end must be greater than start")
        return value


def _confidence(value: Any, *, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GlinerMentionError(f"{label} confidence must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise GlinerMentionError(f"{label} confidence must be finite and between zero and one")
    return result


def _unique_occurrence(text: str, quote: str) -> tuple[int, int]:
    first = text.find(quote)
    if first < 0:
        raise GlinerMentionError("mention text does not occur in the supplied source")
    if text.find(quote, first + 1) >= 0:
        raise GlinerMentionError("mention without offsets is ambiguous in the supplied source")
    return first, first + len(quote)


def _map_span(text: str, label: str, raw_span: Any) -> MentionSpan:
    if isinstance(raw_span, str):
        quote = raw_span
        confidence = None
        start, end = _unique_occurrence(text, quote)
    elif isinstance(raw_span, Mapping):
        extra = set(raw_span) - {"text", "start", "end", "confidence"}
        if extra:
            raise GlinerMentionError(f"unexpected span fields for {label}: {sorted(extra)}")
        raw_text = raw_span.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise GlinerMentionError(f"{label} span text must be a non-empty string")
        quote = raw_text
        confidence = _confidence(raw_span.get("confidence"), label=label)
        has_start, has_end = "start" in raw_span, "end" in raw_span
        if has_start != has_end:
            raise GlinerMentionError(f"{label} span must provide both start and end offsets")
        if has_start:
            start, end = raw_span["start"], raw_span["end"]
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
            ):
                raise GlinerMentionError(f"{label} offsets must be integer character offsets")
        else:
            start, end = _unique_occurrence(text, quote)
    else:
        raise GlinerMentionError(f"{label} span must be a string or span mapping")

    if not (0 <= start < end <= len(text)):
        raise GlinerMentionError(f"{label} offsets fall outside the supplied source")
    if text[start:end] != quote:
        raise GlinerMentionError(f"{label} quote does not exactly match its source offsets")
    return MentionSpan(
        text=quote,
        label=label,
        start=start,
        end=end,
        confidence=confidence,
        asserted=False,
    )


def parse_gliner_mentions(
    text: str, raw: Any, allowed_labels: Iterable[str]
) -> tuple[MentionSpan, ...]:
    """Validate an SDK result and map every supported entity span in source order.

    The SDK may return either ``{"entities": {label: [spans]}}`` or the bare
    type-to-spans mapping. Legacy strings are grounded only when they occur
    exactly once; no ambiguous repeated mention is assigned a guessed offset.
    Confidence remains unknown when the SDK did not return it.
    """
    if not isinstance(text, str) or not text:
        raise GlinerMentionError("source text must be a non-empty string")
    if isinstance(allowed_labels, str | bytes):
        raise GlinerMentionError("allowed labels must be a collection, not a string")
    labels = tuple(allowed_labels)
    if not labels or any(not isinstance(label, str) or not label.strip() for label in labels):
        raise GlinerMentionError("allowed labels must be non-empty strings")
    if len(labels) != len(set(labels)):
        raise GlinerMentionError("allowed labels must be unique")
    if not isinstance(raw, Mapping):
        raise GlinerMentionError("GLiNER result must be a mapping")
    if any(not isinstance(key, str) for key in raw):
        raise GlinerMentionError("GLiNER result keys must be strings")

    if "entities" in raw:
        if set(raw) != {"entities"}:
            raise GlinerMentionError(
                f"unexpected top-level GLiNER result keys: {sorted(set(raw) - {'entities'})}"
            )
        typed = raw["entities"]
    else:
        typed = raw
    if not isinstance(typed, Mapping):
        raise GlinerMentionError("entities must be a label-to-spans mapping")
    if any(not isinstance(key, str) for key in typed):
        raise GlinerMentionError("entity labels must be strings")

    extras = set(typed) - set(labels)
    if extras:
        raise GlinerMentionError(f"unsupported entity labels: {sorted(extras)}")

    mentions: list[MentionSpan] = []
    for label in labels:
        spans = typed.get(label, [])
        if spans is None or spans == "":
            continue
        if not isinstance(spans, list):
            raise GlinerMentionError(f"{label} mentions must be a list")
        mentions.extend(_map_span(text, label, span) for span in spans)
    return tuple(sorted(mentions, key=lambda mention: (mention.start, mention.end, mention.label)))


def bind_gliner_mentions(
    record: MemoryRecord, mentions: Iterable[MentionSpan]
) -> tuple[EntityMention, ...]:
    """Bind exact mentions to one authoritative record/source envelope.

    Callers must first map each mention to the single source record whose text
    it quotes. A merged or concatenated output has no implicit source owner.
    """
    message = record.source.message or {}
    sender = message.get("sender") if isinstance(message, dict) else None
    sender = sender if isinstance(sender, dict) else {}
    sender_value = sender.get("id")
    sender_id = sender_value if isinstance(sender_value, str) and sender_value.strip() else None
    sender_role = message.get("role") if isinstance(message, dict) else None
    if not isinstance(sender_role, str) or not sender_role.strip():
        sender_role = record.source.role
    message_id = message.get("message_id") if isinstance(message, dict) else None
    if not isinstance(message_id, str) or not message_id.strip():
        message_id = None
    created_at_value = (
        message.get("created_at", message.get("timestamp")) if isinstance(message, dict) else None
    )
    if isinstance(created_at_value, datetime):
        created_at = created_at_value.isoformat()
    elif isinstance(created_at_value, str) and created_at_value.strip():
        created_at = created_at_value
    else:
        created_at = None

    # Bind the actual current source text even if a caller used model_copy(update=)
    # and carried forward an older, non-empty record.content_fingerprint.
    source_content_fingerprint = fingerprint_payload({"content": record.content})
    source_identity = {
        "namespace": record.namespace,
        "record_id": record.record_id,
        "version": record.version,
        "content_fingerprint": source_content_fingerprint,
        "source_role": record.source.role,
        "source_channel": record.source.channel,
        "source_message_id": message_id,
        "sender_id": sender_id,
        "sender_role": sender_role,
        "created_at": created_at,
        "parents": list(record.source.parents),
    }
    source_binding_hash = fingerprint_payload(source_identity)
    bound: list[EntityMention] = []
    for mention in mentions:
        start, end = mention.start, mention.end
        if not (0 <= start < end <= len(record.content)):
            raise GlinerMentionError("GLiNER mention offsets fall outside the source record")
        if record.content[start:end] != mention.text:
            raise GlinerMentionError("GLiNER mention quote does not match the source record")
        bound.append(
            EntityMention(
                text=mention.text,
                label=mention.label,
                start=start,
                end=end,
                confidence=mention.confidence,
                asserted=False,
                source_namespace=record.namespace,
                source_record_id=record.record_id,
                source_version=record.version,
                source_content_fingerprint=source_content_fingerprint,
                source_binding_hash=source_binding_hash,
                source_role=record.source.role,
                source_channel=record.source.channel,
                source_message_id=message_id,
                sender_id=sender_id,
                sender_role=sender_role,
                created_at=created_at,
                parents=tuple(record.source.parents),
            )
        )
    return tuple(bound)
