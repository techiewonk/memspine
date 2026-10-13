"""Config area: storage (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memspine.config import constants
from memspine.exceptions import ConfigError
from memspine.store.events import EventLogMode


class EventLogConfig(BaseModel):
    """At-rest lifetime of the memory_events log (D-45)."""

    model_config = ConfigDict(extra="forbid")

    mode: EventLogMode = EventLogMode.FULL
    retention_days: int = Field(default=constants.EVENT_LOG_RETENTION_DAYS, ge=1)
    compress: bool = False


class EncryptionConfig(BaseModel):
    """Encryption at rest for the SQLite event log and read model (#52, ADR-035).

    ``none`` (default) leaves the database file plain. ``sqlcipher`` opens every
    connection through SQLCipher (``pip install memspine[encrypt]``; a missing
    driver fails with ``MissingServiceError``), keyed with the passphrase held in
    the environment variable named by ``key_env``. The key is read from that
    variable only, never from config, and never logged. Derived stores outside the
    SQLite file (LanceDB vectors, the Tantivy index, disk caches) are not
    encrypted by this option; ADR-035 lists them."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "sqlcipher"] = "none"
    key_env: str | None = None

    @model_validator(mode="after")
    def _key_env_named(self) -> EncryptionConfig:
        if self.mode == "sqlcipher" and not self.key_env:
            raise ConfigError(
                "storage.encryption.mode=sqlcipher requires storage.encryption.key_env "
                "(the NAME of the environment variable holding the key)"
            )
        return self


class StorageConfig(BaseModel):
    """Event-log + read-model storage (D-36, Phase 6). ``sqlite`` (default) uses
    ``path`` (a file, or ``:memory:``); ``postgres`` uses ``url`` (a DSN,
    secrets-resolved) and ``data_dir`` as the base directory for the derived
    file-backed projections (LanceDB vectors, Tantivy lexical) that live outside
    the SQL database. Both backends share one dialect-neutral schema (ADR-025)."""

    model_config = ConfigDict(extra="forbid")

    backend: str = "sqlite"  # sqlite | postgres
    path: str = "./memspine.db"  # sqlite db file, or ":memory:"
    url: str | None = None  # postgres DSN, required when backend=postgres
    data_dir: str | None = None  # base dir for derived vector/lexical files (postgres)
    #: #52: SQLCipher encryption at rest (sqlite backend only); off by default.
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)


class VectorConfig(BaseModel):
    """Vector store selection (D-09, amended by ADR-021). ``lance`` (LanceDB) is
    the sole vector store and now a core dependency — the zero-dep SQLite
    brute-force store was removed, so there is no ``auto``/``sqlite`` fallback.
    ``weaviate`` (and future remote stores) keep the config seam open but are
    not built yet; selecting one raises ``ConfigError``. An ``:memory:`` event
    log points LanceDB at a per-engine scratch dir removed on ``stop()`` so the
    projection never outlives its log (D0.1).

    ``quantization`` drives the E4 two-stage rescore (ADR-020), realized by
    LanceDB's native compressed ANN index (IVF_HNSW_SQ / IVF_PQ): ``auto``
    (default) reads the embedder manifest — the default embedders declare none,
    so the exact float32 path is unchanged; ``none`` forces it off;
    ``int8``/``binary`` force that scheme even for an embedder that does not
    declare it (a deployer opting a known-tolerant model in). Matryoshka
    truncation is manifest-only (the model must be trained for it).
    """

    model_config = ConfigDict(extra="forbid")

    backend: str = "lance"  # lance (sole store); weaviate reserved (ADR-021)
    quantization: str = "auto"  # auto | none | int8 | binary (E4/ADR-020)
    #: I1 (isolation review 2026-10-08): keep a BITMAP scalar index on the vector
    #: table's ``namespace`` column (rebuilt every ``NAMESPACE_INDEX_EVERY`` writes once
    #: the table has ``NAMESPACE_INDEX_MIN_ROWS`` rows), so the per-user prefilter
    #: reads one user's rows. Results are unchanged. Off: the column is scanned.
    namespace_index: bool = False
    #: I4 (isolation review 2026-10-08): ``shared`` (unchanged: one table per embedder,
    #: namespace prefilter) or ``per_namespace`` (one table per namespace: a search
    #: touches one user's rows only, and ``erase_namespace`` drops the user's table).
    #: Switching rebuilds the vector projection from the event log.
    isolation: Literal["shared", "per_namespace"] = "shared"


class GraphConfig(BaseModel):
    """Graph store selection (D-26). ``sqlite_adjacency`` is the zero-dep
    default and fallback; ``ladybug`` (the maintained Kùzu fork, ``[graph]``) is
    the graph engine for graph features (ADR-034); ``kuzu`` is a deprecated alias
    for one release (Kùzu was archived on 2025-10-10; ``DeprecationWarning``).
    The store is only constructed when associative memory
    is enabled or this block is set explicitly — ``profile="simple"`` never
    touches it."""

    model_config = ConfigDict(extra="forbid")

    #: GR-1 (ADR-064): ``auto`` (default) = LadybugDB when the ``ladybug`` package is
    #: installed (``[graph]``), else ``sqlite_adjacency``. Explicit values pin one.
    provider: str = "auto"  # auto | sqlite_adjacency | ladybug | kuzu (deprecated) | neo4j
    #: GR-3 (graph engine plan 2026-10-08): embed entity-node names (GP-2 entity nodes)
    #: into the graph store's entity index: LadybugDB keeps them in a FLOAT[] column with
    #: its native HNSW vector index and FTS index; sqlite_adjacency in node properties.
    entity_embeddings: bool = False


class CacheConfig(BaseModel):
    """KV cache selection (D-09, ADR-022 amendment). One cache is built and shared
    by the embedding (E3) and extraction (E3) caches — the ``emb:``/``ext:``
    producer key prefixes already keep them from colliding in a shared store.

    - ``memory`` (default): in-process :class:`MemoryKV`, zero-dep core (slim-core
      D-03 keeps this hand-rolled — cashews never imports into core).
    - ``disk`` (``[cache]``): persistent on-disk cache (cashews → diskcache) at
      ``path`` (a directory); survives restarts.
    - ``redis`` / ``valkey`` (``[cache]``): shared cross-process cache at ``url``
      (cashews → redis-py; valkey is redis-wire-compatible), native TTL.

    ``namespace`` prefixes every key so multiple memspine instances can share
    one disk dir / redis server safely. ``default_ttl_seconds`` (``None`` = no
    expiry) applies when a caller does not pass an explicit TTL. ``url`` is
    secrets-resolved by the config loader (Phase 3)."""

    model_config = ConfigDict(extra="forbid")

    backend: str = "memory"  # memory | disk | redis | valkey (disk/redis/valkey via cashews)
    path: str = "./memspine.cache"  # disk cache directory
    url: str = "redis://localhost:6379/0"  # redis/valkey DSN
    namespace: str = "memspine"
    default_ttl_seconds: float | None = None
    max_entries: int = constants.MEMORY_KV_MAX_ENTRIES  # memory backend cap
