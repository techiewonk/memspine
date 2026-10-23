"""Jina listwise reranker (a ``jina`` ``model.rerank.chain`` entry, ``[st]`` extra, CC-BY-NC-4.0 weights).

``jinaai/jina-reranker-v3.5`` (0.6B, Qwen3-0.6B base) ranks a query against many documents
in one forward pass ("last-but-not-late" listwise scoring) through its custom
``model.rerank(query, documents)``. That call returns documents sorted by
``relevance_score`` with each entry's ``index`` into the input; this adapter puts the
scores back into input order, as the ``Reranker`` port requires.

The weights load on first use, under a lock, in the worker thread that scores. The model
ships custom code, so loading needs ``trust_remote_code=True``: a deployer who picks
``rerank: jina`` opts into running that repository's Python. Engine-built instances
(``weights`` connector injected) share the loaded weights and inference lock per process, keyed by
model and device (ADR-079 / RM-73); the document window stays per holder.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from memspine.clients.weights.shared import SharedWeights
from memspine.clients.weights.transformers import JinaRerankerSpec, load_jina_reranker, require
from memspine.config import constants

__all__ = ["DEFAULT_MODEL", "JinaReranker"]

DEFAULT_MODEL = constants.RERANK_JINA_MODEL
_SERVICE = "services.rerank.jina (transformers + torch)"


class JinaReranker:
    """``Reranker`` port over a Jina listwise reranker."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        device: str | None = None,
        max_documents: int = 64,
        unscored_tail: bool = False,
        weights: SharedWeights[tuple[Any, Any]] | None = None,
    ) -> None:
        require("transformers", _SERVICE)
        require("torch", _SERVICE)
        self._model_id = model
        self._device = device
        self._max_documents = max(1, max_documents)
        #: R3-07 (the ``model.rerank.chain`` stage): a document beyond the window gets None (unscored), not 0.0.
        self.unscored_tail = unscored_tail
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._model: Any = None
        self.reranker_id = f"jina:{model}"
        #: The injected (model, inference lock) connector (an Engine build); None keeps the
        #: private sync load.
        self._weights: SharedWeights[tuple[Any, Any]] | None = weights

    def _load_model(self) -> Any:
        return load_jina_reranker(JinaRerankerSpec(self._model_id, self._device))

    def _adopt(self, loaded: tuple[Any, Any]) -> None:
        self._infer_lock = loaded[1]
        self._model = loaded[0]

    def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        if self._weights is not None:
            raise RuntimeError("a shared Jina reranker loads through rerank() or warm()")
        with self._load_lock:
            if self._model is None:
                self._model = self._load_model()
        return self._model

    async def warm(self) -> None:
        """Load the shared weights and rank one fixed pair (ADR-079 warm hook)."""
        if self._weights is None:
            await asyncio.to_thread(self._ensure_loaded)
            return
        await self._weights.warm()
        self._adopt(await self._weights.get())

    async def aclose(self) -> None:
        """Release this holder's weights; another holder keeps a shared load."""
        self._model = None
        if self._weights is not None:
            await self._weights.aclose()

    def score(self, query: str, documents: list[str]) -> list[Any]:
        model = self._ensure_loaded()
        scores: list[Any] = [None if self.unscored_tail else 0.0] * len(documents)
        # Listwise scoring sees one window of documents at a time; scores from separate
        # windows are not comparable, so a pool beyond the window keeps the tail at 0.0.
        window = documents[: self._max_documents]
        with self._infer_lock:
            results = model.rerank(query, window)
        for item in results:
            scores[int(item["index"])] = float(item["relevance_score"])
        return scores

    async def rerank(self, query: str, documents: list[str]) -> list[Any]:
        if not documents:
            return []
        if self._weights is not None and self._model is None:
            self._adopt(await self._weights.get())
        return await asyncio.to_thread(self.score, query, documents)
