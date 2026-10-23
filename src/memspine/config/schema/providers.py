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

ONNX_MODEL_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
}
STATIC_MODEL_DIMS = {"minishlab/potion-base-8M": 256}
SENTENCE_TRANSFORMERS_MODEL_DIMS = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "Qwen/Qwen3-Embedding-0.6B": 1024,
}
OPENAI_COMPAT_MODEL_DIMS = {
    "text-embedding-3-small": 1536,
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
    connect_timeout_s: float = Field(gt=0)
    statement_timeout_s: float = Field(gt=0)


#: ``launch`` keys that only mean something with ``mode: managed`` (ADR-083).
_MANAGED_ONLY = (
    "version",
    "port",
    "data_dir",
    "binary",
    "download",
    "cache_dir",
    "replica_set",
    "start_timeout_s",
    "cache_size_gb",
)


class MongoLaunchSettings(StrictSettings):
    """ADR-083 ``settings.launch`` of the ``mongodb`` providers: who runs the server.

    ``mode: external`` (default) connects to ``settings.url``. ``mode: managed`` makes memspine
    start and own a local ``mongod`` (a one-member replica set bound to 127.0.0.1):

    - ``version``: required exact pin (``9.0.4``), never "latest";
    - ``port``: required, never picked implicitly;
    - ``data_dir``: the dbpath (default ``<deployment.data_dir>/mongod/<port>``);
    - ``binary``: an existing ``mongod`` executable; without it ``download: true`` is required
      (consent to fetch MongoDB Community Server, SSPL, from fastdl.mongodb.org, verified
      against the published SHA-256);
    - ``cache_dir``: where a downloaded ``mongod`` is kept (default: the user cache directory,
      ``<cache>/memspine/mongod/<version>/``);
    - ``replica_set`` (default ``rs0``), ``start_timeout_s`` (wait for PRIMARY),
      ``cache_size_gb`` (optional WiredTiger cache cap).
    """

    mode: Literal["external", "managed"] = "external"
    version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    port: int | None = Field(default=None, ge=1, le=65535)
    data_dir: str | None = Field(default=None, min_length=1)
    binary: str | None = Field(default=None, min_length=1)
    download: bool = False
    cache_dir: str | None = Field(default=None, min_length=1)
    replica_set: str = Field(
        default=constants.MONGOD_REPLICA_SET, min_length=1, pattern=r"^[A-Za-z0-9_\-]+$"
    )
    start_timeout_s: float = Field(default=constants.MONGOD_START_TIMEOUT_S, gt=0)
    cache_size_gb: float | None = Field(default=None, ge=0.25)

    @model_validator(mode="after")
    def _mode_rules(self) -> MongoLaunchSettings:
        if self.mode == "external":
            stray = [
                key
                for key in _MANAGED_ONLY
                if key in self.model_fields_set and (key != "download" or self.download)
            ]
            if stray:
                raise ValueError(
                    f"mongodb settings.launch.{stray[0]} applies only to launch.mode: managed "
                    "(launch.mode is external: memspine connects to settings.url)"
                )
            return self
        if self.version is None:
            raise ValueError(
                "mongodb settings.launch.version is required with launch.mode: managed "
                "(an exact pin such as 9.0.4; never latest)"
            )
        if self.port is None:
            raise ValueError(
                "mongodb settings.launch.port is required with launch.mode: managed (memspine "
                "never picks a port implicitly)"
            )
        if self.binary is None and not self.download:
            raise ValueError(
                "mongodb settings.launch.mode: managed needs settings.launch.binary (a path to "
                "an existing mongod) or settings.launch.download: true (consent to download "
                f"MongoDB Community Server {self.version}, SSPL licensed, from "
                "fastdl.mongodb.org)"
            )
        return self

    @property
    def managed(self) -> bool:
        return self.mode == "managed"

    def managed_url(self) -> str:
        """The loopback URL of the managed server (ADR-083 section 4)."""
        return (
            f"mongodb://127.0.0.1:{self.port}/?directConnection=true&replicaSet={self.replica_set}"
        )


class MongoRecordsSettings(StrictSettings):
    """``storage.records`` provider ``mongodb`` (RM-112, ``[mongodb]``): a replica set or a
    sharded cluster (multi-document transactions; a standalone server is refused at start).

    ``url`` is required with ``launch.mode: external`` (the default) and must be unset with
    ``launch.mode: managed``, where it is derived from ``launch.port`` (ADR-083)."""

    url: str | None = Field(
        default=None, min_length=1, description="mongodb:// or mongodb+srv:// URL"
    )
    database: str = Field(default="memspine", min_length=1)
    collection_prefix: str = ""
    connect_timeout_s: float = Field(default=10.0, gt=0)
    server_selection_timeout_s: float = Field(default=10.0, gt=0)
    operation_timeout_s: float | None = Field(default=30.0, gt=0)
    launch: MongoLaunchSettings = Field(default_factory=MongoLaunchSettings)

    @model_validator(mode="after")
    def _url_or_launch(self) -> MongoRecordsSettings:
        if self.launch.managed and self.url is not None:
            raise ValueError(
                "mongodb settings.url must be unset with settings.launch.mode: managed (the "
                "URL is derived from settings.launch.port)"
            )
        if not self.launch.managed and self.url is None:
            raise ValueError(
                "mongodb settings.url is required (or set settings.launch.mode: managed to "
                "let memspine run a local mongod)"
            )
        return self

    @property
    def effective_url(self) -> str:
        """``url``, or the managed server's loopback URL."""
        if self.url is not None:
            return self.url
        return self.launch.managed_url()


class MongoDocumentsSettings(StrictSettings):
    """``storage.documents`` provider ``mongodb`` (RM-112): GridFS bucket ``bucket``.

    Unset connection fields come from the ``storage.records`` MongoDB settings, so both slots on
    one deployment share one connector; ``url`` is required when the records store is not
    MongoDB. ``launch`` (ADR-083) unset inherits the records slot's launcher with its URL; a
    managed ``launch`` here derives the URL from its port (the same server as a managed records
    slot when both name the same binary, port and data_dir: one ``mongod`` is shared)."""

    url: str | None = Field(default=None, min_length=1)
    database: str | None = Field(default=None, min_length=1)
    bucket: str = Field(default="documents", min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    max_bytes: int = Field(default=constants.DOCUMENT_MAX_BYTES, ge=1)
    connect_timeout_s: float | None = Field(default=None, gt=0)
    server_selection_timeout_s: float | None = Field(default=None, gt=0)
    operation_timeout_s: float | None = Field(default=None, gt=0)
    launch: MongoLaunchSettings | None = None

    @model_validator(mode="after")
    def _url_or_launch(self) -> MongoDocumentsSettings:
        if self.launch is not None and self.launch.managed and self.url is not None:
            raise ValueError(
                "mongodb documents settings.url must be unset with settings.launch.mode: "
                "managed (the URL is derived from settings.launch.port)"
            )
        if self.launch is not None and not self.launch.managed and self.url is None:
            raise ValueError(
                "mongodb documents settings.launch.mode: external needs settings.url (or "
                "remove settings.launch to inherit the storage.records connection)"
            )
        return self


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


class FilesystemDocumentsSettings(StrictSettings):
    """ADR-081 ``storage.documents`` filesystem provider. ``root`` unset: beside the records
    store (``<base>.documents``, under ``deployment.data_dir`` for a non-file records store)."""

    root: str | None = Field(default=None, min_length=1)
    max_bytes: int = Field(default=constants.DOCUMENT_MAX_BYTES, ge=1)
    fsync: bool = True


class TantivySettings(StrictSettings):
    analyzer: Literal["default", "english"] = "default"
    date_tokens: bool = False


class OnnxEmbeddingSettings(StrictSettings):
    model: str = "BAAI/bge-small-en-v1.5"
    query_instruction: str | None = None
    dim: int | None = Field(default=None, ge=1)
    local_files_only: bool = False
    threads: int = Field(default=0, ge=0)
    batch_size: int = Field(default=32, ge=1)
    providers: tuple[str, ...] = ("CPUExecutionProvider",)
    model_spec: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _dimension_matches_known_model(self) -> OnnxEmbeddingSettings:
        known_dim = ONNX_MODEL_DIMS.get(self.model)
        if self.model_spec is not None:
            supplied_dim = self.model_spec.get("dim")
            if type(supplied_dim) is not int or supplied_dim < 1:
                raise ValueError("ONNX model_spec.dim must be a positive integer")
            known_dim = supplied_dim
            if self.model_spec.get("pooling") == "score":
                raise ValueError("model.embedding requires an embedding model, not a score head")
        elif known_dim is None:
            raise ValueError("Unsupported ONNX model; configure explicit settings.model_spec")
        if self.dim is None:
            if known_dim is None:
                raise ValueError(
                    "settings.dim is required for onnx models without known dimensions"
                )
            object.__setattr__(self, "dim", known_dim)
        elif known_dim is not None and self.dim != known_dim:
            raise ValueError("settings.dim conflicts with the known onnx model dimension")
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


class OpenAICompatEmbeddingSettings(StrictSettings):
    model: str = "text-embedding-3-small"
    dim: int = Field(default=1536, ge=1)
    api_base: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    request_dimensions: bool = False
    query_input_type: str | None = None
    document_input_type: str | None = None
    request_timeout_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _dimension_matches_known_model(self) -> OpenAICompatEmbeddingSettings:
        known_dim = OPENAI_COMPAT_MODEL_DIMS.get(self.model)
        if "dim" not in self.model_fields_set:
            if known_dim is None:
                raise ValueError(
                    "settings.dim is required for OpenAICompat models without known dimensions"
                )
            object.__setattr__(self, "dim", known_dim)
        elif known_dim is not None and self.dim != known_dim and not self.request_dimensions:
            raise ValueError(
                "settings.dim conflicts with the known OpenAICompat model dimension unless request_dimensions is enabled"
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


class OnnxRerankerSettings(StrictSettings):
    model: str = Field(default=constants.RERANK_ONNX_MODEL, min_length=1)
    local_files_only: bool = False
    threads: int = Field(default=0, ge=0)
    batch_size: int = Field(default=32, ge=1)
    providers: tuple[str, ...] = ("CPUExecutionProvider",)
    model_spec: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _score_head(self) -> OnnxRerankerSettings:
        if self.model_spec is None and self.model != constants.RERANK_ONNX_MODEL:
            raise ValueError("Unsupported ONNX reranker; configure explicit settings.model_spec")
        if self.model_spec is not None and self.model_spec.get("pooling") != "score":
            raise ValueError("model.rerank requires an ONNX score head")
        return self


class FlashrankRerankerSettings(StrictSettings):
    model: str | None = None


class OpenAICompatRerankerSettings(StrictSettings):
    model: str = Field(min_length=1)
    api_base: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
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
