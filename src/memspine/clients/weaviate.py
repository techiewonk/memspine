"""Lazy async Weaviate transport shared by one provider-owned collection."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from memspine.config.remote import endpoint_from_env
from memspine.config.schema.remote_providers import WeaviateSettings
from memspine.exceptions import ConfigError, MissingServiceError


class WeaviateClient:
    def __init__(self, settings: WeaviateSettings, *, max_connections: int | None = None) -> None:
        """``max_connections``: the HTTP session pool size (``runtime.shared_resources.http``);
        None keeps the client library default."""
        self.settings = settings
        self._max_connections = max_connections
        self._client: Any = None

    async def connect(self) -> None:
        if self._client is not None:
            return
        try:
            import weaviate
            from weaviate.auth import Auth
            from weaviate.config import AdditionalConfig, ConnectionConfig, Timeout
        except ImportError:
            raise MissingServiceError("provider:weaviate", extra="weaviate") from None
        host, _scheme, port, secure = endpoint_from_env(
            self.settings.url_env, allowed_schemes=("http", "https")
        )
        grpc_host, grpc_port, grpc_secure = host, self.settings.grpc_port, secure
        if self.settings.grpc_url_env is not None:
            grpc_host, _grpc_scheme, grpc_port, grpc_secure = endpoint_from_env(
                self.settings.grpc_url_env, allowed_schemes=("http", "https")
            )
        auth = None
        if self.settings.api_key_env is not None:
            api_key = os.environ.get(self.settings.api_key_env)
            if not api_key:
                raise ConfigError(
                    f"Weaviate authentication needs environment variable {self.settings.api_key_env}"
                )
            auth = Auth.api_key(api_key)
        pool: dict[str, Any] = {}
        if self._max_connections is not None:
            pool["connection"] = ConnectionConfig(session_pool_maxsize=self._max_connections)
        client = weaviate.use_async_with_custom(
            http_host=host,
            http_port=port,
            http_secure=secure,
            grpc_host=grpc_host,
            grpc_port=grpc_port,
            grpc_secure=grpc_secure,
            auth_credentials=auth,
            additional_config=AdditionalConfig(
                timeout=Timeout(
                    init=self.settings.connect_timeout_s,
                    query=self.settings.request_timeout_s,
                    insert=self.settings.request_timeout_s,
                ),
                **pool,
            ),
        )
        self._client = client
        try:
            async with asyncio.timeout(self.settings.connect_timeout_s):
                await client.connect()
                if not await client.is_ready():
                    raise ConnectionError("Weaviate readiness check failed")
        except BaseException:
            try:
                async with asyncio.timeout(self.settings.connect_timeout_s):
                    await self.close()
            except BaseException:
                # Preserve the connection failure or cancellation while ensuring
                # an optional driver cannot hold startup open indefinitely.
                pass
            raise

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def health(self) -> bool:
        if self._client is None:
            return False
        try:
            async with asyncio.timeout(self.settings.request_timeout_s):
                return bool(await self._client.is_ready())
        except Exception:
            return False

    @property
    def native(self) -> Any:
        if self._client is None:
            raise RuntimeError("Weaviate client is not connected")
        return self._client
