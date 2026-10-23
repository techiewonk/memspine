"""The vision/OCR evidence port: an image in, a plain-text description out.

The description is generic and question-independent, so one call per image suffices and the
result is cached by content hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["DESCRIBE_PROMPT", "VisionBackend", "VisionResult"]

DESCRIBE_PROMPT = (
    "Describe this image factually in two or three sentences. Transcribe any visible text "
    "exactly (titles, signs, labels, captions). Name the main objects and the setting. "
    "Name a place or title only if it is readable in the image. Do not guess."
)


@dataclass(frozen=True, slots=True)
class VisionResult:
    text: str
    confidence: float | None = None


class VisionBackend(Protocol):
    name: str

    async def describe(self, data: bytes, mime: str) -> VisionResult: ...
