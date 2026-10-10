"""I9: chunk-and-max wrapper for long documents (opt-in, ``read.rerank_chunk_chars``).

Audit (2026-10-10). The cross-encoder adapters cut a long document silently:

* ``qwen3``: the whole ``<Instruct>/<Query>/<Document>`` string is tokenised with
  ``truncation="longest_first"`` and ``max_length=8192`` minus the fixed template, so
  only a document beyond ~8k tokens is cut, and it is the TAIL that is dropped (the
  head is kept). A reply whose answer sits late in a very long assistant turn is
  scored on its opening.
* ``jina``: ``model.rerank`` applies the model's own context limit; no tail control.
* ``fastembed`` / ``flashrank`` / ``litellm``: the backend limit (typically 512 tokens
  for the ONNX cross-encoders), tail dropped.

Chunk-and-max splits a document longer than ``chunk_chars`` into overlapping windows,
scores each window against the query and gives the document the MAX window score, so
the best-matching passage decides the rank wherever it sits. Documents at or under the
limit are scored as-is, byte-identical to the unwrapped reranker. A leading ``[...]``
tag (the ``rerank_date_prefix`` ``[Date: ...]``) is repeated on every window.
"""

from __future__ import annotations

import re

from memspine.services.rerank.base import Reranker

__all__ = ["ChunkMaxReranker", "split_windows"]

_HEADER = re.compile(r"^\[[^\]\n]{1,80}\]\s")


def split_windows(text: str, chunk_chars: int, overlap: int = 0) -> list[str]:
    """Overlapping character windows over ``text``; one window if it fits.

    Windows end on whitespace where one lies in the last quarter of the window, so a
    word is not cut when it can be avoided."""
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [text]
    overlap = max(0, min(overlap, chunk_chars // 2))
    windows: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + chunk_chars)
        if end < n:
            cut = text.rfind(" ", start + (chunk_chars * 3) // 4, end)
            if cut > start:
                end = cut
        windows.append(text[start:end])
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return windows


class ChunkMaxReranker:
    """``Reranker`` that scores long documents window by window and takes the max."""

    def __init__(self, inner: Reranker, chunk_chars: int, overlap: int = 0) -> None:
        if chunk_chars < 1:
            raise ValueError("chunk_chars must be >= 1")
        self._inner = inner
        self._chunk_chars = chunk_chars
        self._overlap = overlap
        self.reranker_id = f"{inner.reranker_id}+chunkmax{chunk_chars}"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        flat: list[str] = []
        owner: list[int] = []
        for i, doc in enumerate(documents):
            header = ""
            body = doc
            m = _HEADER.match(doc)
            if m and len(doc) > self._chunk_chars:
                header, body = m.group(0), doc[m.end() :]
            windows = split_windows(body, max(1, self._chunk_chars - len(header)), self._overlap)
            for w in windows:
                flat.append(header + w if len(windows) > 1 else doc)
                owner.append(i)
        if len(flat) == len(documents):  # nothing was long: one inner call, unchanged
            return await self._inner.rerank(query, documents)
        scores = await self._inner.rerank(query, flat)
        best = [float("-inf")] * len(documents)
        for idx, s in zip(owner, scores, strict=True):
            best[idx] = max(best[idx], s)
        return best
