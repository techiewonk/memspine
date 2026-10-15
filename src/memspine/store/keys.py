"""Canonical graph key functions (F-01; decisions M-17 B, W-20 C; ER-13, ER-20).

One implementation of graph identity, used by the LIB-35 edge compiler (``derive/edges.py``) and by the
``extract_graph`` write path (behind ``graph.key_v2``).

Key format v2 (``KEY_V == 2``):

* ``base``  = hash(scope, kind, normalised src, normalised relation, normalised dst, extras)
  names WHAT the edge says. It carries no time and no source.
* ``key``   = hash(base, occurrence) names ONE statement of it. The occurrence comes from the cited turns
  (:func:`occurrence_of`), so re-extracting the same turns gives the same key (replay idempotent) while the
  same fact stated again after another value (A -> B -> A) is a new occurrence, hence a new key, instead of
  reopening the closed one.
* the v1 key (today's ``extract_graph`` fingerprint, which has no kind and no occurrence) stays valid as an
  **alias**: a lookup by an old key still finds the edge (:class:`EdgeKeyIndex`).

This module is pure (hashing only) so any layer above ``store`` may use it.
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
    "KEY_V",
    "EdgeKeyDecision",
    "EdgeKeyIndex",
    "base_key",
    "base_tag",
    "edge_key",
    "norm_part",
    "occurrence_of",
    "versioned_key",
]

KEY_V = 2
#: Tag on a v2 fact record naming its base key, so a later occurrence finds its siblings.
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
    """What the edge says (v2), without time or source. ``extras`` carry the event day and numbers."""
    return _digest(
        "gb",
        [
            KEY_V,
            scope,
            norm_part(kind),
            norm_part(src),
            norm_part(rel),
            norm_part(dst),
            list(extras),
        ],
    )


def versioned_key(base: str, occurrence: str) -> str:
    """The v2 key of one occurrence of the edge named by ``base``."""
    return _digest("gk", [KEY_V, base, occurrence])


def base_tag(base: str) -> str:
    return f"{BASE_TAG_PREFIX}{base}"


@dataclass(frozen=True)
class EdgeKeyDecision:
    """Outcome of :meth:`EdgeKeyIndex.decide`.

    ``outcome``: ``duplicate`` (this occurrence, or a live sibling, exists: add provenance, write nothing),
    ``new`` (first time the edge is seen), ``new_occurrence`` (earlier occurrences exist but none is live,
    the A-B-A return). ``via`` says how the duplicate was found: ``key``, ``base`` or ``alias``.
    """

    outcome: str
    key: str
    base: str
    existing_id: str | None = None
    via: str = ""


class EdgeKeyIndex:
    """Edges of one scope indexed by v2 key, v2 base and old (v1) alias key.

    Entries are opaque handles with a liveness flag, so the index stays free of record types.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, tuple[str, bool]] = {}
        self._by_base: dict[str, list[tuple[str, bool]]] = {}
        self._by_alias: dict[str, tuple[str, bool]] = {}

    def add(
        self,
        handle: str,
        live: bool,
        *,
        key: str | None = None,
        base: str | None = None,
        alias: str | None = None,
    ) -> None:
        if key:
            self._by_key[key] = (handle, live)
        if base:
            self._by_base.setdefault(base, []).append((handle, live))
        if alias:
            cur = self._by_alias.get(alias)
            if cur is None or live or not cur[1]:
                self._by_alias[alias] = (handle, live)

    def decide(
        self, base: str, occurrence: str, *, alias_keys: Sequence[str] = ()
    ) -> EdgeKeyDecision:
        """Decide whether an edge statement is a duplicate, new, or a new occurrence.

        ``alias_keys`` are the old (v1) keys this statement also answers to.
        """
        key = versioned_key(base, occurrence)
        if key in self._by_key:
            return EdgeKeyDecision("duplicate", key, base, self._by_key[key][0], "key")
        siblings = self._by_base.get(base, [])
        for handle, live in siblings:
            if live:
                return EdgeKeyDecision("duplicate", key, base, handle, "base")
        legacy = [self._by_alias[a] for a in alias_keys if a in self._by_alias]
        for handle, live in legacy:
            if live:
                return EdgeKeyDecision("duplicate", key, base, handle, "alias")
        if siblings or legacy:
            return EdgeKeyDecision("new_occurrence", key, base)
        return EdgeKeyDecision("new", key, base)
