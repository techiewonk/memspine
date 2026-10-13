"""Config area: rest (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RestApiKeyConfig(BaseModel):
    """#51: one API key of the reference REST auth middleware. The key itself is
    read from the environment variable ``key_env`` (or given in ``key``, which the
    loader can resolve from a secret reference); it is never logged or echoed."""

    model_config = ConfigDict(extra="forbid")

    key_env: str | None = None
    key: str | None = Field(default=None, repr=False)
    principal: str
    namespaces: list[str] = Field(default_factory=list)
    admin: bool = False


class RestJwtConfig(BaseModel):
    """#51: OIDC/JWT bearer verification (needs ``pyjwt``). The verification key
    comes from ``jwks_url`` (asymmetric, fetched by PyJWT) or the environment
    variable ``key_env`` (a PEM public key or an HMAC secret)."""

    model_config = ConfigDict(extra="forbid")

    issuer: str | None = None
    audience: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwks_url: str | None = None
    key_env: str | None = None
    principal_claim: str = "sub"
    namespaces_claim: str = "memspine_namespaces"
    roles_claim: str = "roles"
    admin_role: str = "memspine-admin"


class RestRateLimitConfig(BaseModel):
    """#51: in-memory token bucket per principal (per client address without auth)."""

    model_config = ConfigDict(extra="forbid")

    requests_per_second: float = Field(gt=0.0)
    burst: int = Field(default=10, ge=1)


class RestAuthConfig(BaseModel):
    """#51 reference auth middleware (not a production auth plane, ADR-041)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "api_key", "oidc_jwt"] = "none"
    api_keys: list[RestApiKeyConfig] = Field(default_factory=list)
    jwt: RestJwtConfig = Field(default_factory=RestJwtConfig)

    @model_validator(mode="after")
    def _jwt_is_pinned(self) -> RestAuthConfig:
        """ADR-041 addendum: ``oidc_jwt`` needs an issuer and an audience (a token
        minted for another app or by another issuer is refused), and a pinned
        algorithm family: never ``none``, never HMAC next to an asymmetric family
        (key confusion), never HMAC with a JWKS URL (a public key used as a secret)."""
        if self.mode != "oidc_jwt":
            return self
        cfg = self.jwt
        if not cfg.issuer or not cfg.audience:
            raise ValueError("rest.auth.mode=oidc_jwt needs rest.auth.jwt.issuer and audience")
        algorithms = [a.strip() for a in cfg.algorithms]
        if not algorithms:
            raise ValueError("rest.auth.jwt.algorithms must name at least one algorithm")
        if any(a.lower() == "none" or not a for a in algorithms):
            raise ValueError("rest.auth.jwt.algorithms may not include 'none'")
        families = {a[:2].upper() for a in algorithms}
        if "HS" in families and len(families) > 1:
            raise ValueError("rest.auth.jwt.algorithms may not mix HMAC (HS*) with others")
        if "HS" in families and cfg.jwks_url:
            raise ValueError("rest.auth.jwt.jwks_url needs asymmetric algorithms, not HS*")
        return self


class RestConfig(BaseModel):
    """#51 REST protocol options. Defaults keep the unauthenticated v0.1 app."""

    model_config = ConfigDict(extra="forbid")

    auth: RestAuthConfig = Field(default_factory=RestAuthConfig)
    rate_limit: RestRateLimitConfig | None = None
