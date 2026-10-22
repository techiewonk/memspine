"""Acquire a shared, opened connector for one Engine (RM-113 P0 #4, ADR-079).

Factories map slot settings to a transport spec and call :func:`acquire`; the connector's
:meth:`~memspine.clients.base.Connector.identity` keys the process registry, so two slots on
the same target (Weaviate vector and lexical, Postgres records and pgvector) share one
connector, and anything that differs (target, credentials, transport knobs, event loop for
loop-bound connectors) gets its own. The Engine owns only its lease: the registry closes the
connector when the last holder in any Engine releases it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from memspine.providers.shared import own_shared, resource_key

if TYPE_CHECKING:
    from memspine.clients.base import Connector
    from memspine.clients.http import HttpConnector
    from memspine.config.schema.runtime import SharedHttpPoolConfig
    from memspine.providers.contexts import SharedBuildContext
    from memspine.providers.contracts import ResourceRef

__all__ = ["acquire", "http_connector"]


async def acquire[C: Connector[Any]](
    ctx: SharedBuildContext,
    connector_cls: type[C],
    spec: Any,
    *,
    resource_id: str,
    depends_on: tuple[str, ...] = (),
) -> tuple[C, ResourceRef]:
    """Return an opened connector for ``spec`` (shared through the registry) and the Engine's
    owned :class:`ResourceRef` whose closer releases this Engine's lease.

    The key holds the connector kind, its credential-free target, the auth digest and the
    effective transport knobs, never a secret. A failed open is not cached; the connector
    closes what it opened before the error propagates."""
    identity = connector_cls.identity(spec)
    key = resource_key(
        identity.kind, identity.key_target(), identity.knobs, scope=connector_cls.scope
    )

    async def build() -> C:
        connector = cast(Any, connector_cls)(spec)
        await connector.open()
        return cast(C, connector)

    async def close(connector: C) -> None:
        await connector.close()

    return await own_shared(
        ctx.resources,
        resource_id,
        key,
        build,
        close=close,
        depends_on=depends_on,
        registry=ctx.resource_registry(),
    )


def http_connector(pool: SharedHttpPoolConfig | None = None) -> HttpConnector:
    """An unopened :class:`~memspine.clients.http.HttpConnector` with the Engine's HTTP pool
    options (``runtime.shared_resources.http``); it opens on its first request. The caller
    owns it and closes it."""
    from memspine.clients.http import HttpConnector, HttpSpec

    if pool is None:
        return HttpConnector(HttpSpec())
    return HttpConnector(
        HttpSpec(max_connections=pool.max_connections, keepalive_s=pool.keepalive_s)
    )
