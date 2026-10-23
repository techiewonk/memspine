"""``OllamaVision``: a local Ollama server (``/api/chat`` with an ``images`` list) over the
Engine's shared :class:`~memspine.clients.http.HttpConnector` (RM-113)."""

from __future__ import annotations

import base64
import json

from memspine.clients.http import HttpConnector, HttpError
from memspine.services.vision.base import DESCRIBE_PROMPT, VisionResult

__all__ = ["OllamaVision"]


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
