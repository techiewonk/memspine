"""LIB-19: read-path caches with generation-aware invalidation and POL-1 revalidation (ER-31, R-26).

Pure and unwired (wired by R3-15). No I/O; the clock is injected. Caches are OFF by default: a disabled
cache misses every ``get`` and stores nothing (ER-31 B).

An entry records the store, erasure and consent generations it was written under. Generations are not
part of the lookup slot, so a bump finds the old entry, sees it is out of date, evicts it and misses.
Every read result is revalidated (POL-1) immediately before it is returned, even on a hit.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.public.plugins import CacheKey

CacheKind = Literal["embedding", "extraction", "rerank", "read"]


def _h(*parts: str) -> str:
    blob = json.dumps(list(parts), separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ keys


@dataclass(frozen=True)
class ReadCacheKey:
    """A ``CacheKey`` (ER-31 fields) plus cache kind, kind-specific components and consent generation."""

    kind: CacheKind
    base: CacheKey
    extra: tuple[tuple[str, str], ...] = ()
    consent_generation: int | None = None

    @property
    def store_generation(self) -> int:
        return self.base.store_generation

    @property
    def erasure_generation(self) -> int:
        return self.base.erasure_generation

    def slot(self) -> str:
        """Generation-free digest: the lookup slot. A generation bump reuses the slot and evicts."""
        b = self.base.model_copy(update={"store_generation": 0, "erasure_generation": 0})
        return _h(self.kind, b.digest(), json.dumps(sorted(self.extra)))

    def digest(self) -> str:
        """Full digest including every generation (for logging and audit)."""
        cg = "" if self.consent_generation is None else str(self.consent_generation)
        return _h(self.kind, self.base.digest(), json.dumps(sorted(self.extra)), cg)


def candidate_set_hash(candidate_ids: Iterable[str]) -> str:
    """Order-insensitive hash of a candidate set."""
    return _h(*sorted(set(candidate_ids)))


def embedding_key(
    *,
    scope: OwnerScope,
    text: str,
    embedder_id: str,
    vector_space_version: str,
    config_hash: str = "default",
    store_generation: int = 0,
    erasure_generation: int = 0,
    consent_generation: int | None = None,
) -> ReadCacheKey:
    """Embedding key: embedder id and vector-space version. The raw text is hashed in, because the
    query normalisation (case folding) must not conflate texts a case-sensitive embedder separates."""
    base = CacheKey(
        scope=scope,
        query=text,
        config_hash=_h(config_hash, text),
        model_ids=[embedder_id],
        store_generation=store_generation,
        erasure_generation=erasure_generation,
    )
    return ReadCacheKey(
        "embedding", base, (("vector_space_version", vector_space_version),), consent_generation
    )


def extraction_key(
    *,
    scope: OwnerScope,
    text: str,
    model_id: str,
    prompt_hash: str,
    config_hash: str = "default",
    store_generation: int = 0,
    erasure_generation: int = 0,
    consent_generation: int | None = None,
) -> ReadCacheKey:
    """Extraction key: model id (the old key omitted it) and prompt hash."""
    base = CacheKey(
        scope=scope,
        query=text,
        config_hash=_h(config_hash, text),
        model_ids=[model_id],
        store_generation=store_generation,
        erasure_generation=erasure_generation,
    )
    return ReadCacheKey("extraction", base, (("prompt_hash", prompt_hash),), consent_generation)


def rerank_key(
    *,
    scope: OwnerScope,
    viewer: str | None,
    query: str,
    reranker_id: str,
    candidate_ids: Iterable[str],
    config_hash: str = "default",
    store_generation: int = 0,
    erasure_generation: int = 0,
    consent_generation: int | None = None,
) -> ReadCacheKey:
    """Rerank key: reranker id and the candidate set hash (order-insensitive)."""
    base = CacheKey(
        scope=scope,
        viewer=viewer,
        query=query,
        config_hash=config_hash,
        model_ids=[reranker_id],
        store_generation=store_generation,
        erasure_generation=erasure_generation,
    )
    return ReadCacheKey(
        "rerank", base, (("candidates", candidate_set_hash(candidate_ids)),), consent_generation
    )


def read_key(
    *,
    scope: OwnerScope,
    viewer: str | None,
    query: str,
    config_hash: str,
    model_ids: Iterable[str] = (),
    store_generation: int,
    erasure_generation: int,
    consent_generation: int | None = None,
) -> ReadCacheKey:
    """Read-result key: all seven ER-31 fields plus the optional consent generation."""
    base = CacheKey(
        scope=scope,
        viewer=viewer,
        query=query,
        config_hash=config_hash,
        model_ids=list(model_ids),
        store_generation=store_generation,
        erasure_generation=erasure_generation,
    )
    return ReadCacheKey("read", base, (), consent_generation)


# ------------------------------------------------------------------ revalidation (POL-1)


@dataclass(frozen=True)
class ItemsResult[T]:
    """A read result: items, plus whether revalidation dropped any (callers may recompute)."""

    items: tuple[T, ...]
    revalidated_partial: bool = False
    dropped: int = 0


def revalidate[T](result: ItemsResult[T], predicate: Callable[[T], bool]) -> ItemsResult[T]:
    """POL-1: keep only items the predicate accepts. If any were dropped, mark the result partial."""
    kept = tuple(i for i in result.items if predicate(i))
    n = len(result.items) - len(kept)
    if n == 0:
        return result
    return ItemsResult(items=kept, revalidated_partial=True, dropped=result.dropped + n)


# ------------------------------------------------------------------ backend


@dataclass(frozen=True)
class CacheEntry:
    value: object
    store_generation: int
    erasure_generation: int
    consent_generation: int | None
    expires_at: float | None


class CacheBackend(Protocol):
    def get(self, slot: str) -> CacheEntry | None: ...
    def put(self, slot: str, entry: CacheEntry) -> None: ...
    def delete(self, slot: str) -> None: ...
    def __len__(self) -> int: ...


class InMemoryBackend:
    """LRU with a size bound. TTL is enforced by ``ReadCache`` using its injected clock."""

    def __init__(self, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max = max_entries
        self._d: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, slot: str) -> CacheEntry | None:
        e = self._d.get(slot)
        if e is not None:
            self._d.move_to_end(slot)
        return e

    def put(self, slot: str, entry: CacheEntry) -> None:
        self._d[slot] = entry
        self._d.move_to_end(slot)
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def delete(self, slot: str) -> None:
        self._d.pop(slot, None)

    def __len__(self) -> int:
        return len(self._d)


# ------------------------------------------------------------------ cache


class ReadCache[V]:
    """One cache for one kind. ``enabled=False`` (default): every get misses, nothing is stored."""

    def __init__(
        self,
        kind: CacheKind,
        *,
        enabled: bool = False,
        backend: CacheBackend | None = None,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if ttl_seconds is not None and clock is None:
            raise ValueError("a clock must be injected when a ttl is set")
        self.kind = kind
        self.enabled = enabled
        self._backend: CacheBackend = backend if backend is not None else InMemoryBackend()
        self._ttl = ttl_seconds
        self._clock = clock

    def _check_kind(self, key: ReadCacheKey) -> None:
        if key.kind != self.kind:
            raise ValueError(f"key of kind {key.kind!r} used on a {self.kind!r} cache")

    def put(self, key: ReadCacheKey, value: V) -> None:
        if not self.enabled:
            return
        self._check_kind(key)
        exp = None if self._ttl is None or self._clock is None else self._clock() + self._ttl
        self._backend.put(
            key.slot(),
            CacheEntry(
                value, key.store_generation, key.erasure_generation, key.consent_generation, exp
            ),
        )

    def get(self, key: ReadCacheKey, revalidate: Callable[[V], V] | None = None) -> V | None:
        """Return the cached value or None. ``key`` carries the *current* generations.

        ``revalidate`` (POL-1) runs on every hit before return. If it changes the value (something was
        dropped), the stored entry is evicted so the dropped item cannot come back from the cache.
        """
        if not self.enabled:
            return None
        self._check_kind(key)
        slot = key.slot()
        e = self._backend.get(slot)
        if e is None:
            return None
        if self._stale(e, key):
            self._backend.delete(slot)
            return None
        value: V = e.value  # type: ignore[assignment]
        if revalidate is None:
            return value
        out = revalidate(value)
        if out != value:
            self._backend.delete(slot)
        return out

    def _stale(self, e: CacheEntry, key: ReadCacheKey) -> bool:
        if e.expires_at is not None and self._clock is not None and self._clock() >= e.expires_at:
            return True
        return (
            e.store_generation != key.store_generation
            or e.erasure_generation != key.erasure_generation
            or e.consent_generation != key.consent_generation
        )

    def __len__(self) -> int:
        return len(self._backend)


@dataclass(frozen=True)
class ReadCaches:
    """The four per-kind caches. All off unless ``enabled`` is passed."""

    embedding: ReadCache[object]
    extraction: ReadCache[object]
    rerank: ReadCache[ItemsResult[object]]
    read: ReadCache[ItemsResult[object]]

    @classmethod
    def build(
        cls,
        *,
        enabled: bool = False,
        max_entries: int = 1024,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ReadCaches:
        def mk(kind: CacheKind) -> ReadCache:  # type: ignore[type-arg]
            return ReadCache(
                kind,
                enabled=enabled,
                backend=InMemoryBackend(max_entries),
                ttl_seconds=ttl_seconds,
                clock=clock,
            )

        return cls(mk("embedding"), mk("extraction"), mk("rerank"), mk("read"))
