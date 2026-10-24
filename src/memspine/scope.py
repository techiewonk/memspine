"""F-16c: the stable memory scope (M-05) and the viewer policy (G-12 B).

``Scope`` is the value object every key is built from: **tenant + owner** are its identity (S3-1;
the owner is a user or a shared group). The ``session`` is provenance only: it is carried for
audit but is excluded from equality, hashing and every key, so one owner's entities are shared by
all the sessions and agents that serve that owner. ``Scope`` maps to the engine's namespace path
(``tenant/owner``; a one-segment path is a tenant-level scope with an empty owner).

``ViewerPolicy`` is the single yes/no for a read inside a multi-party namespace: it must name a
viewer, the viewer must be the authenticated principal when one is known, and an unknown
visibility never broadens access (``memspine.trust.visibility.visible_to`` decides per record).
Every refusal carries a reason code (:class:`ScopeRefused`) so a tap event can name it.

Pure and dependency-light on purpose (stdlib, ``errors`` and the durable ``OwnerScope`` schema), so
any layer may import it. Behaviour is gated by ``isolation.scope_keys`` and
``isolation.viewer_required`` (both default off); this module itself changes nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from memspine.exceptions import MemspineError
from memspine.schemas.durable import OwnerScope

__all__ = [
    "REFUSAL_REASONS",
    "Scope",
    "ScopeRefused",
    "ViewerPolicy",
    "cross_scope_reason",
    "system_scope",
]

#: Reason codes carried by :class:`ScopeRefused` and the ``scope.refused`` tap event.
REFUSAL_REASONS: tuple[str, ...] = (
    "viewer_required",
    "viewer_not_principal",
    "cross_tenant",
    "cross_owner",
    "not_visible",
)


class ScopeRefused(MemspineError):
    """A read was refused by the scope boundary; ``reason`` is one of :data:`REFUSAL_REASONS`."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


#: True inside a checked read verb or a :func:`system_scope`: nested verbs are not checked again.
CHECKED: ContextVar[bool] = ContextVar("memspine_scope_checked", default=False)


@contextlib.contextmanager
def system_scope() -> Iterator[None]:
    """Mark internal engine work (erasure, lookups with no caller to name a viewer): the read
    verbs it calls are not checked against the viewer rule."""
    token = CHECKED.set(True)
    try:
        yield
    finally:
        CHECKED.reset(token)


def _digest(domain: str, parts: list[object]) -> str:
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"{domain}_{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]}"


@dataclass(frozen=True)
class Scope:
    """Tenant plus owner (identity) and an optional session (provenance only, never identity)."""

    tenant: str
    owner: str = ""
    owner_kind: Literal["user", "group"] = "user"
    session: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.tenant:
            raise ValueError("a scope needs a tenant")

    # -- construction -------------------------------------------------------------------------
    @classmethod
    def from_namespace(cls, namespace: str, *, session: str | None = None) -> Scope:
        """``tenant/owner/...``: tenant = first segment, owner = the rest (may be empty)."""
        tenant, _, owner = namespace.strip().strip("/").partition("/")
        return cls(tenant=tenant, owner=owner, session=session)

    @classmethod
    def from_owner_scope(cls, scope: OwnerScope, *, session: str | None = None) -> Scope:
        return cls(scope.tenant, scope.owner, scope.owner_kind, session)

    def to_owner_scope(self) -> OwnerScope:
        """The durable schema form (an owner-less scope uses the tenant as owner)."""
        return OwnerScope(
            tenant=self.tenant, owner=self.owner or self.tenant, owner_kind=self.owner_kind
        )

    # -- identity -----------------------------------------------------------------------------
    @property
    def namespace(self) -> str:
        return f"{self.tenant}/{self.owner}" if self.owner else self.tenant

    @property
    def identity(self) -> tuple[str, str]:
        """What every key is built from: (tenant, owner). Never the session."""
        return (self.tenant, self.owner)

    def key(self, domain: str, *parts: object) -> str:
        """A stable key under this scope: the identity plus ``parts`` (a store, cache, entity or
        graph key). Two scopes that differ in tenant or owner never collide; the session never
        changes the key."""
        return _digest(domain, [self.tenant, self.owner, *parts])

    def entity_key(self, entity: str) -> str:
        return self.key("en", " ".join(entity.casefold().split()))

    def graph_key(self, *parts: object) -> str:
        return self.key("gk", *parts)

    def cache_key(self, *parts: object, viewer: str | None = None) -> str:
        """A cache key; the viewer is part of it (one viewer's cached answer is not another's)."""
        return self.key("ck", viewer, *parts)

    def contains(self, namespace: str) -> bool:
        """True when ``namespace`` is exactly this scope (no prefix or parent match)."""
        return namespace == self.namespace


def cross_scope_reason(reader: Scope, record_namespace: str) -> str | None:
    """None when ``record_namespace`` is the reader's scope, else the cross-scope reason code."""
    if reader.contains(record_namespace):
        return None
    other = Scope.from_namespace(record_namespace)
    return "cross_owner" if other.tenant == reader.tenant else "cross_tenant"


@dataclass(frozen=True)
class ViewerPolicy:
    """The viewer rule of one read: required in a multi-party namespace, bound to the principal."""

    multi_party: bool = False
    principal: str | None = None
    viewer: str | None = None

    def check(self) -> str | None:
        """None when the read may go ahead, else the refusal reason code."""
        viewer = (self.viewer or "").strip() or None
        if viewer is not None and self.principal is not None and viewer != self.principal:
            return "viewer_not_principal"
        if self.multi_party and viewer is None:
            return "viewer_required"
        return None

    def require(self) -> str | None:
        """Raise :class:`ScopeRefused` when the read is refused; else the effective viewer."""
        reason = self.check()
        if reason is not None:
            raise ScopeRefused(reason, f"read refused: {reason}")
        return (self.viewer or "").strip() or None
