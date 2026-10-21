"""Per-stage ops ledger and the merged ``ops_report`` (T5-06; G-20; ``observability.ops_ledger``).

One surface over the three ledgers the engine already has:

- :class:`OpsLedger` (this module) times every read verb, write verb, window-extract job and sleep
  stage: runs, errors, items, dropped and deferred counts, and latency. Each run is also appended
  to a LIB-27 :class:`~memspine.observability.stage_ledger.StageEntry` (stage ``<kind>.<name>``),
  so the LIB-27 aggregates work on it.
- The RT-02 :class:`~memspine.economy.slo.SloLedger` (read p95, background lag, backlog).
- The W2-12 :class:`~memspine.economy.ledger.WriteCostLedger` (model calls, tokens, embed calls per
  write and sleep stage, plus the budget).

:func:`ops_report` merges them into one dict. Nothing here holds content: stage names are code
constants, ids are hashes or counters, everything else is a count or a duration. Each Engine binds
its own ledger (``observability.bindings``, ADR-079 decision 5); with nothing bound every hook is a
pass-through.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import Any, Literal

from memspine.observability import bindings as _bindings
from memspine.observability.stage_ledger import StageEntry, StageLedger

__all__ = [
    "KINDS",
    "OpsLedger",
    "StageHandle",
    "installed",
    "note",
    "ops_report",
    "stage",
    "staged",
]

Kind = Literal["read", "write", "sleep"]
KINDS: tuple[str, ...] = ("read", "write", "sleep")
_NOTE_KEYS = (
    "items",
    "dropped",
    "deferred",
    "errors",
    "model_calls",
    "tokens_in",
    "tokens_out",
)


def _id(value: str) -> str:
    """Keep a stage name inside the LIB-27 id alphabet."""
    return "".join(ch if (ch.isalnum() or ch in "._:/@+#=-") else "-" for ch in value)[:100] or "-"


class StageHandle:
    """Counters a running stage may bump through :func:`note` (integers only)."""

    __slots__ = ("counts",)

    def __init__(self) -> None:
        self.counts: dict[str, int] = dict.fromkeys(_NOTE_KEYS, 0)

    def add(self, **counts: int) -> None:
        for key, value in counts.items():
            if key not in self.counts:
                raise KeyError(f"unknown ops counter {key!r} (allowed: {', '.join(_NOTE_KEYS)})")
            self.counts[key] += max(0, int(value))


_CURRENT: ContextVar[StageHandle | None] = ContextVar("memspine_ops_stage", default=None)
_IN_VERB: ContextVar[bool] = ContextVar("memspine_ops_in_verb", default=False)


class OpsLedger:
    """In-process, bounded, thread-safe per-stage ledger for read, write and sleep stages."""

    def __init__(self, *, max_entries: int = 100_000) -> None:
        self.stage_ledger = StageLedger()
        self._max = max_entries
        self._seq = 0
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], dict[str, int]] = {}

    @contextlib.contextmanager
    def stage(self, kind: Kind, name: str, *, verb: bool = False) -> Iterator[StageHandle]:
        handle = StageHandle()
        token = _CURRENT.set(handle)
        vtoken = _IN_VERB.set(True) if verb else None
        started = time.perf_counter()
        outcome = "ok"
        try:
            yield handle
        except TimeoutError:
            outcome = "timeout"
            raise
        except Exception:
            outcome = "error"
            raise
        except BaseException:
            outcome = "skipped"  # cancelled
            raise
        finally:
            _CURRENT.reset(token)
            if vtoken is not None:
                _IN_VERB.reset(vtoken)
            self._record(kind, name, handle, (time.perf_counter() - started) * 1000.0, outcome)

    def _record(self, kind: str, name: str, handle: StageHandle, ms: float, outcome: str) -> None:
        with self._lock:
            self._seq += 1
            row = self._counts.setdefault((kind, name), {"runs": 0, **dict.fromkeys(_NOTE_KEYS, 0)})
            row["runs"] += 1
            if outcome in ("error", "timeout"):
                row["errors"] += 1
            for key, value in handle.counts.items():
                row[key] += value
            if len(self.stage_ledger) < self._max:
                with contextlib.suppress(ValueError):  # a guard reject never breaks a verb
                    self.stage_ledger.append(
                        StageEntry(
                            stage=f"{kind}.{_id(name)}",
                            scope_hash="none",
                            request_or_job_id=f"{kind}-{self._seq}",
                            cost_class="generative",
                            calls=handle.counts["model_calls"],
                            prompt_tokens=handle.counts["tokens_in"],
                            completion_tokens=handle.counts["tokens_out"],
                            duration_ms=ms,
                            outcome=outcome,  # type: ignore[arg-type]
                            is_job=kind != "read",
                        )
                    )

    def stages(self) -> dict[str, dict[str, dict[str, Any]]]:
        """``{kind: {stage: row}}`` with counters and p50 / p95 / total latency."""
        agg = self.stage_ledger.aggregate("stage")
        out: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in KINDS}
        with self._lock:
            counts = {k: dict(v) for k, v in self._counts.items()}
        for (kind, name), row in sorted(counts.items()):
            a = agg.get(f"{kind}.{_id(name)}")
            out[kind][name] = {
                **row,
                "p50_ms": None if a is None else a.duration_ms_p50,
                "p95_ms": None if a is None else a.duration_ms_p95,
                "total_ms": 0.0 if a is None else round(a.duration_ms_total, 3),
            }
        return out


# --------------------------------------------------------------------------------- binding
def installed() -> OpsLedger | None:
    """The ops ledger bound for the current Engine (``observability.ops_ledger``), if any."""
    ledger: OpsLedger | None = _bindings.resolve("ops_ledger", None)
    return ledger


@contextlib.contextmanager
def stage(kind: Kind, name: str) -> Iterator[StageHandle | None]:
    """Time one stage in the bound ledger; a null context (yields None) when none is."""
    ledger = installed()
    if ledger is None:
        yield None
        return
    with ledger.stage(kind, name) as handle:
        yield handle


def note(**counts: int) -> None:
    """Bump counters on the running stage (dropped, deferred, items, ...); no-op outside one."""
    handle = _CURRENT.get()
    if handle is not None:
        handle.add(**counts)


def staged[F: Callable[..., Awaitable[Any]]](kind: Kind) -> Callable[[F], F]:
    """Decorator for the public async verbs: the outermost call is one stage run named after the
    function (a verb called from another verb is not counted twice). Items = len of a list result.
    """

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> Any:
            ledger = installed()
            if ledger is None or _IN_VERB.get():
                return await fn(*args, **kwargs)
            with ledger.stage(kind, fn.__name__, verb=True) as handle:
                result = await fn(*args, **kwargs)
                if isinstance(result, (list, tuple)):
                    handle.add(items=len(result))
                return result

        return inner  # type: ignore[return-value]

    return deco


# --------------------------------------------------------------------------------- report
def _blank_row() -> dict[str, Any]:
    return {
        "runs": 0,
        **dict.fromkeys(_NOTE_KEYS, 0),
        "embed_calls": 0,
        "rerank_calls": 0,
        "p50_ms": None,
        "p95_ms": None,
        "total_ms": 0.0,
    }


def ops_report(
    ops: OpsLedger | None,
    *,
    slo: Any = None,
    cost: Any = None,
    namespace: str | None = None,
    otel: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the ops ledger, the SLO ledger and the write cost ledger into one report.

    ``stages`` is ``{read|write|sleep: {stage: row}}``; a row carries runs, errors, items, dropped,
    deferred, model_calls, embed_calls, rerank_calls, tokens_in/out and p50 / p95 / total ms. Cost
    rows fill the model fields (``namespace`` filters them; engine-wide sleep stages sit under the
    cost ledger's ``"-"`` namespace and are kept in every view). ``slo`` is an SloReport (or None),
    ``cost`` a WriteCostLedger (or None).
    """
    stages: dict[str, dict[str, dict[str, Any]]] = (
        ops.stages() if ops is not None else {k: {} for k in KINDS}
    )
    cost_report: dict[str, Any] | None = None
    if cost is not None:
        from memspine.economy.ledger import SLEEP_POOL

        for rec in cost.select():
            if namespace is not None and rec.namespace not in (namespace, SLEEP_POOL):
                continue
            kind = "sleep" if rec.namespace == SLEEP_POOL else "write"
            row = stages[kind].setdefault(rec.stage, _blank_row())
            if rec.kind == "generative":
                row["model_calls"] += rec.calls
            elif rec.kind == "embedding":
                row["embed_calls"] += rec.calls
            elif rec.kind == "rerank":
                row["rerank_calls"] += rec.calls
            row["tokens_in"] += rec.tokens_in
            row["tokens_out"] += rec.tokens_out
            if rec.outcome in ("error", "timeout"):
                row["errors"] += 1
        cost_report = cost.report(namespace)
    for rows in stages.values():
        for row in rows.values():
            row.setdefault("embed_calls", 0)
            row.setdefault("rerank_calls", 0)
    flat = [row for rows in stages.values() for row in rows.values()]
    totals = {
        key: sum(r[key] for r in flat)
        for key in (
            "runs",
            "errors",
            "items",
            "dropped",
            "deferred",
            "model_calls",
            "tokens_in",
            "tokens_out",
        )
    }
    budget = (cost_report or {}).get("budget")
    totals["deferred_budget"] = int(budget["deferred"]) if budget else 0
    return {
        "enabled": ops is not None,
        "stages": stages,
        "totals": totals,
        "slo": dataclasses.asdict(slo) if slo is not None else None,
        "cost": cost_report,
        "otel": otel,
    }
