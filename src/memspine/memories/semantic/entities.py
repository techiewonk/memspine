"""Entity/fact extraction (M13.3, D-28).

Extraction happens BEFORE conflict detection so the ladder operates on
canonical (entity, attribute) keys. Providers:

- ``llm``    — the extract prompt (D-43) + structured output (D-31); default
               fallback whenever an ``extract`` LLM role is bound,
- ``gliner`` — gliner2 CPU zero-shot NER behind the ``[ner]`` extra (D-28),
- ``off``    — no extraction; records stay unkeyed (dedup still protects).

Alias merges are logged so entity resolution is auditable.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from memspine.exceptions import MissingServiceError
from memspine.observability.logging import get_logger
from memspine.prompts.base import Prompt
from memspine.prompts.models import ExtractedFact, ExtractedFacts
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

    async def extract(self, content: str) -> list[ExtractedFact]:
        result = await structured_call(
            self._llm, self._prompt, {"content": content}, ExtractedFacts
        )
        return [
            fact.model_copy(update={"entity": canonical_entity(fact.entity)})
            for fact in result.facts
        ]


class GlinerEntityExtractor:
    """gliner2 zero-shot NER (D-28), ``[ner]``: entities without an LLM call.

    NER yields entities, not (attribute, value) triples — extracted facts key
    the record (attribute="mention") for conflict grouping; the LLM extractor
    remains the richer default when a role is bound.
    """

    _DEFAULT_LABELS = ("person", "organization", "location", "product")

    def __init__(self, labels: tuple[str, ...] = _DEFAULT_LABELS) -> None:
        try:
            from gliner2 import GLiNER2
        except ImportError as exc:
            raise MissingServiceError("ner:gliner2", extra="ner") from exc
        self._labels = list(labels)
        self._model: Any = GLiNER2.from_pretrained("fastino/gliner2-base")

    async def extract(self, content: str) -> list[ExtractedFact]:
        def _run() -> Any:
            return self._model.extract_entities(content, self._labels)

        raw = await asyncio.to_thread(_run)
        facts: list[ExtractedFact] = []
        entities = raw.get("entities", raw) if isinstance(raw, dict) else raw
        if isinstance(entities, dict):
            for label, names in entities.items():
                for name in names:
                    facts.append(
                        ExtractedFact(
                            entity=canonical_entity(str(name)),
                            attribute="mention",
                            value=str(label),
                        )
                    )
        else:
            # A gliner2 version bump changing the result shape must be loud,
            # not a permanent silent "no entities in anything".
            _log.warning(
                "entity.gliner_unexpected_result_shape",
                shape=type(entities).__name__,
                detail="gliner2 extract_entities returned an unrecognized structure; "
                "no entities extracted — check the installed gliner2 version",
            )
        return facts
