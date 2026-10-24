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

Each Engine binds its own ledger (``observability.bindings``, ADR-079 decision 5); ``installed``
returns the ledger of the current Engine. With nothing bound every hook is a pass-through.
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Engine, text

from memspine.observability import bindings as _bindings
from memspine.observability import tap
from memspine.observability.stage_ledger import StageEntry, StageLedger, scope_hash
from memspine.schemas.durable.common import OwnerScope
from memspine.store.side_tables import (
    BackgroundWriter,
    ensure_side_tables,
    local_engine,
    write_tx,
)

__all__ = [
    "SLEEP_POOL",
    "UNSCOPED",
    "Budget",
    "CallCost",
    "CostScope",
    "LedgerStore",
    "WriteCostLedger",
    "cost_scope",
    "current_scope",
    "installed",
    "record_call",
]

#: Namespace/layer/stage used when a call carries no scope (and for engine-wide sleep stages).
UNSCOPED = "-"
#: Namespace key of engine-wide sleep stages: they run over every namespace and carry no per-call
#: namespace, so their spend (and the sleep budget) is one pool. A namespace-scoped sleep job
#: (``offline.sleep_scoped``) is costed under its real namespace instead.
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
    clean = "".join(ch if (ch.isalnum() or ch in "._:/@+#=-") else "-" for ch in value)[:128]
    # the LIB-27 id guard wants a letter or digit first: an unscoped call ("-") is "unscoped"
    return clean if clean[:1].isalnum() else f"unscoped{clean.strip('-')}"


# --------------------------------------------------------------------------------- persistence
_INSERT = text(
    "INSERT INTO cost_entry (run, ns_hash, layer, stage, kind, model, calls, tokens_in, "
    "tokens_out, latency_ms, outcome) VALUES (:run, :ns_hash, :layer, :stage, :kind, :model, "
    ":calls, :tokens_in, :tokens_out, :latency_ms, :outcome)"
)


class LedgerStore:
    """``economy.ledger_persist``: compact ledger rows in the ``cost_entry`` table, so the totals
    survive a restart. RM-111 WP5: the table is in the storage database (pass its sync engine:
    encrypted with it on SQLite, shared on Postgres); a path (``:memory:`` or a plain SQLite file)
    is for tests and tools. A row is ids, the namespace *hash*, layer, stage, kind, calls, tokens
    and latency: never a name or content. Rows are buffered and written in one transaction every
    ``flush_every`` records, on ``flush()`` and on ``close()``, so the call path pays no per-call
    commit; against the storage database the write runs on a background thread (the event loop
    never waits on SQLite's write lock), and ``close()`` waits for it."""

    def __init__(self, path: str | Engine, *, flush_every: int = 32) -> None:
        if isinstance(path, Engine):
            self.path = path.url.render_as_string(hide_password=True)
            self._engine, self._owned = path, False
            self._writer: BackgroundWriter | None = BackgroundWriter("cost-ledger")
        else:
            self.path = path
            self._engine, self._owned = local_engine(path), True
            self._writer = None
        self._flush_every = max(1, flush_every)
        self._run = f"{int(time.time())}-{id(self) & 0xFFFF:x}"
        self._buf: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._closed = False
        ensure_side_tables(self._engine, ("cost_entry",))

    def load(self, limit: int) -> list[CallCost]:
        """The newest ``limit`` persisted rows, oldest first, as records (namespace = its hash;
        ``seq`` 0, renumbered by the ledger)."""
        with self._lock:
            if self._closed:
                return []
            with self._engine.connect() as con:
                rows = con.execute(
                    text(
                        "SELECT ns_hash, layer, stage, kind, model, calls, tokens_in, tokens_out, "
                        "latency_ms, outcome FROM (SELECT * FROM cost_entry ORDER BY id DESC "
                        "LIMIT :n) AS newest ORDER BY id"
                    ),
                    {"n": limit},
                ).fetchall()
        return [CallCost(0, *r) for r in rows]

    def add(self, rec: CallCost) -> None:
        ns = rec.namespace if rec.namespace == UNSCOPED else _ns_hash(rec.namespace)
        row = {
            "run": self._run,
            "ns_hash": ns,
            "layer": rec.layer,
            "stage": rec.stage,
            "kind": rec.kind,
            "model": rec.model,
            "calls": rec.calls,
            "tokens_in": rec.tokens_in,
            "tokens_out": rec.tokens_out,
            "latency_ms": rec.latency_ms,
            "outcome": rec.outcome,
        }
        with self._lock:
            self._buf.append(row)
            full = len(self._buf) >= self._flush_every
        if full:
            self.flush()

    def _write(self, rows: list[dict[str, Any]]) -> None:
        # a private engine may be one shared connection (``:memory:``): one writer at a time. The
        # storage engine pools its connections, and the buffer lock must never wait on a write.
        with self._lock if self._owned else contextlib.nullcontext(), write_tx(self._engine) as con:
            con.execute(_INSERT, rows)

    def flush(self) -> None:
        with self._lock:
            rows, self._buf = self._buf, []
            if not rows or self._closed:
                return
        if self._writer is not None:
            self._writer.submit(self._write, rows)
        else:
            self._write(rows)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.flush()
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._owned:
                    self._engine.dispose()


