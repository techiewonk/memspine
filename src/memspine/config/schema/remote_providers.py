"""Strict settings for optional remote search providers.

Connection endpoints and credentials are read from named environment variables at
build time. Config files therefore contain references, never secret values.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from memspine.providers.settings import StrictSettings


class OpenSearchSettings(StrictSettings):
    url_env: str = Field(default="MEMSPINE_OPENSEARCH_URL", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    username_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    password_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    index_prefix: str = Field(default="memspine", pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    verify_certs: bool = True
    connect_timeout_s: float = Field(gt=0, allow_inf_nan=False)
    request_timeout_s: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def credentials_are_a_pair(self) -> OpenSearchSettings:
        if (self.username_env is None) != (self.password_env is None):
            raise ValueError("username_env and password_env must be configured together")
        return self


class WeaviateSettings(StrictSettings):
    url_env: str = Field(default="MEMSPINE_WEAVIATE_URL", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    grpc_url_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    collection_prefix: str = Field(default="Memspine", pattern=r"^[A-Z][A-Za-z0-9]{0,39}$")
    grpc_port: int = Field(default=50051, ge=1, le=65535, strict=True)
    connect_timeout_s: float = Field(gt=0, allow_inf_nan=False)
    request_timeout_s: float = Field(gt=0, allow_inf_nan=False)
