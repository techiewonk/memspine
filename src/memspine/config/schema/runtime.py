"""Config area: runtime scheduling (RT-02; P-20, P-21, W-25, G-19 A)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from memspine.config import constants
from memspine.config.schema.base import ConfigModel
from memspine.exceptions import ConfigError
from memspine.providers.settings import ProjectionSwitchSettings, ProviderServicesSettings


class _StrictRuntimeModel(ConfigModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


ProviderServicesConfig = ProviderServicesSettings
ProjectionSwitchConfig = ProjectionSwitchSettings


class EmbeddingSwitchConfig(_StrictRuntimeModel):
    mode: Literal["controlled"] = "controlled"
    strategy: Literal["reembed"] = "reembed"


class ModelSwitchConfig(_StrictRuntimeModel):
    mode: Literal["controlled"] = "controlled"
    historical_derivation: Literal["forward_only"] = "forward_only"


class StorageSwitchConfig(_StrictRuntimeModel):
    mode: Literal["controlled"] = "controlled"
    strategy: Literal["verified_migration"] = "verified_migration"
    auto_delete_source: Literal[False] = False


class CacheSwitchConfig(_StrictRuntimeModel):
    mode: Literal["controlled"] = "controlled"
    strategy: Literal["cold_start"] = "cold_start"


class ServiceSwitchesConfig(_StrictRuntimeModel):
    projections: ProjectionSwitchConfig = Field(default_factory=ProjectionSwitchConfig)
    embeddings: EmbeddingSwitchConfig = Field(default_factory=EmbeddingSwitchConfig)
    models: ModelSwitchConfig = Field(default_factory=ModelSwitchConfig)
    storage: StorageSwitchConfig = Field(default_factory=StorageSwitchConfig)
    cache: CacheSwitchConfig = Field(default_factory=CacheSwitchConfig)


WarmupCategory = Literal["embedder", "reranker", "gliner", "lexical", "llm", "decider"]


class _SharedResourcesModel(ConfigModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SharedSqlPoolConfig(_SharedResourcesModel):
    """SQLAlchemy pool options for the async and shared sync engines (ADR-079 addendum).

    Postgres honours all four. File SQLite honours all four on its queue pools; a SQLCipher
    database (single static connection / no pool) and ``:memory:`` (private per Engine) use
    fixed pools and ignore them."""

    pool_size: int = Field(default=constants.SHARED_SQL_POOL_SIZE, ge=1)
    max_overflow: int = Field(default=constants.SHARED_SQL_MAX_OVERFLOW, ge=0)
    pool_recycle_s: int = Field(default=constants.SHARED_SQL_POOL_RECYCLE_S, ge=1)
    pool_pre_ping: bool = constants.SHARED_SQL_POOL_PRE_PING


class SharedHttpPoolConfig(_SharedResourcesModel):
    """Connection-pool options for HTTP/Bolt clients memspine constructs itself (ADR-079).

    Honoured by OpenSearch (pool size), Weaviate (session pool size) and Neo4j (driver pool
    size; ``keepalive_s`` = liveness-check idle time). LiteLLM and OpenAI-compatible model
    servers use the library's process-global client and ignore them."""

    max_connections: int = Field(default=constants.SHARED_HTTP_MAX_CONNECTIONS, ge=1)
    keepalive_s: float = Field(default=constants.SHARED_HTTP_KEEPALIVE_S, gt=0)


