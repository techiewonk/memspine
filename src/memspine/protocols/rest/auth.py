"""#51 reference auth middleware for the REST app (ADR-041).

**Not a production auth plane.** It shows where authentication plugs in and binds
an authenticated principal and its namespaces to every request; key rotation,
revocation lists, multi-replica rate limits and an admin console are out of scope.

``rest.auth.mode``:

- ``none`` (default): no authentication, the v0.1 behaviour.
- ``api_key``: ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``. Each key
  maps to a principal, the namespace globs it may use and an admin flag. Keys come
  from environment variables (``key_env``) or config (``key``); only their SHA-256
  digests are kept, and no key is ever logged or returned.
- ``oidc_jwt``: ``Authorization: Bearer <jwt>`` verified with PyJWT (``pyjwt``
  must be installed: a clear error otherwise). Principal, namespaces and roles come
  from configurable claims.

Every authenticated request may only address a namespace its principal is allowed
(the ``X-Memspine-Namespace`` header, default ``default``): others get 403. Admin
routes (``/sleep``, ``/rebuild``, ``/export``, ``/quarantine...``) need the admin
flag or role: a dependency on each of those routes checks it, so a mount prefix or
``root_path`` cannot route around it. ``rest.rate_limit`` adds an in-memory token
bucket per principal. Failed authentications are throttled per client address
before any credential is checked (``rest.rate_limit``, else
``REST_AUTH_FAILURE_*``), and a JWKS refetch for an unknown ``kid`` happens at
most once per ``REST_JWKS_MISS_BACKOFF_SECONDS``.

``oidc_jwt`` tokens must carry ``exp``, ``iss`` and ``aud``; the issuer and audience
are required config and the algorithm family is pinned (see ``RestAuthConfig``).
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import importlib
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from memspine.config import constants
from memspine.config.schema import RestConfig, RestJwtConfig, RestRateLimitConfig
from memspine.exceptions import ConfigError

__all__ = [
    "ADMIN_PATHS",
    "AuthError",
    "Authenticator",
    "Principal",
    "RateLimiter",
    "is_admin_path",
    "route_path",
]

#: Route prefixes that need the admin role once authentication is on.
ADMIN_PATHS = ("/sleep", "/rebuild", "/export", "/quarantine")


def is_admin_path(path: str) -> bool:
    """Whether ``path`` is one of the operator-only routes."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in ADMIN_PATHS)


def route_path(scope: Mapping[str, Any]) -> str:
    """The request path as the app's own routes see it: ``scope["path"]`` without
    the ``root_path`` a mount or a proxy prefix put in front of it."""
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", "") or "")
    if root and (path == root or path.startswith(root + "/")):
        return path[len(root) :] or "/"
    return path


class AuthError(Exception):
    """An authentication or authorization failure with its HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Principal:
    """The caller a request is bound to."""

    name: str
    namespaces: tuple[str, ...] = ()
    admin: bool = False

    def may_use(self, namespace: str) -> bool:
        """Whether one of the principal's namespace globs matches ``namespace``."""
        return any(fnmatch.fnmatchcase(namespace, pattern) for pattern in self.namespaces)


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


@dataclass
class _KeyEntry:
    digest: bytes
    principal: Principal


