"""HTTP connection client: one shared httpx.AsyncClient pool (D-24/D-46).

Every HTTP-speaking service (local LLM endpoints today, REST clients later)
receives this client injected. Retry/timeout policy lives here, never in
services. Tests inject ``transport=httpx.MockTransport(...)`` — no network.
"""

from __future__ import annotations

import httpx

from memspine.clients.base import Client
from memspine.exceptions import MemspineError

__all__ = ["HTTPClient"]


class HTTPClient(Client):
    def __init__(
        self,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise MemspineError("HTTPClient is not connected — call connect() first")
        return self._client

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> bool:
        return self._client is not None
