"""MongoDB connector (RM-112, ``[mongodb]``): one replica-set client for the ``records`` slot.

Driver: **pymongo** (>= 4.13), whose native asyncio API (``AsyncMongoClient``) is the upstream
recommendation since Motor was deprecated (May 2025). One package also gives the synchronous
``MongoClient`` the lease table needs (lease holders run in threads and processes, ADR-081).

Layering (``docs/CLIENT_ADAPTER_FACTORY_STANDARD.md``): this connector owns the connections,
deadlines, the ``hello`` health round trip and close. The adapter
(:mod:`memspine.services.storage.mongodb`) receives it and never opens a connection.

It implements the RM-113 :class:`~memspine.clients.base.Connector` protocol; factories share it
through the process registry (``providers.connectors.acquire``), so the ``records`` and
``documents`` slots on one deployment with the same settings use one client.

The records contract needs multi-document transactions (atomic append-plus-outbox), which
MongoDB offers only on a replica set or a sharded cluster. :meth:`MongoConnector.open`
refuses a standalone server with a clear error instead of failing on the first write.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qsl, urlsplit

from memspine.clients._common.deadlines import probe
from memspine.clients._common.secrets import auth_digest
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import MissingServiceError, StorageError

if TYPE_CHECKING:
    from gridfs import AsyncGridFSBucket
    from pymongo import AsyncMongoClient, MongoClient
    from pymongo.asynchronous.database import AsyncDatabase
    from pymongo.synchronous.database import Database

__all__ = ["MongoConnector", "MongoSpec", "StandaloneServerError"]


class StandaloneServerError(StorageError):
    """The server is a standalone ``mongod``: no multi-document transactions."""


@dataclass(frozen=True, slots=True)
class MongoSpec:
    """Transport settings of one MongoDB target (a frozen spec, never a config model)."""

    url: str = field(repr=False)  # may carry credentials
    database: str = "memspine"
    #: overall deadline of :meth:`MongoConnector.open` (server selection plus ``hello``)
    connect_timeout_s: float = 10.0
    server_selection_timeout_s: float = 10.0
    #: client-side operation timeout (pymongo ``timeoutMS``); None leaves the driver default
    operation_timeout_s: float | None = 30.0
    app_name: str = "memspine"
    #: the records slot needs transactions (append-plus-outbox); the documents slot asks for
    #: them too, so both slots on one deployment share one connector
    require_transactions: bool = True


def _canonical_target(url: str) -> tuple[str, str, list[tuple[str, str]], str]:
    """(scheme, sorted hosts, sorted options, userinfo) of a ``mongodb[+srv]://`` URL."""
    parts = urlsplit(url)
    if parts.scheme not in ("mongodb", "mongodb+srv"):
        raise StorageError("a MongoDB URL starts with mongodb:// or mongodb+srv://")
    netloc = parts.netloc
    userinfo, _, hosts = netloc.rpartition("@")
    host_list = ",".join(sorted(h.lower() for h in hosts.split(",") if h))
    options = sorted(parse_qsl(parts.query))
    return parts.scheme, host_list, options, userinfo


