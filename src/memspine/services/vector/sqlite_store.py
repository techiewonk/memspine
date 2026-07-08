"""Zero-dep vector fallback: float32 blobs in SQLite, brute-force cosine.

Correct and dependency-free for the ``simple`` profile and tests; LanceDB
(``[lance]``) is the scalable default (D-09). Vectors are L2-normalized at
upsert so query-time scoring is a dot product.

E4 quantized rescore (ADR-020): when constructed with a ``quantization``
(``"int8"``/``"binary"``) and/or a Matryoshka ``matryoshka_dim``, ``upsert``
also stores a cheap prefilter representation and ``search_rescore`` runs the
two-stage search — quantized/truncated prefilter over an oversampled candidate
window → exact float32 cosine rescore → ``top_k``. Constructed WITHOUT those
(the default), the store is byte-identical to the float32-only path and
``search_rescore`` degenerates to ``query`` (the backward-compat guard).
"""

from __future__ import annotations

import math
import struct

from sqlalchemy import delete, select

from memspine.clients.sqlite import SQLiteClient
from memspine.config import constants
from memspine.services.storage.sqlite.schema import memory_embeddings
from memspine.services.vector.base import VectorHit
from memspine.services.vector.quantize import (
    hamming_score,
    int8_score,
    quantize_binary,
    quantize_int8,
    truncate_normalize,
)

__all__ = ["SQLiteVectorStore"]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(raw: bytes) -> list[float]:
    count = len(raw) // 4
    return list(struct.unpack(f"<{count}f", raw))


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


