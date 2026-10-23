"""Config area: prompts and background workers (the model classes moved to ``model.py``, ADR-082)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, SecretStr, model_validator

from memspine.config.schema.base import ConfigModel


class PromptsConfig(ConfigModel):
    """User prompt customization (D-43): per-prompt overrides that ride the
    ordinary config layering. Keys under an override: body / system / format /
    version / output_model / token_budget (validated by the registry).

    ``partials`` (B1) supplies override fragments for the shared Jinja
    ``{% include %}`` partials (anti-injection block, output footer, format
    instructions): ``prompts.partials.<name>`` maps a partial name to its
    replacement text, consulted before the shipped ``_partials/`` directory.

    ``selection`` (B2) pins per-role default scenario selectors:
    ``prompts.selection.<role>`` is a map with optional ``memory_type`` /
    ``condition`` keys, merged into every ``select(role)`` query the caller
    doesn't override — so a deployment can force a scenario variant of a role's
    prompt without a code change."""

    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    partials: dict[str, str] = Field(default_factory=dict)
    selection: dict[str, dict[str, str]] = Field(default_factory=dict)
    #: W2-07 (decisions W-18, P-07, P-08, P-10; LIB-46): prompt layout v2. When on, the write
    #: prompts that carry a worked example or a per-call variable in the system text
    #: (``window_extract``, ``extract@session3``, ``extract@session4``, ``extract_edges``,
    #: ``extract_edges@session``) are served from ``defaults/v2/``: no worked example, a system
    #: text that is byte-identical for every call (its hash is ``PromptIdentity``), the
    #: coarse-date partial in that stable part, and the same-language partial in the user
    #: part for non-English input only. Default off: every prompt renders as before.
    layout_v2: bool = False
    #: The layout-v2 partials left switched on (screened one at a time, RV15):
    #: ``same_language`` and/or ``coarse_date``. Ignored while ``layout_v2`` is off.
    layout_partials: list[Literal["same_language", "coarse_date"]] = Field(
        default_factory=lambda: ["same_language", "coarse_date"]  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    )


class TemporalWorkerSettings(ConfigModel):
    """Remote Temporal connection and bounded workflow execution settings."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(min_length=1)
    address: str = Field(default="localhost:7233", min_length=1)
    namespace: str = Field(default="default", min_length=1)
    task_queue: str | None = Field(default=None, min_length=1)
    tls: bool = False
    api_key: SecretStr | None = None
    rpc_timeout_s: float = Field(default=30.0, gt=0, le=3600)
    activity_timeout_s: float = Field(default=300.0, gt=0, le=86400)
    workflow_timeout_s: float = Field(default=3600.0, gt=0, le=604800)
    max_concurrent_activities: int = Field(default=4, ge=1, le=1024)
    retry_max_attempts: int = Field(default=3, ge=1, le=100)
    max_result_bytes: int = Field(default=65536, ge=1, le=1048576)


class ResonateWorkerSettings(ConfigModel):
    """Embedded Resonate promise execution and SQLite step journal settings."""

    model_config = ConfigDict(extra="forbid")

    journal_path: str = Field(default="./memspine-resonate.sqlite", min_length=1)
    task_timeout_s: float = Field(default=270.0, gt=0, le=604800)
    step_timeout_s: float = Field(default=240.0, gt=0, le=86400)
    step_lease_s: float = Field(default=360.0, gt=0, le=86400)
    journal_busy_timeout_s: float = Field(default=5.0, gt=0, le=3600)
    max_concurrent_tasks: int = Field(default=4, ge=1, le=1024)

    @model_validator(mode="after")
    def _require_persistent_journal(self) -> ResonateWorkerSettings:
        if self.journal_path == ":memory:" or self.journal_path.startswith("file:"):
            raise ValueError("workers.resonate.journal_path must be a persistent filesystem path")
        return self


class WorkersConfig(ConfigModel):
    """Background runner selection and provider-owned runner settings."""

    model_config = ConfigDict(extra="forbid")

    runner: Literal["inline", "temporal", "taskiq"] = "inline"
    broker_url: str = "redis://localhost:6379/0"
    temporal: TemporalWorkerSettings | None = None
    resonate: ResonateWorkerSettings = Field(default_factory=ResonateWorkerSettings)
    #: D1: autonomous maintenance. When set to a positive number of seconds the
    #: engine starts a background loop that runs the full sleep cycle
    #: (consolidate → … → decay → prune) on that interval. None (default) keeps
    #: v0.1 behavior — the cycle runs only on an explicit ``Engine.sleep()``.
    sleep_interval_seconds: float | None = None
    #: F-04 (W-13, ER-06): the durable outbox. Off (default): window calls queue in memory and
    #: are lost on stop, exactly as before. On: every window job is a row in the ``outbox``
    #: tables first, an inline runner drains it after each write and at start (leftovers), and a
    #: stop persists what it could not finish. Model calls never run inside a namespace lock.
    outbox: bool = False
    #: RT-01 (G-19, W-13): "outbox = truth, taskiq = transport". ``inline`` (default): unchanged,
    #: the inline runner alone. ``taskiq``: with ``outbox`` on, every enqueued job id is also sent
    #: over a taskiq broker (the message carries only the id), and ``start`` re-sends every
    #: unfinished row. Without ``outbox`` it has no effect.
    transport: Literal["inline", "taskiq"] = "inline"
    #: RT-01: which broker carries the ids when ``transport="taskiq"``. ``memory``: taskiq's
    #: InMemoryBroker, drained in this process (library mode). ``redis``: a Redis stream on
    #: ``broker_url``; this process only enqueues and ``python -m memspine.api.worker`` drains.
    transport_broker: Literal["memory", "redis"] = "memory"

    @model_validator(mode="after")
    def _validate_temporal_deadlines(self) -> WorkersConfig:
        if self.runner != "temporal":
            return self
        temporal = self.temporal
        if temporal is None:
            raise ValueError(
                "workers.temporal with deployment_id is required for runner='temporal'"
            )
        resonate = self.resonate
        if resonate.step_lease_s <= temporal.activity_timeout_s + resonate.journal_busy_timeout_s:
            raise ValueError(
                "workers.resonate.step_lease_s must exceed temporal.activity_timeout_s "
                "plus journal_busy_timeout_s"
            )
        if resonate.task_timeout_s <= resonate.step_timeout_s:
            raise ValueError("workers.resonate.task_timeout_s must exceed step_timeout_s")
        if temporal.activity_timeout_s <= resonate.task_timeout_s:
            raise ValueError(
                "workers.temporal.activity_timeout_s must exceed resonate.task_timeout_s"
            )
        activity_budget = (
            temporal.activity_timeout_s * temporal.retry_max_attempts
            + resonate.step_lease_s * (temporal.retry_max_attempts - 1)
        )
        if temporal.workflow_timeout_s <= activity_budget:
            raise ValueError(
                "workers.temporal.workflow_timeout_s must exceed the configured Activity retry budget"
            )
        return self
