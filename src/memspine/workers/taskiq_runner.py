"""Taskiq runner seam (D-16, ``[taskiq]``): Redis/Valkey-Streams brokered work.

D-42 §3 (the MemOS ``SchedulerRedisQueue`` pattern, adopted):

- **per-scope stream keys** — one stream per ``(namespace-scope, task_label)``,
  built by :func:`stream_key`, so one tenant's backlog never starves another's;
- **priority labels** mirroring the pipeline set (:data:`PIPELINE_PRIORITIES`
  covers exactly ``workers.pipelines.PIPELINES``: check_watches / consolidate /
  reorganize / decay_sweep / compress / sleep_compute / event_log_prune);
- **consumer-group delivery + XAUTOCLAIM claim-recovery** — every run appends
  a durable work marker (XADD), claims it into the consumer group (XREADGROUP)
  and acknowledges on success (XACK); a crashed run leaves its marker pending,
  and :func:`claim_stale` (XAUTOCLAIM) hands it to the next consumer.

HONESTY NOTE (P7, same posture as ``dbos_runner``): this is the seam plus the
durable at-least-once work record — not yet a distributed worker fleet.
``PipelineContext`` holds live connections and callables, so the pipeline body
executes in-process; re-execution after claim-recovery is safe because
pipelines are idempotent by the D-17 contract. A separate taskiq worker fleet
consuming these streams (push delivery for watches/subscriptions, ADR-016) is
the deployment story. The import is guarded: without ``pip install
memspine[taskiq]`` construction raises :class:`MissingServiceError` naming the
extra (D-10) — unless a ready client (e.g. fakeredis in tests, D-41) is
injected. Broker outages degrade loudly to inline execution; they never take
the engine down.
"""

from __future__ import annotations

from typing import Any

from memspine.config import constants
from memspine.exceptions import MemspineError, MissingServiceError
from memspine.observability.logging import get_logger
from memspine.workers.pipelines import Pipeline, PipelineContext

__all__ = [
    "CONSUMER_GROUP",
    "PIPELINE_PRIORITIES",
    "STREAM_PREFIX",
    "TaskiqRunner",
    "claim_stale",
    "ensure_group",
    "priority_for",
    "stream_key",
]

_log = get_logger(__name__)

STREAM_PREFIX = "memspine"
CONSUMER_GROUP = "memspine-workers"

#: Priority labels, lower = more urgent (D-42 §3). Mirrors ``PIPELINES`` —
#: ``test_taskiq_runner`` asserts the two sets never drift. Watches fire first
#: (a due reminder beats housekeeping); pruning always goes last.
PIPELINE_PRIORITIES: dict[str, int] = {
    "check_watches": 0,
    "consolidate": 1,
    "reorganize": 2,
    "decay_sweep": 3,
    "compress": 4,
    "sleep_compute": 5,
    "event_log_prune": 6,
}

#: Labels outside the known pipeline set (deployment-registered pipelines,
#: E7 sleep_compute overrides) sort after everything shipped.
DEFAULT_PRIORITY = max(PIPELINE_PRIORITIES.values()) + 1


def stream_key(namespace: str, label: str) -> str:
    """The per-scope stream key (D-42 §3): ``memspine:<namespace>:<label>``."""
    if not namespace or not label:
        raise MemspineError("stream_key needs a non-empty namespace scope and task label")
    return f"{STREAM_PREFIX}:{namespace}:{label}"


def priority_for(label: str) -> int:
    """The priority carried on a work marker; unknown labels sort last."""
    return PIPELINE_PRIORITIES.get(label, DEFAULT_PRIORITY)


async def ensure_group(redis: Any, key: str) -> None:
    """Idempotently create the consumer group on a per-scope stream."""
    try:
        await redis.xgroup_create(key, CONSUMER_GROUP, id="0", mkstream=True)
    except Exception as exc:  # redis.ResponseError, but the client is duck-typed
        if "BUSYGROUP" not in str(exc):
            raise