# --------------------------------------------------------------------------------- ledger
class WriteCostLedger:
    """Per-call cost records, aggregated per layer, namespace and stage. Thread-safe, bounded.

    With a ``store`` (``economy.ledger_persist``) every record is also persisted and the earlier
    runs' rows are reloaded here, so the totals survive a restart. A reloaded row knows only its
    namespace hash: ``report(namespace)`` matches it by hash, and ``by_namespace`` names it once the
    namespace is seen again in this run. Budget spend is not reloaded (it is per period)."""

    def __init__(
        self,
        *,
        budget: Budget | None = None,
        max_records: int = 100_000,
        store: LedgerStore | None = None,
    ) -> None:
        self.budget = budget
        self.stage_ledger = StageLedger()  # LIB-27 view: ids, counts, durations only
        self._records: list[CallCost] = []
        self._max = max_records
        self._seq = 0
        self._lock = threading.Lock()
        self.store = store
        self._names: dict[str, str] = {}  # namespace hash -> name, learned from this run's calls
        self._reloaded: set[str] = set()  # namespace hashes that came from the store
        if store is not None:
            for old in store.load(max_records):
                self._seq += 1
                rec = dataclasses.replace(old, seq=self._seq)
                self._records.append(rec)
                self._reloaded.add(rec.namespace)
                self._stage_entry(rec)

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
                self._stage_entry(rec)
                if sc.namespace != UNSCOPED:
                    self._names[_ns_hash(sc.namespace)] = sc.namespace
        if self.store is not None:
            with contextlib.suppress(Exception):  # persistence never fails a model call
                self.store.add(rec)
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

    def _stage_entry(self, rec: CallCost) -> None:
        """The LIB-27 view of one record (ids, counts, durations only)."""
        reloaded = rec.namespace in self._reloaded and rec.namespace != UNSCOPED
        self.stage_ledger.append(
            StageEntry(
                stage=_id(rec.stage),
                scope_hash=rec.namespace if reloaded else _ns_hash(rec.namespace),
                request_or_job_id=f"call-{rec.seq}",
                cost_class=rec.kind,
                model_id=_id(rec.model) if rec.model else None,
                prompt_tokens=rec.tokens_in,
                completion_tokens=rec.tokens_out,
                calls=rec.calls,
                duration_ms=rec.latency_ms,
                outcome=rec.outcome,
                is_job=True,
            )
        )

    def flush(self) -> None:
        """Write buffered rows to the store (a no-op without ``economy.ledger_persist``)."""
        if self.store is not None:
            self.store.flush()

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    def _label(self, namespace: str) -> str:
        """A record's namespace name: a reloaded hash shows its name once this run has seen it."""
        return self._names.get(namespace, namespace)

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
            if (
                namespace is None
                or r.namespace == namespace
                or (r.namespace in self._reloaded and r.namespace == _ns_hash(namespace))
            )
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
            split["by_namespace"][self._label(r.namespace)].append(r)
        out: dict[str, Any] = {"namespace": namespace, **self._totals(rows)}
        for name, groups in split.items():
            out[name] = {k: self._totals(v) for k, v in sorted(groups.items())}
        out["budget"] = None
        if self.budget is not None:
            spaces = (
                [namespace]
                if namespace
                else sorted(
                    {
                        self._label(r.namespace)
                        for r in rows
                        if r.namespace not in self._reloaded
                        or r.namespace in self._names
                        or r.namespace == UNSCOPED
                    }
                )
            )
            out["budget"] = {
                "deferred": self.budget.deferred,
                "namespaces": {ns: self.budget.status(ns) for ns in spaces},
            }
        return out


# --------------------------------------------------------------------------------- binding
def installed() -> WriteCostLedger | None:
    """The cost ledger bound for the current Engine (``economy.ledger``), if any."""
    ledger: WriteCostLedger | None = _bindings.resolve("cost_ledger", None)
    return ledger


def record_call(kind: Kind, **fields: Any) -> None:
    """Model-seam hook: record into the bound ledger; a pass-through when none is bound.
    Never raises into a model call."""
    ledger = installed()
    if ledger is None:
        return
    with contextlib.suppress(Exception):
        ledger.record(kind, **fields)
