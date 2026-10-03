"""Retrievers shared by the reference systems.

Two, for a reason. ``BM25Retriever`` is exact, offline and free, so every
plumbing test and every ablation that is not *about* embeddings can run without
a model. ``FastEmbedRetriever`` is the dense leg — the one that makes the
MemPalace comparison a like-for-like test rather than an analogy, since their
claim rests specifically on *default embeddings over raw text*.

Which one ran is recorded in the system's ``describe()`` and therefore in the
manifest. A dense claim tested with the lexical retriever would be a different
experiment wearing the same name.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


@dataclass(slots=True)
class Unit:
    """One indexable unit and the turns it came from."""

    unit_id: str
    text: str
    turn_ids: tuple[str, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Retriever(Protocol):
    retriever_id: str

    def describe(self) -> Mapping[str, Any]: ...

    def add(self, unit: Unit) -> None: ...

    def search(self, query: str, top_k: int) -> list[tuple[Unit, float]]: ...

    def remove(self, unit_id: str) -> None: ...

    def clear(self) -> None: ...


class BM25Retriever:
    """Okapi BM25, pure stdlib. Deterministic and exactly reproducible.

    Scores are recomputed from the current corpus on every search rather than
    cached, because the corpus grows between queries in a streaming run and a
    stale IDF would quietly change what "top-k" means mid-item.
    """

    retriever_id = "bm25"

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._units: list[Unit] = []
        self._tokens: list[list[str]] = []
        self._df: dict[str, int] = {}

    def describe(self) -> Mapping[str, Any]:
        return {"retriever_id": self.retriever_id, "k1": self.k1, "b": self.b, "dense": False}

    def add(self, unit: Unit) -> None:
        tokens = tokenize(unit.text)
        self._units.append(unit)
        self._tokens.append(tokens)
        for term in set(tokens):
            self._df[term] = self._df.get(term, 0) + 1

    def remove(self, unit_id: str) -> None:
        """Drop a unit (and its document frequencies); unknown ids are ignored."""
        for i in range(len(self._units) - 1, -1, -1):
            if self._units[i].unit_id == unit_id:
                for term in set(self._tokens[i]):
                    self._df[term] -= 1
                    if not self._df[term]:
                        del self._df[term]
                del self._units[i]
                del self._tokens[i]

    def clear(self) -> None:
        self._units.clear()
        self._tokens.clear()
        self._df.clear()

    def search(self, query: str, top_k: int) -> list[tuple[Unit, float]]:
        if not self._units:
            return []
        q_terms = tokenize(query)
        n = len(self._units)
        avgdl = sum(len(t) for t in self._tokens) / n
        scored: list[tuple[int, float]] = []
        for i, tokens in enumerate(self._tokens):
            if not tokens:
                continue
            counts: dict[str, int] = {}
            for term in tokens:
                counts[term] = counts.get(term, 0) + 1
            dl = len(tokens)
            score = 0.0
            for term in q_terms:
                tf = counts.get(term)
                if not tf:
                    continue
                df = self._df.get(term, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = tf + self.k1 * (1 - self.b + self.b * dl / avgdl)
                score += idf * (tf * (self.k1 + 1)) / denom
            if score > 0:
                scored.append((i, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return [(self._units[i], score) for i, score in scored[:top_k]]


class FastEmbedRetriever:
    """Dense retrieval over ``fastembed`` (ONNX, CPU, local, free).

    memspine already depends on fastembed for its own default embedder (D-08),
    so a dense run costs nothing beyond CPU time and a one-off model download —
    which matters, because it means the MemPalace comparison can be run
    honestly without a paid API.
    """

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", batch_size: int = 32) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - needs the dependency
            raise RuntimeError(
                "FastEmbedRetriever needs fastembed — `uv sync` inside memspine, "
                "or run the lexical BM25Retriever and say so in the manifest"
            ) from exc
        self._embedder = TextEmbedding(model_name=model)
        self.model = model
        self.batch_size = batch_size
        self.retriever_id = f"fastembed:{model}"
        self._units: list[Unit] = []
        self._vectors: list[Sequence[float]] = []

    def describe(self) -> Mapping[str, Any]:
        return {"retriever_id": self.retriever_id, "model": self.model, "dense": True}

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(map(float, vector)) for vector in self._embedder.embed(list(texts))]

    def add(self, unit: Unit) -> None:
        self._units.append(unit)
        self._vectors.append(self._embed([unit.text])[0])

    def add_many(self, units: Sequence[Unit]) -> None:
        """Batch path — the embedder is far faster per item this way."""
        if not units:
            return
        vectors = self._embed([u.text for u in units])
        self._units.extend(units)
        self._vectors.extend(vectors)

    def remove(self, unit_id: str) -> None:
        keep = [i for i, unit in enumerate(self._units) if unit.unit_id != unit_id]
        self._units = [self._units[i] for i in keep]
        self._vectors = [self._vectors[i] for i in keep]

    def clear(self) -> None:
        self._units.clear()
        self._vectors.clear()

    def search(self, query: str, top_k: int) -> list[tuple[Unit, float]]:
        if not self._units:
            return []
        q = self._embed([query])[0]
        scored = [(i, cosine(q, vector)) for i, vector in enumerate(self._vectors)]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return [(self._units[i], score) for i, score in scored[:top_k]]


class HybridRetriever:
    """Sparse + dense, fused by reciprocal rank (RRF).

    Condition 3 of the evaluation plan's five baselines, and the same fusion
    memspine's own read path uses by default (D-25). RRF is used rather than
    score addition because BM25 scores and cosine similarities are not on a
    common scale, and adding them is a quiet way to let one leg dominate.
    """

    def __init__(self, sparse: Retriever, dense: Retriever, k: int = 60) -> None:
        self.sparse = sparse
        self.dense = dense
        self.k = k
        self.retriever_id = f"hybrid-rrf({sparse.retriever_id}+{dense.retriever_id})"

    def describe(self) -> Mapping[str, Any]:
        return {
            "retriever_id": self.retriever_id,
            "fusion": "rrf",
            "rrf_k": self.k,
            "dense": True,
            "legs": [dict(self.sparse.describe()), dict(self.dense.describe())],
        }

    def add(self, unit: Unit) -> None:
        self.sparse.add(unit)
        self.dense.add(unit)

    def remove(self, unit_id: str) -> None:
        self.sparse.remove(unit_id)
        self.dense.remove(unit_id)

    def clear(self) -> None:
        self.sparse.clear()
        self.dense.clear()

    def search(self, query: str, top_k: int) -> list[tuple[Unit, float]]:
        pool = max(top_k * 4, top_k)
        fused: dict[str, float] = {}
        units: dict[str, Unit] = {}
        for leg in (self.sparse, self.dense):
            for rank, (unit, _) in enumerate(leg.search(query, pool)):
                units[unit.unit_id] = unit
                fused[unit.unit_id] = fused.get(unit.unit_id, 0.0) + 1.0 / (self.k + rank + 1)
        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        return [(units[unit_id], score) for unit_id, score in ordered[:top_k]]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
