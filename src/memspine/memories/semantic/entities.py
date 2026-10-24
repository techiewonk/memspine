"""Entity/fact extraction (M13.3, D-28).

Extraction happens BEFORE conflict detection so the ladder operates on
canonical (entity, attribute) keys. Providers:

- ``llm``    — the extract prompt (D-43) + structured output (D-31); default
               fallback whenever an ``extract`` LLM role is bound,
- ``gliner`` — gliner2 CPU zero-shot NER behind the ``[local]`` extra (D-28),
               source-grounded, unasserted entity mentions without an LLM call,
- ``off``    — no extraction; records stay unkeyed (dedup still protects).

Alias merges are logged so entity resolution is auditable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from memspine.config.schema.model import GlinerSettings
from memspine.derive.gliner_mentions import MentionSpan, parse_gliner_mentions
from memspine.observability.logging import get_logger
from memspine.prompts.base import Prompt
from memspine.prompts.models import ExtractedFact, ExtractedFacts
from memspine.services.extraction.base import ExtractionModels
from memspine.services.llm.base import LLMService
from memspine.services.llm.structured import structured_call

__all__ = ["EntityExtractor", "GlinerEntityExtractor", "LLMEntityExtractor"]

_log = get_logger(__name__)


@runtime_checkable
class EntityExtractor(Protocol):
    async def extract(self, content: str) -> list[ExtractedFact]: ...


def canonical_entity(name: str) -> str:
    """Minimal alias normalization (full resolution graph lands with P6)."""
    canonical = " ".join(name.strip().lower().split())
    if canonical != name:
        _log.info("entity.alias_merged", raw=name, canonical=canonical)
    return canonical


class LLMEntityExtractor:
    """Default extraction path (D-28 fallback): extract prompt + typed output."""

    def __init__(self, llm: LLMService, prompt: Prompt) -> None:
        self._llm = llm
        self._prompt = prompt

    @property
    def prompt_version(self) -> str:
        return self._prompt.prompt_version

    @property
    def model_id(self) -> str:
        """The producer's identity for the extraction cache key (R3-15): a model swap must not
        serve facts another model extracted."""
        return str(getattr(self._llm, "provider_id", type(self._llm).__name__))

    async def extract(self, content: str) -> list[ExtractedFact]:
        result = await structured_call(
            self._llm, self._prompt, {"content": content}, ExtractedFacts
        )
        return [
            fact.model_copy(update={"entity": canonical_entity(fact.entity)})
            for fact in result.facts
        ]


class GlinerEntityExtractor:
    """gliner2 zero-shot NER (D-28), ``[local]``: entities without an LLM call.

    NER yields source-grounded annotations, not (entity, attribute) assertions.
    The semantic store persists spans separately from the fact-conflict ladder;
    the LLM extractor remains the fact-extraction path when a role is bound.
    """

    _DEFAULT_LABELS = ("person", "organization", "location", "product")

    def __init__(
        self,
        pool: ExtractionModels,
        settings: GlinerSettings,
        labels: tuple[str, ...] = _DEFAULT_LABELS,
    ) -> None:
        self._labels = list(labels)
        self._settings = settings
        # RM-73: one Engine-lifetime lease, not an acquire/release per call.
        self._model = pool.binding(settings)
        missing = set(("entities",)) - set(settings.tasks)
        if missing:
            raise ValueError(
                "model.extraction.semantic settings.tasks must include 'entities' for semantic extraction"
            )

    @property
    def model_id(self) -> str:
        """Effective producer identity for any extraction cache or provenance record."""
        labels = ",".join(self._labels)
        settings = self._settings
        return (
            f"gliner2:{settings.model}@{settings.revision or 'default'}:{settings.device}:"
            f"local={settings.local_files_only}:tasks={','.join(sorted(settings.tasks))}:"
            f"concurrency={settings.inference_concurrency}:grounded-mentions[{labels}]"
        )

    async def extract(self, content: str) -> list[ExtractedFact]:
        """Satisfy the fact-extractor protocol without inventing assertions.

        Call ``extract_mentions`` when source-grounded NER annotations are needed.
        """
        return []

    async def extract_mentions(self, content: str) -> tuple[MentionSpan, ...]:
        """Return every exact source mention as an explicitly unasserted span."""
        raw = await self._model.call(
            lambda model: model.extract_entities(
                content,
                self._labels,
                include_confidence=True,
                include_spans=True,
            )
        )
        return parse_gliner_mentions(content, raw, self._labels)