class SharedResourcesConfig(_SharedResourcesModel):
    """RM-78 (ADR-079): sharing, warm-up and retention of provider resources.

    - ``scope``: ``process`` shares keyed clients and weights with every Engine in the
      process; ``engine`` gives each Engine a private registry.
    - ``warmup``: ``lazy`` loads on first use; ``background`` (default) returns from
      ``start()`` and warms in an Engine-owned task; ``blocking`` warms before ``start()``
      returns and raises naming the category on failure or timeout.
    - ``keep_warm``: model weights stay loaded at zero holders until process exit (or until
      ``idle_ttl_s`` / ``memory_budget_mb`` / ``max_models_per_category`` evicts them);
      connections always close on their last release.
    - ``model_server``: reserved for RM-79; only ``null`` / ``"off"`` are accepted.
    """

    scope: Literal["process", "engine"] = constants.SHARED_RESOURCES_SCOPE
    warmup: Literal["lazy", "background", "blocking"] = constants.SHARED_WARMUP_MODE
    warmup_timeout_s: float = Field(default=constants.SHARED_WARMUP_TIMEOUT_S, gt=0)
    warmup_concurrency: int = Field(default=constants.SHARED_WARMUP_CONCURRENCY, ge=1)
    warmup_include: tuple[WarmupCategory, ...] = constants.SHARED_WARMUP_CATEGORIES
    warmup_exclude: tuple[WarmupCategory, ...] = ()
    keep_warm: bool = constants.SHARED_KEEP_WARM
    idle_ttl_s: float | None = Field(default=None, gt=0)
    memory_budget_mb: int | None = Field(default=None, ge=1)
    max_models_per_category: int | None = Field(default=None, ge=1)
    sql: SharedSqlPoolConfig = Field(default_factory=SharedSqlPoolConfig)
    http: SharedHttpPoolConfig = Field(default_factory=SharedHttpPoolConfig)
    model_server: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _exclude_subtracts_from_default_include(cls, value: Any) -> Any:
        """With no explicit ``warmup_include`` the default (all) minus ``warmup_exclude`` is
        stored, so the two lists are always disjoint and a dumped config re-validates."""
        if isinstance(value, Mapping) and "warmup_include" not in value:
            excluded = value.get("warmup_exclude")
            if isinstance(excluded, (list, tuple)) and excluded:
                return {
                    **value,
                    "warmup_include": [
                        c for c in constants.SHARED_WARMUP_CATEGORIES if c not in excluded
                    ],
                }
        return value

    @model_validator(mode="after")
    def _consistent(self) -> SharedResourcesConfig:
        if self.model_server not in (None, "off"):
            raise ConfigError(
                "runtime.shared_resources.model_server is reserved for the opt-in local model "
                "server (RM-79, not implemented); use null or 'off'"
            )
        for name in ("warmup_include", "warmup_exclude"):
            values: tuple[str, ...] = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"runtime.shared_resources.{name} has duplicate categories")
        overlap = sorted(set(self.warmup_include) & set(self.warmup_exclude))
        if overlap:
            raise ValueError(
                "runtime.shared_resources.warmup_include and warmup_exclude must be disjoint "
                f"(both name {', '.join(overlap)})"
            )
        if self.idle_ttl_s is not None and not self.keep_warm:
            raise ValueError("runtime.shared_resources.idle_ttl_s requires keep_warm: true")
        return self

    def warmup_categories(self) -> tuple[str, ...]:
        """The categories warmed by ``background`` / ``blocking`` (include minus exclude)."""
        return tuple(c for c in self.warmup_include if c not in self.warmup_exclude)


class RuntimeConfig(ConfigModel):
    """In-process scheduling of model work and derived work. Everything is OFF by default.

    - ``scheduler_v2`` (RT-02): turns on micro-batched encoder executors (document and query
      embedding), the backpressure policy on the durable outbox (needs ``workers.outbox``), the
      SLO ledger and the ``runtime.sched.*`` / ``runtime.backpressure.*`` tap events. Off
      (default): nothing changes.
    - ``model_concurrency`` (RT-02, P-20): the number of model calls (LLM, embedding, rerank) that
      may run at once across ALL pipelines in this process; 0 (default) = no gate, as before.
      Waiters are served foreground read, then foreground write, then background derive.
      ``background_reserve`` slots can never be taken by background derive.
    - Backpressure (W-25 B): over ``backlog_coalesce_*`` new derived work is still queued but
      rides the waiting job and is marked underived; over ``backlog_max_*`` it is rejected
      before it is accepted (a receipt, never a drop; the caller keeps it and retries).
      0 disables a cap. ``read_p95_budget_s`` / ``lag_p95_budget_s`` only colour the SLO report.
    """

    model_config = ConfigDict(extra="forbid")

    environment: Literal["development", "production"] = "development"
    deployment_id: str | None = Field(default=None, min_length=1)
    # No defaults: each deadline must be stated even when startup is unbounded.
    provider_services: ProviderServicesConfig
    service_switches: ServiceSwitchesConfig = Field(default_factory=ServiceSwitchesConfig)
    #: RM-78 (ADR-079): sharing, warm-up and retention of provider resources.
    shared_resources: SharedResourcesConfig = Field(default_factory=SharedResourcesConfig)

    scheduler_v2: bool = False
    model_concurrency: int = Field(default=0, ge=0)
    background_reserve: int = Field(default=1, ge=0)
    encoder_batch_max: int = Field(default=32, ge=1)
    encoder_batch_wait_ms: float = Field(default=5.0, ge=0)
    backlog_coalesce_items: int = Field(default=200, ge=0)
    backlog_coalesce_age_s: float = Field(default=60.0, ge=0)
    backlog_max_items: int = Field(default=2000, ge=0)
    backlog_max_age_s: float = Field(default=900.0, ge=0)
    backlog_max_total_items: int = Field(default=0, ge=0)
    backlog_retry_after_s: float = Field(default=1.0, ge=0)
    read_p95_budget_s: float | None = Field(default=None, gt=0)
    lag_p95_budget_s: float | None = Field(default=None, gt=0)