class MongoConnector:
    """Owns the async client (event loop) and, on demand, a sync client (lease threads)."""

    kind: ClassVar[str] = "mongodb"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: MongoSpec) -> None:
        _canonical_target(spec.url)  # fail fast on a malformed URL
        self._spec = spec
        self._client: AsyncMongoClient[dict[str, Any]] | None = None
        self._sync: MongoClient[dict[str, Any]] | None = None
        self._sync_lock = threading.Lock()
        self.topology: str | None = None  # "replica_set:<name>" or "sharded" after open()

    # ---------------------------------------------------------------- identity
    @classmethod
    def identity(cls, spec: MongoSpec) -> ConnectorIdentity:
        """Credential-free identity: hosts, database and options as the target, the user info
        as an auth digest, the transport settings as knobs. Two slots naming one deployment
        with the same settings (records and documents) share one connector."""
        scheme, hosts, options, userinfo = _canonical_target(spec.url)
        return ConnectorIdentity(
            cls.kind,
            {
                "scheme": scheme,
                "hosts": hosts,
                "database": spec.database,
                "options_sha256": hashlib.sha256(repr(options).encode()).hexdigest(),
            },
            auth_digest(userinfo) if userinfo else None,
            {
                "connect_timeout_s": spec.connect_timeout_s,
                "server_selection_timeout_s": spec.server_selection_timeout_s,
                "operation_timeout_s": spec.operation_timeout_s,
                "app_name": spec.app_name,
                "require_transactions": spec.require_transactions,
            },
            {"kind": "mongodb", "hosts": hosts, "database": spec.database},
        )

    @classmethod
    def locality(cls, spec: MongoSpec) -> Locality:
        """A MongoDB deployment is shared by every process that reaches it (the declared
        trait; a loopback address is usually a sidecar or a port forward)."""
        return "cluster"

    @property
    def spec(self) -> MongoSpec:
        return self._spec

    # ---------------------------------------------------------------- lifecycle
    def _options(self) -> dict[str, Any]:
        spec = self.spec
        options: dict[str, Any] = {
            "appname": spec.app_name,
            "connectTimeoutMS": int(spec.connect_timeout_s * 1000),
            "serverSelectionTimeoutMS": int(spec.server_selection_timeout_s * 1000),
            "w": "majority",
            "tz_aware": True,
        }
        if spec.operation_timeout_s is not None:
            options["timeoutMS"] = int(spec.operation_timeout_s * 1000)
        return options

    async def open(self) -> None:
        """Connect within the deadline, check the topology, close again on any failure."""
        if self._client is not None:
            return
        try:
            from pymongo import AsyncMongoClient
        except ImportError as exc:
            raise MissingServiceError("storage:mongodb", extra="mongodb") from exc
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            self.spec.url, **self._options()
        )
        try:
            async with asyncio.timeout(self.spec.connect_timeout_s):
                hello = await client.admin.command("hello")
            self.topology = self._check_topology(hello)
        except BaseException:
            await client.close()
            raise
        self._client = client

    def _check_topology(self, hello: dict[str, Any]) -> str:
        if hello.get("setName"):
            return f"replica_set:{hello['setName']}"
        if hello.get("msg") == "isdbgrid":
            return "sharded"
        if self.spec.require_transactions:
            raise StandaloneServerError(
                "MongoDB records store needs a replica set (or a sharded cluster): the "
                "server is a standalone mongod, which has no multi-document transactions, "
                "so append-plus-outbox cannot be atomic. Start mongod with --replSet and "
                "run rs.initiate() (a one-member replica set is enough for development)."
            )
        return "standalone"

    async def health(self, deadline_s: float = 5.0) -> bool:
        """A real ``hello`` round trip within ``deadline_s`` (a writable primary or a router)."""
        client = self._client
        if client is None:
            return False

        async def check() -> bool:
            hello = await client.admin.command("hello")
            return bool(hello.get("isWritablePrimary") or hello.get("msg") == "isdbgrid")

        return await probe(deadline_s, check)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()
        with self._sync_lock:
            sync, self._sync = self._sync, None
        if sync is not None:
            await asyncio.to_thread(sync.close)

    # ---------------------------------------------------------------- handles
    @property
    def handle(self) -> AsyncMongoClient[dict[str, Any]]:
        """The native async client (given only to the adapter)."""
        if self._client is None:
            raise StorageError("MongoConnector is not open: call open() first")
        return self._client

    @property
    def database(self) -> AsyncDatabase[dict[str, Any]]:
        return self.handle[self.spec.database]

    def gridfs_bucket(self, name: str) -> AsyncGridFSBucket:
        """A GridFS bucket ``name`` (``<name>.files`` / ``<name>.chunks``) on this database,
        majority-acknowledged like every other write (the documents slot, RM-112)."""
        from gridfs import AsyncGridFSBucket

        return AsyncGridFSBucket(self.database, bucket_name=name)

    def sync_database(self) -> Database[dict[str, Any]]:
        """The same database over a synchronous client, opened on first use (lease table)."""
        self.handle  # noqa: B018 - only an open connector lends a sync handle
        with self._sync_lock:
            if self._sync is None:
                from pymongo import MongoClient

                self._sync = MongoClient(self.spec.url, **self._options())
            return self._sync[self.spec.database]