async def claim_stale(
    redis: Any,
    namespace: str,
    label: str,
    consumer: str,
    min_idle_ms: int = constants.TASKIQ_CLAIM_MIN_IDLE_MS,
) -> list[str]:
    """XAUTOCLAIM claim-recovery (D-42 §3): transfer pending work markers idle
    longer than ``min_idle_ms`` to ``consumer`` and return their ids. Safe to
    re-run the claimed pipelines — they are idempotent (D-17)."""
    key = stream_key(namespace, label)
    _cursor, entries, *_deleted = await redis.xautoclaim(
        key, CONSUMER_GROUP, consumer, min_idle_time=min_idle_ms
    )
    return [
        entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
        for entry_id, _fields in entries
    ]


class TaskiqRunner:
    """TaskRunner over per-scope Redis Streams (D-16/D-42 §3).

    ``scope`` is the namespace component of every stream key. v0.1 pipelines
    are engine-wide (they iterate namespaces internally), so the default scope
    is ``engine``; per-namespace scoping joins when pipelines take a namespace.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        scope: str = "engine",
        consumer: str = "memspine-engine",
        redis: Any | None = None,
    ) -> None:
        if redis is None:
            try:
                import taskiq  # noqa: F401  — the extra gate (D-10)
                import taskiq_redis  # noqa: F401
                from redis.asyncio import Redis
            except ImportError as exc:
                raise MissingServiceError("workers.taskiq", extra="taskiq") from exc
            redis = Redis.from_url(url)
        self._redis: Any = redis
        self._scope = scope
        self._consumer = consumer
        self._pipelines: dict[str, Pipeline] = {}
        self._warned_degraded = False

    def register(self, name: str, pipeline: Pipeline) -> None:
        self._pipelines[name] = pipeline

    async def run(self, name: str, ctx: PipelineContext) -> dict[str, object]:
        pipeline = self._pipelines.get(name)
        if pipeline is None:
            return {"status": "error", "error": f"unknown pipeline {name!r}"}
        marker = await self._enqueue_and_claim(name)
        try:
            stats = await pipeline(ctx)
        except Exception as exc:  # dead-letter, not crash (D-18); marker stays
            # pending in the group — the claim-recovery surface (XAUTOCLAIM).
            _log.warning(
                "pipeline.dead_letter",
                pipeline=name,
                runner="taskiq",
                error=str(exc),
                exc_info=True,
            )
            return {"status": "error", "error": str(exc)}
        if marker is not None:
            await self._ack(name, marker)
        return stats

    async def close(self) -> None:
        aclose = getattr(self._redis, "aclose", None)
        if callable(aclose):
            await aclose()

    # ── stream mechanics ─────────────────────────────────────────────────────

    async def _enqueue_and_claim(self, label: str) -> str | None:
        """Append the durable work marker and claim it into the consumer group.

        Returns the marker id, or None when the broker is unreachable — the
        pipeline still runs (loudly degraded), because background maintenance
        must never depend on broker health (D-16/D-18)."""
        key = stream_key(self._scope, label)
        try:
            await ensure_group(self._redis, key)
            marker = await self._redis.xadd(
                key, {"label": label, "priority": str(priority_for(label))}
            )
            # Move the marker into this consumer's pending list so XACK/XAUTOCLAIM
            # apply; ">" delivers never-delivered entries (ours included).
            await self._redis.xreadgroup(CONSUMER_GROUP, self._consumer, {key: ">"}, count=64)
            return marker.decode() if isinstance(marker, bytes) else str(marker)
        except Exception as exc:
            if not self._warned_degraded:
                self._warned_degraded = True
                _log.warning(
                    "taskiq.stream_degraded",
                    detail="broker unreachable: pipelines execute inline without "
                    "the durable work marker (claim-recovery unavailable)",
                    error=str(exc),
                )
            return None

    async def _ack(self, label: str, marker: str) -> None:
        try:
            await self._redis.xack(stream_key(self._scope, label), CONSUMER_GROUP, marker)
        except Exception as exc:  # an unacked-but-done marker is safe (idempotent)
            _log.warning("taskiq.ack_failed", pipeline=label, marker=marker, error=str(exc))
