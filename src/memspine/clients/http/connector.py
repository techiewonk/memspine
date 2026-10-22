"""Shared HTTP connector (RM-113 P1 #14): one ``httpx.AsyncClient`` (connection pool) for the
HTTP adapters of one Engine — asset fetch, vision (Ollama) and external evidence search.

The connector owns the pool, its limits and default deadline, and never follows redirects on
its own (an adapter that allows a redirect checks it first: the asset fetcher refuses a
cross-host one). Adapters receive the connector and call :meth:`HttpConnector.client`, which
opens the pool on first use: an ``AsyncClient`` opens no socket until a request, and is bound
to the event loop it first runs on, so opening lazily keeps a lazily built adapter usable.
:meth:`HttpConnector.close` closes the pool; a later :meth:`HttpConnector.client` reopens it
(an Engine restarted on a new loop gets a fresh pool).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlsplit

from memspine.clients._common.deadlines import check_deadline, probe
from memspine.clients._common.endpoints import is_loopback
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import StorageError

__all__ = ["HttpConnector", "HttpError", "HttpReply", "HttpSpec", "HttpTimeout"]


class HttpError(Exception):
    """The request did not complete (connection, protocol or read failure). The message is
    the transport error's type name only: URLs and bodies may carry credentials."""


class HttpTimeout(HttpError):
    """The request exceeded its deadline."""


@dataclass(frozen=True, slots=True)
class HttpReply:
    """A completed response. ``headers`` keys are lower-case; ``body`` holds at most
    ``max_bytes + 1`` bytes when a cap was given (``truncated`` = more were available)."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    truncated: bool = False

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status < 400 and "location" in self.headers


@dataclass(frozen=True, slots=True)
class HttpSpec:
    """Pool settings of one shared HTTP client.

    ``timeout_s``: the default per-request deadline (adapters may pass a tighter one);
    ``max_connections`` / ``keepalive_s`` (``runtime.shared_resources.http``): pool limits,
    None keeps the httpx default; ``base_url``: optional health-check target (a ``GET`` on it
    is the round trip); ``transport``: an injected httpx transport (tests), not identity."""

    timeout_s: float = 10.0
    max_connections: int | None = None
    keepalive_s: float | None = None
    base_url: str | None = None
    transport: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        check_deadline(self.timeout_s, "timeout_s")


class HttpConnector:
    """One pooled ``httpx.AsyncClient``; a :class:`~memspine.clients.base.Connector`."""

    kind: ClassVar[str] = "http"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: HttpSpec) -> None:
        self._spec = spec
        self._client: Any = None

    @classmethod
    def identity(cls, spec: HttpSpec) -> ConnectorIdentity:
        origin = None
        if spec.base_url:
            parts = urlsplit(spec.base_url)
            origin = f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{parts.port}"
        return ConnectorIdentity(
            cls.kind,
            {"base_url": origin},
            None,
            {
                "timeout_s": spec.timeout_s,
                "max_connections": spec.max_connections,
                "keepalive_s": spec.keepalive_s,
            },
        )

    @classmethod
    def locality(cls, spec: HttpSpec) -> Locality:
        host = urlsplit(spec.base_url).hostname if spec.base_url else None
        return "host" if host and is_loopback(host) else "cluster"

    @property
    def spec(self) -> HttpSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        if self._client is None:
            raise StorageError("HTTP connector is not open — call open() first")
        return self._client

    async def open(self) -> None:
        if self._client is not None:
            return
        import httpx

        spec = self._spec
        limits: dict[str, Any] = {}
        if spec.max_connections is not None:
            limits["max_connections"] = spec.max_connections
        if spec.keepalive_s is not None:
            limits["keepalive_expiry"] = spec.keepalive_s
        self._client = httpx.AsyncClient(
            timeout=spec.timeout_s,
            limits=httpx.Limits(**limits),
            follow_redirects=False,
            transport=spec.transport,
        )

    async def client(self) -> Any:
        """The open ``httpx.AsyncClient`` (opened on first use)."""
        if self._client is None:
            await self.open()  # no await before the client is set: no race on one loop
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        timeout_s: float | None = None,
        max_bytes: int | None = None,
    ) -> HttpReply:
        """One request, never following a redirect; the body is streamed and capped at
        ``max_bytes + 1`` bytes. Transport failures raise :class:`HttpError` /
        :class:`HttpTimeout` (adapters never see the driver's exception types)."""
        import httpx

        client = await self.client()
        try:
            async with client.stream(
                method,
                url,
                headers=dict(headers or {}),
                params=dict(params) if params is not None else None,
                json=json,
                timeout=timeout_s if timeout_s is not None else self._spec.timeout_s,
            ) as response:
                body = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if max_bytes is not None and len(body) > max_bytes:
                        truncated = True
                        break
                return HttpReply(
                    int(response.status_code),
                    {k.lower(): v for k, v in response.headers.items()},
                    bytes(body),
                    truncated,
                )
        except httpx.TimeoutException as exc:
            raise HttpTimeout(type(exc).__name__) from None
        except (httpx.HTTPError, OSError) as exc:
            raise HttpError(type(exc).__name__) from None

    async def health(self, deadline_s: float) -> bool:
        """``GET base_url`` answered below 500; without a base URL, whether the pool is open."""
        client = self._client
        if client is None:
            return False
        base_url = self._spec.base_url
        if base_url is None:
            return True

        async def check() -> bool:
            response = await client.get(base_url)
            return int(response.status_code) < 500

        return await probe(deadline_s, check)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
