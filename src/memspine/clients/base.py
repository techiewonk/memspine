"""The connector protocol (RM-113, docs/CLIENT_ADAPTER_FACTORY_STANDARD.md section 1).

A connector owns one connection (or pool, index handle, session) to one external system:
auth, deadlines, a real health round trip, an idempotent close, its credential-free
identity and its locality. Adapters (``services/<slot>/<tech>.py``) receive an opened
connector and use only :attr:`Connector.handle` (or the connector's typed helpers); they
never construct or open the transport. Factories map slot settings to a transport **spec**
(a frozen dataclass, never a pydantic config model), share the connector through the
process registry (ADR-079, :func:`memspine.providers.connectors.acquire`) and own its close.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

__all__ = ["Connector", "ConnectorIdentity", "Locality", "Scope"]

#: Where the target lives, for the ADR-081 ``deployment.locality`` checks: ``process`` (an
#: in-process database or index), ``host`` (a file or server on this machine) or ``cluster``.
Locality = Literal["process", "host", "cluster"]
#: ``loop`` = the native handle is bound to the event loop that opened it (async drivers);
#: ``thread`` = the handle is thread-safe and may be shared across event loops.
Scope = Literal["loop", "thread"]


@dataclass(frozen=True, slots=True)
class ConnectorIdentity:
    """Credential-free identity of one connector target.

    ``target`` names the physical target (host, port, path, database) without credentials;
    ``auth_digest`` is a one-way digest of the credentials, so two principals on one target
    never share a connection; ``knobs`` are the effective transport settings that change the
    connection (timeouts, pool sizes). ``canonical`` is the physical-target identity used to
    detect two roles writing one target (no credentials, no transport settings).
    """

    kind: str
    target: Mapping[str, Any]
    auth_digest: str | None = None
    knobs: Mapping[str, Any] = field(default_factory=dict)
    canonical: Mapping[str, Any] | None = None

    def key_target(self) -> dict[str, Any]:
        """The registry target: ``kind`` plus the target plus the auth digest (no secrets)."""
        out: dict[str, Any] = {"kind": self.kind, **self.target}
        if self.auth_digest is not None:
            out["auth_digest"] = self.auth_digest
        return out

    def descriptor_target(self) -> dict[str, Any]:
        """The provider descriptor's ``target_identity``: the target, the auth digest and the
        physical ``canonical_target`` (the fingerprint input; no secrets)."""
        out: dict[str, Any] = dict(self.target)
        if self.auth_digest is not None:
            out["auth_digest"] = self.auth_digest
        if self.canonical is not None:
            out["canonical_target"] = dict(self.canonical)
        return out


@runtime_checkable
class Connector[H](Protocol):
    """One connection to one external system (see the module docstring).

    ``open()`` connects within the spec's deadline and closes whatever it opened on failure;
    a second call is a no-op. ``health(deadline_s)`` is a real round trip (``SELECT 1``,
    ``RETURN 1``, ``is_ready``, ``table_names``), never a paid call, and returns False instead
    of raising. ``close()`` is idempotent. ``handle`` is the native object, raising
    :class:`~memspine.exceptions.StorageError` before ``open()``.
    """

    kind: ClassVar[str]
    scope: ClassVar[Scope]

    @classmethod
    def identity(cls, spec: Any) -> ConnectorIdentity: ...

    @classmethod
    def locality(cls, spec: Any) -> Locality: ...

    @property
    def spec(self) -> Any: ...

    async def open(self) -> None: ...

    async def health(self, deadline_s: float) -> bool: ...

    async def close(self) -> None: ...

    @property
    def handle(self) -> H: ...
