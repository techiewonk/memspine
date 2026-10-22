"""GLiNER topic tagger for topical domains (RM-106; ADR-080 decisions 9 and 12).

Opt-in: built only when ``models.bindings.gliner_topic`` binds a GLiNER profile with task
``classification`` and ``local_files_only: true`` (offline only; never a generative model).
The candidate labels come from two sources (owner decision, both):

* **pack domains**: every domain id the active vocabulary packs declare (predicate, property
  and pack top domains), offered to the model in readable form (``health/condition`` ->
  ``health condition``);
* **operator label map** (``models.topic.labels``): free topic label -> domain id. An operator
  label equal to a readable pack label overrides it.

The best classification hit at or above ``models.topic.min_confidence`` sets the domain; an
unscored hit (no confidence) never does. The result feeds the ``gliner_topic`` domain source,
the lowest precedence of RM-91. Nothing here downloads a model: the pool loads the bound
checkpoint lazily, offline, on the first call.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from memspine.config import constants
from memspine.semantics.domains import declared_domains, is_domain_id
from memspine.semantics.registry import SemanticRegistry, load_builtin_registry

__all__ = ["TOPIC_FIELD", "GlinerTopicTagger", "parse_topic_hits", "readable_label"]

#: The classification field name the tagger asks GLiNER2 for.
TOPIC_FIELD = "topic"


def readable_label(domain: str) -> str:
    """The text a domain id is offered to the model as (``health/condition`` -> ``health condition``)."""
    return domain.replace("/", " ").replace("_", " ")


def parse_topic_hits(result: Any, labels: Mapping[str, str]) -> list[tuple[str, float | None]]:
    """``{'topic': x}`` in any of GLiNER2's shapes (a label, ``{'label', 'confidence'}``, or a
    list of either) -> the (label, confidence) pairs whose label is a candidate. Any other
    shape or label gives no hit: an unexpected output leaves the domain unknown."""
    value = result.get(TOPIC_FIELD) if isinstance(result, Mapping) else None
    items = value if isinstance(value, list) else [value]
    hits: list[tuple[str, float | None]] = []
    for item in items:
        confidence: float | None = None
        if isinstance(item, Mapping):
            label, raw = item.get("label"), item.get("confidence")
            try:
                confidence = None if raw is None else float(raw)
            except (TypeError, ValueError):
                confidence = None
        else:
            label = item
        if isinstance(label, str) and label in labels:
            hits.append((label, confidence))
    return hits


class GlinerTopicTagger:
    """GLiNER2 classification over the candidate topic labels -> at most one domain id.

    ``atag`` is async (the pool runs the model in a worker thread). Results are cached per
    text (bounded LRU) so a sleep-cycle recompile does not classify an unchanged claim again;
    a failure is not cached and propagates (``semantics.domains.atopic_domains`` logs it and
    leaves the domain unknown)."""

    def __init__(
        self,
        pool: Any,
        settings: Any,
        *,
        registry: SemanticRegistry | None = None,
        labels: Mapping[str, str] | None = None,
        min_confidence: float = constants.TOPIC_MIN_CONFIDENCE,
        cache_size: int = constants.TOPIC_CACHE_SIZE,
    ) -> None:
        if not getattr(settings, "local_files_only", False):
            raise ValueError("the GLiNER topic tagger is offline only (local_files_only)")
        if "classification" not in tuple(getattr(settings, "tasks", ())):
            raise ValueError("the bound GLiNER profile must include task 'classification'")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must lie in [0, 1]")
        candidates: dict[str, str] = {}
        for domain in declared_domains(registry or load_builtin_registry()):
            candidates.setdefault(readable_label(domain), domain)
        for label, target in (labels or {}).items():
            if not is_domain_id(target):
                raise ValueError(f"topic label {label!r} maps to an invalid domain id {target!r}")
            candidates[label] = target
        if not candidates:
            raise ValueError("the topic tagger has no candidate labels (no pack domains, no map)")
        self._settings = settings
        self._labels: Mapping[str, str] = MappingProxyType(candidates)
        self._min = min_confidence
        self._cache: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._cache_size = max(0, cache_size)
        self._model = pool.binding(settings)

    @property
    def labels(self) -> Mapping[str, str]:
        """Candidate label -> the domain id a hit on it sets."""
        return self._labels

    @property
    def tagger_id(self) -> str:
        s = self._settings
        return f"gliner2:{s.model}@{s.revision or 'default'}:topic-v1:min-{self._min:g}"

    def _classify_sync(self, extractor: Any, text: str) -> Any:
        options = list(self._labels)
        if hasattr(extractor, "create_schema") and hasattr(extractor, "extract"):
            schema = extractor.create_schema().classification(TOPIC_FIELD, options)
            try:
                return extractor.extract(text, schema, include_confidence=True)
            except TypeError:
                return extractor.extract(text, schema)
        try:
            return extractor.classify_text(text, {TOPIC_FIELD: options}, include_confidence=True)
        except TypeError:
            return extractor.classify_text(text, {TOPIC_FIELD: options})

    def domains_of(self, result: Any) -> tuple[str, ...]:
        """The domain of the best scored hit at or above the threshold (or none)."""
        scored = [
            (confidence, label)
            for label, confidence in parse_topic_hits(result, self._labels)
            if confidence is not None and confidence >= self._min
        ]
        if not scored:
            return ()
        best = max(scored, key=lambda pair: pair[0])[1]
        return (self._labels[best],)

    async def atag(self, text: str) -> Sequence[str]:
        cached = self._cache.get(text)
        if cached is not None:
            self._cache.move_to_end(text)
            return cached
        raw = await self._model.call(lambda model: self._classify_sync(model, text))
        found = self.domains_of(raw)
        if self._cache_size:
            self._cache[text] = found
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return found
