"""BI-10: retained cold source text is inflated before background grounding."""

from __future__ import annotations

from memspine.compose.compression import CompressionPolicy
from memspine.ingest.windows import _load_source_record
from memspine.store.records import MemoryRecord


class _Store:
    def __init__(self, record: MemoryRecord | None) -> None:
        self.record = record

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        assert record_id == "source-1"
        return self.record


async def test_cold_source_is_inflated_exactly_before_grounding() -> None:
    policy = CompressionPolicy.bind()
    raw = MemoryRecord(
        record_id="source-1",
        namespace="acme/alice",
        memory_type="episodic",
        content="Alice wrote café 東京 and kept every byte. " * 8,
        tier="dormant",
    )
    cold = policy.compress(raw)
    assert cold.content == "" and cold.content_zstd is not None

    loaded = await _load_source_record(_Store(cold), policy, "source-1")  # type: ignore[arg-type]

    assert loaded is not None
    assert loaded.content == raw.content
    assert loaded.content_fingerprint == raw.content_fingerprint
    assert loaded.content_zstd is None


async def test_missing_cold_source_stays_missing() -> None:
    assert (
        await _load_source_record(_Store(None), CompressionPolicy.bind(), "source-1")  # type: ignore[arg-type]
        is None
    )
