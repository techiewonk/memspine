"""Write-side cost ledger and budget (W2-12; W-26, P-22 C; built on LIB-27 ``stage_ledger``).

One component for W-26 and P-22. ``WriteCostLedger`` counts, per call, the model calls, tokens in
and out, latency and embed calls of a write, keyed by namespace, layer and stage (window extract,
each sleep stage). Which namespace/layer/stage a call belongs to rides a ``ContextVar`` set by the
caller (``cost_scope``); the model seams (LLM router, embedding cache) only report numbers.
Every record is also appended to a LIB-27 ``StageLedger`` (ids, counts and durations only, no
content), so the LIB-27 aggregates and targets work on it unchanged.

``Budget`` is an optional spend limit per namespace and period. It never drops anything: the
caller asks ``exhausted(namespace)`` before starting derivation, and a spent budget pauses the
work, which the window writer parks as underived items in the outbox (``resume`` lets them go).

Process-wide install (``install`` / ``installed``) mirrors ``economy.slo``; with nothing
installed every hook is a pass-through.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from memspine.observability import tap
from memspine.observability.stage_ledger import StageEntry, StageLedger, scope_hash
from memspine.schemas.durable.common import OwnerScope

__all__ = [
    "SLEEP_POOL",
    "UNSCOPED",
    "Budget",
    "CallCost",
    "CostScope",
    "WriteCostLedger",
    "cost_scope",
    "current_scope",
    "install",
    "installed",
    "record_call",
]

#: Namespace/layer/stage used when a call carries no scope (and for engine-wide sleep stages).
UNSCOPED = "-"
#: Namespace key of engine-wide sleep stages: they run over every namespace and carry no per-call
#: namespace, so their spend (and the sleep budget) is one pool.
SLEEP_POOL = UNSCOPED

Kind = Literal["generative", "embedding", "rerank", "tool"]
Outcome = Literal["ok", "error", "timeout", "skipped", "retried"]


@dataclass(frozen=True)
class CostScope:
    namespace: str = UNSCOPED
    layer: str = UNSCOPED
    stage: str = UNSCOPED


_NONE = CostScope()
_SCOPE: ContextVar[CostScope] = ContextVar("memspine_cost_scope", default=_NONE)


def current_scope() -> CostScope:
    return _SCOPE.get()


@contextlib.contextmanager
def cost_scope(
    *, namespace: str | None = None, layer: str | None = None, stage: str | None = None
) -> Iterator[None]:
    """Attribute the model calls made in this block (and the tasks it spawns). Fields left None
    keep the enclosing scope's value."""
    cur = _SCOPE.get()
    token = _SCOPE.set(
        CostScope(
            namespace=cur.namespace if namespace is None else namespace,
            layer=cur.layer if layer is None else layer,
            stage=cur.stage if stage is None else stage,
        )
    )
    try:
        yield
    finally:
        _SCOPE.reset(token)


@dataclass(frozen=True)
class CallCost:
    """One recorded call (or a batch of ``calls``). Numbers and ids only."""

    seq: int
    namespace: str
    layer: str
    stage: str
    kind: Kind
    model: str | None
    calls: int
    tokens_in: int
    tokens_out: int
    latency_ms: float
    outcome: Outcome


# --------------------------------------------------------------------------------- budget
class _Spend:
    __slots__ = ("calls", "paused", "period_start", "tokens")

    def __init__(self, start: float) -> None:
        self.calls = 0
        self.tokens = 0
        self.period_start = start
        self.paused = False