class SQLiteVectorStore:
    def __init__(
        self,
        client: SQLiteClient,
        *,
        quantization: str | None = None,
        matryoshka_dim: int | None = None,
        oversample: int = constants.RESCORE_OVERSAMPLE,
    ) -> None:
        if quantization not in (None, "int8", "binary"):
            raise ValueError(f"unknown quantization {quantization!r} (valid: int8, binary, None)")
        self._client = client
        self._quantization = quantization
        self._matryoshka_dim = matryoshka_dim
        self._oversample = max(1, oversample)

    @property
    def _rescore_active(self) -> bool:
        """The two-stage path is live only when a prefilter representation
        actually exists — quantization or Matryoshka truncation is configured."""
        return self._quantization is not None or self._matryoshka_dim is not None

    def _prefilter_vector(self, normalized: list[float]) -> list[float]:
        """Apply Matryoshka truncation for the prefilter stage (no-op without)."""
        if self._matryoshka_dim is None:
            return normalized
        return truncate_normalize(normalized, self._matryoshka_dim)

    def _encode(self, normalized: list[float]) -> bytes | None:
        """Build the stored prefilter codes from a full normalized vector."""
        prefilter = self._prefilter_vector(normalized)
        if self._quantization == "int8":
            return quantize_int8(prefilter)
        if self._quantization == "binary":
            return quantize_binary(prefilter)
        return None  # Matryoshka-only: the prefilter reads truncated float32.

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        normalized = _normalize(vector)
        values: dict[str, object] = {
            "namespace": namespace,
            "embedder_id": embedder_id,
            "dim": len(normalized),
            "vector": _pack(normalized),
            # Recompute the prefilter columns on every upsert (they must track
            # the current vector); NULL when the store carries no quantization.
            "quantized_vec": self._encode(normalized),
            "quantization": self._quantization,
        }
        stmt = sqlite_insert(memory_embeddings).values(record_id=record_id, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["record_id"], set_=values)
        async with self._client.engine.begin() as conn:
            await conn.execute(stmt)

    async def query(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        query_vec = _normalize(vector)
        # embedder_id + dim filters: rows from a different embedder (model swap
        # without rebuild) are excluded — comparing across dimensions would
        # silently truncate and rank garbage.
        stmt = select(memory_embeddings.c.record_id, memory_embeddings.c.vector).where(
            memory_embeddings.c.namespace == namespace,
            memory_embeddings.c.embedder_id == embedder_id,
            memory_embeddings.c.dim == len(query_vec),
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        hits = [
            VectorHit(
                record_id=row[0],
                score=sum(a * b for a, b in zip(query_vec, _unpack(row[1]), strict=True)),
            )
            for row in rows
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    async def search_rescore(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        """E4 two-stage search (ADR-020): a cheap same-scale prefilter over the
        whole group picks an oversampled candidate window; an exact float32
        cosine rescore then re-ranks only that window → ``top_k``.

        Stage 1 reads only the small quantized codes (NOT the float corpus) for
        rows coded under the store's active scheme; rows with NULL codes or a
        different stored scheme/dim (a mixed table right after enabling
        quantization, or a config flip without a rebuild) are scored by float
        cosine from a SECOND small SELECT over just that subset. Every candidate
        — coded or float-fallback — lands on a common ~[-1, 1] scale, so the
        window drops none unfairly. Stage 2 reads float vectors for the window
        only. This is why ``search_rescore`` reads LESS than ``query`` in the
        common (all-coded) case, which is E4's whole point.

        With no quantization/Matryoshka configured this is exactly ``query`` —
        the backward-compat guard that keeps ``profile="simple"`` byte-identical.
        """
        if not self._rescore_active:
            return await self.query(namespace, vector, embedder_id, top_k)

        query_vec = _normalize(vector)
        prefilter_query = self._prefilter_vector(query_vec)
        pf_dim = len(prefilter_query)
        # The active query code (None when Matryoshka-only: no scheme configured,
        # so every row is scored on the float-fallback path).
        active_code: bytes | None = None
        if self._quantization == "int8":
            active_code = quantize_int8(prefilter_query)
        elif self._quantization == "binary":
            active_code = quantize_binary(prefilter_query)

        # Stage 1a — read ONLY the cheap codes (+ their scheme) for the group.
        code_stmt = select(
            memory_embeddings.c.record_id,
            memory_embeddings.c.quantized_vec,
            memory_embeddings.c.quantization,
        ).where(
            memory_embeddings.c.namespace == namespace,
            memory_embeddings.c.embedder_id == embedder_id,
            memory_embeddings.c.dim == len(query_vec),
        )
        async with self._client.engine.connect() as conn:
            code_rows = (await conn.execute(code_stmt)).all()

        scored: list[tuple[float, str]] = []
        fallback_ids: list[str] = []
        for record_id, quantized, scheme in code_rows:
            # A row is "coded" only if it carries codes built under the SAME
            # active scheme AND the same byte length (a changed matryoshka_dim
            # keeps the scheme string but shifts the length) — else the compare
            # would zip mismatched bytes. Anything else falls back to float.
            if (
                active_code is not None
                and quantized is not None
                and scheme == self._quantization
                and len(quantized) == len(active_code)
            ):
                scored.append((self._coded_score(active_code, quantized, pf_dim), record_id))
            else:
                fallback_ids.append(record_id)

        # Stage 1b — fetch float vectors ONLY for the fallback subset (empty in
        # the common post-rebuild case) and score them on the SAME ~[-1, 1] scale
        # as the coded rows, so neither cohort is squeezed out of the window.
        if fallback_ids:
            fb_stmt = select(memory_embeddings.c.record_id, memory_embeddings.c.vector).where(
                memory_embeddings.c.record_id.in_(fallback_ids)
            )
            async with self._client.engine.connect() as conn:
                fb_rows = (await conn.execute(fb_stmt)).all()
            for record_id, float_bytes in fb_rows:
                stored = self._prefilter_vector(_unpack(float_bytes))
                scored.append(
                    (sum(a * b for a, b in zip(stored, prefilter_query, strict=True)), record_id)
                )

        # Merge the same-scale scores and take the oversample window.
        scored.sort(key=lambda item: item[0], reverse=True)
        window_ids = [rid for _, rid in scored[: max(top_k * self._oversample, top_k)]]
        if not window_ids:
            return []

        # Stage 2 — read float vectors for the window ONLY and rescore exactly.
        rescore_stmt = select(memory_embeddings.c.record_id, memory_embeddings.c.vector).where(
            memory_embeddings.c.record_id.in_(window_ids)
        )
        async with self._client.engine.connect() as conn:
            rescore_rows = (await conn.execute(rescore_stmt)).all()
        hits = [
            VectorHit(
                record_id=record_id,
                score=sum(a * b for a, b in zip(query_vec, _unpack(float_bytes), strict=True)),
            )
            for record_id, float_bytes in rescore_rows
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    def _coded_score(self, query_code: bytes, stored_code: bytes, pf_dim: int) -> float:
        """Normalized ~[-1, 1] prefilter score for a row coded under the active
        scheme — int8 dot or negative Hamming, both mapped onto the common scale
        the float-fallback cosine already lives on."""
        if self._quantization == "int8":
            return int8_score(query_code, stored_code)
        return hamming_score(query_code, stored_code, pf_dim)

    async def delete(self, record_id: str) -> None:
        async with self._client.engine.begin() as conn:
            await conn.execute(
                delete(memory_embeddings).where(memory_embeddings.c.record_id == record_id)
            )

    async def delete_all(self) -> None:
        async with self._client.engine.begin() as conn:
            await conn.execute(delete(memory_embeddings))

    async def exists(self, record_id: str) -> bool:
        """M7 ``forget --verify`` support: is a row still present?"""
        stmt = select(memory_embeddings.c.record_id).where(
            memory_embeddings.c.record_id == record_id
        )
        async with self._client.engine.connect() as conn:
            return (await conn.execute(stmt)).first() is not None
