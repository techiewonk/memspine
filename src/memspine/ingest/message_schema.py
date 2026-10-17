"""Small structural helpers for the public message mapping contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Required, TypedDict

__all__ = [
    "MessageInput",
    "MessageRecord",
    "Sender",
    "ValidatedMessageBatch",
    "is_message_mapping",
]


class Sender(TypedDict, total=False):
    id: str
    kind: str
    name: str


class MessageInput(TypedDict, total=False):
    message_id: str
    conversation_id: str
    session_id: str
    reply_to: str
    sender: Sender
    role: str
    speaker: str
    name: str
    content: Any
    created_at: datetime | str
    updated_at: datetime | str
    timestamp: datetime | str
    metadata: dict[str, Any]


class MessageRecord(TypedDict, total=False):
    message_id: Required[str]
    conversation_id: Required[str]
    session_id: Required[str]
    created_at: Required[datetime | str]
    timestamp: Required[datetime | str]
    _created_at_generated: Required[bool]
    reply_to: str
    sender: Sender
    role: str
    speaker: str
    name: str
    content: Any
    updated_at: datetime | str
    metadata: dict[str, Any]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({deepcopy(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(v) for v in value)
    return deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {deepcopy(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    if isinstance(value, frozenset):
        return {_thaw(v) for v in value}
    return deepcopy(value)


@dataclass(frozen=True, init=False)
class ValidatedMessageBatch:
    """Immutable snapshot of messages already resolved by ``normalise_messages``.

    Input mappings and nested JSON-like values are copied and frozen. ``to_messages`` always
    returns fresh dictionaries for gateway payload construction and application.
    """

    _messages: tuple[Mapping[str, Any], ...]

    def __init__(self, messages: Sequence[Mapping[str, Any]]) -> None:
        frozen: list[Mapping[str, Any]] = []
        for index, message in enumerate(messages):
            if not is_message_mapping(message):
                raise ValueError(f"message {index} must be a mapping with string keys")
            required = ("message_id", "session_id", "conversation_id", "role", "content")
            if any(not isinstance(message.get(key), str) for key in required):
                raise ValueError(f"message {index} is missing resolved identity or text fields")
            if not message["message_id"].strip() or not message["session_id"].strip():
                raise ValueError(f"message {index} has empty resolved identity fields")
            if not message["conversation_id"].strip():
                raise ValueError(f"message {index} has empty conversation_id")
            frozen.append(_freeze(message))
        object.__setattr__(self, "_messages", tuple(frozen))

    @classmethod
    def from_resolved(cls, messages: Sequence[Mapping[str, Any]]) -> ValidatedMessageBatch:
        return cls(messages)

    def to_messages(self) -> list[dict[str, Any]]:
        return [_thaw(message) for message in self._messages]

    def __len__(self) -> int:
        return len(self._messages)


def is_message_mapping(value: object) -> bool:
    """Return whether a value is a caller-owned message mapping."""
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)
