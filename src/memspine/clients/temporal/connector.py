"""Temporal cluster connector (RM-113 section 2): the one ``temporalio`` ``Client.connect``.

The Temporal runner (``workers/temporal_runner.py``) receives an opened connector and uses its
:attr:`TemporalConnector.handle` (the ``temporalio.client.Client``); it never connects itself.
``temporalio`` is imported only in :meth:`TemporalConnector.open` (the ``[temporal]`` extra).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

from memspine.clients._common.secrets import Secret, auth_digest
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import StorageError

__all__ = ["TemporalConnector", "TemporalSpec"]


@dataclass(frozen=True, slots=True)
class TemporalSpec:
    """Transport spec: frontend address, namespace, TLS config (a ``temporalio`` TLSConfig,
    or a bool), API key and the connect deadline."""

    address: str
    namespace: str
    tls: Any = False
    api_key: Secret | None = None
    connect_timeout_s: float = 30.0


class TemporalConnector:
    """One Temporal client for one frontend address and namespace."""

    kind: ClassVar[str] = "temporal"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: TemporalSpec) -> None:
        self._spec = spec
        self._client: Any | None = None

    @classmethod
    def identity(cls, spec: TemporalSpec) -> ConnectorIdentity:
        return ConnectorIdentity(
            kind=cls.kind,
            target={"address": spec.address, "namespace": spec.namespace},
            auth_digest=auth_digest(spec.api_key.reveal()) if spec.api_key else None,
            knobs={"tls": bool(spec.tls), "connect_timeout_s": spec.connect_timeout_s},
        )

    @classmethod
    def locality(cls, spec: TemporalSpec) -> Locality:
        return "cluster"

    @property
    def spec(self) -> TemporalSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        if self._client is None:
            raise StorageError("Temporal connector is not open")
        return self._client

    async def open(self) -> None:
        if self._client is not None:
            return
        from temporalio.client import Client

        spec = self._spec
        async with asyncio.timeout(spec.connect_timeout_s):
            self._client = await Client.connect(
                spec.address,
                namespace=spec.namespace,
                tls=spec.tls,
                api_key=spec.api_key.reveal() if spec.api_key else None,
            )

    async def health(self, deadline_s: float) -> bool:
        if self._client is None:
            return False
        check = getattr(getattr(self._client, "service_client", None), "check_health", None)
        if check is None:
            return False
        try:
            async with asyncio.timeout(deadline_s):
                return bool(await check())
        except Exception:
            return False

    async def close(self) -> None:
        # temporalio clients hold no explicit close; dropping the reference releases it.
        self._client = None
