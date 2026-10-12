"""Hierarchical namespaces (M8.5) — the ONE enforcement point for scoping.

A namespace is a ``/``-separated path of lowercase segments, e.g.
``org/team-a/agent-7``. Phase 0 fixed the path grammar and containment rules;
P7 adds :func:`grant_allows` — the single yes/no every cross-namespace read
must pass (shared memory, ADR-016). Grant *records* live in
``memories/shared``; the decision lives here so no reader can reimplement it
subtly differently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from memspine.exceptions import NamespaceError

__all__ = ["grant_allows", "is_within", "parent", "validate_namespace"]

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


def grant_allows(
    reader: str,
    record_namespace: str,
    memory_type: str,
    grants: Mapping[str, AbstractSet[str] | None],
) -> bool:
    """May ``reader`` see a record of ``memory_type`` living in
    ``record_namespace``? The ONE grant-enforcement decision (ADR-016).

    ``grants`` maps grantor namespace -> allowed memory types for this reader
    (``None`` value = every type). Rules, in order:

    - Own namespace is always readable (exact match — grants never widen or
      narrow a reader's own scope, and hierarchy conveys nothing here).
    - ``shared`` bookkeeping records (grants, subscriptions) NEVER cross a
      grant: a grantee must not enumerate the grantor's other grants.
    - Otherwise the record's namespace must hold a grant whose scope covers
      the record's memory type.
    """
    if record_namespace == reader:
        return True
    if memory_type == "shared":
        return False
    if record_namespace not in grants:
        return False
    scope = grants[record_namespace]
    return scope is None or memory_type in scope
