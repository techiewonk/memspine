"""The vision/OCR evidence port: an image in, a plain-text description out.

``NoopVision`` (default) produces nothing, so a run with it is text-only by construction.
``OllamaVision`` talks to a local Ollama server (``/api/chat`` with an ``images`` list) over
the Engine's shared :class:`~memspine.clients.http.HttpConnector` (RM-113).
The description is generic and question-independent, so one call per image suffices and the
result is cached by content hash.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Protocol

from memspine.clients.http import HttpConnector, HttpError

__all__ = ["DESCRIBE_PROMPT", "NoopVision", "OllamaVision", "VisionBackend", "VisionResult"]

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


class NoopVision:
    """No vision available: evidence stays ``skipped`` and the context stays text-only."""

    name = "none"

    async def describe(self, data: bytes, mime: str) -> VisionResult:
        return VisionResult(text="")


class OllamaVision:
    def __init__(
        self,
        model: str,
        http: HttpConnector,
        base_url: str = "http://localhost:11434",
        *,
        timeout: float = 120.0,
        prompt: str = DESCRIBE_PROMPT,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.prompt = prompt
        self.name = f"ollama:{model}"
        self._http = http

    async def describe(self, data: bytes, mime: str) -> VisionResult:
        body = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [
                {
                    "role": "user",
                    "content": self.prompt,
                    "images": [base64.b64encode(data).decode("ascii")],
                }
            ],
        }
        try:
            reply = await self._http.request(
                "POST", f"{self.base_url}/api/chat", json=body, timeout_s=self.timeout
            )
            if reply.status >= 400:
                raise RuntimeError(f"ollama vision call failed: HTTP {reply.status}")
            payload = json.loads(reply.body)
        except (HttpError, ValueError) as exc:
            raise RuntimeError(f"ollama vision call failed: {exc}") from exc
        return VisionResult(text=str((payload.get("message") or {}).get("content") or "").strip())
