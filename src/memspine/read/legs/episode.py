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

from memspine.read.legs.core import Hit, LegContext, LegOutput, LegRegistry
from memspine.read.legs.lexical import LexicalLeg

__all__ = ["EPISODE_LAYERS", "EpisodeLeg", "carry_score", "with_episode_leg"]

EPISODE_LAYERS: tuple[str, ...] = ("L2",)
_RRF_K = 60.0
#: how many times fetch_k the vector search is asked for, since it ranks every layer together
_VECTOR_OVERSAMPLE = 4
_VECTOR_FLOOR = 50


def carry_score(episode_score: float, carry: float, hops: int = 1) -> float:
    """The score a lifted turn inherits: ``episode_score * carry ** hops``."""
    return episode_score * (carry ** max(hops, 1))


class EpisodeLeg:
    """Episodes (L2) by vector and lexical match. Family ``derived``, name ``episode``."""

    name = "episode"
    family = "derived"
    layers = EPISODE_LAYERS

    def __init__(self) -> None:
        self._lexical = LexicalLeg(layers=EPISODE_LAYERS, name="episode_lexical")

    async def run(self, ctx: LegContext) -> LegOutput:
        reg = ctx.layer_registry
        if reg is not None and not reg.is_populated("L2"):
            return LegOutput([], "no_records:L2")
        episodes = ctx.records_for(self.layers)
        if not episodes:
            return LegOutput([], "no_records:L2")
        ids = {r.record_id for r in episodes}
        rankings: list[list[str]] = []
        if ctx.vector_search is not None:
            raw = await ctx.vector_search(max(ctx.fetch_k * _VECTOR_OVERSAMPLE, _VECTOR_FLOOR))
            rankings.append([h.record_id for h in raw if h.record_id in ids])
        lexical = await self._lexical.run(ctx)
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
