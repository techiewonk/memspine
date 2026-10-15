"""Quotas checked before acceptance (LIB-07, schema session 9, ER-08).

Pure: stdlib and ``memspine.schemas`` only; the clock is injected. A rejected request produces a typed
``QuotaExceeded`` and consumes nothing. Accepted work is never dropped later; that is the job book's side.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from memspine.schemas.public import QuotaExceeded

__all__ = ["QuotaGate", "QuotaPolicy", "QuotaRequest", "RoundRobinPicker", "TokenBucket"]

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class QuotaPolicy:
    """Limits per scope (tenant+owner). ``None`` disables a quota.

    ``rate`` is the number of requests per ``rate_window_seconds`` (bucket capacity = ``rate``,
    refilled continuously); ``burst`` overrides the capacity.
    """

    max_storage_bytes: int | None = None
    max_queue_depth: int | None = None
    rate: float | None = None
    rate_window_seconds: float = 60.0
    burst: float | None = None

    def gate(self, clock: Clock) -> QuotaGate:
        return QuotaGate(self, clock)

    def __post_init__(self) -> None:
        if self.rate_window_seconds <= 0:
            raise ValueError("rate_window_seconds must be positive")
        for name in ("max_storage_bytes", "max_queue_depth", "rate", "burst"):
            v = getattr(self, name)
            if v is not None and v < 0:
                raise ValueError(f"{name} must be >= 0")


@dataclass(frozen=True)
class QuotaRequest:
    """What a write would add. ``scope_id`` is any hashable key (tenant, owner)."""

    scope_id: Any
    storage_current: int = 0  # bytes already stored for the scope
    storage_add: int = 0  # bytes this request would add
    queue_current: int = 0  # accepted, unfinished jobs for the scope
    queue_add: int = 1  # jobs this request would enqueue
    cost: float = 1.0  # rate tokens


class TokenBucket:
    """Token bucket on an injected clock."""

    def __init__(self, capacity: float, per_second: float, clock: Clock) -> None:
        self.capacity = capacity
        self.per_second = per_second
        self._clock = clock
        self._tokens = capacity
        self._at = clock()

    def _refill(self) -> None:
        now = self._clock()
        dt = (now - self._at).total_seconds()
        if dt > 0:
            self._tokens = min(self.capacity, self._tokens + dt * self.per_second)
        self._at = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def retry_after(self, cost: float) -> float | None:
        """Seconds until ``cost`` tokens are available; ``None`` if it can never be (cost > capacity
        or no refill)."""
        self._refill()
        if cost > self.capacity:
            return None
        if self._tokens >= cost:
            return 0.0
        if self.per_second <= 0:
            return None
        return (cost - self._tokens) / self.per_second

    def take(self, cost: float) -> bool:
        self._refill()
        if self._tokens < cost:
            return False
        self._tokens -= cost
        return True


class QuotaGate:
    """Stateful checker: one bucket per scope. ``check`` runs before acceptance."""

    def __init__(self, policy: QuotaPolicy, clock: Clock) -> None:
        self.policy = policy
        self._clock = clock
        self._buckets: dict[Any, TokenBucket] = {}

    def _bucket(self, scope_id: Any) -> TokenBucket | None:
        p = self.policy
        if p.rate is None:
            return None
        b = self._buckets.get(scope_id)
        if b is None:
            b = TokenBucket(
                p.burst if p.burst is not None else p.rate,
                p.rate / p.rate_window_seconds,
                self._clock,
            )
            self._buckets[scope_id] = b
        return b

    def check(self, req: QuotaRequest) -> QuotaExceeded | None:
        """``None`` if accepted (and rate tokens consumed); otherwise the typed rejection, with
        nothing consumed. Order: storage, queue depth, rate."""
        p = self.policy
        if (
            p.max_storage_bytes is not None
            and req.storage_current + req.storage_add > p.max_storage_bytes
        ):
            return QuotaExceeded(
                quota="storage", limit=p.max_storage_bytes, current=req.storage_current
            )
        if p.max_queue_depth is not None and req.queue_current + req.queue_add > p.max_queue_depth:
            return QuotaExceeded(
                quota="queue_depth", limit=p.max_queue_depth, current=req.queue_current
            )
        bucket = self._bucket(req.scope_id)
        if bucket is not None:
            wait = bucket.retry_after(req.cost)
            if wait != 0.0:
                return QuotaExceeded(
                    quota="rate",
                    limit=p.rate or 0.0,
                    current=max(bucket.capacity - bucket.tokens, 0.0),
                    retry_after=wait,
                )
            bucket.take(req.cost)
        return None


class RoundRobinPicker:
    """Per-tenant fairness: pick the next scope in sorted order after the last one served, and within
    it the oldest job. ``scope_of`` maps a candidate to its fairness key (default: ``job.scope`` tenant
    then owner)."""

    def __init__(self, scope_of: Callable[[Any], Any] | None = None) -> None:
        self._scope_of = scope_of or (lambda j: (j.scope.tenant, j.scope.owner, j.scope.owner_kind))
        self._last: Any = None

    def next_scope(self, ready: Sequence[Any]) -> Any:
        """Round-robin over distinct scope keys."""
        keys = sorted(set(ready))
        if not keys:
            raise ValueError("no ready scopes")
        nxt = next((k for k in keys if self._last is None or k > self._last), keys[0])
        self._last = nxt
        return nxt

    def pick(self, candidates: Sequence[Any]) -> Any:
        scope = self.next_scope([self._scope_of(c) for c in candidates])
        return min((c for c in candidates if self._scope_of(c) == scope), key=lambda j: j.seq)
