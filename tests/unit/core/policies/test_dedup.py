"""Two-stage dedup sketches (M5/D-27): minhash round-trip, LSH recall, simhash."""

from __future__ import annotations

import random

import numpy as np
import pytest
import xxhash
from datasketch import MinHash, MinHashLSH

from memspine.core.policies.dedup import DedupPolicy
from memspine.store.records import MemoryRecord


def rec(content: str) -> MemoryRecord:
    return MemoryRecord(namespace="n", memory_type="semantic", content=content)


def _reference_simhash64(text: str) -> int:
    weights = [0] * 64
    for token in text.lower().split():
        digest = xxhash.xxh64_intdigest(token.encode("utf-8"))
        for bit in range(64):
            weights[bit] += 1 if (digest >> bit) & 1 else -1
    value = sum(1 << bit for bit, weight in enumerate(weights) if weight > 0)
    return value - (1 << 64) if value >= (1 << 63) else value


def test_signature_round_trip_preserves_similarity() -> None:
    policy = DedupPolicy.bind()
    text = "alice prefers her coffee black in the morning"
    original = policy.minhash(text)
    restored = policy.minhash_from_signature(policy.minhash_signature(text))
    assert original.jaccard(restored) == 1.0


@pytest.mark.parametrize("num_perm", [64, 128, 256, 512])
def test_batched_minhash_preserves_persisted_signature_and_lsh_candidates(num_perm: int) -> None:
    policy = DedupPolicy.bind({"minhash_num_perm": num_perm, "lsh_threshold": 0.4})
    batch_size = max(1, 65_536 // num_perm)
    boundary_text = " ".join(f"token-{index}" for index in range(batch_size * 2 + 1))
    texts = [
        "",
        "one",
        "repeat repeat repeat repeated",
        "café niño Málaga 東京",
        "alice prefers dark coffee in the morning",
        "alice likes strong dark coffee each morning",
        "unrelated records discuss database indexes and query planning",
        boundary_text,
    ]
    reference_index = MinHashLSH(threshold=0.4, num_perm=num_perm)
    actual_index = policy.new_index()
    for index, text in enumerate(texts):
        reference = MinHash(num_perm=num_perm)
        for token in set(text.lower().split()):
            reference.update(token.encode())
        actual = policy.minhash(text)
        reference_signature = bytes(reference.hashvalues.astype("<u8").tobytes())
        assert bytes(actual.hashvalues.astype("<u8").tobytes()) == reference_signature
        assert actual.scheme == reference.scheme
        assert actual.seed == reference.seed
        for expected_perm, actual_perm in zip(
            reference.permutations, actual.permutations, strict=True
        ):
            np.testing.assert_array_equal(actual_perm, expected_perm)
        restored = policy.minhash_from_signature(policy.minhash_signature(text))
        np.testing.assert_array_equal(restored.hashvalues, reference.hashvalues)

        record = rec(text).model_copy(update={"minhash_sig": reference_signature})
        reference_index.insert(record.record_id, reference)
        policy.index_add(actual_index, record)

    query = "alice enjoys dark coffee early in the morning"
    reference_query = MinHash(num_perm=num_perm)
    for token in set(query.lower().split()):
        reference_query.update(token.encode())
    actual_query_record = policy.annotate(rec(query))
    reference_candidates = {str(item) for item in reference_index.query(reference_query)}
    actual_candidates = set(policy.candidates(actual_index, actual_query_record))
    assert actual_candidates == reference_candidates


def test_lsh_finds_near_duplicates_not_strangers() -> None:
    policy = DedupPolicy.bind({"lsh_threshold": 0.5})
    index = policy.new_index()
    a = policy.annotate(rec("alice prefers her coffee black in the morning"))
    b = policy.annotate(rec("completely unrelated sentence about database indexes"))
    policy.index_add(index, a)
    policy.index_add(index, b)

    near_dup = policy.annotate(rec("alice prefers her coffee black in the mornings"))
    candidates = policy.candidates(index, near_dup)
    assert a.record_id in candidates
    assert b.record_id not in candidates

    stranger = policy.annotate(rec("quantum flux capacitors resonate at dawn"))
    assert policy.candidates(index, stranger) == []


def test_annotate_sets_both_sketches_and_index_add_is_idempotent() -> None:
    policy = DedupPolicy.bind()
    record = policy.annotate(rec("some content"))
    assert record.simhash is not None and record.minhash_sig
    assert len(record.minhash_sig) == 128 * 8  # num_perm x uint64

    index = policy.new_index()
    policy.index_add(index, record)
    policy.index_add(index, record)  # double-insert must not raise
    assert record.record_id in policy.candidates(index, record)


def test_simhash_close_for_similar_texts_and_fits_sqlite() -> None:
    policy = DedupPolicy.bind()
    a = policy.simhash64("the quick brown fox jumps over the lazy dog")
    b = policy.simhash64("the quick brown fox jumps over the lazy dogs")
    c = policy.simhash64("entirely different topic on financial regulation")

    def dist(x: int, y: int) -> int:
        return bin((x ^ y) & ((1 << 64) - 1)).count("1")

    assert dist(a, b) < dist(a, c)
    for value in (a, b, c):  # signed 64-bit: storable in SQLite INTEGER
        assert -(1 << 63) <= value < (1 << 63)


def test_simhash_chunked_kernel_preserves_votes_and_persisted_signature() -> None:
    policy = DedupPolicy.bind()
    rng = random.Random(27081)
    texts = [
        "",
        "one",
        "one two",
        "repeat repeat repeat",
        "niño café Málaga 東京",
        " ".join(f"token-{rng.randrange(500)}" for _ in range(8193)),
    ]
    for text in texts:
        expected_signature = policy.minhash_signature(text)
        record = policy.annotate(rec(text))
        assert record.simhash == _reference_simhash64(text)
        assert record.minhash_sig == expected_signature

    # Two inputs create exactly tied bit votes; ties must remain unset.
    tied_text = "alpha beta"
    hashes = [xxhash.xxh64_intdigest(token.encode("utf-8")) for token in tied_text.split()]
    tied_bits = [bit for bit in range(64) if ((hashes[0] >> bit) & 1) != ((hashes[1] >> bit) & 1)]
    assert tied_bits
    result = policy.simhash64(tied_text) & ((1 << 64) - 1)
    for bit in tied_bits:
        assert ((result >> bit) & 1) == 0
