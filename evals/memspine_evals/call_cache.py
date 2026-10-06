"""A content-addressed disk cache for paid LiteLLM calls (screening runs).

``install_call_cache(path)`` wraps ``litellm.acompletion`` and ``litellm.aembedding``
(whatever they are at the time: the real transport, or :mod:`.stub_llm` in tests) with
a SQLite cache, so a second run that sends the same request is served from disk at $0.

What is cached:

* **Embeddings**, one row per input text, keyed on ``model``, ``input_type``,
  ``dimensions`` and the text. A batch is split: cached texts are served, only the
  missing ones go to the provider (in their original order), and the vectors are
  stored as float64, so a replay is bit-identical.
* **Engine-role completions** (extract / mine, summarize, reflect, anticipate,
  extract_edges, plan, ...), keyed on ``model``, the full ``messages``,
  ``temperature``, ``max_tokens``, ``response_format`` and the other sampling
  options that change the reply (``top_p``, ``stop``, ``seed``, ``tools``,
  ``tool_choice``). Credentials, endpoints and timeouts are not part of the key.

What is never cached:

* a completion with a non-zero ``temperature`` (or ``n`` > 1, or streaming): it is
  passed through and counted as ``uncacheable``. A request that sends **no**
  temperature (the engine's roles do not set one) is keyed with ``temperature:
  null`` and cached: a hit replays the reply the reference run recorded, which is
  exactly what a screen of memory-side changes against that run needs;
* reader and judge completions, unless the cache was installed with
  ``cache_reader=True``: the runner marks them with :func:`reader_scope`, and inside
  that scope the cache passes them through (counted as ``bypassed``).

A hit returns the stored reply with its usage zeroed (the original token counts are
kept for ``usd_saved``), so whoever meters the reply charges nothing for it. Keys are
the SHA-256 of a canonical JSON form (sorted keys, no whitespace), so the same request
gives the same key in every process. Several processes may share one cache file
(WAL mode, a busy timeout); a key is written once (``INSERT OR IGNORE``).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import time
from array import array
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

__all__ = [
    "CACHE_FILE",
    "CacheStats",
    "CallCache",
    "chat_key",
    "embed_key",
    "install_call_cache",
    "reader_scope",
]

#: The SQLite file inside ``--cache-dir``.
CACHE_FILE = "llm_cache.sqlite"
#: Bumped when the key or row format changes (old rows are then simply never hit).
KEY_VERSION = 1

#: Completion options that change the reply, and so belong in the key.
_CHAT_KEY_OPTIONS = (
    "max_tokens",
    "response_format",
    "top_p",
    "stop",
    "seed",
    "tools",
    "tool_choice",
)

_READER_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "memspine_evals_reader_scope", default=False
)


@contextmanager
def reader_scope() -> Iterator[None]:
    """Mark the calls made inside the block as reader / judge calls (not cached unless
    the cache was installed with ``cache_reader=True``)."""
    token = _READER_SCOPE.set(True)
    try:
        yield
    finally:
        _READER_SCOPE.reset(token)


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _temperature(kwargs: Mapping[str, Any]) -> float | None:
    value = kwargs.get("temperature")
    return None if value is None else float(value)


def chat_key(kwargs: Mapping[str, Any]) -> str:
    """The cache key of a completion request (see the module docstring)."""
    payload: dict[str, Any] = {
        "v": KEY_VERSION,
        "kind": "chat",
        "model": str(kwargs.get("model") or ""),
        "messages": list(kwargs.get("messages") or []),
        "temperature": _temperature(kwargs),
    }
    for option in _CHAT_KEY_OPTIONS:
        payload[option] = kwargs.get(option)
    return _digest(payload)


def embed_key(model: str, text: str, input_type: str | None, dimensions: int | None) -> str:
    """The cache key of one embedded text."""
    return _digest(
        {
            "v": KEY_VERSION,
            "kind": "embed",
            "model": model,
            "input_type": input_type,
            "dimensions": None if dimensions is None else int(dimensions),
            "text": text,
        }
    )


def _approx_tokens(text: str) -> int:
    """The harness's heuristic count (four characters per token), for ``usd_saved``."""
    return max(1, len(text) // 4) if text else 0


@dataclass
class CacheStats:
    """Counters since the cache was installed. ``snapshot`` / ``since`` give per-arm deltas."""

    #: completions served from disk / sent to the provider and stored
    chat_hits: int = 0
    chat_misses: int = 0
    #: completions passed through: non-zero temperature, n > 1, streaming, empty reply
    uncacheable: int = 0
    #: reader / judge completions passed through (no ``cache_reader``)
    bypassed: int = 0
    #: embedded texts served from disk / sent to the provider and stored
    embed_hits: int = 0
    embed_misses: int = 0
    #: per model: hits and the tokens those hits would have cost
    chat_hit_tokens: dict[str, list[int]] = field(default_factory=dict)  # model -> [in, out]
    embed_hit_tokens: dict[str, int] = field(default_factory=dict)  # model -> tokens

    def snapshot(self) -> CacheStats:
        return CacheStats(
            chat_hits=self.chat_hits,
            chat_misses=self.chat_misses,
            uncacheable=self.uncacheable,
            bypassed=self.bypassed,
            embed_hits=self.embed_hits,
            embed_misses=self.embed_misses,
            chat_hit_tokens={m: list(t) for m, t in self.chat_hit_tokens.items()},
            embed_hit_tokens=dict(self.embed_hit_tokens),
        )

    def since(self, before: CacheStats) -> CacheStats:
        """The counters accumulated after ``before`` was taken."""
        chat_tokens: dict[str, list[int]] = {}
        for model, (p, c) in self.chat_hit_tokens.items():
            p0, c0 = before.chat_hit_tokens.get(model, [0, 0])
            if p - p0 or c - c0:
                chat_tokens[model] = [p - p0, c - c0]
        embed_tokens = {
            m: t - before.embed_hit_tokens.get(m, 0)
            for m, t in self.embed_hit_tokens.items()
            if t - before.embed_hit_tokens.get(m, 0)
        }
        return CacheStats(
            chat_hits=self.chat_hits - before.chat_hits,
            chat_misses=self.chat_misses - before.chat_misses,
            uncacheable=self.uncacheable - before.uncacheable,
            bypassed=self.bypassed - before.bypassed,
            embed_hits=self.embed_hits - before.embed_hits,
            embed_misses=self.embed_misses - before.embed_misses,
            chat_hit_tokens=chat_tokens,
            embed_hit_tokens=embed_tokens,
        )

    def usd_saved(
        self,
        prices_per_mtok: Mapping[str, tuple[float, float]] | None = None,
        service_prices: Mapping[str, float] | None = None,
    ) -> float:
        """What the hits would have cost: completion tokens at ``prices_per_mtok``,
        embedding tokens at ``service_prices["embed:<model>"]`` (USD per 1M tokens)."""
        prices = prices_per_mtok or {}
        services = service_prices or {}
        usd = 0.0
        for model, (p, c) in self.chat_hit_tokens.items():
            p_in, p_out = prices.get(model, (0.0, 0.0))
            usd += p / 1e6 * p_in + c / 1e6 * p_out
        for model, tokens in self.embed_hit_tokens.items():
            usd += tokens / 1e6 * services.get(f"embed:{model}", 0.0)
        return usd

    def to_dict(
        self,
        prices_per_mtok: Mapping[str, tuple[float, float]] | None = None,
        service_prices: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        return {
            "cache_hits": self.chat_hits + self.embed_hits,
            "cache_misses": self.chat_misses + self.embed_misses,
            "usd_saved": round(self.usd_saved(prices_per_mtok, service_prices), 8),
            "chat_hits": self.chat_hits,
            "chat_misses": self.chat_misses,
            "embed_hits": self.embed_hits,
            "embed_misses": self.embed_misses,
            "uncacheable": self.uncacheable,
            "bypassed": self.bypassed,
            "chat_hit_tokens": {m: list(t) for m, t in self.chat_hit_tokens.items()},
            "embed_hit_tokens": dict(self.embed_hit_tokens),
        }


def _response(content: str, finish_reason: str, model: str | None) -> Any:
    """A LiteLLM-shaped completion reply (what the engine adapter and readers read)."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason, index=0
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=None,
            cache_read_input_tokens=None,
        ),
        model=model,
        memspine_cache_hit=True,
    )


def _vector_bytes(vector: Any) -> bytes:
    return array("d", (float(v) for v in vector)).tobytes()


def _bytes_vector(blob: bytes) -> list[float]:
    values = array("d")
    values.frombytes(blob)
    return list(values)


class CallCache:
    """The SQLite store plus the wrapping transport functions."""

    def __init__(self, cache_dir: str | Path, cache_reader: bool = False) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / CACHE_FILE
        self.cache_reader = cache_reader
        self.stats = CacheStats()
        self._db = sqlite3.connect(str(self.path), timeout=60.0, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=60000")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS chat (key TEXT PRIMARY KEY, model TEXT, "
            "content TEXT, finish_reason TEXT, prompt_tokens INTEGER, "
            "completion_tokens INTEGER, created REAL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS embed (key TEXT PRIMARY KEY, model TEXT, "
            "vector BLOB, created REAL)"
        )
        self._inner_completion: Any = None
        self._inner_embedding: Any = None

    def close(self) -> None:
        self._db.close()

    def describe(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "cache_reader": self.cache_reader,
            "key_version": KEY_VERSION,
        }

    # -- completion ------------------------------------------------------------

    def cacheable(self, kwargs: Mapping[str, Any]) -> bool:
        temperature = _temperature(kwargs)
        if temperature not in (None, 0.0):
            return False
        return not (kwargs.get("stream") or int(kwargs.get("n") or 1) != 1)

    async def acompletion(self, **kwargs: Any) -> Any:
        if _READER_SCOPE.get() and not self.cache_reader:
            self.stats.bypassed += 1
            return await self._inner_completion(**kwargs)
        if not self.cacheable(kwargs):
            self.stats.uncacheable += 1
            return await self._inner_completion(**kwargs)
        key = chat_key(kwargs)
        row = self._db.execute(
            "SELECT content, finish_reason, prompt_tokens, completion_tokens, model "
            "FROM chat WHERE key = ?",
            (key,),
        ).fetchone()
        if row is not None:
            content, finish, prompt_tokens, completion_tokens, model = row
            self.stats.chat_hits += 1
            acc = self.stats.chat_hit_tokens.setdefault(str(model), [0, 0])
            acc[0] += int(prompt_tokens or 0)
            acc[1] += int(completion_tokens or 0)
            return _response(str(content), str(finish or "stop"), kwargs.get("model"))
        response = await self._inner_completion(**kwargs)
        try:
            choice = response.choices[0]
            content = choice.message.content
        except (AttributeError, IndexError, TypeError):
            content = None
        if content is None:
            self.stats.uncacheable += 1
            return response
        usage = getattr(response, "usage", None)
        self._db.execute(
            "INSERT OR IGNORE INTO chat VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                str(kwargs.get("model") or ""),
                str(content),
                str(getattr(choice, "finish_reason", "") or "stop"),
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
                time.time(),
            ),
        )
        self.stats.chat_misses += 1
        return response

    # -- embedding -------------------------------------------------------------

    async def aembedding(self, **kwargs: Any) -> Any:
        inputs = kwargs.get("input") or []
        texts = [inputs] if isinstance(inputs, str) else [str(t) for t in inputs]
        model = str(kwargs.get("model") or "")
        input_type = kwargs.get("input_type")
        dimensions = kwargs.get("dimensions")
        keys = [embed_key(model, t, input_type, dimensions) for t in texts]
        vectors: list[list[float] | None] = []
        for key in keys:
            row = self._db.execute("SELECT vector FROM embed WHERE key = ?", (key,)).fetchone()
            vectors.append(None if row is None else _bytes_vector(row[0]))
        missing = [i for i, v in enumerate(vectors) if v is None]
        hits = len(texts) - len(missing)
        if hits:
            self.stats.embed_hits += hits
            self.stats.embed_hit_tokens[model] = self.stats.embed_hit_tokens.get(model, 0) + sum(
                _approx_tokens(texts[i]) for i, v in enumerate(vectors) if v is not None
            )
        usage = SimpleNamespace(prompt_tokens=0, total_tokens=0)
        if missing:
            call = dict(kwargs)
            call["input"] = [texts[i] for i in missing]
            response = await self._inner_embedding(**call)
            fresh = [list(item["embedding"]) for item in response.data]
            if len(fresh) != len(missing):
                raise RuntimeError(
                    f"embedding provider returned {len(fresh)} vectors for {len(missing)} inputs"
                )
            now = time.time()
            for index, vector in zip(missing, fresh, strict=True):
                vectors[index] = vector
                self._db.execute(
                    "INSERT OR IGNORE INTO embed VALUES (?, ?, ?, ?)",
                    (keys[index], model, _vector_bytes(vector), now),
                )
            self.stats.embed_misses += len(missing)
            usage = getattr(response, "usage", usage)
        data = [
            {"embedding": vector, "index": i, "object": "embedding"}
            for i, vector in enumerate(vectors)
        ]
        return SimpleNamespace(data=data, usage=usage, model=model, memspine_cache_hits=hits)


@contextmanager
def install_call_cache(cache_dir: str | Path, cache_reader: bool = False) -> Iterator[CallCache]:
    """Wrap LiteLLM's completion and embedding entry points with a disk cache for the
    duration of the block (the rerank entry point is left alone)."""
    import litellm

    cache = CallCache(cache_dir, cache_reader=cache_reader)
    saved = (litellm.acompletion, litellm.aembedding)
    cache._inner_completion, cache._inner_embedding = saved
    litellm.acompletion = cache.acompletion
    litellm.aembedding = cache.aembedding
    try:
        yield cache
    finally:
        litellm.acompletion, litellm.aembedding = saved
        cache.close()
