"""Config area: runtime scheduling (RT-02; P-20, P-21, W-25, G-19 A)."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from memspine.config.schema.base import ConfigModel
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
