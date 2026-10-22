"""OpenSearch connector (RM-113): a lazy async transport for provider-owned lexical indexes.

The endpoint and credentials come from environment variables named by the spec (never from
config values); :meth:`OpenSearchClient.identity` is the single identity parser, shared by the
factory's registry key and the descriptor's target identity."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, ClassVar

from memspine.clients._common.deadlines import probe
from memspine.clients._common.endpoints import endpoint_from_env, is_loopback
from memspine.clients._common.secrets import auth_digest
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import ConfigError, MissingServiceError, StorageError

__all__ = ["OpenSearchClient", "OpenSearchSpec"]

_SCHEMES = ("http", "https")


@dataclass(frozen=True, slots=True)
class OpenSearchSpec:
    """Transport spec: environment variable names, TLS, deadlines and the pool size.

    ``max_connections``: the aiohttp connector limit (``runtime.shared_resources.http``);
    None keeps the client library default."""

    connect_timeout_s: float
    request_timeout_s: float
    url_env: str = "MEMSPINE_OPENSEARCH_URL"
    username_env: str | None = None
    password_env: str | None = None
    verify_certs: bool = True
    max_connections: int | None = None


def _credentials(spec: OpenSearchSpec) -> tuple[str, str] | None:
    if spec.username_env is None or spec.password_env is None:
        return None
    username = os.environ.get(spec.username_env)
    password = os.environ.get(spec.password_env)
    if username is None or password is None:
        missing = spec.username_env if username is None else spec.password_env
        raise ConfigError(f"OpenSearch authentication needs environment variable {missing}")
    return username, password


class OpenSearchClient:
    """One ``AsyncOpenSearch`` client per target; a :class:`~memspine.clients.base.Connector`."""

    kind: ClassVar[str] = "opensearch"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: OpenSearchSpec) -> None:
        self._spec = spec
        self._client: Any = None

    @classmethod
    def identity(cls, spec: OpenSearchSpec) -> ConnectorIdentity:
        endpoint = endpoint_from_env(spec.url_env, allowed_schemes=_SCHEMES)
        for name in (spec.username_env, spec.password_env):
            if name and name not in os.environ:
                raise ConfigError(f"OpenSearch authentication needs environment variable {name}")
        username = os.environ.get(spec.username_env, "") if spec.username_env else ""
        password = os.environ.get(spec.password_env, "") if spec.password_env else ""
        host = endpoint.host.lower()
        return ConnectorIdentity(
            cls.kind,
            {"kind": "opensearch", "host": host, "scheme": endpoint.scheme, "port": endpoint.port},
            auth_digest(username, password),
            {
                "verify_certs": spec.verify_certs,
                "connect_timeout_s": spec.connect_timeout_s,
                "request_timeout_s": spec.request_timeout_s,
                "max_connections": spec.max_connections,
            },
            {"kind": "opensearch", "host": host, "port": endpoint.port},
        )

    @classmethod
    def locality(cls, spec: OpenSearchSpec) -> Locality:
        endpoint = endpoint_from_env(spec.url_env, allowed_schemes=_SCHEMES)
        return "host" if is_loopback(endpoint.host) else "cluster"

    @property
    def spec(self) -> OpenSearchSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        if self._client is None:
            raise StorageError("OpenSearch client is not open — call open() first")
        return self._client

    async def open(self) -> None:
        if self._client is not None:
            return
        try:
            from opensearchpy import AsyncHttpConnection, AsyncOpenSearch
        except ImportError:
            raise MissingServiceError("lexical:opensearch", extra="opensearch") from None
        spec = self._spec
        endpoint = endpoint_from_env(spec.url_env, allowed_schemes=_SCHEMES)
        auth = _credentials(spec)
        pool: dict[str, Any] = (
            {} if spec.max_connections is None else {"maxsize": spec.max_connections}
        )
        client = AsyncOpenSearch(
            hosts=[{"host": endpoint.host, "port": endpoint.port, "scheme": endpoint.scheme}],
            http_auth=auth,
            use_ssl=endpoint.secure,
            verify_certs=spec.verify_certs,
            connection_class=AsyncHttpConnection,
            timeout=spec.request_timeout_s,
            max_retries=2,
            retry_on_timeout=True,
            retry_on_status=(429, 502, 503, 504),
            **pool,
        )
        self._client = client
        try:
            async with asyncio.timeout(spec.connect_timeout_s):
                if not await client.ping():
                    raise ConnectionError("OpenSearch health check failed")
        except BaseException:
            try:
                async with asyncio.timeout(spec.connect_timeout_s):
                    await self.close()
            except BaseException:
                pass
            raise

    async def health(self, deadline_s: float) -> bool:
        """``GET /_cluster/health`` answered with a non-red status."""
        client = self._client
        if client is None:
            return False

        async def check() -> bool:
            status = await client.cluster.health()
            return str(status.get("status", "red")) != "red"

        return await probe(deadline_s, check)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()
