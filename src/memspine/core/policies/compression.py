"""Compression policy (M6/D-32): zstd cold-tier compression, views-not-replacements.

``compress`` moves ``content`` into ``content_zstd`` (zstd, one shared level —
constants.ZSTD_LEVEL, same as event payloads at rest, D-45) and leaves the
``content_fingerprint`` untouched, so the original stays verifiable after a
round-trip. Summarized *views* of memories never replace originals (M6);
compression only changes at-rest encoding, never meaning.

E5 assembly-stage fallbacks (llmlingua, ``[compress]``) stay config-reserved.
"""

from __future__ import annotations

from typing import ClassVar

import zstandard
from pydantic import Field

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord
from memspine.exceptions import StorageError

__all__ = ["CompressionPolicy"]


class CompressionOptions(PolicyOptions):
    cold_tier_zstd_level: int = constants.ZSTD_LEVEL
    # Tiers whose records get their content compressed at rest (M3 names).
    compress_tiers: list[str] = Field(default_factory=lambda: ["dormant"])
    # E5 ordered fallbacks (assembly-time, [compress] extra) — reserved.
    assembly_stage: list[str] = Field(default_factory=lambda: ["drop_lowest_score"])


class CompressionPolicy(BindablePolicy):
    name: ClassVar[str] = "compression"
    Options: ClassVar[type[PolicyOptions]] = CompressionOptions

    def _options(self) -> CompressionOptions:
        options = self.options
        assert isinstance(options, CompressionOptions)
        return options

    def should_compress(self, record: MemoryRecord) -> bool:
        """Cold-tier candidates: the record's tier is in the configured set and
        its content has not already been moved to the compressed column."""
        return (
            record.tier in self._options().compress_tiers
            and record.content_zstd is None
            and bool(record.content)
        )

    def compress(self, record: MemoryRecord) -> MemoryRecord:
        """Move content into ``content_zstd``. The fingerprint (computed from
        the original content at write time) is deliberately preserved: it is
        what makes the round-trip verifiable."""
        compressor = zstandard.ZstdCompressor(level=self._options().cold_tier_zstd_level)
        return record.model_copy(
            update={
                "content_zstd": compressor.compress(record.content.encode("utf-8")),
                "content": "",
            }
        )

    def inflate(self, record: MemoryRecord) -> MemoryRecord:
        """Restore compressed content for reads. No-op for uncompressed records."""
        if record.content_zstd is None or record.content:
            return record
        raw = zstandard.ZstdDecompressor().decompress(record.content_zstd)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:  # corrupt at-rest data must be loud
            raise StorageError(
                f"cold-tier content for record {record.record_id} is not valid UTF-8"
            ) from exc
        return record.model_copy(update={"content": content, "content_zstd": None})
