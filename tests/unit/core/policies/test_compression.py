"""Compression policy (M6/D-32): cold-tier zstd round-trip."""

from __future__ import annotations

import pytest
import zstandard

from memspine.core.policies.compression import CompressionPolicy
from memspine.core.records import MemoryRecord
from memspine.exceptions import StorageError


def rec(tier: str = "dormant", content: str = "a long-forgotten memory " * 20) -> MemoryRecord:
    return MemoryRecord(namespace="agent/a", memory_type="episodic", content=content, tier=tier)


def test_round_trip_preserves_content_and_fingerprint() -> None:
    policy = CompressionPolicy.bind()
    original = rec()
    packed = policy.compress(original)
    assert packed.content == ""
    assert packed.content_zstd is not None
    assert len(packed.content_zstd) < len(original.content.encode())  # actually smaller

    restored = policy.inflate(packed)
    assert restored.content == original.content
    assert restored.content_zstd is None
    assert restored.content_fingerprint == original.content_fingerprint  # verifiable


def test_should_compress_only_configured_tiers_and_once() -> None:
    policy = CompressionPolicy.bind()
    assert policy.should_compress(rec(tier="dormant"))
    assert not policy.should_compress(rec(tier="hot"))
    assert not policy.should_compress(rec(tier="cold"))  # default: dormant only
    assert not policy.should_compress(policy.compress(rec()))  # already packed
    assert not policy.should_compress(rec(content=""))  # nothing to pack

    wider = CompressionPolicy.bind({"compress_tiers": ["cold", "dormant"]})
    assert wider.should_compress(rec(tier="cold"))


def test_inflate_is_noop_for_uncompressed_records() -> None:
    policy = CompressionPolicy.bind()
    record = rec(tier="hot")
    assert policy.inflate(record) is record


def test_corrupt_cold_tier_data_is_loud() -> None:
    policy = CompressionPolicy.bind()
    packed = policy.compress(rec())
    assert packed.content_zstd is not None
    corrupt = packed.model_copy(update={"content_zstd": b"\x28\xb5\x2f\xfdjunk"})
    with pytest.raises((StorageError, zstandard.ZstdError)):
        policy.inflate(corrupt)
