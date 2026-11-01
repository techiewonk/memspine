"""``derive.PromptContextBuilder`` (W2-06b; W-16, P-09, P-11): the NEW / CONTEXT / KNOWN parts of the
window prompt.

NEW is the numbered lines to extract from; CONTEXT the last turns of the previous window (pronoun
resolution only, never cited); KNOWN the entities the store already holds, with aliases.

KNOWN sources, in priority order, all optional:
  * ``prepass``  hook for the P-23 pre-pass (W2-13, not built yet);
  * ``resolver`` hook for the online resolver (W2-09, not built yet);
  * ``store``    the entity records and aliases already in the store.
Entries are deduplicated on the normalised name (an alias that is another entry's name is dropped),
the ones named in the NEW lines come first, and the list is capped. A failing source is skipped: a
prompt is never blocked by its context.

DS-11 (ADR-090 item 6): every entry carries its provenance, the producer (``source``), the
entity index entry it was read from (``entity_id``, ``version``) and when the index came to know
it (``known_seq``), plus its visibility (the owning namespace and an optional grantee). The
builder keeps only entries visible to the window's namespace and reports the chosen entries with
their provenance (``PromptContext.manifest``). KNOWN names are hints for the extraction prompt:
they never key, link or assert anything.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

from memspine.observability.logging import get_logger
from memspine.prompts.layout_aliases import render_known

__all__ = [
    "KnownEntity",
    "KnownSource",
    "KnownVisibility",
    "PromptContext",
    "PromptContextBuilder",
]

_log = get_logger(__name__)


@dataclass(frozen=True)
class KnownVisibility:
    """Who may see a known entity: its owning namespace, and a grantee namespace when it is
    shown through a read grant."""

    namespace: str
    grantee: str | None = None

    def visible_to(self, namespace: str) -> bool:
        return namespace in (self.namespace, self.grantee)


@dataclass(frozen=True)
class KnownEntity:
    """One KNOWN hint with its provenance (DS-11).

    ``source`` is the producer (``entity_index``, ``prepass``, ``resolver``); ``entity_id`` and
    ``version`` (the index sequence of the entry's latest name or alias row) name the index
    entry it was read from; ``known_seq`` is the index sequence at which the name became known.
    ``visibility`` None means the hint was derived from the caller's own window text."""

    name: str
    aliases: tuple[str, ...] = ()
    source: str = ""
    entity_id: str | None = None
    version: int = 0
    known_seq: int | None = None
    visibility: KnownVisibility | None = None

    def visible_to(self, namespace: str) -> bool:
        return self.visibility is None or self.visibility.visible_to(namespace)


#: ``(namespace, new text) -> entities``
KnownSource = Callable[[str, str], Awaitable[Sequence[KnownEntity]]]


@dataclass(frozen=True)
class PromptContext:
    context: str = ""
    known: str = ""
    known_count: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    #: DS-11: the chosen KNOWN entries with their provenance, in prompt order
    manifest: tuple[KnownEntity, ...] = ()
    #: DS-11: entries dropped because the window's namespace may not see them
    hidden: int = 0


def _visible(found: Iterable[KnownEntity], ns: str) -> tuple[list[KnownEntity], int]:
    kept: list[KnownEntity] = []
    hidden = 0
    for entity in found:
        if entity.visible_to(ns):
            kept.append(entity)
        else:
            hidden += 1
    return kept, hidden


def _key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


class PromptContextBuilder:
    def __init__(
        self,
        store: KnownSource | None = None,
        *,
        cap: int = 40,
        context_turns: int = 3,
        max_aliases: int = 3,
        max_chars: int = 1600,
        prepass: KnownSource | None = None,
        resolver: KnownSource | None = None,
    ) -> None:
        self.cap = cap
        self.context_turns = context_turns
        self.max_aliases = max_aliases
        self.max_chars = max_chars
        #: Hook: the P-23 pre-pass (W2-13). None until built.
        self.prepass: KnownSource | None = prepass
        #: Hook: the online resolver (W2-09). None until built.
        self.resolver: KnownSource | None = resolver
        self.store = store

    def tail(self, line_texts: Sequence[str]) -> list[str]:
        """The CONTEXT lines a window leaves for the next one."""
        return list(line_texts[-self.context_turns :]) if self.context_turns else []

    async def build(self, ns: str, new_text: str, context_lines: Sequence[str]) -> PromptContext:
        context = "\n".join(context_lines[-self.context_turns :]) if self.context_turns else ""
        if self.cap <= 0:
            return PromptContext(context=context)
        merged: dict[str, tuple[str, list[str]]] = {}
        provenance: dict[str, KnownEntity] = {}
        by_source: dict[str, int] = {}
        hidden = 0
        for label, source in (
            ("prepass", self.prepass),
            ("resolver", self.resolver),
            ("store", self.store),
        ):
            if source is None:
                continue
            try:
                found = await source(ns, new_text)
            except Exception as exc:
                _log.warning("prompt_context.source_failed", source=label, error=str(exc)[:200])
                continue
            # DS-11: only entries the window's namespace may see.
            visible, dropped = _visible(found, ns)
            hidden += dropped
            fresh = 0
            for entity in visible:
                key = _key(entity.name)
                if not key:
                    continue
                if key not in merged:
                    merged[key] = (entity.name, [])
                    provenance[key] = entity
                    fresh += 1
                for alias in entity.aliases:
                    if _key(alias) != key and alias not in merged[key][1]:
                        merged[key][1].append(alias)
            by_source[label] = fresh
        if hidden:
            by_source["hidden"] = hidden
        # An alias that is another entry's own name is that entry, not an alias.
        entries = [
            (key, name, [a for a in aliases if _key(a) not in merged])
            for key, (name, aliases) in merged.items()
        ]
        haystack = _key(new_text)
        mentioned = [
            e for e in entries if _key(e[1]) in haystack or any(_key(a) in haystack for a in e[2])
        ]
        rest = [e for e in entries if e not in mentioned]
        chosen = (mentioned + rest)[: self.cap]
        rendered = render_known(
            [(name, aliases) for _, name, aliases in chosen],
            max_aliases=self.max_aliases,
            max_chars=self.max_chars,
        )
        count = len(rendered.splitlines()) if rendered else 0
        return PromptContext(
            context=context,
            known=rendered,
            known_count=count,
            by_source=by_source,
            manifest=tuple(provenance[key] for key, _, _ in chosen[:count]),
            hidden=hidden,
        )
