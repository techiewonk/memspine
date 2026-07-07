"""Hierarchical namespaces (M8.5) — the ONE enforcement point for scoping.

A namespace is a ``/``-separated path of lowercase segments, e.g.
``org/team-a/agent-7``. Grants land in Phase 4 (shared memory); Phase 0 fixes
the path grammar and containment rules every later phase relies on.
"""

from __future__ import annotations

import re

from memspine.exceptions import NamespaceError

__all__ = ["is_within", "parent", "validate_namespace"]

_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MAX_DEPTH = 16


def validate_namespace(path: str) -> str:
    """Normalize and validate a namespace path; returns the canonical form."""
    cleaned = path.strip().strip("/")
    if not cleaned:
        raise NamespaceError("namespace cannot be empty")
    segments = cleaned.split("/")
    if len(segments) > _MAX_DEPTH:
        raise NamespaceError(f"namespace deeper than {_MAX_DEPTH} segments: {path!r}")
    for segment in segments:
        if not _SEGMENT.match(segment):
            raise NamespaceError(
                f"invalid namespace segment {segment!r} in {path!r} "
                "(lowercase alphanumerics, '-' and '_', must start alphanumeric)"
            )
    return "/".join(segments)


def parent(path: str) -> str | None:
    """The containing namespace, or None at the root."""
    canonical = validate_namespace(path)
    head, _, _tail = canonical.rpartition("/")
    return head or None


def is_within(child: str, ancestor: str) -> bool:
    """True if ``child`` equals or is nested under ``ancestor``."""
    c = validate_namespace(child)
    a = validate_namespace(ancestor)
    return c == a or c.startswith(a + "/")
