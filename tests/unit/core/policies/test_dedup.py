"""Two-stage dedup sketches (M5/D-27): minhash round-trip, LSH recall, simhash."""

from __future__ import annotations

from memspine.core.policies.dedup import DedupPolicy
from memspine.core.records import MemoryRecord


def rec(content: str) -> MemoryRecord:
    return MemoryRecord(namespace="n", memory_type="semantic", content=content)


def test_signature_round_trip_preserves_similarity() -> None:
    policy = DedupPolicy.bind()
    text = "alice prefers her coffee black in the morning"
    original = policy.minhash(text)
    restored = policy.minhash_from_signature(policy.minhash_signature(text))
    assert original.jaccard(restored) == 1.0


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
