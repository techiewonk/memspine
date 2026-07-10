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

**Two compressions, orthogonal (F4 decision rule):**

- **zstd cold tier** (``compress``/``inflate``) is *at-rest* and **reversible** —
  it changes only the storage encoding, never meaning, and the original is
  recoverable byte-for-byte (the preserved fingerprint proves it). Applies to
  dormant records.
- **llmlingua assembly** (``fit_assembly``) is *in-context* and **lossy** — it
  drops tokens to fit a budget for one prompt; the stored record is unchanged.

They must not be conflated: ``fit_assembly`` operates on **inflated** content, so
a record that reached it still cold-compressed (``content_zstd`` set, ``content``
empty) is a bug — it is refused loudly rather than silently compressing empty
text. F1 makes the llmlingua-2 model configurable; F2 adds per-placement-band
budgets (squeeze volatile before the cacheable stable prefix); F3 adds a
``preserve`` force-tokens list + a per-block ``target_token`` cap so entity keys
survive; every compressed block emits a ``compression.block_compressed`` M11
event.
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


def _load_llmlingua(
    rate: float = constants.ASSEMBLY_COMPRESS_RATE,
    *,
    model: str = constants.LLMLINGUA_MODEL,
    use_llmlingua2: bool = True,
    device_map: str = "cpu",
    target_token: int | None = None,
    preserve: list[str] | None = None,
) -> Callable[[str], str] | None:
    """Lazy llmlingua loader (``[compress]``): a block-compress callable, or
    None (with one info log) when the extra is absent. Module-level so tests
    can substitute a deterministic compressor.

    F1: ``model``/``use_llmlingua2``/``device_map`` are config-surfaced
    (``read.compression.llmlingua_model`` …) so a deployment picks the LLMLingua-2
    model without a code change — torch stays behind ``[compress]``, never core
    (D-03). F3: ``target_token`` caps the per-block output length and ``preserve``
    becomes llmlingua ``force_tokens`` so entity names/numbers the ``(entity,
    attribute)`` retrieval key depends on are never compressed away."""
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
    compressor = PromptCompressor(
        model_name=model, use_llmlingua2=use_llmlingua2, device_map=device_map
    )
    force_tokens = list(preserve) if preserve else None

    def compress(text: str) -> str:
        kwargs: dict[str, object] = {}
        if target_token is not None:
            kwargs["target_token"] = target_token
        else:
            kwargs["rate"] = rate
        if force_tokens:
            kwargs["force_tokens"] = force_tokens
        result = compressor.compress_prompt([text], **kwargs)
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
    # E5 llmlingua per-block keep ratio (v0.2 A6): lower => more aggressive
    # compression. Only consulted by the llmlingua stage.
    assembly_rate: float = constants.ASSEMBLY_COMPRESS_RATE
    # F1: llmlingua-2 model selection (torch stays behind [compress]).
    llmlingua_model: str = constants.LLMLINGUA_MODEL
    use_llmlingua2: bool = True
    llmlingua_device_map: str = "cpu"
    # F3: hard per-block output cap (overrides assembly_rate when set) + a list
    # of tokens (entity names, numbers) llmlingua must never drop (force_tokens).
    assembly_target_token: int | None = None
    preserve: list[str] = Field(default_factory=list)
    # F2: optional per-placement-band token budgets. Keys are band names
    # ("stable" = persona/procedural/semantic prefix, "volatile" = everything
    # else); volatile bands over their budget are block-compressed FIRST so the
    # cacheable stable prefix (E2) is squeezed last. Empty => single-value default.
    stage_budgets: dict[str, int] = Field(default_factory=dict)

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

    @staticmethod
    def _band(record: MemoryRecord) -> str:
        """F2 placement band: ``stable`` = the cacheable persona/procedural/
        semantic prefix (E2), ``volatile`` = everything else (episodic/working/
        derived). Mirrors assembly._PLACEMENT_RANK's cache boundary."""
        if record.source.channel == "persona" or record.memory_type in {"procedural", "semantic"}:
            return "stable"
        return "volatile"

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

        F2: when ``stage_budgets`` is set, over-budget placement bands are
        block-compressed FIRST — volatile (episodic/working) before stable — so
        the cacheable persona/procedural/semantic prefix (E2) is squeezed last.
        """
        items = list(selected)
        options = self._options()

        def total() -> int:
            return sum(estimate(record.content) for record, _ in items)

        if options.stage_budgets:
            items = self._squeeze_bands(items, options.stage_budgets, estimate)

        for stage in options.assembly_stage:
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

    def _squeeze_bands(
        self,
        items: list[tuple[MemoryRecord, float]],
        budgets: dict[str, int],
        estimate: Callable[[str], int],
    ) -> list[tuple[MemoryRecord, float]]:
        """F2: block-compress the records of any placement band whose total
        exceeds its ``stage_budgets`` entry — volatile first, so the cacheable
        stable prefix (E2) is only compressed if it alone blows its budget."""
        band_totals: dict[str, int] = {}
        for record, _ in items:
            band = self._band(record)
            band_totals[band] = band_totals.get(band, 0) + estimate(record.content)
        to_compress: set[str] = set()
        for band in ("volatile", "stable"):  # squeeze volatile before stable
            budget = budgets.get(band)
            if budget is not None and band_totals.get(band, 0) > budget:
                to_compress |= {
                    record.record_id
                    for record, _ in items
                    if self._band(record) == band and not self.protected(record)
                }
        return self._block_compress(items, only=to_compress) if to_compress else items

    def _block_compress(
        self,
        items: list[tuple[MemoryRecord, float]],
        only: set[str] | None = None,
    ) -> list[tuple[MemoryRecord, float]]:
        """llmlingua-compress each non-protected block (F1/F3). ``only`` limits
        compression to those record ids (F2 per-band squeeze); None = all."""
        options = self._options()
        compress = _load_llmlingua(
            options.assembly_rate,
            model=options.llmlingua_model,
            use_llmlingua2=options.use_llmlingua2,
            device_map=options.llmlingua_device_map,
            target_token=options.assembly_target_token,
            preserve=options.preserve,
        )
        if compress is None:
            return items
        out: list[tuple[MemoryRecord, float]] = []
        for record, score in items:
            if self.protected(record) or (only is not None and record.record_id not in only):
                out.append((record, score))
                continue
            # F4: fit_assembly operates on INFLATED content — a cold-tier record
            # (content_zstd set, content empty) must have been inflated upstream;
            # compressing its empty content would silently drop it.
            if record.content_zstd is not None and not record.content:
                raise StorageError(
                    f"record {record.record_id} reached assembly compression still "
                    "cold-compressed (content_zstd set, content empty) — inflate first"
                )
            before = len(record.content)
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
            # F4: M11 observability — one structured event per compressed block.
            _log.info(
                "compression.block_compressed",
                record_id=record.record_id,
                memory_type=record.memory_type,
                chars_before=before,
                chars_after=len(compressed),
            )
            out.append((record.model_copy(update={"content": compressed}), score))
        return out
