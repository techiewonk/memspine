"""R3-04 (R-05): the read-leg registry. A leg is a named, family-tagged candidate generator.

``Leg`` is the port of ``docs/v2/areas/read.md`` R-05: ``name``, ``family`` (``dense`` |
``lexical`` | ``structured`` | ``derived``) and an async ``run``. :class:`LegRegistry` holds the
legs; :func:`run_legs` runs the chosen ones together, times each, and emits one ``read.leg``
wiretap event per leg (family, count, latency, empty reason). A leg that has nothing to say
returns an empty :class:`LegOutput` with a reason (no records on its layer, no query terms, no
time window, no embedder); it never raises for that. Only a leg marked ``required`` (the vector
leg) lets an exception through; any other leg degrades to empty with ``error:<Type>``.

The legs registered by default are ``vector`` (dense), ``lexical`` (lexical) and ``temporal``
(structured). Each name is also entered in the source-family registry
(:mod:`memspine.read.registry`) so the existing fusion votes them in the right family.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from memspine.observability import tap
from memspine.read.registry import READ_LEGS, leg_family
from memspine.store.records import MemoryRecord

__all__ = [
    "Family",
    "Hit",
    "Leg",
    "LegContext",
    "LegOutput",
    "LegRegistry",
    "run_legs",
]

Family = Literal["dense", "lexical", "structured", "derived"]

#: schema family -> the source family fusion votes in (``read.registry``)
_SOURCE_FAMILY: dict[str, str] = {
    "dense": "semantic",
    "lexical": "lexical",
    "structured": "other",
    "derived": "other",
}


@dataclass(frozen=True)
class Hit:
    record_id: str
    score: float


@dataclass
class LegOutput:
    """What a leg returned: its hits, best first, and why it is empty when it is."""

    hits: Sequence[Any]
    empty_reason: str | None = None


@dataclass
class LegContext:
    """Everything a leg may read for one query (no engine handle, so legs stay pure)."""

    query: str
    namespace: str
    fetch_k: int
    #: the live records of the namespace (any layer); each leg picks its layers
    records: Sequence[MemoryRecord] = ()
    language: str = "en"
    namespace_languages: Mapping[str, str] = field(default_factory=dict)
    lexical_scorer: str = "bm25"
    #: the question's reference time (the caller's as-of, else now): relative phrases resolve here
    reference_time: datetime | None = None
    #: the newest record time, for a yearless date in the question
    year_ref: datetime | None = None
    week: str = "calendar"
    #: ``None`` when no embedder is up: the vector leg then reports ``no_embedder``
    vector_search: Callable[[int], Awaitable[Sequence[Any]]] | None = None
    #: R3-05c: which layers hold live records (``store.layer_registry.LayerRegistry``); ``None``
    #: means "read it off ``records``"
    layer_registry: Any | None = None
    #: R3-05b: the compiled graph edges (``read.legs.graph.HopEdge``) and the seed entity node ids
    #: the graph one-hop leg walks from; ``graph_min_seeds`` seeds make a node convergent
    graph_edges: Sequence[Any] = ()
    graph_seeds: Sequence[str] = ()
    graph_min_seeds: int = 2

    def records_for(self, layers: Sequence[str]) -> list[MemoryRecord]:
        wanted = set(layers)
        return [r for r in self.records if r.layer in wanted]

    def tokeniser_for(self) -> Any:
        """The tokeniser of this namespace's language (``namespace_languages`` overrides)."""
        from memspine.read.legs.lexical import make_tokeniser

        lang = self.namespace_languages.get(self.namespace, self.language)
        tok = _TOKENISERS.get(lang)
        if tok is None:
            tok = _TOKENISERS[lang] = make_tokeniser(lang)
        return tok


_TOKENISERS: dict[str, Any] = {}


@runtime_checkable
class Leg(Protocol):
    name: str
    family: Family
    layers: tuple[str, ...]

    async def run(self, ctx: LegContext) -> LegOutput: ...


class LegRegistry:
    """Name -> leg. Registering also tells the fusion registry the leg's source family."""

    def __init__(self) -> None:
        self._legs: dict[str, Leg] = {}
        self._required: set[str] = set()

    def register(self, leg: Leg, *, required: bool = False, replace: bool = False) -> Leg:
        if leg.name in self._legs and not replace:
            raise ValueError(f"leg {leg.name!r} is already registered")
        self._legs[leg.name] = leg
        if required:
            self._required.add(leg.name)
        if leg_family(leg.name) == "other" and leg.family in _SOURCE_FAMILY:
            source = _SOURCE_FAMILY[leg.family]
            if source != "other":
                READ_LEGS.register(leg.name, source, replace=True)
        return leg

    def get(self, name: str) -> Leg:
        try:
            return self._legs[name]
        except KeyError:
            raise KeyError(f"unknown leg {name!r} (known: {', '.join(self._legs)})") from None

    def names(self) -> tuple[str, ...]:
        return tuple(self._legs)

    def family_of(self, name: str) -> str:
        return self.get(name).family

    def is_required(self, name: str) -> bool:
        return name in self._required


async def _run_one(
    registry: LegRegistry, name: str, ctx: LegContext, deadline_s: float | None
) -> tuple[str, LegOutput]:
    leg = registry.get(name)
    t0 = time.perf_counter()
    try:
        coro = leg.run(ctx)
        out = await (asyncio.wait_for(coro, deadline_s) if deadline_s else coro)
    except Exception as exc:
        if registry.is_required(name):
            raise
        out = LegOutput([], f"error:{type(exc).__name__}")
    ms = round((time.perf_counter() - t0) * 1000, 2)
    tap.event(
        "read.leg",
        leg=name,
        family=leg.family,
        layers=list(leg.layers),
        count=len(out.hits),
        latency_ms=ms,
        empty_reason=out.empty_reason if not out.hits else None,
    )
    return name, out


async def run_legs(
    registry: LegRegistry,
    names: Sequence[str],
    ctx: LegContext,
    *,
    deadline_s: float | None = None,
) -> dict[str, LegOutput]:
    """Run the named legs together; results are keyed by leg name, in ``names`` order."""
    done = await asyncio.gather(*(_run_one(registry, n, ctx, deadline_s) for n in names))
    return dict(done)
