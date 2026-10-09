"""Local torch embedder over sentence-transformers (``embedding.provider: st``, ``[st]`` extra).

For models fastembed cannot load, e.g. Qwen3-Embedding-0.6B (BF16 on the GPU). Queries
take the model's retrieval instruction (``embedding.query_instruction``), documents
are embedded bare. The weights load on first use inside a worker thread.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from memspine.exceptions import MissingServiceError
from memspine.services.embedding.base import EmbedderManifest

__all__ = ["SentenceTransformersEmbedding"]


class SentenceTransformersEmbedding:
    def __init__(
        self,
        model: str,
        dim: int,
        *,
        query_instruction: str | None = None,
        device: str | None = None,
        dtype: str | None = None,
        batch_size: int = 32,
        query_prompt_name: str | None = None,
        document_prompt_name: str | None = None,
        trust_remote_code: bool = False,
    ) -> None:
        try:
            importlib.import_module("sentence_transformers")
        except ImportError as exc:
            raise MissingServiceError("services.embedding.st", extra="st") from exc
        self._model_name = model
        self._dim = dim
        self._query_instruction = query_instruction or None
        self._device = device
        self._dtype = dtype
        self._batch_size = max(1, batch_size)
        self._query_prompt_name = query_prompt_name
        self._document_prompt_name = document_prompt_name
        self._trust_remote_code = trust_remote_code
        self._model: Any = None
        self._lock = asyncio.Lock()

    @property
    def embedder_id(self) -> str:
        return f"st:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        return EmbedderManifest(embedder_id=self.embedder_id, dim=self._dim)

    async def _ensure_model(self) -> Any:
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    self._model = await asyncio.to_thread(self._load)
        return self._model

    def _load(self) -> Any:
        from sentence_transformers import SentenceTransformer

        kwargs: dict[str, Any] = {}
        if self._dtype:
            kwargs["model_kwargs"] = {"torch_dtype": self._dtype}
        model = SentenceTransformer(
            self._model_name,
            device=self._device,
            trust_remote_code=self._trust_remote_code,
            **kwargs,
        )
        model.max_seq_length = min(model.max_seq_length or 8192, 8192)
        return model

    async def embed(
        self, texts: list[str], *, prompt_name: str | None = None
    ) -> list[list[float]]:
        model = await self._ensure_model()
        prompt_name = prompt_name or self._document_prompt_name

        def _run() -> list[list[float]]:
            vecs = model.encode(
                texts,
                batch_size=self._batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                **({"prompt_name": prompt_name} if prompt_name else {}),
            )
            return [[float(x) for x in v[: self._dim]] for v in vecs]

        return await asyncio.to_thread(_run)

    @property
    def query_variant(self) -> str:
        tag = (self._query_instruction or "") + "|" + (self._query_prompt_name or "")
        if tag == "|":
            return ""
        import xxhash

        return ":" + xxhash.xxh64_hexdigest(tag.encode())

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        if self._query_prompt_name:
            return await self.embed(texts, prompt_name=self._query_prompt_name)
        if self._query_instruction is None:
            return await self.embed(texts)
        return await self.embed([self._query_instruction + t for t in texts])
