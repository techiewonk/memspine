"""Lazy async OpenSearch transport for provider-owned lexical indexes."""

from __future__ import annotations

import os
from typing import Any

from memspine.config.remote import endpoint_from_env
from memspine.config.schema.remote_providers import OpenSearchSettings
from memspine.exceptions import ConfigError, MissingServiceError


class OpenSearchClient:
    def __init__(self, settings: OpenSearchSettings, *, max_connections: int | None = None) -> None:
        """``max_connections``: the aiohttp connector limit (``runtime.shared_resources.http``);
        None keeps the client library default."""
        self.settings = settings
        self._max_connections = max_connections
        self._client: Any = None

    async def connect(self) -> None:
        if self._client is not None:
            return
        try:
            from opensearchpy import AsyncHttpConnection, AsyncOpenSearch
        except ImportError:
            raise MissingServiceError("lexical:opensearch", extra="opensearch") from None
        host, scheme, port, secure = endpoint_from_env(
            self.settings.url_env, allowed_schemes=("http", "https")
        )
        auth = None
        if self.settings.username_env is not None and self.settings.password_env is not None:
            username = os.environ.get(self.settings.username_env)
            password = os.environ.get(self.settings.password_env)
            if username is None or password is None:
                missing = (
                    self.settings.username_env if username is None else self.settings.password_env
                )
                raise ConfigError(f"OpenSearch authentication needs environment variable {missing}")
            auth = (username, password)
        pool: dict[str, Any] = (
            {} if self._max_connections is None else {"maxsize": self._max_connections}
        )
        client = AsyncOpenSearch(
            hosts=[{"host": host, "port": port, "scheme": scheme}],
            http_auth=auth,
            use_ssl=secure,
            verify_certs=self.settings.verify_certs,
            connection_class=AsyncHttpConnection,
            timeout=self.settings.request_timeout_s,
            max_retries=2,
            retry_on_timeout=True,
            retry_on_status=(429, 502, 503, 504),
            **pool,
        )
        self._client = client
        try:
            import asyncio

            async with asyncio.timeout(self.settings.connect_timeout_s):
                if not await client.ping():
                    raise ConnectionError("OpenSearch health check failed")
        except BaseException:
            try:
                async with asyncio.timeout(self.settings.connect_timeout_s):
                    await self.close()
            except BaseException:
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
            import asyncio

            async with asyncio.timeout(self.settings.request_timeout_s):
                return bool(await self._client.ping())
        except Exception:
            return False

    @property
    def native(self) -> Any:
        if self._client is None:
            raise RuntimeError("OpenSearch client is not connected")
        return self._client
