"""Canonical graph assertion identity.

A base key names what the assertion says under its owner scope. An occurrence
key additionally names the cited source turns, making repeated extraction
idempotent while allowing an A-B-A return to create a new occurrence.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BASE_TAG_PREFIX",
    "EdgeKeyDecision",
    "EdgeKeyIndex",
    "base_key",
    "base_tag",
    "edge_key",
    "norm_part",
    "occurrence_key",
    "occurrence_of",
]

#: Tag on a fact record naming its base key, so a later occurrence finds its siblings.
BASE_TAG_PREFIX = "edge_base:"


def _digest(domain: str, parts: list[Any]) -> str:
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"{domain}_{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]}"


def norm_part(text: str) -> str:
    """Normalise an endpoint or relation: NFKC, casefold, collapsed whitespace."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def edge_key(
    kind: str, label: str, src: str, dst: str, occurrence_id: str, tenant: str, owner: str
) -> str:
    """The LIB-35 compiled-edge key: kind + endpoints + occurrence, under the owner scope."""
    return _digest("ge", [kind, label, src, dst, occurrence_id, tenant, owner])


def occurrence_of(cited_ids: Iterable[str]) -> str:
    """Occurrence of an extracted edge: the sorted set of source records (cited turns) it came from."""
    ids = sorted(set(cited_ids))
    if not ids:
        raise ValueError("an occurrence needs at least one cited source")
    return _digest("oc", ids)


def base_key(
    scope: str,
    kind: str,
    src: str,
    rel: str,
    dst: str,
    extras: Sequence[Any] = (),
) -> str:
    """What the edge says, without time or source. ``extras`` carry the event day and numbers."""
    return _digest(
        "gb",
        [
            scope,
            norm_part(kind),
            norm_part(src),
            norm_part(rel),
            norm_part(dst),
            list(extras),
        ],
    )


def occurrence_key(base: str, occurrence: str) -> str:
    """The key of one occurrence of the edge named by ``base``."""
    return _digest("gk", [base, occurrence])


def base_tag(base: str) -> str:
    return f"{BASE_TAG_PREFIX}{base}"


@dataclass(frozen=True)
class EdgeKeyDecision:
    """Outcome of :meth:`EdgeKeyIndex.decide`.

    ``outcome``: ``duplicate`` (this occurrence, or a live sibling, exists: add provenance, write nothing),
    ``new`` (first time the edge is seen), ``new_occurrence`` (earlier occurrences exist but none is live,
    the A-B-A return). ``via`` says how the duplicate was found: ``key`` or ``base``.
    """

    outcome: str
    key: str
    base: str
    existing_id: str | None = None
    via: str = ""


class EdgeKeyIndex:
    """Edges of one scope indexed by assertion key and occurrence base.

    Entries are opaque handles with a liveness flag, so the index stays free of record types.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, tuple[str, bool]] = {}
        self._by_base: dict[str, list[tuple[str, bool]]] = {}

    def add(
        self,
        handle: str,
        live: bool,
        *,
        key: str | None = None,
        base: str | None = None,
    ) -> None:
        if key:
            self._by_key[key] = (handle, live)
        if base:
            self._by_base.setdefault(base, []).append((handle, live))

    def decide(self, base: str, occurrence: str) -> EdgeKeyDecision:
        """Decide whether an assertion is a duplicate, new, or a new occurrence."""
        key = occurrence_key(base, occurrence)
        if key in self._by_key:
            return EdgeKeyDecision("duplicate", key, base, self._by_key[key][0], "key")
        siblings = self._by_base.get(base, [])
        for handle, live in siblings:
            if live:
                return EdgeKeyDecision("duplicate", key, base, handle, "base")
        if siblings:
            return EdgeKeyDecision("new_occurrence", key, base)
        return EdgeKeyDecision("new", key, base)
