"""KNOWN sources for the window prompt (W2-06c): the W2-13 pre-pass and the W2-09 resolver.

Both are :data:`~memspine.derive.prompt_context.KnownSource` callables, ``(namespace, new text) ->
entities``, plugged into ``PromptContextBuilder(prepass=..., resolver=...)``. The builder merges them
with the store source, deduplicates on the normalised name, puts entities named in the NEW lines first
and applies the cap, so these only report what they know.

  * ``prepass_known``  the pre-pass hints over the NEW lines: every entity / alias span becomes a
                       ``KnownEntity`` (the surface as written). No model call (rules; GLiNER2 when
                       the shared pre-pass loaded it).
  * ``resolver_known`` the resolver registry's live entities of the namespace with their canonical
                       name and aliases, entities named in the NEW lines first.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from memspine.derive.prompt_context import KnownEntity, KnownSource

__all__ = ["prepass_known", "resolver_known"]

#: the "[n] [date] " prefix the window transcript puts on each NEW line
_LINE_PREFIX = re.compile(r"^\[\d+\]\s*(?:\[[^\]]*\]\s*)?")


def prepass_known(prepass: Any) -> KnownSource:
    """``prepass``: a shared :class:`~memspine.derive.prepass.Prepass`."""

    async def source(ns: str, text: str) -> list[KnownEntity]:
        lines = [_LINE_PREFIX.sub("", ln) for ln in text.splitlines() if ln.strip()]
        records = [SimpleNamespace(record_id=f"known:{hash(ln)}", content=ln) for ln in lines]
        hints = await prepass.ahints(records)
        seen: dict[str, None] = {}
        for span in hints.entities:
            seen.setdefault(span.text, None)
        return [KnownEntity(name) for name in seen]

    return source


def resolver_known(stage: Any, *, limit: int = 200) -> KnownSource:
    """``stage``: the :class:`~memspine.derive.resolve_stage.EntityResolveStage` (its registry is
    read live, so entities resolved by earlier windows show up in the next prompt)."""

    async def source(ns: str, text: str) -> list[KnownEntity]:
        registry = stage.registry
        low = text.lower()
        entities = []
        for key in registry.scopes():
            if key and key[-1] == ns:  # (namespace,) or (tenant, owner)
                entities.extend(registry.active(key))
        named = [e for e in entities if any(n.lower() in low for n in e.names() if n)]
        rest = [e for e in entities if e not in named]
        return [KnownEntity(e.name, tuple(e.aliases)) for e in (named + rest)[:limit]]

    return source