class Budget:
    """Spend limit per namespace: generative calls and tokens (in + out) per period.

    ``None`` limits are not checked; ``period_s=None`` is one period for the whole run. A rolled
    period resets the spend. ``exhausted`` is the only gate: it says the work must wait. A call
    already in flight is never cancelled, so a namespace can overshoot by one window.
    """

    def __init__(
        self,
        *,
        max_calls: int | None = None,
        max_tokens: int | None = None,
        period_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.period_s = period_s
        self._clock = clock
        self._spend: dict[str, _Spend] = {}
        self._lock = threading.RLock()
        self.deferred = 0  # items held back, ever (a counter, for the report)

    def _roll(self, namespace: str) -> _Spend:
        now = self._clock()
        sp = self._spend.get(namespace)
        if sp is None:
            sp = self._spend[namespace] = _Spend(now)
        elif self.period_s is not None and now - sp.period_start >= self.period_s:
            sp.calls = sp.tokens = 0
            sp.period_start = now
        return sp

    def charge(self, namespace: str, *, calls: int = 0, tokens: int = 0) -> None:
        with self._lock:
            sp = self._roll(namespace)
            sp.calls += calls
            sp.tokens += tokens

    def exhausted(self, namespace: str) -> bool:
        with self._lock:
            sp = self._roll(namespace)
            return (self.max_calls is not None and sp.calls >= self.max_calls) or (
                self.max_tokens is not None and sp.tokens >= self.max_tokens
            )

    def note_paused(self, namespace: str) -> None:
        with self._lock:
            self._roll(namespace).paused = True
            self.deferred += 1
        tap.event("economy.budget", action="pause", namespace_hash=_ns_hash(namespace))

    def resume(self, namespace: str, *, reset: bool = False) -> bool:
        """Lift the pause when the budget allows more work (``reset=True``: grant a fresh period
        first). Returns whether the namespace may derive now."""
        with self._lock:
            sp = self._roll(namespace)
            if reset:
                sp.calls = sp.tokens = 0
                sp.period_start = self._clock()
            ok = not self.exhausted(namespace)
            if ok:
                sp.paused = False
        if ok:
            tap.event("economy.budget", action="resume", namespace_hash=_ns_hash(namespace))
        return ok

    def status(self, namespace: str) -> dict[str, Any]:
        with self._lock:
            sp = self._roll(namespace)
            return {
                "calls": sp.calls,
                "tokens": sp.tokens,
                "max_calls": self.max_calls,
                "max_tokens": self.max_tokens,
                "period_s": self.period_s,
                "paused": sp.paused,
            }


def _ns_hash(namespace: str) -> str:
    return scope_hash(OwnerScope(tenant="-", owner=namespace))


def _id(value: str) -> str:
    """Keep a stage or model name inside the LIB-27 id alphabet (its guard rejects the rest)."""
    return "".join(ch if (ch.isalnum() or ch in "._:/@+#=-") else "-" for ch in value)[:128] or "-"


# --------------------------------------------------------------------------------- ledger
class WriteCostLedger:
    """Per-call cost records, aggregated per layer, namespace and stage. Thread-safe, bounded."""

    def __init__(self, *, budget: Budget | None = None, max_records: int = 100_000) -> None:
        self.budget = budget
        self.stage_ledger = StageLedger()  # LIB-27 view: ids, counts, durations only
        self._records: list[CallCost] = []
        self._max = max_records
        self._seq = 0
        self._lock = threading.Lock()

    def record(
        self,
        kind: Kind,
        *,
        model: str | None = None,
        calls: int = 1,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        outcome: Outcome = "ok",
        scope: CostScope | None = None,
    ) -> CallCost:
        sc = scope or current_scope()
        with self._lock:
            self._seq += 1
            rec = CallCost(
                self._seq,
                sc.namespace,
                sc.layer,
                sc.stage,
                kind,
                model,
                max(0, calls),
                max(0, tokens_in),
                max(0, tokens_out),
                max(0.0, latency_ms),
                outcome,
            )
            if len(self._records) < self._max:
                self._records.append(rec)
                self.stage_ledger.append(
                    StageEntry(
                        stage=_id(sc.stage),
                        scope_hash=_ns_hash(sc.namespace),
                        request_or_job_id=f"call-{rec.seq}",
                        cost_class=kind,
                        model_id=_id(model) if model else None,
                        prompt_tokens=rec.tokens_in,
                        completion_tokens=rec.tokens_out,
                        calls=rec.calls,
                        duration_ms=rec.latency_ms,
                        outcome=outcome,
                        is_job=True,
                    )
                )
        if self.budget is not None and kind == "generative":
            self.budget.charge(sc.namespace, calls=rec.calls, tokens=rec.tokens_in + rec.tokens_out)
        if tap.enabled():
            tap.event(
                "economy.cost",
                kind=kind,
                stage=sc.stage,
                layer=sc.layer,
                namespace_hash=_ns_hash(sc.namespace),
                model=model,
                calls=rec.calls,
                tokens_in=rec.tokens_in,
                tokens_out=rec.tokens_out,
                latency_ms=round(rec.latency_ms, 3),
                outcome=outcome,
            )
        return rec

    @property
    def records(self) -> tuple[CallCost, ...]:
        with self._lock:
            return tuple(self._records)

    def select(
        self,
        *,
        namespace: str | None = None,
        stages: tuple[str, ...] | None = None,
        kind: str | None = None,
    ) -> list[CallCost]:
        return [
            r
            for r in self.records
            if (namespace is None or r.namespace == namespace)
            and (stages is None or r.stage in stages)
            and (kind is None or r.kind == kind)
        ]

    def budget_exhausted(self, namespace: str) -> bool:
        return self.budget is not None and self.budget.exhausted(namespace)

    @staticmethod
    def _totals(rows: list[CallCost]) -> dict[str, Any]:
        return {
            "model_calls": sum(r.calls for r in rows if r.kind == "generative"),
            "embed_calls": sum(r.calls for r in rows if r.kind == "embedding"),
            "rerank_calls": sum(r.calls for r in rows if r.kind == "rerank"),
            "tokens_in": sum(r.tokens_in for r in rows),
            "tokens_out": sum(r.tokens_out for r in rows),
            "latency_ms": round(sum(r.latency_ms for r in rows), 3),
            "failures": sum(1 for r in rows if r.outcome in ("error", "timeout")),
        }

    def report(self, namespace: str | None = None) -> dict[str, Any]:
        """Totals plus the same totals split by layer, stage and namespace. ``namespace`` filters
        the rows (engine-wide sleep stages are under ``"-"``: see the unfiltered view)."""
        rows = self.select(namespace=namespace)
        split: dict[str, dict[str, list[CallCost]]] = {
            "by_layer": defaultdict(list),
            "by_stage": defaultdict(list),
            "by_namespace": defaultdict(list),
        }
        for r in rows:
            split["by_layer"][r.layer].append(r)
            split["by_stage"][r.stage].append(r)
            split["by_namespace"][r.namespace].append(r)
        out: dict[str, Any] = {"namespace": namespace, **self._totals(rows)}
        for name, groups in split.items():
            out[name] = {k: self._totals(v) for k, v in sorted(groups.items())}
        out["budget"] = None
        if self.budget is not None:
            spaces = [namespace] if namespace else sorted({r.namespace for r in rows})
            out["budget"] = {
                "deferred": self.budget.deferred,
                "namespaces": {ns: self.budget.status(ns) for ns in spaces},
            }
        return out


# --------------------------------------------------------------------------------- install
_LEDGER: WriteCostLedger | None = None


def install(ledger: WriteCostLedger | None) -> WriteCostLedger | None:
    """Make ``ledger`` the process-wide sink (None removes it); returns the previous one."""
    global _LEDGER
    previous, _LEDGER = _LEDGER, ledger
    return previous


def installed() -> WriteCostLedger | None:
    return _LEDGER


def record_call(kind: Kind, **fields: Any) -> None:
    """Model-seam hook: record into the installed ledger; a pass-through when none is installed.
    Never raises into a model call."""
    ledger = _LEDGER
    if ledger is None:
        return
    with contextlib.suppress(Exception):
        ledger.record(kind, **fields)
