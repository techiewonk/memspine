# ruff: noqa: E501, RUF001
"""W2-09: online entity resolution as an envelope stage (``write.resolver_v2``; W-19, M-15).

Order 300 in ``derive/envelope_stages.py``. For the subject and the object mention of every envelope the
stage resolves the surface against the entities of the envelope's own scope, with no model call:

1. rules (LIB-04 :class:`~memspine.derive.resolve.OnlineResolver`): exact name, alias, spelling
   variant, nickname, numeric, exonym; a context or attribute conflict (two people called Noor in
   different cities) keeps the mentions apart; a doubtful rule verdict is ``pending_merge``;
2. embedding similarity of the surface to the scope's entities (bounded vector cache), only for
   candidates the rules did not rule out (type, number, designator or context conflicts veto):
   cosine >= ``resolve_high`` is the same entity, cosine in [``resolve_grey``, ``resolve_high``) is the
   grey zone, below it the mention is a new entity.

The result per mention: ``existing`` attaches the canonical entity id; ``pending_merge`` creates a fresh
entity, attaches ITS id as the provisional one (I-5) and records the candidates, and never merges; ``new``
creates the entity. Merging a pending pair is a later decision (sleep reconciliation, or an operator) made
through :meth:`EntityRegistry.merge` and is undone by :meth:`EntityRegistry.split`; both are logged as
``memory.marker`` events (``entity.*``) that replay through F-16b migrate.

Envelope fields written (W2-10 reads them):

* ``subject.entity_id`` / ``object.entity_id`` (:class:`~memspine.schemas.durable.assertion.Mention`):
  the canonical id (existing or new) or the provisional id of a pending mention. Already-set ids are kept.
* ``extras["resolve"]["subject" | "object"]`` = ``{"verdict": existing|new|pending_merge, "method": ...,
  "confidence": float, "pending_with": [ids]}``. Key names avoid the reserved ``*_id`` suffix.

Entities are keyed by ``(tenant, owner)`` with ``isolation.scope_keys_v2`` on (the session is never part
of the key), by the namespace string otherwise.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from memspine.derive.entity_registry import ENTITY_MARKERS, Entity, EntityRegistry, ScopeKey
from memspine.derive.envelope_stages import EnvelopeStage, EnvelopeStageContext
from memspine.derive.resolve import (
    Candidate,
    MentionView,
    OnlineResolver,
    entity_id_for,
    normalise_name,
    resolve_pair,
)
from memspine.observability import tap
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.common import OwnerScope
from memspine.store.events import EventKind, MemoryEvent

__all__ = ["RESOLVE_ORDER", "EntityResolveStage", "ResolveSettings", "build_resolve_stage"]

RESOLVE_ORDER = 300
Embed = Callable[[list[str]], Awaitable[list[list[float]]]]

#: rule reasons of a ``different`` verdict that are only "no rule matched"; every other reason is a veto
_NO_VETO = frozenset({"no_match", "empty_name"})
#: past this many live entities in a scope the candidates are prefiltered (shared name prefix + top-k)
PREFILTER_ABOVE = 256
PREFILTER_TOP_K = 48
#: vectors of the few best embedding candidates are checked against the rules' vetoes
EMBED_CHECK = 3
_FIRST_PERSON = re.compile(r"\bI(?:['’](?:m|ve|ll|d))?\b")


@dataclass(frozen=True)
class ResolveSettings:
    high: float = 0.9
    grey: float = 0.7
    max_cache: int = 1024
    scoped: bool = True


def _cosine(u: Sequence[float], v: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v, strict=False))
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(y * y for y in v))
    return dot / (nu * nv) if nu and nv else 0.0


class _VectorCache:
    """Bounded LRU of name vectors by normalised surface (text only, no scope data)."""

    def __init__(self, embed: Embed, max_size: int) -> None:
        self._embed = embed
        self._max = max(1, max_size)
        self._data: OrderedDict[str, list[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    async def vectors(self, surfaces: Sequence[str]) -> list[list[float]]:
        keys = [normalise_name(s) or s for s in surfaces]
        missing = list(dict.fromkeys(k for k in keys if k not in self._data))
        if missing:
            for k, v in zip(missing, await self._embed(missing), strict=True):
                self._data[k] = v
        for k in keys:
            self._data.move_to_end(k)
        out = [self._data[k] for k in keys]
        while len(self._data) > self._max:
            self._data.popitem(last=False)
        return out


class EntityResolveStage:
    """The stage state: registry, rule resolver, vector cache, and the event-log hooks."""

    def __init__(
        self,
        settings: ResolveSettings | None = None,
        *,
        registry: EntityRegistry | None = None,
        embed: Embed | None = None,
        storage: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings or ResolveSettings()
        self.registry = registry or EntityRegistry()
        self._rules = OnlineResolver(self.settings.max_cache)
        self._vec = _VectorCache(embed, self.settings.max_cache) if embed is not None else None
        self._storage = storage
        self._loaded = storage is None
        self._lock = asyncio.Lock()

    def as_stage(self) -> EnvelopeStage:
        return EnvelopeStage("resolve", RESOLVE_ORDER, self.run)

    # --- log -------------------------------------------------------------------------------------
    async def _load(self) -> None:
        """Rebuild the registry from the logged ``entity.*`` markers, once per process."""
        async with self._lock:
            if self._loaded:
                return
            storage = self._storage() if self._storage else None
            after = 0
            while storage is not None:
                events = await storage.read_events(after_seq=after, limit=1000)
                if not events:
                    break
                for ev in events:
                    if ev.kind is EventKind.MARKER and ev.payload.get("marker") in ENTITY_MARKERS:
                        self.registry.apply(ev.payload)
                after = max(e.seq for e in events if e.seq is not None)
            self._loaded = True

    async def flush(self, namespace: str) -> int:
        """Append the queued registry payloads to the event log; returns how many."""
        payloads = self.registry.drain()
        storage = self._storage() if self._storage else None
        if storage is not None:
            for p in payloads:
                await storage.append_event(
                    MemoryEvent(
                        kind=EventKind.MARKER, namespace=namespace, actor="resolver", payload=p
                    )
                )
        return len(payloads)

    # --- stage -----------------------------------------------------------------------------------
    async def run(
        self, envelopes: list[AssertionEnvelope], ctx: EnvelopeStageContext
    ) -> list[AssertionEnvelope]:
        await self._load()
        out: list[AssertionEnvelope] = []
        async with self._lock:
            for env in envelopes:
                out.append(await self._resolve_envelope(env, ctx))
            events = await self.flush(ctx.namespace)
        if tap.enabled():
            tap.event(
                "write.resolve.window",
                window=ctx.window_id,
                envelopes=len(out),
                events=events,
                vectors=len(self._vec) if self._vec else 0,
            )
        return out

    def _key(self, env: AssertionEnvelope, ctx: EnvelopeStageContext) -> ScopeKey:
        if self.settings.scoped:
            return (env.scope.tenant, env.scope.owner)
        return (ctx.namespace,)

    async def _resolve_envelope(
        self, env: AssertionEnvelope, ctx: EnvelopeStageContext
    ) -> AssertionEnvelope:
        key = self._key(env, ctx)
        update: dict[str, Any] = {}
        info: dict[str, Any] = dict(env.extras.get("resolve", {}))
        for which in ("subject", "object"):
            mention: Mention | None = getattr(env, which)
            if mention is None or mention.entity_id is not None:
                continue
            res = await self.resolve_surface(
                mention.surface,
                scope=env.scope,
                key=key,
                text=env.text,
                speaker=env.speaker,
                notes=ctx.notes,
                window=ctx.window_id,
            )
            update[which] = mention.model_copy(update={"entity_id": res["entity"]})
            info[which] = {k: v for k, v in res.items() if k != "entity"}
        if not update:
            return env
        return env.model_copy(update={**update, "extras": {**env.extras, "resolve": info}})

    async def resolve_surface(
        self,
        surface: str,
        *,
        scope: OwnerScope,
        key: ScopeKey,
        text: str = "",
        speaker: str | None = None,
        notes: Counter[str] | None = None,
        window: str = "",
    ) -> dict[str, Any]:
        """Resolve one surface in one scope and update the registry (events are queued, not yet logged)."""
        notes = notes if notes is not None else Counter()
        text = _FIRST_PERSON.sub("", text)  # a capitalised "I" would count as a shared anchor
        view = MentionView(surface=surface, context=text, speaker=speaker)
        live = self.registry.active(key)
        pool = await self._prefilter(live, surface)
        cands = [
            Candidate(e.entity_id, e.name, scope, e.type, e.context, tuple(e.aliases), e.lang)
            for e in pool
        ]
        d = self._rules.resolve(view, scope, cands)
        verdict, method, conf, entity, pending = (
            d.kind,
            d.reason,
            1.0,
            d.entity_id,
            list(d.candidates),
        )

        if verdict == "new" and pool:
            hit = await self._embedding_step(view, pool)
            if hit is not None:
                kind, target, cos = hit
                if kind == "existing":
                    verdict, entity, method, conf = "existing", target, "embed", cos
                else:
                    verdict, pending, method, conf = "pending_merge", [target], "embed_grey", cos
        elif verdict == "pending_merge":
            conf = 0.5

        if verdict == "existing":
            self.registry.add_alias(key, entity, surface, context=text)
            notes["resolve_existing"] += 1
        else:
            eid = self._unique_id(scope, key, entity)
            self.registry.create(
                key, eid, surface, context=text, pending_with=pending if verdict != "new" else ()
            )
            entity = eid
            notes["resolve_pending" if verdict == "pending_merge" else "resolve_new"] += 1
        if tap.enabled():
            tap.event(
                "write.resolve.mention",
                window=window,
                verdict=verdict,
                method=method,
                confidence=round(conf, 3),
                entity=entity,
                pending_with=pending if verdict == "pending_merge" else [],
            )
        return {
            "entity": entity,
            "verdict": verdict,
            "method": method,
            "confidence": round(conf, 4),
            "pending_with": pending if verdict == "pending_merge" else [],
        }

    def _unique_id(self, scope: OwnerScope, key: ScopeKey, proposed: str) -> str:
        if self.registry.get(key, proposed) is None:
            return proposed
        n = 1
        while True:  # a re-used ordinal (after merges) must not collide with a kept entity
            cand = entity_id_for(scope, "", f"{proposed}", n)
            if self.registry.get(key, cand) is None:
                return cand
            n += 1

    async def _prefilter(self, live: list[Entity], surface: str) -> list[Entity]:
        if len(live) <= PREFILTER_ABOVE:
            return live
        stems = {t[:3] for t in normalise_name(surface).split() if len(t) >= 3}
        shared = [
            e
            for e in live
            if any(t[:3] in stems for n in e.names() for t in normalise_name(n).split())
        ]
        top: list[Entity] = []
        if self._vec is not None:
            q = (await self._vec.vectors([surface]))[0]
            vecs = await self._vec.vectors([e.name for e in live])
            ranked = sorted(zip(live, vecs, strict=True), key=lambda p: -_cosine(q, p[1]))
            top = [e for e, _ in ranked[:PREFILTER_TOP_K]]
        seen: set[str] = set()
        out: list[Entity] = []
        for e in [*shared, *top]:
            if e.entity_id not in seen:
                seen.add(e.entity_id)
                out.append(e)
        return out

    async def _embedding_step(
        self, view: MentionView, pool: list[Entity]
    ) -> tuple[str, str, float] | None:
        """Best non-vetoed candidate by name cosine: ('existing' | 'grey', entity id, cosine)."""
        if self._vec is None:
            return None
        names: list[tuple[Entity, str]] = [(e, n) for e in pool for n in e.names()]
        q = (await self._vec.vectors([view.surface]))[0]
        vecs = await self._vec.vectors([n for _, n in names])
        best: dict[str, float] = {}
        for (e, _), v in zip(names, vecs, strict=True):
            c = _cosine(q, v)
            if c > best.get(e.entity_id, -1.0):
                best[e.entity_id] = c
        by_id = {e.entity_id: e for e in pool}
        for eid, cos in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:EMBED_CHECK]:
            if cos < self.settings.grey:
                break
            e = by_id[eid]
            other = MentionView(e.name, e.type, e.context, e.lang)
            pair = resolve_pair(view, other)
            if pair.verdict == "different" and pair.reason not in _NO_VETO:
                continue
            if _context_conflict(view, other):
                continue
            return ("existing" if cos >= self.settings.high else "grey", eid, cos)
        return None


def _context_conflict(a: MentionView, b: MentionView) -> bool:
    """The rules' homonym test on two same-surface views: a context conflict vetoes an embedding merge."""
    twin = MentionView(a.surface, a.type, a.context, a.lang, a.speaker)
    probe = MentionView(a.surface, b.type, b.context, b.lang)
    return resolve_pair(twin, probe).verdict == "different"


def build_resolve_stage(
    settings: ResolveSettings | None = None,
    *,
    embed: Embed | None = None,
    storage: Callable[[], Any] | None = None,
) -> EntityResolveStage:
    return EntityResolveStage(settings, embed=embed, storage=storage)
