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
in batches in a worker thread, so the event loop stays free.

On CPU (``device`` unset or ``"cpu"``) the weights are kept in float32. The checkpoint is
stored in bfloat16 and transformers>=5 loads it as such; on a CPU without native bf16
that is ~6x slower per pair, and the left-padded batches then drift from unbatched
scores by up to ~0.05 P(yes). In float32 the batched scores match the model card's
unbatched formulation exactly (checked against the real 0.6B weights, 2026-10-06).
"""

from __future__ import annotations

import asyncio
import importlib
import math
import threading
from collections.abc import Sequence
from typing import Any

from memspine.exceptions import MissingServiceError

__all__ = ["DEFAULT_INSTRUCTION", "DEFAULT_MODEL", "Qwen3Reranker", "format_pair"]

DEFAULT_MODEL = "Qwen/Qwen3-Reranker-0.6B"
#: The task instruction (the model card recommends one per task; this one is for memory).
DEFAULT_INSTRUCTION = (
    "Given a question about a user's past conversations, judge whether the memory helps answer it"
)
_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the "
    'Query and the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n<|im_start|>user\n"
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_SERVICE = "services.rerank.qwen3 (transformers + torch)"


def format_pair(instruction: str, query: str, document: str) -> str:
    """The user-turn text for one (query, document) pair, as on the model card."""
    return f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"


def _import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise MissingServiceError(_SERVICE, extra="st") from exc


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
    ) -> None:
        # Fail at construction (not first use) when the extra is missing, so the
        # factory's swallow-to-None turns it into one skip log at engine start.
        self._transformers = _import("transformers")
        self._torch = _import("torch")
        self._model_id = model
        self._instruction = instruction
        self._batch_size = max(1, batch_size)
        self._max_length = max_length
        self._device = device
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._tokenizer: Any = None
        self._model: Any = None
        self._prefix: list[int] = []
        self._suffix: list[int] = []
        self._yes_id = -1
        self._no_id = -1
        self.reranker_id = f"qwen3:{model}"

    # -- loading ---------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load tokenizer and weights once; concurrent first calls wait on the lock."""
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            auto_tok = self._transformers.AutoTokenizer
            auto_lm = self._transformers.AutoModelForCausalLM
            tokenizer = auto_tok.from_pretrained(self._model_id, padding_side="left")
            model = auto_lm.from_pretrained(self._model_id).eval()
            if self._device is None or str(self._device).startswith("cpu"):
                model = model.float()  # bf16 on CPU: slow and drifts under padding
            if self._device is not None:
                model = model.to(self._device)
            self._prefix = list(tokenizer.encode(_PREFIX, add_special_tokens=False))
            self._suffix = list(tokenizer.encode(_SUFFIX, add_special_tokens=False))
            self._yes_id = int(tokenizer.convert_tokens_to_ids("yes"))
            self._no_id = int(tokenizer.convert_tokens_to_ids("no"))
            self._tokenizer = tokenizer
            self._model = model  # last: it is the "loaded" flag

    # -- scoring ---------------------------------------------------------------------

    def _encode(self, texts: Sequence[str]) -> Any:
        tok = self._tokenizer
        budget = self._max_length - len(self._prefix) - len(self._suffix)
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
        enc["input_ids"] = [self._prefix + list(ids) + self._suffix for ids in enc["input_ids"]]
        batch = tok.pad(enc, padding=True, return_tensors="pt", max_length=self._max_length)
        if self._device is not None:
            batch = {k: v.to(self._device) for k, v in batch.items()}
        return batch

    def _score_batch(self, texts: Sequence[str]) -> list[float]:
        batch = self._encode(texts)
        with self._torch.no_grad():
            last = self._model(**batch).logits[:, -1, :]
            yes = [float(v) for v in last[:, self._yes_id].tolist()]
            no = [float(v) for v in last[:, self._no_id].tolist()]
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

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        return await asyncio.to_thread(self.score, query, documents)


def _p_yes(yes: float, no: float) -> float:
    diff = no - yes
    if diff > 0:
        e = math.exp(-diff)
        return e / (1.0 + e)
    return 1.0 / (1.0 + math.exp(diff))
