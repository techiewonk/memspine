"""Upcaster registry for durable kinds (S1-2, M-26).

``@upcaster(kind, from_version)`` registers a pure ``dict -> dict`` function taking a payload at
``from_version`` to ``from_version + 1``. ``upcast(kind, payload)`` applies the chain up to the latest
version. Unknown fields must survive (S1-3 condition 2): an upcaster may only drop keys it lists in
``consumes``; any other input key missing from its output is an error.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "DuplicateUpcasterError",
    "NewerThanKnownError",
    "UnknownKindError",
    "UpcastError",
    "UpcastGapError",
    "declare_kind",
    "latest_version",
    "registered_versions",
    "reset_registry",
    "upcast",
    "upcaster",
]

Payload = dict[str, Any]
UpcastFn = Callable[[Payload], Payload]


class UpcastError(ValueError):
    """Base class for upcasting failures."""


class DuplicateUpcasterError(UpcastError):
    """Two upcasters registered for the same (kind, from_version)."""


class UpcastGapError(UpcastError):
    """The chain has no upcaster for some version below the latest."""


class NewerThanKnownError(UpcastError):
    """The payload is newer than the code knows (written by a newer release)."""


class UnknownKindError(UpcastError):
    """No upcaster or model declares this kind."""


_UPCASTERS: dict[tuple[str, int], tuple[UpcastFn, frozenset[str]]] = {}
_DECLARED: dict[str, int] = {}


def declare_kind(kind: str, latest: int) -> None:
    """Declare the latest version a kind is written at (called for ``DurableModel`` subclasses)."""
    if latest < 1:
        raise ValueError(f"latest version must be >= 1, got {latest}")
    _DECLARED[kind] = max(latest, _DECLARED.get(kind, 0))


def upcaster(
    kind: str, from_version: int, *, consumes: tuple[str, ...] = ()
) -> Callable[[UpcastFn], UpcastFn]:
    """Register ``fn`` as the ``from_version -> from_version + 1`` upcaster for ``kind``."""
    if from_version < 1:
        raise ValueError("from_version must be >= 1")

    def register(fn: UpcastFn) -> UpcastFn:
        key = (kind, from_version)
        if key in _UPCASTERS:
            raise DuplicateUpcasterError(
                f"upcaster for {kind!r} v{from_version} already registered"
            )
        _UPCASTERS[key] = (fn, frozenset(consumes))
        return fn

    return register


def latest_version(kind: str) -> int:
    """Latest known version of ``kind`` (declared by a model or implied by registered upcasters)."""
    candidates = [v + 1 for (k, v) in _UPCASTERS if k == kind]
    if kind in _DECLARED:
        candidates.append(_DECLARED[kind])
    if not candidates:
        raise UnknownKindError(f"unknown durable kind {kind!r}")
    return max(candidates)


def registered_versions(kind: str) -> list[int]:
    """Sorted ``from_version`` values registered for ``kind``."""
    return sorted(v for (k, v) in _UPCASTERS if k == kind)


def reset_registry() -> None:
    """Clear all registrations (tests only)."""
    _UPCASTERS.clear()
    _DECLARED.clear()


def upcast(kind: str, payload: Mapping[str, Any]) -> Payload:
    """Return a copy of ``payload`` upcast to the latest version of ``kind``.

    The input is never mutated. Raises :class:`UpcastGapError`, :class:`NewerThanKnownError` or
    :class:`UnknownKindError`.
    """
    latest = latest_version(kind)
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise UpcastError(
            f"payload of kind {kind!r} lacks a valid integer schema_version: {version!r}"
        )
    if version > latest:
        raise NewerThanKnownError(
            f"payload of kind {kind!r} is at v{version}, code knows up to v{latest}"
        )
    current: Payload = copy.deepcopy(dict(payload))
    while version < latest:
        entry = _UPCASTERS.get((kind, version))
        if entry is None:
            raise UpcastGapError(f"no upcaster for {kind!r} v{version} -> v{version + 1}")
        fn, consumes = entry
        result = fn(copy.deepcopy(current))
        lost = [
            k for k in current if k not in result and k not in consumes and k != "schema_version"
        ]
        if lost:
            raise UpcastError(
                f"upcaster {kind!r} v{version} dropped fields {sorted(lost)} (list them in consumes=)"
            )
        result["schema_version"] = version + 1
        current = result
        version += 1
    return current
