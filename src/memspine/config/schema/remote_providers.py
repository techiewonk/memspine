"""Strict settings for optional remote search and document providers.

Connection endpoints and credentials are read from named environment variables at
build time. Config files therefore contain references, never secret values.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from memspine.config import constants
from memspine.providers.settings import StrictSettings

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"


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


class WeaviateDocumentsSettings(WeaviateSettings):
    """ADR-081 ``storage.documents`` Weaviate provider (RM-112). The connection fields are the
    vector/lexical ones, so a documents slot on the same deployment shares their client.

    ``max_bytes`` is capped at 64 MiB (``DOCUMENT_MAX_BYTES``): Weaviate is not a blob store,
    and the bytes travel base64-encoded as BLOB chunk objects of ``chunk_bytes`` (at most
    4 MiB, so one insert stays far below Weaviate's message limits)."""

    max_bytes: int = Field(
        default=constants.DOCUMENT_MAX_BYTES, ge=1, le=constants.DOCUMENT_MAX_BYTES
    )
    chunk_bytes: int = Field(
        default=constants.DOCUMENT_WEAVIATE_CHUNK_BYTES,
        ge=1,
        le=constants.DOCUMENT_WEAVIATE_MAX_CHUNK_BYTES,
    )


class S3DocumentsSettings(StrictSettings):
    """ADR-081 ``storage.documents`` S3-compatible provider (RM-112, ``[aws]``).

    ``endpoint_url_env`` names the variable holding an S3-compatible endpoint (MinIO, Ceph RGW,
    R2); unset is AWS. Credentials: ``access_key_id_env`` + ``secret_access_key_env`` (and
    optionally ``session_token_env``) name environment variables; with none of them the
    standard boto3 chain applies (``profile`` optional; instance and task roles). ``prefix``
    is empty or ends with ``/``. ``part_size_bytes``: the multipart part size (S3 minimum
    5 MiB). ``conditional_writes``: head merges use ``If-Match``; set false for a store
    without conditional-write support (merges are then safe within one process only).
    ``create_bucket``: create a missing bucket at start (development stores)."""

    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    prefix: str = Field(default="memspine/", pattern=r"^(?:[A-Za-z0-9!_.*'()-]+/)*$")
    endpoint_url_env: str | None = Field(default=None, pattern=_ENV_NAME)
    region: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,32}$")
    profile: str | None = Field(default=None, min_length=1)
    access_key_id_env: str | None = Field(default=None, pattern=_ENV_NAME)
    secret_access_key_env: str | None = Field(default=None, pattern=_ENV_NAME)
    session_token_env: str | None = Field(default=None, pattern=_ENV_NAME)
    addressing_style: Literal["auto", "path", "virtual"] = "auto"
    checksum_mode: Literal["when_supported", "when_required"] = "when_supported"
    connect_timeout_s: float = Field(gt=0, allow_inf_nan=False)
    request_timeout_s: float = Field(gt=0, allow_inf_nan=False)
    max_bytes: int = Field(
        default=constants.DOCUMENT_MAX_BYTES, ge=1, le=constants.DOCUMENT_S3_MAX_BYTES
    )
    part_size_bytes: int = Field(
        default=constants.DOCUMENT_S3_PART_BYTES,
        ge=constants.DOCUMENT_S3_MIN_PART_BYTES,
        le=constants.DOCUMENT_S3_MAX_BYTES,
    )
    conditional_writes: bool = True
    create_bucket: bool = False

    @model_validator(mode="after")
    def _credentials_are_consistent(self) -> S3DocumentsSettings:
        if (self.access_key_id_env is None) != (self.secret_access_key_env is None):
            raise ValueError(
                "access_key_id_env and secret_access_key_env must be configured together"
            )
        if self.session_token_env is not None and self.access_key_id_env is None:
            raise ValueError("session_token_env needs access_key_id_env and secret_access_key_env")
        if self.profile is not None and self.access_key_id_env is not None:
            raise ValueError("choose either profile or static credentials, not both")
        if any(part in (".", "..") for part in self.prefix.split("/")):
            raise ValueError("prefix must not contain a '.' or '..' segment")
        return self
