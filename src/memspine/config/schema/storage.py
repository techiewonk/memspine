"""Config area: storage (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from memspine.config import constants
from memspine.config.schema.base import ConfigModel
from memspine.exceptions import ConfigError
from memspine.store.events import EventLogMode


class EventLogConfig(ConfigModel):
    """At-rest lifetime of the memory_events log (D-45)."""

    model_config = ConfigDict(extra="forbid")

    mode: EventLogMode = EventLogMode.FULL
    retention_days: int = Field(default=constants.EVENT_LOG_RETENTION_DAYS, ge=1)
    compress: bool = False


class EncryptionConfig(ConfigModel):
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


#: ADR-081 / RM-111: removed keys and their new location. There are no aliases (ADR-068);
#: an old key is rejected with an error naming where it moved.
MOVED_KEYS: dict[str, str] = {
    "storage.profile": "removed with no replacement key (Alembic always runs for durable "
    "SQL; start from the 'embedded' or 'server' config template)",
    "storage.provider": "storage.records.provider",
    "storage.settings": "storage.records.settings (settings.data_dir moved to deployment.data_dir)",
    "vector": "storage.vector",
    "cache": "storage.cache",
    "graph.provider": "storage.graph.provider",
    "graph.settings": "storage.graph.settings",
    "graph.mirror": "storage.graph_mirror (null = off)",
    "read.lexical_provider": "storage.lexical.provider",
    "read.lexical_settings": "storage.lexical.settings",
    "runtime.deployment_id": "deployment.id",
}


def reject_moved_keys(value: Any, area: str, keys: tuple[str, ...]) -> None:
    """Raise a ``ConfigError`` naming the new location of a removed key (ADR-081)."""
    if not isinstance(value, Mapping):
        return
    for key in keys:
        if key in value:
            dotted = f"{area}.{key}" if area else key
            target = MOVED_KEYS[dotted]
            hint = target if target.startswith("removed") else f"use {target}"
            raise ConfigError(f"config key {dotted!r} was removed by ADR-081; {hint}")


class SlotConfig(ConfigModel):
    """One storage slot: a provider name plus that provider's native settings (ADR-081).

    Settings belong to the selected descriptor, which validates them strictly."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    settings: dict[str, JsonValue] = Field(default_factory=dict)


class RecordsSlotConfig(SlotConfig):
    """``storage.records``: event log, records, lineage, offsets, outbox and job store."""

    provider: str = Field(default="sqlite", min_length=1)


class VectorSlotConfig(SlotConfig):
    """``storage.vector``: the vector projection (required)."""

    provider: str = Field(default="lancedb", min_length=1)


class LexicalSlotConfig(SlotConfig):
    """``storage.lexical``: the BM25 lexical projection (required; D-25)."""

    provider: str = Field(default="tantivy", min_length=1)


class GraphSlotConfig(SlotConfig):
    """``storage.graph``: the primary graph projection; ``auto`` selects a compatible
    installed provider (ladybug, else SQL adjacency)."""

    provider: str = Field(default="auto", min_length=1)

    @model_validator(mode="after")
    def _auto_settings_are_empty(self) -> GraphSlotConfig:
        if self.provider == "auto" and self.settings:
            raise ConfigError(
                "storage.graph.settings cannot be set when storage.graph.provider='auto'; "
                "choose a provider explicitly"
            )
        return self


class CacheSlotConfig(SlotConfig):
    """``storage.cache``: the KV cache (E3)."""

    provider: str = Field(default="memory", min_length=1)


class StorageConfig(ConfigModel):
    """ADR-081: one slot per data category. Each slot is ``{provider, settings}``.

    ``vector`` and ``lexical`` are required (never null). ``graph_mirror`` is optional
    (null = off) and needs a provider with the ``mirror`` capability. ``documents`` is
    reserved for non-structured payloads; no built-in provider exists yet."""

    model_config = ConfigDict(extra="forbid")

    records: RecordsSlotConfig = Field(default_factory=RecordsSlotConfig)
    vector: VectorSlotConfig = Field(default_factory=VectorSlotConfig)
    lexical: LexicalSlotConfig = Field(default_factory=LexicalSlotConfig)
    graph: GraphSlotConfig = Field(default_factory=GraphSlotConfig)
    graph_mirror: SlotConfig | None = None
    cache: CacheSlotConfig = Field(default_factory=CacheSlotConfig)
    documents: SlotConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_moved_and_null(cls, value: Any) -> Any:
        return cls.reject_moved_and_null(value)

    @staticmethod
    def reject_moved_and_null(value: Any) -> Any:
        """Reject removed keys, null required slots and a non-null documents slot."""
        reject_moved_keys(value, "storage", ("profile", "provider", "settings"))
        if isinstance(value, Mapping):
            for slot in ("records", "vector", "lexical", "graph", "cache"):
                if slot in value and value[slot] is None:
                    raise ConfigError(
                        f"storage.{slot} cannot be null; select a provider "
                        "(vector and lexical are required slots)"
                    )
            if value.get("documents") is not None:
                raise ConfigError(
                    "storage.documents is not available until the RM-111 documents work "
                    "package ships a provider; leave it null"
                )
        return value


class DeploymentConfig(ConfigModel):
    """ADR-081: where and how this deployment runs.

    - ``id``: stable deployment identity (was ``runtime.deployment_id``). Required by shared
      (cluster) providers and in production.
    - ``locality``: ``single_process`` (default) | ``multi_process`` | ``multi_node``.
      Declared now; the validation rules that use it are RM-111 WP6.
    - ``data_dir``: base directory for host-local slots (LanceDB, Tantivy, LadybugDB, disk
      cache) when the records store is not a file. Unset, the placement base is today's:
      the SQLite database path, so paths and projection fingerprints are unchanged."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1)
    locality: Literal["single_process", "multi_process", "multi_node"] = "single_process"
    data_dir: str | None = Field(default=None, min_length=1)


class GraphConfig(ConfigModel):
    """Semantic graph policies. The graph provider is ``storage.graph`` (ADR-081)."""

    model_config = ConfigDict(extra="forbid")

    entity_embeddings: bool = False
    key_v2: bool = False
    edge_compiler_v2: bool = False

    @model_validator(mode="before")
    @classmethod
    def _reject_moved(cls, value: Any) -> Any:
        reject_moved_keys(value, "graph", ("provider", "settings", "mirror"))
        return value
