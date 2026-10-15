"""R3-05c (R-10): the episode leg over L2, whose hits are lifted to member turns.

L2 holds episodes: window episodes, ``kind:episode`` semantic records, session-close episodes
(OW-A) whose parents are the cited turns. The leg searches those records by meaning and by words
and returns *episode* hits; the read pipeline
(``memspine.read.pipeline.lift.lift_episode_hits``) then walks each hit to its member turns
through lineage and hands the turn ids to fusion.

The two searches are fused by reciprocal rank inside the leg, so the leg returns one ranking with a
score in ``(0, 1]``. When the namespace holds no L2 record the leg is empty with the reason
``no_records:L2``, never an error (a template without episodes simply has no such leg).

:func:`carry_score` is the score rule of the lift: a turn takes the score of the episode that lifted
it, damped by ``carry`` once per hop; a turn lifted by several episodes keeps the best.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from memspine.read.legs.core import Hit, LegContext, LegOutput, LegRegistry
from memspine.read.legs.lexical import LexicalLeg
from memspine.store.records import MemoryRecord

__all__ = [
    "DEFAULT_EPISODE_KINDS",
    "EPISODE_KINDS",
    "EPISODE_LAYERS",
    "EpisodeLeg",
    "carry_score",
    "episode_kinds_of",
    "with_episode_leg",
]

EPISODE_LAYERS: tuple[str, ...] = ("L2",)
_RRF_K = 60.0
#: how many times fetch_k the first vector search asks for, since it ranks every layer together
_VECTOR_OVERSAMPLE = 4
_VECTOR_FLOOR = 50
#: MF-12: the vector store ranks every layer together and has no layer column, so the leg widens
#: the request (x2 per round) until it holds ``fetch_k`` episode hits, the store runs out of
#: candidates, or this many candidates were asked for
_VECTOR_WIDEN_CAP = 1024

#: MF-11: the kinds of L2 record the leg can search, and what each one is.
#:
#: * ``episode``: a ``kind:episode`` semantic record (a stitched or standalone episode).
#: * ``session_summary``: a session-close record (channel ``session_close``): the session summary
#:   and the session-close episode; its parents are the cited turns.
#: * ``episode_fragment``: a window episode (tag ``episode_fragment`` / channel ``window_episode``).
#: * ``consolidation``: a cluster summary (channel ``consolidation``). Off by default: it merges
#:   several episodes into a topic digest, so a hit lifts to a wide, loosely related turn set.
#: * ``anticipation``: a predicted-need cue (channel ``anticipation``). Off by default: it is a
#:   forward-looking guess, not a record of what was said, so its lift returns turns that only
#:   share the cue's wording.
EPISODE_KINDS: tuple[str, ...] = (
    "episode",
    "session_summary",
    "episode_fragment",
    "consolidation",
    "anticipation",
)
DEFAULT_EPISODE_KINDS: tuple[str, ...] = ("episode", "session_summary", "episode_fragment")


def episode_kinds_of(record: MemoryRecord) -> set[str]:
    """The episode kinds a record belongs to (empty: it is some other L2 record)."""
    tags = set(record.tags)
    channel = record.source.channel
    kinds: set[str] = set()
    if "kind:episode" in tags:
        kinds.add("episode")
    if channel == "session_close" or "session_close" in tags:
        kinds.add("session_summary")
    if "episode_fragment" in tags or channel == "window_episode":
        kinds.add("episode_fragment")
    if channel == "consolidation":
        kinds.add("consolidation")
    if channel == "anticipation":
        kinds.add("anticipation")
    return kinds


def carry_score(episode_score: float, carry: float, hops: int = 1) -> float:
    """The score a lifted turn inherits: ``episode_score * carry ** hops``."""
    return episode_score * (carry ** max(hops, 1))


class EpisodeLeg:
    """Episodes (L2) by vector and lexical match. Family ``derived``, name ``episode``."""

    name = "episode"
    family = "derived"
    layers = EPISODE_LAYERS

    def __init__(self, kinds: Iterable[str] = DEFAULT_EPISODE_KINDS) -> None:
        listed = list(kinds)
        wanted = tuple(dict.fromkeys(listed))
        if not wanted or any(k not in EPISODE_KINDS for k in wanted):
            raise ValueError(
                f"episode leg kinds must be a non-empty subset of {EPISODE_KINDS}, got {listed!r}"
            )
        self.kinds = frozenset(wanted)
        self._lexical = LexicalLeg(layers=EPISODE_LAYERS, name="episode_lexical")

    async def run(self, ctx: LegContext) -> LegOutput:
        reg = ctx.layer_registry
        if reg is not None and not reg.is_populated("L2"):
            return LegOutput([], "no_records:L2")
        l2 = ctx.records_for(self.layers)
        if not l2:
            return LegOutput([], "no_records:L2")
        # MF-11: filter by kind, not only by layer
        episodes = [r for r in l2 if episode_kinds_of(r) & self.kinds]
        if not episodes:
            return LegOutput([], "no_records:episode")
        ids = {r.record_id for r in episodes}
        rankings: list[list[str]] = []
        if ctx.vector_search is not None:
            rankings.append(await self._vector_ranking(ctx, ids))
        lexical = await self._lexical.run(replace(ctx, records=episodes))
        rankings.append([h.record_id for h in lexical.hits])
        fused: dict[str, float] = {}
        for ranking in rankings:
            for rank, rid in enumerate(ranking):
                fused[rid] = fused.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        if not fused:
            return LegOutput([], "no_match")
        top = (len(rankings)) / (_RRF_K + 1)  # best possible score: first in every ranking
        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[: ctx.fetch_k]
        return LegOutput([Hit(rid, s / top) for rid, s in ordered])

    async def _vector_ranking(self, ctx: LegContext, ids: set[str]) -> list[str]:
        """MF-12: episode ids by vector rank. The search covers every layer, so ask for more
        (x2 per round, capped)
        until ``fetch_k`` episode hits are in hand or the store has no more."""
        assert ctx.vector_search is not None
        k = max(ctx.fetch_k * _VECTOR_OVERSAMPLE, _VECTOR_FLOOR)
        want = min(ctx.fetch_k, len(ids))
        while True:
            raw = list(await ctx.vector_search(k))
            ranking = [h.record_id for h in raw if h.record_id in ids]
            if len(ranking) >= want or len(raw) < k or k >= _VECTOR_WIDEN_CAP:
                return ranking
            k = min(k * 2, _VECTOR_WIDEN_CAP)


_EXTENDED: dict[int, LegRegistry] = {}


def with_episode_leg(base: LegRegistry) -> LegRegistry:
    """``base`` plus the episode leg, as a new registry (the shared one is never changed)."""
    cached = _EXTENDED.get(id(base))
    if cached is not None and "episode" in cached.names():
        return cached
    out = LegRegistry()
    for name in base.names():
        out.register(base.get(name), required=base.is_required(name))
    out.register(EpisodeLeg())  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    _EXTENDED[id(base)] = out
    return out
