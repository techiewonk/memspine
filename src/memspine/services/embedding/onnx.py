"""ONNX embedding adapter; model arithmetic and downloads belong to its connector."""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.clients.weights.onnx import text_weights
from memspine.clients.weights.onnx_catalogue import (
    CATALOGUE,
    OnnxModelSpec,
    legacy_embedder_id,
    resolve_model_spec,
)
from memspine.clients.weights.shared import SharedWeights
from memspine.services.embedding.base import EmbedderManifest

_KNOWN_DIMS = {name: spec.dim for name, spec in CATALOGUE.items() if spec.pooling != "score"}


class OnnxEmbedding:
    def __init__(
        self,
        model: str = "BAAI/bge-small-en-v1.5",
        query_instruction: str | None = None,
        dim: int | None = None,
        *,
        weights: SharedWeights[Any] | None = None,
        model_spec: dict[str, Any] | OnnxModelSpec | None = None,
        batch_size: int = 256,
        threads: int = 0,
        providers: list[str] | None = None,
        local_files_only: bool = False,
    ) -> None:
        self._model_name = model
        self._spec = resolve_model_spec(model, model_spec)
        if self._spec.pooling == "score":
            raise ValueError("ONNX score model cannot be used for embedding")
        if dim is not None and dim != self._spec.dim:
            raise ValueError("configured ONNX dimension conflicts with known model metadata")
        if batch_size < 1:
            raise ValueError("ONNX batch_size must be positive")
        self._dim = self._expected_dim = self._spec.dim
        self._query_instruction = query_instruction or None
        self._model_spec = model_spec
        self._batch_size = batch_size
        self._model: Any = None
        self._weights = (
            weights
            if weights is not None
            else text_weights(
                model,
                model_spec=model_spec,
                threads=threads,
                providers=providers,
                local_files_only=local_files_only,
            )
        )

    @property
    def embedder_id(self) -> str:
        if self._model_spec is None:
            return f"embedder:{self._model_name}"
        # An arbitrary export must not impersonate the certified catalogue space.
        import hashlib
        import json
        from dataclasses import asdict

        suffix = hashlib.sha256(
            json.dumps(asdict(self._spec), sort_keys=True).encode()
        ).hexdigest()[:16]
        return f"onnx:{self._model_name}:{suffix}"

    @property
    def legacy_embedder_id(self) -> str | None:
        return legacy_embedder_id(
            self._model_name, model_spec=self._model_spec, query_instruction=self._query_instruction
        )

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        return EmbedderManifest(embedder_id=self.embedder_id, dim=self.dim)

    async def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = await self._weights.get()
        return self._model

    async def warm(self) -> None:
        await self._weights.warm()

    async def aclose(self) -> None:
        self._model = None
        await self._weights.aclose()

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = await self._ensure_model()
        vectors = await asyncio.to_thread(
            lambda: [
                list(map(float, vector))
                for vector in model.embed(texts, batch_size=self._batch_size)
            ]
        )
        if len(vectors) != len(texts) or any(
            len(vector) != self._expected_dim for vector in vectors
        ):
            raise ValueError("ONNX output dimension/count differs from configured model metadata")
        return vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        prefix = self._spec.passage_prefix
        return await self._embed([prefix + text for text in texts] if prefix else texts)

    @property
    def query_variant(self) -> str:
        instruction = self._query_instruction or self._spec.query_prefix
        if not instruction:
            return ""
        import xxhash

        return ":" + xxhash.xxh64_hexdigest(instruction.encode())

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        prefix = self._query_instruction or self._spec.query_prefix
        return await self._embed([prefix + text for text in texts] if prefix else texts)
