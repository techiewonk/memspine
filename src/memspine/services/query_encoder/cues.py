"""``cues`` query encoder (#61, ADR-050): match a query to H8 anticipatory cues.

H8 (``consolidation.anticipate``) and :meth:`Engine.add_cues` write cue records at
write time: short future questions ("what snacks should we buy for Alice's party?")
whose parent is the turn that answers them. This encoder keeps a cue -> target index
of their content words (built as cues are written, and loaded once per namespace
from storage after a restart) and, at read, matches the query by token overlap: a
cue matches when the query contains at least ``QUERY_ENCODER_CUE_MIN_OVERLAP`` of
the cue's content words. Training-free and model-free; a *trained* encoder
(Madeleine) is future work (ADR-050).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from memspine.config import constants
from memspine.core.query_shape import content_words
from memspine.core.records import MemoryRecord
from memspine.services.query_encoder.base import CueMatch, EncodedQuery

__all__ = ["CueQueryEncoder"]

#: namespace -> its cue records (the engine lists them from storage).
LoadCues = Callable[[str], Awaitable[list[MemoryRecord]]]


class _Entry:
    __slots__ = ("cue_id", "target_id", "text", "words")

    def __init__(self, record: MemoryRecord) -> None:
        self.cue_id = record.record_id
        self.target_id = record.source.parents[0]
        self.text = record.content
        self.words = content_words(record.content)


class CueQueryEncoder:
    """Cue -> target index over content words; read-time matching by overlap."""

    encoder_id = "cues"

    def __init__(
        self,
        load: LoadCues,
        *,
        min_overlap: float = constants.QUERY_ENCODER_CUE_MIN_OVERLAP,
        max_targets: int = constants.QUERY_ENCODER_MAX_TARGETS,
    ) -> None:
        self._load = load
        self._min_overlap = min_overlap
        self._max_targets = max_targets
        self._index: dict[str, dict[str, _Entry]] = {}

    def observe(self, record: MemoryRecord) -> None:
        """Index a cue record as it is written (no-op for anything else, or for a
        namespace not loaded yet: its first ``encode`` loads every cue)."""
        if constants.CUE_TAG not in record.tags or not record.source.parents:
            return
        entries = self._index.get(record.namespace)
        if entries is not None:
            entries[record.record_id] = _Entry(record)

    async def _entries(self, namespace: str) -> dict[str, _Entry]:
        entries = self._index.get(namespace)
        if entries is None:
            entries = {
                r.record_id: _Entry(r)
                for r in await self._load(namespace)
                if constants.CUE_TAG in r.tags and r.source.parents
            }
            self._index[namespace] = entries
        return entries

    async def encode(self, namespace: str, query: str) -> EncodedQuery:
        words = content_words(query)
        if not words:
            return EncodedQuery(query=query)
        best: dict[str, CueMatch] = {}
        for entry in (await self._entries(namespace)).values():
            if not entry.words:
                continue
            score = len(words & entry.words) / len(entry.words)
            if score < self._min_overlap:
                continue
            seen = best.get(entry.target_id)
            if seen is None or (-score, entry.cue_id) < (-seen.score, seen.cue_id):
                best[entry.target_id] = CueMatch(entry.cue_id, entry.target_id, score, entry.text)
        matches = sorted(best.values(), key=lambda m: (-m.score, m.target_id))
        matches = matches[: self._max_targets]
        return EncodedQuery(
            query=query,
            expansions=tuple(m.text for m in matches),
            matches=tuple(matches),
        )
