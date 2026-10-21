"""GLiNER2 decision provider (H24), behind the existing ``[ner]`` extra (gliner2, D-28).

GLiNER2 (fastino-ai/GLiNER2, Apache-2.0) classifies a text into one of several labelled,
described options with a schema-driven encoder (205M-340M parameters, CPU-first). The model is
loaded lazily on first use and runs in a worker thread so the event loop is never blocked.

API surface used here, and how sure we are of it:

* ``from gliner2 import AutoExtractor`` and ``AutoExtractor.from_pretrained(<hf id>)`` dispatches
  to the model architecture declared in the checkpoint config.
* The default checkpoint ``fastino/gliner2.5-base-v1`` uses the boundary architecture.
* Classification goes through ``create_schema().classification(...)`` + ``extract(...)`` when the
  model exposes them, and through ``classify_text(...)`` otherwise. ``include_confidence`` is
  dropped on a ``TypeError``. The shape of the result is checked by ``parse_choice``.
* ``entities(text)`` (GP-3, the graph read leg's optional seed finder) calls
  ``extract_entities(text, labels)``; ``parse_entities`` reads the
  ``{"entities": {label: [names]}}`` shape and ignores anything else.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from memspine.config.schema.llm import GlinerSettings
from memspine.exceptions import MissingServiceError
from memspine.models.runtime.gliner_runtime import GLiNERModelPool

__all__ = [
    "DEFAULT_MODEL",
    "GLiNER2Decision",
    "auto_extractor_class",
    "parse_choice",
    "parse_entities",
]

DEFAULT_MODEL = GlinerSettings().model
_SERVICE = "gliner2 decision provider"
_FIELD = "choice"
#: GP-3: the entity types the graph leg asks for when it seeds from a query.
_ENTITY_LABELS = ("person", "place", "organization", "event", "thing")


def auto_extractor_class() -> Any:
    """Return ``gliner2.AutoExtractor`` without loading a checkpoint.

    Raises ``MissingServiceError(extra="ner")`` when gliner2 or its local runtime is
    unavailable, or when the package does not expose ``AutoExtractor``. Importing
    the package does not download a model.
    """
    try:
        module = importlib.import_module("gliner2")
    except ImportError as exc:
        raise MissingServiceError(_SERVICE, extra="ner") from exc
    try:
        # gliner2>=2.0 resolves this lazily; a base install without the local
        # stack raises ImportError here rather than while importing the package.
        cls = getattr(module, "AutoExtractor", None)
    except ImportError as exc:
        raise MissingServiceError(_SERVICE, extra="ner") from exc
    if cls is None:
        raise MissingServiceError(_SERVICE, extra="ner")
    return cls


class GLiNER2Decision:
    """``DecisionProvider`` backed by a GLiNER2 classification head.

    The engine-owned model pool coalesces concurrent loads across every GLiNER consumer.
    """

    def __init__(self, settings: GlinerSettings, pool: GLiNERModelPool) -> None:
        if "classification" not in settings.tasks:
            raise ValueError(
                "the bound GLiNER profile must include task 'classification' for the decision provider"
            )
        self.settings = settings
        # RM-73: one Engine-lifetime lease, not an acquire/release per call.
        self._model = pool.binding(settings)

    @property
    def provider_id(self) -> str:
        settings = self.settings
        task_set = ",".join(sorted(settings.tasks))
        return (
            f"gliner2:{settings.model}@{settings.revision or 'default'}:{settings.device}:"
            f"local={settings.local_files_only}:tasks={task_set}:"
            f"concurrency={settings.inference_concurrency}:classification-v1:{_FIELD}"
        )

    def _choose_sync(
        self, extractor: Any, text: str, options: Mapping[str, str]
    ) -> tuple[str, float | None]:
        described = dict(options)
        if hasattr(extractor, "create_schema") and hasattr(extractor, "extract"):
            schema = extractor.create_schema().classification(_FIELD, described)
            try:
                result = extractor.extract(text, schema, include_confidence=True)
            except TypeError:
                result = extractor.extract(text, schema)
        else:
            try:
                result = extractor.classify_text(text, {_FIELD: described}, include_confidence=True)
            except TypeError:
                result = extractor.classify_text(text, {_FIELD: list(described)})
        return parse_choice(result, options)

    async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float | None]:
        return await self._model.call(lambda model: self._choose_sync(model, text, options))

    @property
    def entities_model_id(self) -> str:
        settings = self.settings
        return (
            f"gliner2:{settings.model}@{settings.revision or 'default'}:{settings.device}:"
            f"local={settings.local_files_only}:tasks={','.join(sorted(settings.tasks))}:"
            f"concurrency={settings.inference_concurrency}:entities-v1[{','.join(_ENTITY_LABELS)}]"
        )

    async def entities(self, text: str) -> list[str]:
        """GP-3: the entity names in ``text`` (seeds for the graph read leg)."""
        if "entities" not in self.settings.tasks:
            raise ValueError(
                "the bound GLiNER profile must include task 'entities' for decision entity lookup"
            )
        raw = await self._model.call(
            lambda model: model.extract_entities(text, list(_ENTITY_LABELS))
        )
        return parse_entities(raw)


def parse_entities(result: Any) -> list[str]:
    """``{"entities": {label: [names]}}`` (or the inner mapping) -> the names, in
    order, each once. Names may be strings or ``{"text": ...}`` mappings; any
    other shape gives no names (the leg then falls back to its rules)."""
    entities = result.get("entities", result) if isinstance(result, Mapping) else None
    names: list[str] = []
    if not isinstance(entities, Mapping):
        return names
    for found in entities.values():
        for item in found if isinstance(found, list) else []:
            name = item.get("text") if isinstance(item, Mapping) else item
            if isinstance(name, str) and name.strip() and name not in names:
                names.append(name)
    return names


def parse_choice(result: Any, options: Mapping[str, str]) -> tuple[str, float | None]:
    """``{'choice': {'label': x, 'confidence': p}}`` or ``{'choice': x}`` -> (label, p).

    A bare label (no confidence, e.g. a build without ``include_confidence``) gives
    ``None``: the confidence is unknown, so a confidence gate must not treat it as sure.
    A result outside ``options`` (a gliner2 shape change) raises loudly rather than
    routing on a guess.
    """
    value = result.get(_FIELD) if isinstance(result, Mapping) else None
    if isinstance(value, list) and value:
        value = value[0]
    confidence: float | None = None
    if isinstance(value, Mapping):
        label, raw = value.get("label"), value.get("confidence")
        confidence = None if raw is None else float(raw)
    else:
        label = value
    if label not in options:
        raise ValueError(f"gliner2 returned an unrecognised choice: {result!r}")
    return str(label), confidence
