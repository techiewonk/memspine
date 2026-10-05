"""Query encoder port (#61, ADR-050), ``services/query_encoder``.

A query encoder maps a read query onto memory the query's own words would not
reach: Madeleine-style anticipation, where a question asked weeks later in other
words lands on the turn written to answer it. The port is read-time only and never
calls an LLM. ``read.query_encoder`` selects the implementation: ``none`` (the
default, :class:`NoopQueryEncoder`, reads are byte-identical) or ``cues``
(:class:`~memspine.services.query_encoder.cues.CueQueryEncoder`).

An encoder only *proposes*: the engine re-checks every match (the cue is live,
unquarantined and at or above ``read.cue_min_trust``) and the proposed targets
then pass the same read gates as any other hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from memspine.core.records import MemoryRecord

__all__ = ["CueMatch", "EncodedQuery", "NoopQueryEncoder", "QueryEncoder"]


@dataclass(frozen=True, slots=True)
class CueMatch:
    """One stored key the query matched: the key record, its target, the score."""

    cue_id: str
    target_id: str
    score: float
    text: str


@dataclass(frozen=True, slots=True)
class EncodedQuery:
    """The query as the encoder sees it.

    ``expansions`` are the matched key texts (the query "expanded" with them, kept
    for inspection); ``matches`` the records they point at, best first. Empty
    ``matches`` means the encoder adds nothing to the read.
    """

    query: str
    expansions: tuple[str, ...] = ()
    matches: tuple[CueMatch, ...] = field(default=())


@runtime_checkable
class QueryEncoder(Protocol):
    """``encode`` proposes extra retrieval targets for ``query``; ``observe`` is the
    write-time hook the engine calls for each record it writes; ``evict`` is the
    hook it calls when a record is forgotten, quarantined or archived, so erased
    text does not stay in process memory."""

    encoder_id: str

    async def encode(self, namespace: str, query: str) -> EncodedQuery: ...

    def observe(self, record: MemoryRecord) -> None: ...

    def evict(self, namespace: str, record_id: str) -> None: ...


class NoopQueryEncoder:
    """The default: proposes nothing, indexes nothing."""

    encoder_id = "none"

    async def encode(self, namespace: str, query: str) -> EncodedQuery:
        return EncodedQuery(query=query)

    def observe(self, record: MemoryRecord) -> None:
        return None

    def evict(self, namespace: str, record_id: str) -> None:
        return None
