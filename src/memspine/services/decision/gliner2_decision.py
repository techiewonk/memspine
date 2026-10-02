"""GLiNER2 decision provider (H24), behind the existing ``[ner]`` extra (gliner2, D-28).

GLiNER2 (fastino-ai/GLiNER2, Apache-2.0) classifies a text into one of several labelled,
described options with a schema-driven encoder (74M-340M parameters, CPU-first). The model is
loaded lazily on first use and runs in a worker thread so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from memspine.exceptions import MissingServiceError

__all__ = ["GLiNER2Decision"]

DEFAULT_MODEL = "fastino/gliner2.5-base-v1"


class GLiNER2Decision:
    provider_id = "gliner2"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._extractor: Any = None

    def _load(self) -> Any:
        if self._extractor is None:
            try:
                from gliner2 import AutoExtractor
            except ImportError as exc:  # pragma: no cover - needs the extra
                raise MissingServiceError("gliner2 decision provider", extra="ner") from exc
            self._extractor = AutoExtractor.from_pretrained(self.model)
        return self._extractor

    def _choose_sync(self, text: str, options: Mapping[str, str]) -> tuple[str, float]:
        extractor = self._load()
        schema = extractor.create_schema().classification("choice", dict(options))
        result = extractor.extract(text, schema, include_confidence=True)
        return parse_choice(result, options)

    async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float]:
        return await asyncio.to_thread(self._choose_sync, text, options)


def parse_choice(result: Any, options: Mapping[str, str]) -> tuple[str, float]:
    """``{'choice': {'label': x, 'confidence': p}}`` or ``{'choice': x}`` -> (label, p).

    A result outside ``options`` (a gliner2 shape change) raises loudly rather than
    routing on a guess.
    """
    value = result.get("choice") if isinstance(result, Mapping) else None
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, Mapping):
        label, confidence = value.get("label"), float(value.get("confidence", 0.0))
    else:
        label, confidence = value, 1.0
    if label not in options:
        raise ValueError(f"gliner2 returned an unrecognised choice: {result!r}")
    return str(label), confidence
