"""E4 vector quantization primitives (plan Part B §E4 / ADR-020).

Pure-Python, zero-dep (core stays slim, D-03): the SQLite vector store uses
these to build a cheap prefilter representation over an oversampled candidate
window, then re-ranks the survivors with an exact float32 cosine (``rescore``).

Every vector reaching here is already L2-normalized at upsert, so per-dimension
values live in ``[-1, 1]`` — the int8 calibration range is fixed at ``[-1, 1]``
(no per-vector min/max to store or drift). Quantization is lossy on purpose:
the rescore stage restores full-precision ranking, so the prefilter only has to
be *directional*, not exact (~95% recall at ~4-32x smaller prefilter reads).
"""

from __future__ import annotations

import struct

__all__ = [
    "hamming",
    "hamming_score",
    "int8_dot",
    "int8_score",
    "quantize_binary",
    "quantize_int8",
    "truncate_normalize",
]

#: Full-scale int8 magnitude: unit-normalized components in [-1, 1] map onto
#: [-127, 127] (127, not 128, keeps the range symmetric — no asymmetric clip).
_INT8_SCALE = 127


def truncate_normalize(vector: list[float], dim: int) -> list[float]:
    """Matryoshka prefix truncation: keep the first ``dim`` components and
    re-normalize (a truncated prefix is no longer unit-length). ``dim`` at or
    above the vector length is a no-op — never pads."""
    if dim >= len(vector):
        return vector
    prefix = vector[:dim]
    norm = sum(component * component for component in prefix) ** 0.5
    if norm == 0.0:
        return prefix
    return [component / norm for component in prefix]


def quantize_int8(vector: list[float]) -> bytes:
    """Linear symmetric int8: ``round(x * 127)`` clamped to [-127, 127]."""
    codes = []
    for component in vector:
        code = round(component * _INT8_SCALE)
        codes.append(max(-_INT8_SCALE, min(_INT8_SCALE, code)))
    return struct.pack(f"<{len(codes)}b", *codes)


def int8_dot(a: bytes, b: bytes) -> int:
    """Dot product of two int8 code strings — monotonic with cosine over
    unit-normalized inputs, so it ranks the prefilter without a divide."""
    left = struct.unpack(f"<{len(a)}b", a)
    right = struct.unpack(f"<{len(b)}b", b)
    return sum(x * y for x, y in zip(left, right, strict=True))


def int8_score(a: bytes, b: bytes) -> float:
    """Normalized int8 similarity in ~[-1, 1]: ``int8_dot / (127^2 * dim)`` (one
    int8 byte per dim, so ``dim = len(a)``). Monotonic with the raw dot, hence
    with cosine — but on a COMMON scale so int8, binary, and float-fallback
    candidates can be ranked together in one merged prefilter list (ADR-020 §1:
    "mixed tables never drop candidates")."""
    dim = len(a)
    if dim == 0:
        return 0.0
    return int8_dot(a, b) / (_INT8_SCALE * _INT8_SCALE * dim)


def quantize_binary(vector: list[float]) -> bytes:
    """Sign-bit packing: one bit per dimension (``1`` when ``x >= 0``), MSB-first
    within each byte, zero-padded to a byte boundary. ~32x smaller than float32.

    Sign convention: the ``>= 0`` boundary maps an EXACT-zero component to a set
    bit (treated as non-negative). A genuinely all-zero vector therefore packs to
    an all-ones code and reads Hamming-0 (a spurious "perfect match") against any
    all-positive query — a degenerate case with no meaningful direction. This is
    tolerated, not guarded: the binary stage is only a lossy prefilter and the
    exact float32 rescore (which scores the zero vector at cosine 0) corrects the
    ranking before anything reaches a result."""
    out = bytearray((len(vector) + 7) // 8)
    for index, component in enumerate(vector):
        if component >= 0.0:
            out[index >> 3] |= 0x80 >> (index & 7)
    return bytes(out)


def hamming(a: bytes, b: bytes) -> int:
    """Population count of the XOR — the number of differing sign bits. Smaller
    means more similar, so the prefilter ranks by ``-hamming``."""
    return int.from_bytes(bytes(x ^ y for x, y in zip(a, b, strict=True)), "big").bit_count()


def hamming_score(a: bytes, b: bytes, dim: int) -> float:
    """Normalized sign-bit similarity in [-1, 1]: ``(dim - 2*hamming) / dim`` —
    ``1`` at Hamming 0 (all signs agree), ``-1`` at Hamming ``dim`` (all differ),
    monotonically decreasing in Hamming. ``dim`` is the true component count (not
    ``len(a)`` bytes); zero-padding bits never differ, so they add no distance.
    Puts binary candidates on the same ~[-1, 1] scale as int8 and float-fallback
    for one merged prefilter ranking (ADR-020 §1)."""
    if dim == 0:
        return 0.0
    return (dim - 2 * hamming(a, b)) / dim
