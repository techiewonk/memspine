# ruff: noqa: E501
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
"""

from __future__ import annotations

import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from memspine.observability.logging import get_logger
from memspine.prompts.layout_aliases import render_known

__all__ = ["KnownEntity", "KnownSource", "PromptContext", "PromptContextBuilder"]

_log = get_logger(__name__)


@dataclass(frozen=True)
class KnownEntity:
    name: str
    aliases: tuple[str, ...] = ()


#: ``(namespace, new text) -> entities``
KnownSource = Callable[[str, str], Awaitable[Sequence[KnownEntity]]]


@dataclass(frozen=True)
class PromptContext:
    context: str = ""
    known: str = ""
    known_count: int = 0
    by_source: dict[str, int] = field(default_factory=dict)


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
        by_source: dict[str, int] = {}
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
            fresh = 0
            for entity in found:
                key = _key(entity.name)
                if not key:
                    continue
                if key not in merged:
                    merged[key] = (entity.name, [])
                    fresh += 1
                for alias in entity.aliases:
                    if _key(alias) != key and alias not in merged[key][1]:
                        merged[key][1].append(alias)
            by_source[label] = fresh
        # An alias that is another entry's own name is that entry, not an alias.
        entries = [
            (name, [a for a in aliases if _key(a) not in merged])
            for name, aliases in merged.values()
        ]
        haystack = _key(new_text)
        mentioned = [
            e for e in entries if _key(e[0]) in haystack or any(_key(a) in haystack for a in e[1])
        ]
        rest = [e for e in entries if e not in mentioned]
        chosen = (mentioned + rest)[: self.cap]
        rendered = render_known(chosen, max_aliases=self.max_aliases, max_chars=self.max_chars)
        return PromptContext(
            context=context,
            known=rendered,
            known_count=len(rendered.splitlines()) if rendered else 0,
            by_source=by_source,
        )
