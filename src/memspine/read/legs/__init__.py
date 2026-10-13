"""R3-04 (R-05, R-06, R-07): the core retrieval legs behind one registry.

``REGISTRY`` carries the three always-on legs: ``vector`` (dense), ``lexical`` (language-aware
BM25 through the Tokeniser / scorer ports, :mod:`.lexical`) and ``temporal`` (event intervals in
an anchored time window, :mod:`.temporal`). Wired behind ``read.legs_v2``; see
``memspine.read.pipeline.legs.registry_base_legs``.
"""

from __future__ import annotations

from memspine.read.legs.core import (
    Family,
    Hit,
    Leg,
    LegContext,
    LegOutput,
    LegRegistry,
    run_legs,
)
from memspine.read.legs.episode import EpisodeLeg, carry_score, with_episode_leg
from memspine.read.legs.lexical import (
    Bm25Scorer,
    LexicalLeg,
    LexicalScorer,
    Tokeniser,
    UnicodeTokeniser,
    make_scorer,
    make_tokeniser,
    register_scorer,
)
from memspine.read.legs.temporal import TemporalLeg, TimeWindow, event_intervals

__all__ = [
    "REGISTRY",
    "Bm25Scorer",
    "EpisodeLeg",
    "Family",
    "Hit",
    "Leg",
    "LegContext",
    "LegOutput",
    "LegRegistry",
    "LexicalLeg",
    "LexicalScorer",
    "TemporalLeg",
    "TimeWindow",
    "Tokeniser",
    "UnicodeTokeniser",
    "VectorLeg",
    "carry_score",
    "event_intervals",
    "make_scorer",
    "make_tokeniser",
    "register_scorer",
    "run_legs",
    "with_episode_leg",
]


class VectorLeg:
    """The dense leg: the vector store's nearest records (the caller supplies the search)."""

    name = "vector"
    family = "dense"
    layers = ("L0", "L1")

    async def run(self, ctx: LegContext) -> LegOutput:
        if ctx.vector_search is None:
            return LegOutput([], "no_embedder")
        hits = list(await ctx.vector_search(ctx.fetch_k))
        return LegOutput(hits, None if hits else "no_match")


REGISTRY = LegRegistry()
REGISTRY.register(VectorLeg(), required=True)
REGISTRY.register(LexicalLeg())
REGISTRY.register(TemporalLeg())
