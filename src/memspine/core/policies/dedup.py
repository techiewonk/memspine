"""Dedup policy (M5/D-27): MinHash-LSH stage-1 candidates → cosine stage-2.

Pure sketch math (datasketch); the semantic store orchestrates the async
stage-2 embedding confirm and the union-preserving merge. Signatures are
persisted on the record (``minhash_sig``/``simhash``) so the LSH index is
reconstructable without re-reading content.
"""

from __future__ import annotations

from typing import ClassVar

import xxhash
from datasketch import MinHash, MinHashLSH

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["DedupPolicy"]


def _tokens(text: str) -> list[str]:
    return text.lower().split()


class DedupOptions(PolicyOptions):
    minhash_num_perm: int = constants.MINHASH_NUM_PERM
    lsh_threshold: float = constants.LSH_THRESHOLD
    cosine_threshold: float = constants.DEDUP_COSINE_THRESHOLD


class DedupPolicy(BindablePolicy):
    name: ClassVar[str] = "dedup"
    Options: ClassVar[type[PolicyOptions]] = DedupOptions

    @property
    def _opts(self) -> DedupOptions:
        options = self.options
        assert isinstance(options, DedupOptions)
        return options

    @property
    def cosine_threshold(self) -> float:
        return self._opts.cosine_threshold

    # ── sketches (computed at write, stored on the record, D-27) ────────────

    def minhash(self, text: str) -> MinHash:
        sketch = MinHash(num_perm=self._opts.minhash_num_perm)
        for token in set(_tokens(text)):
            sketch.update(token.encode())
        return sketch

    def minhash_signature(self, text: str) -> bytes:
        return bytes(self.minhash(text).hashvalues.astype("<u8").tobytes())

    def minhash_from_signature(self, signature: bytes) -> MinHash:
        import numpy as np

        values = np.frombuffer(signature, dtype="<u8")
        # datasketch >= 2.0 requires the hashing scheme when reconstructing;
        # use the same scheme a fresh MinHash of this policy would get.
        scheme = MinHash(num_perm=1).scheme
        return MinHash(num_perm=self._opts.minhash_num_perm, hashvalues=values, scheme=scheme)

    def simhash64(self, text: str) -> int:
        """64-bit simhash over xxh64 token hashes — cheap pre-filter distance.

        Returned as a SIGNED 64-bit integer (two's complement) so it fits a
        SQLite INTEGER column; XOR-popcount distances must mask to 64 bits.
        """
        weights = [0] * 64
        for token in _tokens(text):
            digest = xxhash.xxh64_intdigest(token)
            for bit in range(64):
                weights[bit] += 1 if (digest >> bit) & 1 else -1
        value = 0
        for bit in range(64):
            if weights[bit] > 0:
                value |= 1 << bit
        return value - (1 << 64) if value >= (1 << 63) else value

    def annotate(self, record: MemoryRecord) -> MemoryRecord:
        """Attach both sketches to a record before it passes the write door."""
        return record.model_copy(
            update={
                "simhash": self.simhash64(record.content),
                "minhash_sig": self.minhash_signature(record.content),
            }
        )

    # ── stage-1 index (per-namespace, maintained by the semantic store) ─────

    def new_index(self) -> MinHashLSH:
        return MinHashLSH(threshold=self._opts.lsh_threshold, num_perm=self._opts.minhash_num_perm)

    def index_add(self, index: MinHashLSH, record: MemoryRecord) -> None:
        if record.minhash_sig and record.record_id not in index:
            index.insert(record.record_id, self.minhash_from_signature(record.minhash_sig))

    def candidates(self, index: MinHashLSH, record: MemoryRecord) -> list[str]:
        if not record.minhash_sig:
            return []
        return [str(key) for key in index.query(self.minhash_from_signature(record.minhash_sig))]
