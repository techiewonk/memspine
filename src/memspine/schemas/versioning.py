"""Current durable schema declarations and strict payload validation.

There are no historical transformations or fallback formats. Payloads must state
exactly the schema version declared by the current model; future versions and
unsupported older versions fail explicitly. Unknown payload fields survive.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

__all__ = [
    "NewerThanKnownError",
    "SchemaValidationError",
    "UnknownKindError",
    "UnsupportedSchemaVersionError",
    "declare_kind",
    "latest_version",
    "reset_registry",
    "validate_payload",
]


class SchemaValidationError(ValueError):
    """A payload does not satisfy the current schema contract."""


class NewerThanKnownError(SchemaValidationError):
    """The payload was written with a schema newer than the current code."""


class UnsupportedSchemaVersionError(SchemaValidationError):
    """The payload uses an unsupported earlier schema."""


class UnknownKindError(SchemaValidationError):
    """No current model declares this durable kind."""


_DECLARED: dict[str, int] = {}


def declare_kind(kind: str, latest: int) -> None:
    """Declare the format written by the current model."""
    if isinstance(latest, bool) or not isinstance(latest, int) or latest < 1:
        raise ValueError("schema version must be a positive integer")
    _DECLARED[kind] = max(latest, _DECLARED.get(kind, 0))


def latest_version(kind: str) -> int:
    """The version of the current durable kind."""
    if kind not in _DECLARED:
        raise UnknownKindError(f"unknown durable kind {kind!r}")
    return _DECLARED[kind]


def reset_registry() -> None:
    """Clear declarations in isolated tests."""
    _DECLARED.clear()


def validate_payload(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the current format and return a deep copy without transformation."""
    latest = latest_version(kind)
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise SchemaValidationError(
            f"payload of kind {kind!r} lacks a valid integer schema_version: {version!r}"
        )
    if version > latest:
        raise NewerThanKnownError(
            f"payload of kind {kind!r} uses schema {version}; current schema is {latest}"
        )
    if version < latest:
        raise UnsupportedSchemaVersionError(
            f"payload of kind {kind!r} uses unsupported schema {version}; current schema is {latest}"
        )
    return copy.deepcopy(dict(payload))
