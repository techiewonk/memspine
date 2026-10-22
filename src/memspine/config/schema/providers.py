"""Typed settings for built-in providers.

The selected provider descriptor owns validation and construction. These models
are strict, frozen contracts used by descriptors and by the resolved config.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from memspine.config import constants
from memspine.providers.settings import (
    EmptySettings,
    StrictSettings,
)
from memspine.providers.settings import (
    Neo4jSettings as Neo4jSettings,
)

ProviderSettings = StrictSettings
ProviderSettingsMap = dict[str, JsonValue]

FASTEMBED_MODEL_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}
STATIC_MODEL_DIMS = {"minishlab/potion-base-8M": 256}
SENTENCE_TRANSFORMERS_MODEL_DIMS = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "Qwen/Qwen3-Embedding-0.6B": 1024,
}
LITELLM_MODEL_DIMS = {
    "openai/text-embedding-3-small": 1536,
    "openai/text-embedding-3-large": 3072,
    "openai/text-embedding-ada-002": 1536,
}


class SqliteEncryptionSettings(StrictSettings):
    mode: Literal["none", "sqlcipher"] = "none"
    key_env: str | None = None

    @model_validator(mode="after")
    def _key_environment_is_named(self) -> SqliteEncryptionSettings:
        if self.mode == "sqlcipher" and not self.key_env:
            raise ValueError("sqlite settings.encryption.key_env is required for sqlcipher")
        if (
            self.key_env is not None
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.key_env) is None
        ):
            raise ValueError(
                "sqlite settings.encryption.key_env must be an environment variable name"
            )
        return self


class SqliteSettings(StrictSettings):
    path: str = "./memspine.db"
    busy_timeout_s: float = Field(gt=0)
    encryption: SqliteEncryptionSettings = Field(default_factory=SqliteEncryptionSettings)


class PostgresSettings(StrictSettings):
    url: str = Field(min_length=1)
    data_dir: str = Field(min_length=1)
    connect_timeout_s: float = Field(gt=0)
    statement_timeout_s: float = Field(gt=0)


class LanceDBSettings(StrictSettings):
    quantization: Literal["auto", "none", "int8", "binary"] = "auto"
    namespace_index: bool = False
    isolation: Literal["shared", "per_namespace"] = "shared"


class PgvectorSettings(StrictSettings):
    """Native PostgreSQL vector index settings; the dimension comes from the embedder."""

    index: Literal["hnsw", "none"] = "hnsw"
    hnsw_m: int = Field(default=16, ge=2, le=100)
    hnsw_ef_construction: int = Field(default=64, ge=4, le=1000)
    hnsw_ef_search: int = Field(default=40, ge=1, le=1000)
    namespace_index: bool = True
    provision_projection: bool = False
    create_extension: bool = False


class LadybugSettings(EmptySettings):
    pass


class SqlAdjacencySettings(EmptySettings):
    pass


class MemoryCacheSettings(StrictSettings):
    max_entries: int = Field(default=constants.MEMORY_KV_MAX_ENTRIES, ge=1)


class DiskCacheSettings(StrictSettings):
    path: str = "./memspine.cache"
    namespace: str = Field(default="memspine", min_length=1)
    default_ttl_seconds: float | None = Field(default=None, gt=0)


class RemoteCacheSettings(StrictSettings):
    url: str = Field(min_length=1)
    namespace: str = Field(default="memspine", min_length=1)
    default_ttl_seconds: float | None = Field(default=None, gt=0)
    connect_timeout_s: float = Field(gt=0)
    request_timeout_s: float = Field(gt=0)


class TantivySettings(StrictSettings):
    analyzer: Literal["default", "english"] = "default"
    date_tokens: bool = False


class FastembedEmbeddingSettings(StrictSettings):
    model: str = "BAAI/bge-small-en-v1.5"
    query_instruction: str | None = None
    dim: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _dimension_matches_known_model(self) -> FastembedEmbeddingSettings:
        known_dim = FASTEMBED_MODEL_DIMS.get(self.model)
        if self.dim is None:
            if known_dim is None:
                raise ValueError(
                    "settings.dim is required for fastembed models without known dimensions"
                )
            object.__setattr__(self, "dim", known_dim)
        elif known_dim is not None and self.dim != known_dim:
            raise ValueError("settings.dim conflicts with the known fastembed model dimension")
        return self


class HashEmbeddingSettings(StrictSettings):
    dim: int = Field(default=constants.HASH_EMBEDDING_DIM, ge=1)


class StaticEmbeddingSettings(StrictSettings):
    model: str = "minishlab/potion-base-8M"
    dim: int | None = Field(default=None, ge=1)
    #: RM-38 no-download policy: resolve the artifact locally or fail visibly.
    local_files_only: bool = False

    @model_validator(mode="after")
    def _dimension_matches_known_model(self) -> StaticEmbeddingSettings:
        known_dim = STATIC_MODEL_DIMS.get(self.model)
        if self.dim is None:
            if known_dim is None:
                raise ValueError(
                    "settings.dim is required for static models without known dimensions"
                )
            object.__setattr__(self, "dim", known_dim)
        elif known_dim is not None and self.dim != known_dim:
            raise ValueError("settings.dim conflicts with the known static model dimension")
        return self


class LiteLLMEmbeddingSettings(StrictSettings):
    model: str = "openai/text-embedding-3-small"
    dim: int = Field(default=1536, ge=1)
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None
    request_dimensions: bool = False
    query_input_type: str | None = None
    document_input_type: str | None = None
    request_timeout_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _dimension_matches_known_model(self) -> LiteLLMEmbeddingSettings:
        known_dim = LITELLM_MODEL_DIMS.get(self.model)
        if "dim" not in self.model_fields_set:
            if known_dim is None:
                raise ValueError(
                    "settings.dim is required for LiteLLM models without known dimensions"
                )
            object.__setattr__(self, "dim", known_dim)
        elif known_dim is not None and self.dim != known_dim and not self.request_dimensions:
            raise ValueError(
                "settings.dim conflicts with the known LiteLLM model dimension unless request_dimensions is enabled"
            )
        return self


class SentenceTransformersEmbeddingSettings(StrictSettings):
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    dim: int | None = Field(default=None, ge=1)
    query_instruction: str | None = None
    device: str | None = None
    dtype: str | None = None
    query_prompt_name: str | None = None
    document_prompt_name: str | None = None
    trust_remote_code: bool = False

    @model_validator(mode="after")
    def _dimension_matches_known_model(self) -> SentenceTransformersEmbeddingSettings:
        known_dim = SENTENCE_TRANSFORMERS_MODEL_DIMS.get(self.model)
        if self.dim is None:
            if known_dim is None:
                raise ValueError(
                    "settings.dim is required for sentence-transformers models without known dimensions"
                )
            object.__setattr__(self, "dim", known_dim)
        return self


class FastembedRerankerSettings(StrictSettings):
    model: str = Field(default=constants.RERANK_FASTEMBED_MODEL, min_length=1)


class FlashrankRerankerSettings(StrictSettings):
    model: str | None = None


class LiteLLMRerankerSettings(StrictSettings):
    model: str = Field(min_length=1)
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None
    request_timeout_s: float = Field(gt=0)


class Qwen3RerankerSettings(StrictSettings):
    model: str = Field(default=constants.RERANK_QWEN3_MODEL, min_length=1)
    instruction: str = Field(default=constants.RERANK_QWEN3_INSTRUCTION, min_length=1)
    batch_size: int = Field(default=8, ge=1)
    max_length: int = Field(default=8192, ge=1)
    device: str | None = None
    quant: Literal["4bit", "8bit"] | None = None


class JinaRerankerSettings(StrictSettings):
    model: str = Field(default=constants.RERANK_JINA_MODEL, min_length=1)
    device: str | None = None
    max_documents: int = Field(default=64, ge=1)
