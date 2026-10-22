"""AWS connector (RM-113 P1 #14), ``[aws]``: one boto3 session and one service client.

The connector owns the session and the client; adapters (the Secrets Manager resolver) get
:attr:`AwsConnector.handle`. Creating a boto3 client opens no connection (credentials and the
region are checked on the first call), so opening never blocks on the network. Secrets resolve
in bootstrap phase 1, before an event loop owns anything (D-22), so :meth:`open_blocking` is the
synchronous form; :meth:`open` runs it off the loop. :meth:`health` calls STS
``GetCallerIdentity`` (free; it proves the credentials), never the paid service API.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

from memspine.clients._common.deadlines import probe
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["AwsConnector", "AwsSpec"]


@dataclass(frozen=True, slots=True)
class AwsSpec:
    """One AWS service client: the service name, region and optional named profile
    (credentials come from the standard boto3 chain, never from config values)."""

    service: str
    region_name: str | None = None
    profile_name: str | None = None


class AwsConnector:
    """A boto3 session plus one service client; a :class:`~memspine.clients.base.Connector`."""

    kind: ClassVar[str] = "aws"
    scope: ClassVar[Scope] = "thread"

    def __init__(self, spec: AwsSpec) -> None:
        self._spec = spec
        self._session: Any = None
        self._client: Any = None

    @classmethod
    def identity(cls, spec: AwsSpec) -> ConnectorIdentity:
        return ConnectorIdentity(
            cls.kind,
            {"service": spec.service, "region": spec.region_name, "profile": spec.profile_name},
        )

    @classmethod
    def locality(cls, spec: AwsSpec) -> Locality:
        return "cluster"

    @property
    def spec(self) -> AwsSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        if self._client is None:
            raise StorageError("AWS connector is not open — call open() first")
        return self._client

    def open_blocking(self) -> None:
        """:meth:`open` on the calling thread (bootstrap, before an event loop exists)."""
        if self._client is not None:
            return
        try:
            import boto3
        except ImportError as exc:
            raise MissingServiceError(f"aws:{self._spec.service}", extra="aws") from exc
        session = boto3.session.Session(
            region_name=self._spec.region_name, profile_name=self._spec.profile_name
        )
        self._client = session.client(self._spec.service)
        self._session = session

    async def open(self) -> None:
        if self._client is None:
            await asyncio.to_thread(self.open_blocking)

    async def health(self, deadline_s: float) -> bool:
        """STS ``GetCallerIdentity`` with this session's credentials (free)."""
        session = self._session
        if session is None:
            return False

        def round_trip() -> bool:
            sts = session.client("sts")
            try:
                return bool(sts.get_caller_identity().get("Account"))
            finally:
                sts.close()

        async def check() -> bool:
            return await asyncio.to_thread(round_trip)

        return await probe(deadline_s, check)

    def close_blocking(self) -> None:
        client, self._client, self._session = self._client, None, None
        if client is not None and hasattr(client, "close"):
            client.close()

    async def close(self) -> None:
        await asyncio.to_thread(self.close_blocking)
