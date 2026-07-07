"""Compression policy (M6/D-32 + E5/D-51): cold-tier zstd + assembly-stage fitting.

Cold tier (M6): ``compress`` moves ``content`` into ``content_zstd`` (zstd, one
shared level — constants.ZSTD_LEVEL, same as event payloads at rest, D-45) and
leaves the ``content_fingerprint`` untouched, so the original stays verifiable
after a round-trip. Summarized *views* of memories never replace originals
(M6); compression only changes at-rest encoding, never meaning.

Assembly stage (E5, config-activated, default OFF): ``fit_assembly`` applies
the ordered fallbacks ``drop_lowest_score`` → ``llmlingua`` (``[compress]``,
lazy import, one info log when absent) → ``provider_compaction`` (no-op seam)
to squeeze an over-budget selection into the token budget. Protected blocks —
persona, instruction-flagged, and disputed (RESOLVING) content — are NEVER
dropped or compressed: the E1 ``INSTRUCTION_FLAG_WRAP`` framing must survive
assembly intact, and a persona prefix that shrinks per-turn would defeat E2
prefix caching.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import zstandard
from pydantic import Field, field_validator

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import StorageError
from memspine.observability.logging import get_logger

__all__ = ["ASSEMBLY_STAGES", "CompressionPolicy"]

_log = get_logger(__name__)

#: E5 ordered fallbacks (D-51). Unknown names are rejected at bind time.
ASSEMBLY_STAGES = ("drop_lowest_score", "llmlingua", "provider_compaction")

#: One import probe + one info log per process (skip-log, not spam).
_llmlingua_warned = False


def _load_llmlingua() -> Callable[[str], str] | None:
    """Lazy llmlingua loader (``[compress]``): a block-compress callable, or
    None (with one info log) when the extra is absent. Module-level so tests
    can substitute a deterministic compressor."""
    global _llmlingua_warned
    try:
        from llmlingua import PromptCompressor
    except ImportError:
        if not _llmlingua_warned:
            _llmlingua_warned = True
            _log.info(
                "compression.llmlingua_unavailable",
                detail="E5 llmlingua stage skipped — install with `pip install memspine[compress]`",
            )
        return None
    compressor = PromptCompressor()

    def compress(text: str) -> str:
        result = compressor.compress_prompt([text], rate=constants.ASSEMBLY_COMPRESS_RATE)
        return str(result["compressed_prompt"])

    return compress


class CompressionOptions(PolicyOptions):
    cold_tier_zstd_level: int = constants.ZSTD_LEVEL
    # Tiers whose records get their content compressed at rest (M3 names).
    compress_tiers: list[str] = Field(default_factory=lambda: ["dormant"])
    # E5 master switch: assembly-time fitting is opt-in (default OFF, D-51).
    assembly: bool = False
    # E5 ordered fallbacks (assembly-time). llmlingua self-skips without the
    # [compress] extra; provider_compaction is the no-op seam.
    assembly_stage: list[str] = Field(default_factory=lambda: list(ASSEMBLY_STAGES))

    @field_validator("assembly_stage")
    @classmethod
    def _known_stages(cls, value: list[str]) -> list[str]:
        unknown = [stage for stage in value if stage not in ASSEMBLY_STAGES]
        if unknown:
            raise ValueError(
                f"unknown assembly_stage entries {unknown!r} (valid: {ASSEMBLY_STAGES})"
            )
        return value


class CompressionPolicy(BindablePolicy):
    name: ClassVar[str] = "compression"
    Options: ClassVar[type[PolicyOptions]] = CompressionOptions

    def _options(self) -> CompressionOptions:
        options = self.options
        assert isinstance(options, CompressionOptions)
        return options

    # ── cold tier (M6/D-32) ─────────────────────────────────────────────────

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

    # ── assembly stage (E5/D-51) ────────────────────────────────────────────

    def assembly_enabled(self) -> bool:
        return self._options().assembly

    @staticmethod
    def protected(record: MemoryRecord) -> bool:
        """Blocks E5 must never touch: persona (E2 stable prefix), instruction-
        flagged (the E1 wrap must reach the model verbatim), and disputed
        (RESOLVING) content whose text is still evidence in a conflict."""
        return (
            record.source.channel == "persona"
            or record.instruction_flag
            or record.status is RecordStatus.RESOLVING
        )

    def fit_assembly(
        self,
        selected: list[tuple[MemoryRecord, float]],
        budget_tokens: int,
        estimate: Callable[[str], int],
    ) -> list[tuple[MemoryRecord, float]]:
        """Apply the E5 ordered fallbacks until the selection fits the budget.

        Protected blocks are never dropped or compressed — if they alone
        exceed the budget the result stays over (documented, deliberate:
        correctness of the E1/E2 guarantees beats the budget).
        """
        items = list(selected)

        def total() -> int:
            return sum(estimate(record.content) for record, _ in items)

        for stage in self._options().assembly_stage:
            if total() <= budget_tokens:
                break
            if stage == "drop_lowest_score":
                items = self._drop_lowest(items, budget_tokens, estimate)
            elif stage == "llmlingua":
                items = self._block_compress(items)
            elif stage == "provider_compaction":
                # No-op seam (D-51): provider-side compaction (e.g. context
                # editing APIs) happens outside the engine; the stage exists so
                # configs can order it explicitly.
                _log.debug("compression.provider_compaction_noop")
        return items

    def _drop_lowest(
        self,
        items: list[tuple[MemoryRecord, float]],
        budget_tokens: int,
        estimate: Callable[[str], int],
    ) -> list[tuple[MemoryRecord, float]]:
        kept = list(items)
        while sum(estimate(record.content) for record, _ in kept) > budget_tokens:
            if len(kept) == 1:
                break  # M12 never-empty rule: assembly never returns silently empty
            droppable = [pair for pair in kept if not self.protected(pair[0])]
            if not droppable:
                break  # only protected blocks left — never drop those
            victim = min(droppable, key=lambda pair: pair[1])
            kept = [pair for pair in kept if pair[0].record_id != victim[0].record_id]
        return kept

    def _block_compress(
        self, items: list[tuple[MemoryRecord, float]]
    ) -> list[tuple[MemoryRecord, float]]:
        compress = _load_llmlingua()
        if compress is None:
            return items
        out: list[tuple[MemoryRecord, float]] = []
        for record, score in items:
            if self.protected(record):
                out.append((record, score))
                continue
            try:
                compressed = compress(record.content)
            except Exception as exc:
                # SF-4/ADR-018: one record llmlingua chokes on (odd unicode,
                # model hiccup) must not 500 the whole /assemble. Log and leave
                # that record uncompressed — the budget may stay over, which the
                # next fallback / the E5 "protected stays over" rule already
                # tolerates.
                _log.warning(
                    "compression.block_failed",
                    record_id=record.record_id,
                    error=str(exc),
                )
                out.append((record, score))
                continue
            out.append((record.model_copy(update={"content": compressed}), score))
        return out