class Authenticator:
    """Resolves a request's credentials to a :class:`Principal` (or raises)."""

    def __init__(
        self,
        config: RestConfig,
        env: Mapping[str, str] | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.mode = config.auth.mode
        self._clock = clock
        #: kids a JWKS lookup resolved, and when the last lookup missed.
        self._known_kids: set[str] = set()
        self._last_jwks_miss: float | None = None
        environ = os.environ if env is None else env
        self._keys: list[_KeyEntry] = []
        self._jwt_config: RestJwtConfig = config.auth.jwt
        self._jwt: Any = None
        self._jwt_key: Any = None
        if self.mode == "api_key":
            for index, entry in enumerate(config.auth.api_keys):
                secret = environ.get(entry.key_env) if entry.key_env else entry.key
                if not secret:
                    source = f"env var {entry.key_env}" if entry.key_env else "key"
                    raise ConfigError(f"rest.auth.api_keys[{index}]: {source} is empty or unset")
                principal = Principal(entry.principal, tuple(entry.namespaces), entry.admin)
                self._keys.append(_KeyEntry(_digest(secret), principal))
            if not self._keys:
                raise ConfigError("rest.auth.mode=api_key needs at least one rest.auth.api_keys")
        elif self.mode == "oidc_jwt":
            try:
                jwt = importlib.import_module("jwt")  # optional: pyjwt
            except ImportError as exc:
                raise ConfigError(
                    "rest.auth.mode=oidc_jwt needs PyJWT: pip install 'pyjwt[crypto]'"
                ) from exc
            self._jwt = jwt
            cfg = self._jwt_config
            if cfg.jwks_url:
                self._jwt_key = jwt.PyJWKClient(cfg.jwks_url)
            elif cfg.key_env and environ.get(cfg.key_env):
                self._jwt_key = environ[cfg.key_env]
            else:
                raise ConfigError(
                    "rest.auth.mode=oidc_jwt needs rest.auth.jwt.jwks_url or a set key_env"
                )

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        """The principal behind the request's credentials; 401 when missing or bad."""
        token = _bearer(headers)
        if self.mode == "api_key":
            token = token or headers.get("x-api-key")
            if not token:
                raise AuthError(401, "missing API key")
            digest = _digest(token)
            for entry in self._keys:
                if hmac.compare_digest(digest, entry.digest):
                    return entry.principal
            raise AuthError(401, "invalid API key")
        if not token:
            raise AuthError(401, "missing bearer token")
        return self._verify_jwt(token)

    def _verify_jwt(self, token: str) -> Principal:
        jwt = self._jwt
        cfg = self._jwt_config
        key = self._jwt_key
        if hasattr(key, "get_signing_key_from_jwt"):
            key = self._jwks_key(key, token)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(cfg.algorithms),
                audience=cfg.audience,
                issuer=cfg.issuer,
                options={"require": ["exp", "iss", "aud"], "verify_aud": True},
            )
        except Exception as exc:
            raise AuthError(401, "invalid bearer token") from exc
        subject = claims.get(cfg.principal_claim)
        if not isinstance(subject, str) or not subject:
            raise AuthError(401, f"token has no {cfg.principal_claim!r} claim")
        raw_ns = claims.get(cfg.namespaces_claim, [])
        namespaces = raw_ns.split() if isinstance(raw_ns, str) else [str(n) for n in raw_ns]
        raw_roles = claims.get(cfg.roles_claim, [])
        roles = raw_roles.split() if isinstance(raw_roles, str) else [str(r) for r in raw_roles]
        return Principal(subject, tuple(namespaces), cfg.admin_role in roles)

    def _jwks_key(self, client: Any, token: str) -> Any:
        """The JWKS signing key for ``token``. A ``kid`` no lookup has resolved yet
        may trigger a JWKS fetch only once per ``REST_JWKS_MISS_BACKOFF_SECONDS``:
        inside that window after a miss, an unknown kid is refused unfetched."""
        try:
            header = self._jwt.get_unverified_header(token)
            kid = str(header.get("kid") or "") if isinstance(header, dict) else ""
        except Exception as exc:
            raise AuthError(401, "invalid bearer token") from exc
        now = self._clock()
        last = self._last_jwks_miss
        backoff = constants.REST_JWKS_MISS_BACKOFF_SECONDS
        if kid not in self._known_kids and last is not None and now - last < backoff:
            raise AuthError(401, "invalid bearer token")
        try:
            key = client.get_signing_key_from_jwt(token).key
        except Exception as exc:
            self._last_jwks_miss = now
            raise AuthError(401, "invalid bearer token") from exc
        self._known_kids.add(kid)
        return key


def _bearer(headers: Mapping[str, str]) -> str | None:
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


@dataclass
class RateLimiter:
    """In-memory token bucket per key (one process; not shared across replicas)."""

    config: RestRateLimitConfig
    clock: Any = time.monotonic
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)

    def _tokens(self, key: str, now: float) -> float:
        rate, burst = self.config.requests_per_second, float(self.config.burst)
        tokens, last = self._buckets.get(key, (burst, now))
        return min(burst, tokens + (now - last) * rate)

    def peek(self, key: str) -> bool:
        """Whether ``key``'s bucket holds a token, without taking one."""
        return self._tokens(key, self.clock()) >= 1.0

    def allow(self, key: str) -> bool:
        """Take one token from ``key``'s bucket; False when it is empty."""
        now = self.clock()
        tokens = self._tokens(key, now)
        if key not in self._buckets and len(self._buckets) >= constants.REST_RATE_LIMIT_MAX_KEYS:
            self._prune(now)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True

    def _prune(self, now: float) -> None:
        """Drop the buckets that have refilled: a full bucket carries no state."""
        burst = float(self.config.burst)
        for key in [k for k in self._buckets if self._tokens(k, now) >= burst]:
            del self._buckets[key]
