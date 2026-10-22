"""Weaviate connector (RM-113): a lazy async transport shared by provider-owned collections.

The endpoints and API key come from environment variables named by the spec (never from
config values); :meth:`WeaviateClient.identity` is the single identity parser, shared by the
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

__all__ = ["WeaviateClient", "WeaviateSpec"]

_SCHEMES = ("http", "https")


@dataclass(frozen=True, slots=True)
class WeaviateSpec:
    """Transport spec: environment variable names, the gRPC port, deadlines and pool size.

    ``max_connections``: the HTTP session pool size (``runtime.shared_resources.http``);
    None keeps the client library default."""

    connect_timeout_s: float
    request_timeout_s: float
    url_env: str = "MEMSPINE_WEAVIATE_URL"
    grpc_url_env: str | None = None
    api_key_env: str | None = None
    grpc_port: int = 50051
    max_connections: int | None = None


class WeaviateClient:
    """One async Weaviate client per target; a :class:`~memspine.clients.base.Connector`."""

    kind: ClassVar[str] = "weaviate"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: WeaviateSpec) -> None:
        self._spec = spec
        self._client: Any = None

    @classmethod
    def identity(cls, spec: WeaviateSpec) -> ConnectorIdentity:
        http = endpoint_from_env(spec.url_env, allowed_schemes=_SCHEMES)
        grpc_host, grpc_scheme, grpc_port = http.host, http.scheme, spec.grpc_port
        if spec.grpc_url_env is not None:
            grpc = endpoint_from_env(spec.grpc_url_env, allowed_schemes=_SCHEMES)
            grpc_host, grpc_scheme, grpc_port = grpc.host, grpc.scheme, grpc.port
        token = os.environ.get(spec.api_key_env, "") if spec.api_key_env else ""
        if spec.api_key_env and not token:
            raise ConfigError(
                f"Weaviate authentication needs environment variable {spec.api_key_env}"
            )
        host = http.host.lower()
        return ConnectorIdentity(
            cls.kind,
            {
                "kind": "weaviate",
                "host": host,
                "scheme": http.scheme,
                "port": http.port,
                "grpc_host": grpc_host.lower(),
                "grpc_scheme": grpc_scheme,
                "grpc_port": grpc_port,
            },
            auth_digest(token),
            {
                "connect_timeout_s": spec.connect_timeout_s,
                "request_timeout_s": spec.request_timeout_s,
                "max_connections": spec.max_connections,
            },
            {
                "kind": "weaviate",
                "host": host,
                "port": http.port,
                "grpc_host": grpc_host.lower(),
                "grpc_port": grpc_port,
            },
        )

    @classmethod
    def locality(cls, spec: WeaviateSpec) -> Locality:
        http = endpoint_from_env(spec.url_env, allowed_schemes=_SCHEMES)
        return "host" if is_loopback(http.host) else "cluster"

    @property
    def spec(self) -> WeaviateSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        if self._client is None:
            raise StorageError("Weaviate client is not open — call open() first")
        return self._client

    async def open(self) -> None:
        if self._client is not None:
            return
        try:
            import weaviate
            from weaviate.auth import Auth
            from weaviate.config import AdditionalConfig, ConnectionConfig, Timeout
        except ImportError:
            raise MissingServiceError("provider:weaviate", extra="weaviate") from None
        spec = self._spec
        http = endpoint_from_env(spec.url_env, allowed_schemes=_SCHEMES)
        grpc_host, grpc_port, grpc_secure = http.host, spec.grpc_port, http.secure
        if spec.grpc_url_env is not None:
            grpc = endpoint_from_env(spec.grpc_url_env, allowed_schemes=_SCHEMES)
            grpc_host, grpc_port, grpc_secure = grpc.host, grpc.port, grpc.secure
        auth = None
        if spec.api_key_env is not None:
            api_key = os.environ.get(spec.api_key_env)
            if not api_key:
                raise ConfigError(
                    f"Weaviate authentication needs environment variable {spec.api_key_env}"
                )
            auth = Auth.api_key(api_key)
        pool: dict[str, Any] = {}
        if spec.max_connections is not None:
            pool["connection"] = ConnectionConfig(session_pool_maxsize=spec.max_connections)
        client = weaviate.use_async_with_custom(
            http_host=http.host,
            http_port=http.port,
            http_secure=http.secure,
            grpc_host=grpc_host,
            grpc_port=grpc_port,
            grpc_secure=grpc_secure,
            auth_credentials=auth,
            additional_config=AdditionalConfig(
                timeout=Timeout(
                    init=spec.connect_timeout_s,
                    query=spec.request_timeout_s,
                    insert=spec.request_timeout_s,
                ),
                **pool,
            ),
        )
        self._client = client
        try:
            async with asyncio.timeout(spec.connect_timeout_s):
                await client.connect()
                if not await client.is_ready():
                    raise ConnectionError("Weaviate readiness check failed")
        except BaseException:
            try:
                async with asyncio.timeout(spec.connect_timeout_s):
                    await self.close()
            except BaseException:
                # Preserve the connection failure or cancellation while ensuring
                # an optional driver cannot hold startup open indefinitely.
                pass
            raise

    async def health(self, deadline_s: float) -> bool:
        """``GET /v1/.well-known/ready``."""
        client = self._client
        if client is None:
            return False

        async def check() -> bool:
            return bool(await client.is_ready())

        return await probe(deadline_s, check)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()
