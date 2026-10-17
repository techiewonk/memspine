"""Config area: storage (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

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


class StorageConfig(ConfigModel):
    """Event-log and read-model storage selection; native options are nested."""

    model_config = ConfigDict(extra="forbid")

    profile: Literal["embedded", "server"] = "embedded"
    provider: str = "sqlite"
    settings: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _resolve_provider(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        profile = data.get("profile", "embedded")
        if "provider" not in data:
            data["provider"] = "postgres" if profile == "server" else "sqlite"
        elif data["provider"] is None:
            raise ConfigError(
                "storage.provider cannot be null; omit it to resolve from storage.profile"
            )
        if profile == "server" and data["provider"] != "postgres":
            raise ConfigError(
                f"storage.profile=server needs storage.provider=postgres (got {data['provider']!r})"
            )
        return data


class VectorConfig(ConfigModel):
    """Vector store selection and cross-provider projection policy."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "lancedb"
    settings: dict[str, JsonValue] = Field(default_factory=dict)


class GraphMirrorConfig(ConfigModel):
    """Optional graph mirror with independent provider settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: str = "neo4j"
    settings: dict[str, JsonValue] = Field(default_factory=dict)


class GraphConfig(ConfigModel):
    """Primary graph selection plus semantic graph policies."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "auto"
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    mirror: GraphMirrorConfig = Field(default_factory=GraphMirrorConfig)
    entity_embeddings: bool = False
    key_v2: bool = False
    edge_compiler_v2: bool = False

    @model_validator(mode="after")
    def _auto_settings_are_empty(self) -> GraphConfig:
        if self.provider == "auto" and self.settings:
            raise ConfigError(
                "graph.settings cannot be set when graph.provider='auto'; choose a provider explicitly"
            )
        return self


class CacheConfig(ConfigModel):
    """KV cache selection; constructor options live in provider settings."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "memory"
    settings: dict[str, JsonValue] = Field(default_factory=dict)
