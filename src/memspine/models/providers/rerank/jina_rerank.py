"""Jina listwise reranker (``read.rerank: jina``, ``[st]`` extra, CC-BY-NC-4.0 weights).

``jinaai/jina-reranker-v3.5`` (0.6B, Qwen3-0.6B base) ranks a query against many documents
in one forward pass ("last-but-not-late" listwise scoring) through its custom
``model.rerank(query, documents)``. That call returns documents sorted by
``relevance_score`` with each entry's ``index`` into the input; this adapter puts the
scores back into input order, as the ``Reranker`` port requires.

The weights load on first use, under a lock, in the worker thread that scores. The model
ships custom code, so loading needs ``trust_remote_code=True``: a deployer who picks
``rerank: jina`` opts into running that repository's Python.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from typing import Any

from memspine.exceptions import MissingServiceError

__all__ = ["DEFAULT_MODEL", "JinaReranker"]

DEFAULT_MODEL = "jinaai/jina-reranker-v3.5"
_SERVICE = "services.rerank.jina (transformers + torch)"


class JinaReranker:
    """``Reranker`` port over a Jina listwise reranker."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        device: str | None = None,
        max_documents: int = 64,
    ) -> None:
        try:
            self._transformers = importlib.import_module("transformers")
            importlib.import_module("torch")
        except ImportError as exc:
            raise MissingServiceError(_SERVICE, extra="st") from exc
        self._model_id = model
        self._device = device
        self._max_documents = max(1, max_documents)
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._model: Any = None
        self.reranker_id = f"jina:{model}"

    def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                model = self._transformers.AutoModel.from_pretrained(
                    self._model_id, dtype="auto", trust_remote_code=True
                )
                if self._device is not None:
                    model = model.to(self._device)
                self._model = model.eval()
        return self._model

    def score(self, query: str, documents: list[str]) -> list[float]:
        model = self._ensure_loaded()
        scores = [0.0] * len(documents)
        # Listwise scoring sees one window of documents at a time; scores from separate
        # windows are not comparable, so a pool beyond the window keeps the tail at 0.0.
        window = documents[: self._max_documents]
        with self._infer_lock:
            results = model.rerank(query, window)
        for item in results:
            scores[int(item["index"])] = float(item["relevance_score"])
        return scores

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        return await asyncio.to_thread(self.score, query, documents)
