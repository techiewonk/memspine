"""Temporal TaskRunner with an embedded, application-journaled Resonate root.

Temporal owns durable macro orchestration. Each Activity rebuilds its process-local
pipeline context and uses an application-owned SQLite result journal so successful
pipeline outputs survive Activity retries. This is not exactly-once execution: a
crash after a pipeline side effect but before its result is journaled may repeat it.
Pipelines and their external writes must remain idempotent/reconcilable.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from memspine.clients._common.secrets import Secret
from memspine.clients.temporal import TemporalConnector, TemporalSpec
from memspine.derive.pipeline.stages.context import Pipeline, PipelineContext
from memspine.exceptions import ConfigError, MissingServiceError
from memspine.workers.step_journal import (
    CompletedStep,
    InputConflictError,
    JsonValue,
    StepBusyError,
    StepJournal,
)


class _WorkflowHandle(Protocol):
    async def result(self) -> dict[str, object]: ...


class _TemporalClient(Protocol):
    async def start_workflow(
        self, workflow: str, argument: Mapping[str, object], **kwargs: object
    ) -> _WorkflowHandle: ...


class _TemporalWorker(Protocol):
    async def run(self) -> None: ...

    async def shutdown(self) -> None: ...


if TYPE_CHECKING:
    from resonate.context import Context
    from temporalio.client import TLSConfig

__all__ = ["TemporalRunner", "fingerprint_input", "workflow_id_for_invocation"]


def workflow_id_for_invocation(deployment_id: str, invocation_id: str) -> str:
    """Return a stable, bounded Temporal ID without exposing deployment text."""
    deployment_digest = hashlib.sha256(deployment_id.encode()).hexdigest()[:16]
    return f"memspine-{deployment_digest}-{invocation_id}"


def fingerprint_input(value: object) -> str:
    """Fingerprint a caller's immutable business input without sending it to Temporal."""
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigError("input fingerprint source must be JSON-serializable") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


def _seconds(name: str, value: float, *, allow_zero: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (value == 0 and not allow_zero)
    ):
        comparator = "non-negative" if allow_zero else "positive"
        raise ConfigError(f"{name} must be finite and {comparator}")
    return float(value)


def _positive_int(name: str, value: int) -> int:
    if type(value) is not int or value < 1:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _configuration_fingerprint(config: object) -> str:
    dump = getattr(config, "model_dump", None)
    if not callable(dump):
        raise ConfigError("pipeline context config must support model_dump(mode='json')")
    try:
        snapshot = dump(mode="json")
        if isinstance(snapshot, dict):
            storage = snapshot.get("storage")
            if isinstance(storage, dict) and storage.get("provider") == "sqlite":
                settings = storage.get("settings")
                if isinstance(settings, dict) and isinstance(settings.get("path"), str):
                    path = settings["path"]
                    if path not in {":memory:", "file::memory:"} and not path.startswith("file:"):
                        settings["path"] = str(Path(path).expanduser().resolve())
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigError("pipeline context config is not canonically serializable") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


def _validate_temporal_storage(config: object) -> None:
    storage = getattr(config, "storage", None)
    settings = getattr(storage, "settings", None)
    provider = getattr(storage, "provider", None)
    path = settings.get("path") if isinstance(settings, Mapping) else None
    if (
        provider == "sqlite"
        and isinstance(path, str)
        and (path in {":memory:", "file::memory:"} or path.startswith("file:"))
    ):
        raise ConfigError("Temporal workers require filesystem-backed SQLite storage")


