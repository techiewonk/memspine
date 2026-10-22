"""Qwen3-Reranker as a local cross-encoder (G5a, ``read.rerank: qwen3``, ``[st]`` extra).

Qwen/Qwen3-Reranker-0.6B (Apache-2.0) is a causal LM fine-tuned to answer "yes" or "no"
to *does this document meet the query, under this instruction?*. The score is the
model's documented formulation (model card, "Usage with Transformers"):

* the prompt is a fixed system turn ("Judge whether the Document meets the requirements
  based on the Query and the Instruct provided. Note that the answer can only be "yes"
  or "no".") followed by ``<Instruct>: ... <Query>: ... <Document>: ...`` as the user
  turn and an empty ``<think>`` block opening the assistant turn;
* the relevance score is P(yes) from the last position's logits restricted to the
  ``yes`` and ``no`` tokens: ``softmax([no, yes])[1]``.

transformers and torch are imported lazily: constructing the reranker only checks they
are importable (raising :class:`MissingServiceError` naming the ``st`` extra when they
are not, which the factory turns into a skipped rerank stage); the weights load on the
first ``rerank`` call, under a lock, inside the worker thread that scores. Scoring runs
in batches in a worker thread, so the event loop stays free. Engine-built instances
(``weights`` connector injected, :mod:`memspine.clients.weights.transformers`) share the loaded tokenizer, weights and inference lock per process,
keyed by model, device and quantisation (ADR-079 / RM-73); the instruction, batch size and
length budget stay per holder.

On CPU (``device`` unset or ``"cpu"``) the weights are kept in float32. The checkpoint is
stored in bfloat16 and transformers>=5 loads it as such; on a CPU without native bf16
that is ~6x slower per pair, and the left-padded batches then drift from unbatched
scores by up to ~0.05 P(yes). In float32 the batched scores match the model card's
unbatched formulation exactly (checked against the real 0.6B weights, 2026-10-06).
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Sequence
from typing import Any

from memspine.clients.weights.shared import SharedWeights
from memspine.clients.weights.transformers import (
    Qwen3RerankerSpec,
    load_qwen3_reranker,
    require,
)
from memspine.clients.weights.transformers import Qwen3Weights as _Qwen3Weights
from memspine.config import constants

__all__ = ["DEFAULT_INSTRUCTION", "DEFAULT_MODEL", "Qwen3Reranker", "format_pair"]

DEFAULT_MODEL = constants.RERANK_QWEN3_MODEL
#: The task instruction (the model card recommends one per task; this one is for memory).
DEFAULT_INSTRUCTION = constants.RERANK_QWEN3_INSTRUCTION
_SERVICE = "services.rerank.qwen3 (transformers + torch)"


def format_pair(instruction: str, query: str, document: str) -> str:
    """The user-turn text for one (query, document) pair, as on the model card."""
    return f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"


class Qwen3Reranker:
    """``Reranker`` port over Qwen3-Reranker (P(yes) per document, input order)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        instruction: str = DEFAULT_INSTRUCTION,
        batch_size: int = 8,
        max_length: int = 8192,
        device: str | None = None,
        quant: str | None = None,
        weights: SharedWeights[_Qwen3Weights] | None = None,
    ) -> None:
        # Fail at construction (not first use) when the extra is missing, so the
        # factory's swallow-to-None turns it into one skip log at engine start.
        require("transformers", _SERVICE)
        self._torch = require("torch", _SERVICE)
        self._model_id = model
        self._instruction = instruction
        self._batch_size = max(1, batch_size)
        self._max_length = max_length
        self._device = device
        self._quant = quant
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._tokenizer: Any = None
        self._model: Any = None
        self._prefix: list[int] = []
        self._suffix: list[int] = []
        self._yes_id = -1
        self._no_id = -1
        self.reranker_id = f"qwen3:{model}" + (f":{quant}" if quant else "")
        #: The injected weights connector (an Engine build); None keeps the private sync load
        #: through :func:`~memspine.clients.weights.transformers.load_qwen3_reranker`.
        self._weights: SharedWeights[_Qwen3Weights] | None = weights
        self._warmed = False

    # -- loading ---------------------------------------------------------------------

    def _load_weights(self) -> _Qwen3Weights:
        return load_qwen3_reranker(Qwen3RerankerSpec(self._model_id, self._device, self._quant))

    def _adopt(self, loaded: _Qwen3Weights) -> None:
        self._prefix = loaded.prefix
        self._suffix = loaded.suffix
        self._yes_id = loaded.yes_id
        self._no_id = loaded.no_id
        self._tokenizer = loaded.tokenizer
        self._infer_lock = loaded.infer_lock
        self._model = loaded.model  # last: it is the "loaded" flag

    def _ensure_loaded(self) -> None:
        """Load tokenizer and weights once; concurrent first calls wait on the lock."""
        if self._model is not None:
            return
        if self._weights is not None:
            raise RuntimeError("a shared Qwen3 reranker loads through rerank() or warm()")
        with self._load_lock:
            if self._model is not None:
                return
            self._adopt(self._load_weights())

    async def warm(self) -> None:
        """Load the shared weights and score one fixed pair (ADR-079 warm hook)."""
        if self._weights is None:
            await asyncio.to_thread(self._ensure_loaded)
            return
        await self._weights.warm()
        loaded = await self._weights.get()
        self._adopt(loaded)
        if not self._warmed:  # the scoring warm-up is this adapter's, once per holder
            await asyncio.to_thread(self._score_with, loaded, "warm-up", ["warm-up"])
            self._warmed = True

    async def aclose(self) -> None:
        """Release this holder's weights; another holder keeps a shared load."""
        self._model = None
        self._tokenizer = None
        if self._weights is not None:
            await self._weights.aclose()

    # -- scoring ---------------------------------------------------------------------

    def _encode(self, texts: Sequence[str], loaded: _Qwen3Weights | None = None) -> Any:
        tok = self._tokenizer if loaded is None else loaded.tokenizer
        prefix = self._prefix if loaded is None else loaded.prefix
        suffix = self._suffix if loaded is None else loaded.suffix
        budget = self._max_length - len(prefix) - len(suffix)
        # B-5: query and document text is untrusted. ``split_special_tokens``
        # tokenizes a literal ``<|im_end|>`` in a memory as plain text, so a
        # stored document cannot close the user turn and forge the chat
        # template (the fixed prefix and suffix are encoded separately).
        enc = tok(
            list(texts),
            padding=False,
            truncation="longest_first",
            return_attention_mask=False,
            max_length=budget,
            split_special_tokens=True,
        )
        enc["input_ids"] = [prefix + list(ids) + suffix for ids in enc["input_ids"]]
        batch = tok.pad(enc, padding=True, return_tensors="pt", max_length=self._max_length)
        if self._device is not None:
            batch = {k: v.to(self._device) for k, v in batch.items()}
        return batch

    def _score_batch(
        self, texts: Sequence[str], loaded: _Qwen3Weights | None = None
    ) -> list[float]:
        batch = self._encode(texts, loaded)
        model = self._model if loaded is None else loaded.model
        yes_id = self._yes_id if loaded is None else loaded.yes_id
        no_id = self._no_id if loaded is None else loaded.no_id
        with self._torch.no_grad():
            last = model(**batch).logits[:, -1, :]
            yes = [float(v) for v in last[:, yes_id].tolist()]
            no = [float(v) for v in last[:, no_id].tolist()]
        # softmax over the two logits, P(yes) = 1 / (1 + exp(no - yes)), overflow-safe
        return [_p_yes(y, n) for y, n in zip(yes, no, strict=True)]

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Synchronous scoring (the worker-thread body of :meth:`rerank`)."""
        if not documents:
            return []
        self._ensure_loaded()
        texts = [format_pair(self._instruction, query, d) for d in documents]
        scores: list[float] = []
        with self._infer_lock:  # HF fast tokenizers are not safe to share across threads
            for start in range(0, len(texts), self._batch_size):
                scores.extend(self._score_batch(texts[start : start + self._batch_size]))
        return scores

    def _score_with(
        self, loaded: _Qwen3Weights, query: str, documents: Sequence[str]
    ) -> list[float]:
        """Score against an explicit load (the shared warm hook, before any holder adopts)."""
        texts = [format_pair(self._instruction, query, d) for d in documents]
        scores: list[float] = []
        with loaded.infer_lock:
            for start in range(0, len(texts), self._batch_size):
                scores.extend(self._score_batch(texts[start : start + self._batch_size], loaded))
        return scores

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        if self._weights is not None and self._model is None:
            self._adopt(await self._weights.get())
        return await asyncio.to_thread(self.score, query, documents)


def _p_yes(yes: float, no: float) -> float:
    diff = no - yes
    if diff > 0:
        e = math.exp(-diff)
        return e / (1.0 + e)
    return 1.0 / (1.0 + math.exp(diff))
