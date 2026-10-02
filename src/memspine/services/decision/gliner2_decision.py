"""GLiNER2 decision provider (H24), behind the existing ``[ner]`` extra (gliner2, D-28).

GLiNER2 (fastino-ai/GLiNER2, Apache-2.0) classifies a text into one of several labelled,
described options with a schema-driven encoder (205M-340M parameters, CPU-first). The model is
loaded lazily on first use and runs in a worker thread so the event loop is never blocked.

API surface used here, and how sure we are of it:

* ``from gliner2 import GLiNER2`` and ``GLiNER2.from_pretrained(<hf id>)``: the same calls as
  the NER adapter (``memories/semantic/entities.py``) and graphiti's ``GLiNER2Client``.
  ``AutoExtractor`` is accepted as a fallback import name.
* The default checkpoint ``fastino/gliner2-base-v1`` is the id graphiti documents.
* Classification goes through ``create_schema().classification(...)`` + ``extract(...)`` when the
  model exposes them, and through ``classify_text(...)`` otherwise. ``include_confidence`` is
  dropped on a ``TypeError``. The shape of the result is checked by ``parse_choice``.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from collections.abc import Mapping
from typing import Any

from memspine.exceptions import MissingServiceError

__all__ = ["DEFAULT_MODEL", "GLiNER2Decision", "gliner2_class", "parse_choice"]

DEFAULT_MODEL = "fastino/gliner2-base-v1"
_SERVICE = "gliner2 decision provider"
_CLASS_NAMES = ("GLiNER2", "AutoExtractor")
_FIELD = "choice"


def gliner2_class() -> Any:
    """The gliner2 model class (``GLiNER2``, else ``AutoExtractor``).

    Raises ``MissingServiceError(extra="ner")`` when gliner2 is not installed or exposes
    neither name. Importing the package does not download a model.
    """
    try:
        module = importlib.import_module("gliner2")
    except ImportError as exc:
        raise MissingServiceError(_SERVICE, extra="ner") from exc
    for name in _CLASS_NAMES:
        cls = getattr(module, name, None)
        if cls is not None:
            return cls
    raise MissingServiceError(_SERVICE, extra="ner")


class GLiNER2Decision:
    """``DecisionProvider`` backed by a GLiNER2 classification head.

    The first load runs under a lock, so concurrent reads load the model once. A failed load
    is cached and re-raised on later calls, so a broken install is not retried on every read.
    """

    provider_id = "gliner2"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._extractor: Any = None
        self._load_error: Exception | None = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._extractor is not None:
                return self._extractor
            if self._load_error is not None:
                raise self._load_error
            try:
                self._extractor = gliner2_class().from_pretrained(self.model)
            except Exception as exc:
                self._load_error = exc
                raise
            return self._extractor

    def _choose_sync(self, text: str, options: Mapping[str, str]) -> tuple[str, float]:
        extractor = self._load()
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

    async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float]:
        return await asyncio.to_thread(self._choose_sync, text, options)


def parse_choice(result: Any, options: Mapping[str, str]) -> tuple[str, float]:
    """``{'choice': {'label': x, 'confidence': p}}`` or ``{'choice': x}`` -> (label, p).

    A result outside ``options`` (a gliner2 shape change) raises loudly rather than
    routing on a guess.
    """
    value = result.get(_FIELD) if isinstance(result, Mapping) else None
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, Mapping):
        label, confidence = value.get("label"), float(value.get("confidence", 0.0))
    else:
        label, confidence = value, 1.0
    if label not in options:
        raise ValueError(f"gliner2 returned an unrecognised choice: {result!r}")
    return str(label), confidence