class TemporalRunner:
    """Durable :class:`TaskRunner` backed by a Temporal worker.

    Temporal and Resonate are optional dependencies and are imported only when
    ``start`` is called. The Activity payload contains identifiers and a config
    digest only; it never serializes a ``PipelineContext``, config, storage handle,
    callable, or credential.
    """

    def __init__(
        self,
        *,
        address: str,
        namespace: str,
        task_queue: str,
        deployment_id: str,
        journal_path: str | Path,
        context_factory: Callable[[], PipelineContext],
        rpc_timeout_s: float = 30,
        activity_timeout_s: float = 300,
        workflow_timeout_s: float = 3600,
        max_concurrent_activities: int = 4,
        retry_max_attempts: int = 3,
        resonate_task_timeout_s: float = 270,
        step_timeout_s: float = 240,
        step_lease_s: float = 360,
        journal_busy_timeout_s: float = 5,
        resonate_max_concurrent_tasks: int = 4,
        max_result_bytes: int = 65_536,
        tls: bool | TLSConfig | None = None,
        api_key: str | None = None,
    ) -> None:
        for label, value in (
            ("address", address),
            ("namespace", namespace),
            ("task_queue", task_queue),
            ("deployment_id", deployment_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"Temporal {label} must be a non-empty string")
        if not callable(context_factory):
            raise ConfigError("Temporal context_factory must be callable")
        self.address = address
        self.namespace = namespace
        self.task_queue = task_queue
        self.deployment_id = deployment_id
        self.journal_path = Path(journal_path).expanduser().resolve()
        if str(journal_path) in {":memory:"} or str(journal_path).startswith("file:"):
            raise ConfigError("Temporal Resonate journal_path must be filesystem-backed")
        self.context_factory = context_factory
        self.rpc_timeout_s = _seconds("rpc_timeout_s", rpc_timeout_s)
        self.activity_timeout_s = _seconds("activity_timeout_s", activity_timeout_s)
        self.workflow_timeout_s = _seconds("workflow_timeout_s", workflow_timeout_s)
        self.resonate_task_timeout_s = _seconds("resonate_task_timeout_s", resonate_task_timeout_s)
        self.step_timeout_s = _seconds("step_timeout_s", step_timeout_s)
        self.step_lease_s = _seconds("step_lease_s", step_lease_s)
        self.journal_busy_timeout_s = _seconds(
            "journal_busy_timeout_s", journal_busy_timeout_s, allow_zero=True
        )
        self.max_concurrent_activities = _positive_int(
            "max_concurrent_activities", max_concurrent_activities
        )
        self.retry_max_attempts = _positive_int("retry_max_attempts", retry_max_attempts)
        self.resonate_max_concurrent_tasks = _positive_int(
            "resonate_max_concurrent_tasks", resonate_max_concurrent_tasks
        )
        self.max_result_bytes = _positive_int("max_result_bytes", max_result_bytes)
        if self.step_timeout_s > self.resonate_task_timeout_s:
            raise ConfigError("step_timeout_s must not exceed resonate_task_timeout_s")
        if self.resonate_task_timeout_s >= self.activity_timeout_s:
            raise ConfigError("resonate_task_timeout_s must be less than activity_timeout_s")
        if self.step_lease_s <= self.activity_timeout_s:
            raise ConfigError("step_lease_s must exceed activity_timeout_s")
        activity_schedule_budget = (
            self.activity_timeout_s * self.retry_max_attempts
            + self.step_lease_s * (self.retry_max_attempts - 1)
        )
        if activity_schedule_budget >= self.workflow_timeout_s:
            raise ConfigError("workflow_timeout_s must exceed all configured Activity attempts")
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise ConfigError("Temporal api_key must be non-empty when provided")
        self.tls = tls
        self._api_key = api_key
        self._pipelines: dict[str, Pipeline] = {}
        self._client: object | None = None
        self._worker: object | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self._journal: StepJournal | None = None
        #: One embedded Resonate runtime per runner start (ADR-079), reused by every
        #: activity; ``_resonate_calls`` maps a per-attempt promise id to its pipeline call.
        self._resonate: Any = None
        self._resonate_calls: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(address={self.address!r}, namespace={self.namespace!r}, "
            f"task_queue={self.task_queue!r}, deployment_id={self.deployment_id!r}, "
            "api_key=<redacted>)"
        )

    def register(self, name: str, pipeline: Pipeline) -> None:
        if self._started or self._closing:
            raise RuntimeError("pipelines must be registered before TemporalRunner.start()")
        if not isinstance(name, str) or not name.strip() or name in self._pipelines:
            raise ConfigError("pipeline names must be non-empty and unique")
        if not callable(pipeline):
            raise ConfigError("pipeline must be callable")
        self._pipelines[name] = pipeline

    async def start(self) -> None:
        if self._started:
            return
        if not self._pipelines:
            raise ConfigError("TemporalRunner needs at least one registered pipeline")
        try:
            from resonate.resonate import Resonate
            from resonate.retry import Never
            from temporalio import activity
            from temporalio.worker import Worker

            from memspine.workers.temporal_workflow import PipelineWorkflow
        except ImportError as exc:
            raise MissingServiceError("Temporal worker", "temporal") from exc

        self._journal = await asyncio.to_thread(
            StepJournal,
            self.journal_path,
            lease_seconds=self.step_lease_s,
            busy_timeout_seconds=self.journal_busy_timeout_s,
        )
        # RM-113: the cluster connection is the clients/temporal connector's
        connector = TemporalConnector(
            TemporalSpec(
                self.address,
                self.namespace,
                tls=self.tls,
                api_key=Secret(self._api_key) if self._api_key else None,
                connect_timeout_s=self.rpc_timeout_s,
            )
        )
        await connector.open()
        client = connector.handle
        self._client = client

        calls = self._resonate_calls

        async def run_pipeline(invocation: dict[str, Any]) -> dict[str, Any]:
            from temporalio.exceptions import ApplicationError

            if not isinstance(invocation, Mapping):
                raise ApplicationError(
                    "Invalid pipeline invocation", type="InvalidInvocation", non_retryable=True
                )
            deployment_id = invocation.get("deployment_id")
            invocation_id = invocation.get("invocation_id")
            operation_id = invocation.get("operation_id")
            pipeline_name = invocation.get("pipeline_name")
            expected_config = invocation.get("config_fingerprint")
            input_fingerprint = invocation.get("input_fingerprint")
            if (
                deployment_id != self.deployment_id
                or not isinstance(invocation_id, str)
                or not isinstance(operation_id, str)
                or not operation_id.strip()
                or len(operation_id) > 512
                or not isinstance(pipeline_name, str)
                or not isinstance(expected_config, str)
                or not isinstance(input_fingerprint, str)
                or len(input_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in input_fingerprint)
                or not isinstance(invocation.get("activity_timeout_s"), (int, float))
                or isinstance(invocation.get("activity_timeout_s"), bool)
                or not isinstance(invocation.get("retry_max_attempts"), int)
                or isinstance(invocation.get("retry_max_attempts"), bool)
                or not isinstance(invocation.get("step_lease_s"), (int, float))
                or isinstance(invocation.get("step_lease_s"), bool)
                or not isinstance(invocation.get("max_result_bytes"), int)
                or isinstance(invocation.get("max_result_bytes"), bool)
            ):
                raise ApplicationError(
                    "Pipeline invocation identity does not match this worker",
                    type="InvocationMismatch",
                    non_retryable=True,
                )
            pipeline = self._pipelines.get(pipeline_name)
            if pipeline is None:
                raise ApplicationError(
                    "Worker does not have the requested pipeline",
                    type="UnknownPipeline",
                    non_retryable=True,
                )
            context = self.context_factory()
            if inspect.isawaitable(context):
                context = await context
            if not isinstance(context, PipelineContext):
                raise ConfigError("context_factory must return PipelineContext")
            _validate_temporal_storage(context.config)
            actual_config = _configuration_fingerprint(context.config)
            if actual_config != expected_config:
                raise ApplicationError(
                    "Worker configuration does not match invocation",
                    type="ConfigurationMismatch",
                    non_retryable=True,
                )
            if self._journal is None:
                raise RuntimeError("Temporal journal is unavailable")
            journal_operation_id = hashlib.sha256(
                f"{self.deployment_id}\0{operation_id}".encode()
            ).hexdigest()
            binding = hashlib.sha256(
                f"{self.deployment_id}\0{pipeline_name}\0{actual_config}\0{input_fingerprint}".encode()
            ).hexdigest()
            activity_info = activity.info()
            owner_id = (
                f"{activity_info.workflow_run_id}:{activity_info.activity_id}:"
                f"{activity_info.attempt}:{uuid.uuid4().hex}"
            )[:512]
            try:
                claim = await asyncio.to_thread(
                    self._journal.claim_step,
                    journal_operation_id,
                    f"pipeline/{pipeline_name}",
                    binding,
                    owner_id,
                )
            except StepBusyError as exc:
                raise ApplicationError(
                    "A prior pipeline attempt still holds its journal lease",
                    type="PipelineAttemptBusy",
                    next_retry_delay=timedelta(seconds=self.step_lease_s),
                ) from exc
            except InputConflictError as exc:
                raise ApplicationError(
                    "Logical operation was reused with different input",
                    type="InputConflict",
                    non_retryable=True,
                ) from exc
            if isinstance(claim, CompletedStep):
                if not isinstance(claim.output, dict):
                    raise ConfigError("completed pipeline journal value is not an object")
                completed_bytes = json.dumps(
                    claim.output, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
                if len(completed_bytes) > self.max_result_bytes:
                    raise ConfigError("completed pipeline result exceeds configured result limit")
                return cast(dict[str, object], claim.output)

            async def execute() -> dict[str, Any]:
                async with asyncio.timeout(self.step_timeout_s):
                    return await pipeline(context)

            # A fresh promise id per attempt: the runtime outlives activities, and a retried
            # attempt must execute again rather than read the earlier attempt's promise.
            call_id = hashlib.sha256(f"{invocation_id}\0{owner_id}".encode()).hexdigest()
            calls[call_id] = execute
            try:
                handle = runtime.options(
                    timeout=timedelta(seconds=self.resonate_task_timeout_s)
                ).run(call_id, "memspine.pipeline.activity", call_id)
                result = await handle.result()
                if not isinstance(result, dict):
                    raise ConfigError("pipeline result must be a dictionary")
                # Validate the JSON boundary before committing an Activity result.
                encoded_result = json.dumps(
                    result, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
                if len(encoded_result) > self.max_result_bytes:
                    raise ConfigError("pipeline result exceeds configured result limit")
                await asyncio.to_thread(self._journal.complete, claim, cast(JsonValue, result))
                return cast(dict[str, object], result)
            except BaseException:
                # Preserve the lease after uncertain execution/cancellation. A later
                # attempt may proceed only after the lease expires; it cannot race a
                # still-running prior attempt and commit stale output.
                raise
            finally:
                calls.pop(call_id, None)

        activity_function = activity.defn(name="memspine.run_pipeline")(run_pipeline)
        try:
            runtime = Resonate(
                env={},
                autostart=False,
                retry_policy=Never(),
                max_concurrent_tasks=self.resonate_max_concurrent_tasks,
            )

            async def coordinator(resonate_context: Context, call_id: str) -> dict[str, Any]:
                call = calls[call_id]

                async def execute(_: Context) -> dict[str, Any]:
                    return await call()

                return await cast(asyncio.Future[dict[str, Any]], resonate_context.run(execute))

            runtime.register(coordinator, name="memspine.pipeline.activity")
            self._resonate = runtime
            runtime.start()

            worker = Worker(
                client,
                task_queue=self.task_queue,
                activities=[activity_function],
                workflows=[PipelineWorkflow],
                max_concurrent_activities=self.max_concurrent_activities,
            )
            self._worker = worker
            self._worker_task = asyncio.create_task(worker.run(), name="memspine-temporal-worker")
            await asyncio.sleep(0)
            if self._worker_task.done():
                with suppress(Exception):
                    await self._worker_task
                raise RuntimeError("Temporal worker stopped during startup")
            self._started = True
        except BaseException:
            cleanup_task = asyncio.create_task(self.close())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup_task)
            except Exception:
                pass
            raise

    async def run(
        self,
        name: str,
        ctx: PipelineContext,
        *,
        operation_id: str | None = None,
        input_fingerprint: str | None = None,
    ) -> dict[str, object]:
        if not self._started or self._closing or self._client is None:
            raise RuntimeError("TemporalRunner is not started")
        if self._worker_task is None or self._worker_task.done():
            raise RuntimeError("Temporal worker is not running")
        if name not in self._pipelines:
            raise ConfigError(f"unknown Temporal pipeline {name!r}")
        fingerprint = _configuration_fingerprint(ctx.config)
        _validate_temporal_storage(ctx.config)
        invocation_id = uuid.uuid4().hex
        if operation_id is not None and input_fingerprint is None:
            raise ConfigError("input_fingerprint is required with an explicit operation_id")
        if operation_id is None and input_fingerprint is not None:
            raise ConfigError("input_fingerprint is only accepted with operation_id")
        logical_operation_id = operation_id or invocation_id
        if (
            not isinstance(logical_operation_id, str)
            or not logical_operation_id.strip()
            or len(logical_operation_id) > 512
        ):
            raise ConfigError("operation_id must be a non-empty string of at most 512 characters")
        if input_fingerprint is None:
            input_fingerprint = hashlib.sha256(
                f"{self.deployment_id}\0{name}\0{fingerprint}\0{invocation_id}".encode()
            ).hexdigest()
        elif (
            not isinstance(input_fingerprint, str)
            or len(input_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in input_fingerprint)
        ):
            raise ConfigError("input_fingerprint must be a lowercase SHA-256 hex digest")
        invocation = {
            "deployment_id": self.deployment_id,
            "invocation_id": invocation_id,
            "operation_id": hashlib.sha256(
                f"{self.deployment_id}\0{logical_operation_id}".encode()
            ).hexdigest(),
            "input_fingerprint": input_fingerprint,
            "pipeline_name": name,
            "config_fingerprint": fingerprint,
            "activity_timeout_s": self.activity_timeout_s,
            "retry_max_attempts": self.retry_max_attempts,
            "step_lease_s": self.step_lease_s,
            "max_result_bytes": self.max_result_bytes,
        }
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

        handle = await cast(_TemporalClient, self._client).start_workflow(
            "memspine.pipeline",
            invocation,
            id=workflow_id_for_invocation(self.deployment_id, invocation_id),
            task_queue=self.task_queue,
            execution_timeout=timedelta(seconds=self.workflow_timeout_s),
            rpc_timeout=timedelta(seconds=self.rpc_timeout_s),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        return await handle.result()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        worker = cast(_TemporalWorker | None, self._worker)
        worker_task = self._worker_task
        shutdown_task: asyncio.Task[None] | None = None
        try:
            if worker is not None:
                shutdown_task = asyncio.create_task(
                    worker.shutdown(), name="memspine-temporal-shutdown"
                )
                with suppress(Exception):
                    await asyncio.shield(shutdown_task)
            if worker_task is not None:
                with suppress(Exception):
                    await asyncio.shield(worker_task)
                    # The worker task already reported its own failure; do not
                    # expose SDK exception text that may contain endpoint data.
        except asyncio.CancelledError:
            if shutdown_task is not None:
                with suppress(Exception):
                    await asyncio.shield(shutdown_task)
            if worker_task is not None:
                with suppress(Exception):
                    await asyncio.shield(worker_task)
            raise
        finally:
            runtime, self._resonate = self._resonate, None
            self._resonate_calls.clear()
            if runtime is not None:
                # After the worker drained: no activity still uses the runtime.
                stop_task = asyncio.create_task(runtime.stop())
                with suppress(Exception):
                    await asyncio.shield(stop_task)
            self._worker = None
            self._worker_task = None
            self._client = None
            self._journal = None
            self._started = False
            self._closing = False
