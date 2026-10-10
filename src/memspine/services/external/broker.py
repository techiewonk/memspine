"""The external-evidence broker: privacy filter, cache, call budget, explicit failure.

``lookup`` never raises into a read. It returns a :class:`ExternalResult` whose ``status`` says
what happened: ``ok`` | ``cache_hit`` | ``cache_miss`` (cache-only mode) | ``filtered_empty``
(no safe topic word) | ``budget_exhausted`` | ``provider_unavailable`` | ``provider_error`` |
``no_results``. Only ``ok`` and ``cache_hit`` carry snippets.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from memspine.services.external.privacy import PublicQuery, build_public_query
from memspine.services.external.provider import (
    ExternalProvider,
    ExternalSnippet,
    ProviderError,
    ProviderUnavailableError,
)

__all__ = [
    "PUBLIC_KNOWLEDGE_CLAUSE",
    "PUBLIC_MARKER",
    "EvidenceCache",
    "ExternalBroker",
    "ExternalResult",
    "ExternalStats",
    "format_public_block",
]

_log = structlog.get_logger("memspine.external")

PUBLIC_MARKER = "[public knowledge]"

#: Carried inside every block, so any reader sees it with the evidence. The engine never
#: stores external text and never counts it as support for a question about a person.
PUBLIC_KNOWLEDGE_CLAUSE = (
    "General background from outside this conversation. It may inform a qualified guess about "
    "what is likely or what would suit, but it never shows that any person in the memories did, "
    "owns, visited, said or experienced anything: only the memories can show that. State such "
    "a guess as a guess, and keep facts about people to what the memories say."
)


@dataclass(slots=True)
class ExternalStats:
    network_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    refused: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class ExternalResult:
    status: str
    public_query: str = ""
    snippets: tuple[ExternalSnippet, ...] = ()
    provider: str = ""

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "cache_hit") and bool(self.snippets)


class EvidenceCache:
    """Query -> snippets. A JSON file under ``directory`` when given, else in memory."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self._path = Path(directory) / "external_cache.json" if directory else None
        self._data: dict[str, list[dict[str, str]]] = {}
        if self._path is not None and self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    @staticmethod
    def key(provider: str, query: str) -> str:
        return hashlib.sha256(f"{provider}\x00{query.strip().lower()}".encode()).hexdigest()[:24]

    def get(self, key: str) -> list[ExternalSnippet] | None:
        rows = self._data.get(key)
        return None if rows is None else [ExternalSnippet(**row) for row in rows]

    def put(self, key: str, snippets: Iterable[ExternalSnippet]) -> None:
        self._data[key] = [{"title": s.title, "text": s.text, "url": s.url} for s in snippets]
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._data)


@dataclass(slots=True)
class ExternalBroker:
    provider: ExternalProvider
    cache: EvidenceCache = field(default_factory=EvidenceCache)
    max_calls: int = 20
    max_results: int = 3
    stats: ExternalStats = field(default_factory=ExternalStats)

    async def lookup(
        self, question: str, private_terms: Iterable[str], *, allow_network: bool
    ) -> ExternalResult:
        query: PublicQuery | None = build_public_query(question, private_terms)
        if query is None:
            self.stats.refused += 1
            return ExternalResult("filtered_empty")
        key = self.cache.key(self.provider.name, query.text)
        cached = self.cache.get(key)
        if cached is not None:
            self.stats.cache_hits += 1
            snippets = tuple(cached[: self.max_results])
            return ExternalResult(
                "cache_hit" if snippets else "no_results", query.text, snippets, self.provider.name
            )
        self.stats.cache_misses += 1
        if not allow_network:
            return ExternalResult("cache_miss", query.text, (), self.provider.name)
        if self.stats.network_calls >= self.max_calls:
            self.stats.refused += 1
            return ExternalResult("budget_exhausted", query.text, (), self.provider.name)
        self.stats.network_calls += 1
        try:
            found = await self.provider.search(query.text, self.max_results)
        except ProviderUnavailableError:
            self.stats.network_calls -= 1  # nothing was sent
            self.stats.errors += 1
            return ExternalResult("provider_unavailable", query.text, (), self.provider.name)
        except ProviderError as exc:
            self.stats.errors += 1
            _log.warning("external.provider_error", error=str(exc))
            return ExternalResult("provider_error", query.text, (), self.provider.name)
        self.cache.put(key, found)  # an empty answer is cached too: the budget is not re-spent
        snippets = tuple(found[: self.max_results])
        return ExternalResult(
            "ok" if snippets else "no_results", query.text, snippets, self.provider.name
        )


def format_public_block(result: ExternalResult) -> str:
    """The context text for usable external evidence: marker, clause, then one line each."""
    lines = [f"{PUBLIC_MARKER} {PUBLIC_KNOWLEDGE_CLAUSE}"]
    for snippet in result.snippets:
        title = f"{snippet.title}: " if snippet.title else ""
        source = f" ({snippet.url})" if snippet.url else ""
        lines.append(f"- {title}{snippet.text}{source}")
    return "\n".join(lines)
