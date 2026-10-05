"""The runner: insert the history, ask the questions, grade the answers.

One loop, one protocol, any system, any dataset. The order is fixed and
deliberate (the LongMemEval-V2 precedent): history is inserted **sequentially**
in dataset order, a query sees only what was inserted before it, and the
context the system returns is truncated to the declared budget before the
reader ever sees it.

Two guards exist because this repo has spent real days on runs that turned out
to mean less than they appeared to:

* ``max_model_calls`` — a hard cap, checked before every call. A run cannot
  quietly cost more than it was authorised to.
* ``expect_model_calls`` — when ``False``, any model call is an error. This is
  what makes "no model calls" a property of the run rather than a claim in a
  commit message.
"""

from __future__ import annotations

import inspect
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import (
    DatasetAdapter,
    DepositResult,
    EvalItem,
    Query,
    Reader,
    SystemAdapter,
    Turn,
    visible_evidence,
)
from .credentials import is_auth_error
from .judge import Judge, recall_over_units, unit_ranking
from .metrics import CostModel, Ledger, Stage
from .provenance import ReaderSpec, RunManifest, RunProtocol, SystemSpec
from .results import ResultRow, ResultWriter, RowStatus, RunSummary, aggregate
from .tokens import HeuristicTokenCounter, TokenCounter, truncate_to_budget
from .trace import DepositTrace, TraceWriter, cycle_from_context


class ModelCallBudgetExceeded(RuntimeError):
    """Raised before the call that would breach ``max_model_calls``."""


class UnexpectedModelCall(RuntimeError):
    """Raised when a run declared offline makes a model call anyway."""


class ProviderCredentialError(RuntimeError):
    """C-3: a provider rejected the credentials (expired, invalid, unauthorised).

    Fatal for the run: every later call would fail the same way, so the runner stops
    cleanly (UNATTEMPTED rows for the rest, the summary written) instead of recording
    each remaining question as an ERROR row.
    """


class _Abort(Exception):
    """Internal: carries the partial rows and the unrun schedule up to ``run``."""

    def __init__(
        self,
        cause: BaseException,
        item: EvalItem,
        rows: list[ResultRow],
        remaining: list[Query],
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.item = item
        self.rows = rows
        self.remaining = remaining


@dataclass(slots=True)
class RunConfig:
    run_id: str
    protocol: RunProtocol
    out_dir: Path = Path("runs")
    cost_model: CostModel = field(default_factory=CostModel)
    include_trace_content: bool = False
    max_items: int | None = None
    max_queries_per_item: int | None = None
    max_model_calls: int | None = None
    expect_model_calls: bool = True
    fail_fast: bool = False
    labels: Mapping[str, Any] = field(default_factory=dict)
    #: free-form run limits recorded next to the caps above (e.g. an item-id filter, the
    #: scope of a provider call budget)
    extra_limits: Mapping[str, Any] = field(default_factory=dict)
    #: G3 dollar cap per arm. The reader's own provider budget (``reader.budget``,
    #: Bedrock) enforces it before each reader/judge call; engine-side spend is charged
    #: to the same meter as it is observed and stops the next call.
    max_usd: float | None = None
    #: USD per 1M (input, output) tokens by model id, for the meter the runner builds
    #: when the reader brings none.
    prices_per_mtok: Mapping[str, tuple[float, float]] | None = None
    #: C-6: ``"embed:<model>"`` (USD per 1M tokens) / ``"rerank:<model>"`` (USD per
    #: 1,000 searches) for the engine's paid services, charged as their use is observed.
    service_prices: Mapping[str, float] | None = None

    def limits(self) -> dict[str, Any]:
        return {
            "max_items": self.max_items,
            "max_queries_per_item": self.max_queries_per_item,
            "max_model_calls": self.max_model_calls,
            "max_usd": self.max_usd,
            "expect_model_calls": self.expect_model_calls,
            "fail_fast": self.fail_fast,
            **dict(self.extra_limits),
        }


class EvalRunner:
    """Drives one (dataset x system x protocol) run to a provenanced result."""

    def __init__(
        self,
        dataset: DatasetAdapter,
        system: SystemAdapter,
        reader: Reader,
        judge: Judge,
        config: RunConfig,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.dataset = dataset
        self.system = system
        self.reader = reader
        self.judge = judge
        self.config = config
        self.counter = token_counter or HeuristicTokenCounter()
        self.ledger = Ledger()
        self._model_calls = 0
        self._judge_calls = 0
        self._rng = random.Random(config.protocol.seed)
        #: per loop stage, per engine role: {"model", "calls", "prompt", "completion"}
        self.engine_llm: dict[str, dict[str, dict[str, Any]]] = {}
        #: #33: per loop stage, per engine prompt version: calls and tokens (CPC by stage)
        self.engine_prompts: dict[str, dict[str, dict[str, Any]]] = {}
        #: C-5: per-query rerank audit (``meta["reranked"]`` and the engine's counters)
        self.rerank: dict[str, Any] = {
            "mode": None,
            "queries": 0,
            "reranked": 0,
            "calls": 0,
            "failures": 0,
        }
        #: C-6: engine services observed (``meta["engine_services"]``), priced or not
        self.engine_services: dict[str, float] = {}
        self._meter, self._own_meter = self._spend_meter()
        try:
            params = inspect.signature(reader.answer).parameters
        except (TypeError, ValueError):  # pragma: no cover - builtins without a signature
            params = {}  # type: ignore[assignment]
        self._reader_takes_date = "question_date" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

    # -- manifest ------------------------------------------------------------

    def build_manifest(self) -> RunManifest:
        info = self.dataset.info()
        return RunManifest.build(
            run_id=self.config.run_id,
            dataset=info,
            system=SystemSpec(
                system_id=self.system.system_id,
                version=str(self.system.describe().get("version", "unknown")),
                config=self.system.describe(),
            ),
            reader=ReaderSpec(
                reader_id=self.reader.reader_id,
                model=self.reader.model,
                makes_model_calls=self.reader.makes_model_calls,
                params=dict(self.reader.describe()),
            ),
            judge=self.judge.spec,
            protocol=self.config.protocol,
            token_counter=dict(self.counter.describe()),
            labels=self.config.labels,
            limits=self.config.limits(),
        )

    # -- spend ---------------------------------------------------------------

    def _spend_meter(self) -> tuple[Any, bool]:
        """The dollar meter for this arm: the reader's provider budget when it has one
        (it also checks reader/judge calls before they are made), else one the runner
        builds when a cap or a price table is declared. ``(meter, runner_owned)``."""
        budget = getattr(self.reader, "budget", None)
        if budget is not None and callable(getattr(budget, "charge", None)):
            if self.config.max_usd is not None and getattr(budget, "max_usd", None) is None:
                budget.max_usd = self.config.max_usd
            if self.config.service_prices and not getattr(budget, "service_prices", None):
                budget.service_prices = dict(self.config.service_prices)
            return budget, False
        if (
            self.config.max_usd is None
            and not self.config.prices_per_mtok
            and not self.config.service_prices
        ):
            return None, False
        from .bedrock import CallBudget

        meter = CallBudget(
            max_calls=2**62,  # the call cap is the runner's own (max_model_calls)
            prices_per_mtok=dict(self.config.prices_per_mtok or {}),
            max_usd=self.config.max_usd,
            service_prices=dict(self.config.service_prices or {}),
        )
        return meter, True

    def _spent(self) -> float | None:
        """Dollars metered so far (None without a meter)."""
        return float(self._meter.spent_usd()) if self._meter is not None else None

    def _charge_services(self, meta: Mapping[str, Any]) -> None:
        """C-6: charge the engine's observed paid-service use (``meta["engine_services"]``:
        ``{"embed:<model>": tokens, "rerank:<model>": searches}``) to the meter."""
        services = meta.get("engine_services")
        if not services:
            return
        for key, units in services.items():
            kind, _, model = str(key).partition(":")
            self.engine_services[key] = self.engine_services.get(key, 0.0) + float(units or 0)
            charge = getattr(self._meter, "charge_service", None)
            if callable(charge):
                charge(kind, model, float(units or 0))

    def _audit_rerank(self, meta: Mapping[str, Any]) -> None:
        """C-5: count, per query, whether the system's reranker ran and how often it failed."""
        if "reranked" not in meta and "rerank_mode" not in meta:
            return
        audit = self.rerank
        if meta.get("rerank_mode") is not None:
            audit["mode"] = meta.get("rerank_mode")
        audit["queries"] += 1
        audit["reranked"] += 1 if meta.get("reranked") else 0
        audit["calls"] += int(meta.get("rerank_calls", 0) or 0)
        audit["failures"] += int(meta.get("rerank_failures", 0) or 0)

    def rerank_summary(self) -> dict[str, Any]:
        """The C-5 audit block of ``summary.json``. ``rerank_unavailable``: a reranker is
        configured, questions ran, and it never once returned scores."""
        audit = dict(self.rerank)
        configured = audit["mode"] not in (None, "off")
        audit["configured"] = configured
        audit["rerank_unavailable"] = bool(
            configured and audit["queries"] > 0 and audit["calls"] - audit["failures"] <= 0
        )
        return audit

    def _check_spend(self, what: str) -> None:
        """Stop before ``what`` once observed spend has reached the dollar cap."""
        if self._meter is not None:
            self._meter.check_usd(what=what)

    def _charge_engine(self, stage: Stage, meta: Mapping[str, Any]) -> None:
        """Charge the engine's observed LLM use (``meta["engine_llm"]``) to the meter.

        #33: ``meta["engine_prompts"]`` (the same calls, split per prompt version) is
        tallied per stage for the summary only; the meter is charged once, per role."""
        self._tally_engine_prompts(stage, meta.get("engine_prompts"))
        usage = meta.get("engine_llm")
        if not usage:
            return
        bucket = self.engine_llm.setdefault(stage.value, {})
        for role, used in usage.items():
            acc = bucket.setdefault(
                role, {"model": used.get("model", ""), "calls": 0, "prompt": 0, "completion": 0}
            )
            for key in ("calls", "prompt", "completion"):
                acc[key] += int(used.get(key, 0) or 0)
            if self._meter is not None:
                self._meter.charge(
                    str(used.get("model", "")),
                    int(used.get("prompt", 0) or 0),
                    int(used.get("completion", 0) or 0),
                    calls=int(used.get("calls", 0) or 0),
                )

    def _tally_engine_prompts(self, stage: Stage, prompts: Any) -> None:
        """#33: add one result's per-prompt engine use to the stage's tally."""
        if not isinstance(prompts, Mapping):
            return
        bucket = self.engine_prompts.setdefault(stage.value, {})
        for key, used in prompts.items():
            acc = bucket.setdefault(
                str(key),
                {
                    "prompt_id": used.get("prompt_id"),
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_calls": 0,
                },
            )
            for name in ("calls", "input_tokens", "output_tokens", "estimated_calls"):
                acc[name] += int(used.get(name, 0) or 0)

    # -- guards --------------------------------------------------------------

    def _account_model_calls(self, calls: int, who: str) -> None:
        if calls <= 0:
            return
        if not self.config.expect_model_calls:
            raise UnexpectedModelCall(
                f"{who} made {calls} model call(s) in a run declared offline "
                "(expect_model_calls=False)"
            )
        if (
            self.config.max_model_calls is not None
            and self._model_calls + calls > self.config.max_model_calls
        ):
            raise ModelCallBudgetExceeded(
                f"{who} would take the run to {self._model_calls + calls} model calls, "
                f"over the declared cap of {self.config.max_model_calls}"
            )
        self._model_calls += calls

    # -- run -----------------------------------------------------------------

    async def run(self) -> RunSummary:
        manifest = self.build_manifest()
        out_dir = Path(self.config.out_dir) / self.config.run_id
        results_path = out_dir / "results.jsonl"
        trace_path = out_dir / "trace.jsonl"

        rows: list[ResultRow] = []
        aborted: BaseException | None = None
        crashed: BaseException | None = None
        self.summary_path = out_dir / "summary.json"
        with (
            ResultWriter(results_path, manifest) as writer,
            TraceWriter(trace_path, self.config.include_trace_content) as tracer,
        ):
            items = iter(self.dataset.items())
            max_items = self.config.max_items
            n_items = 0
            try:
                for item in items:
                    if max_items is not None and n_items >= max_items:
                        break
                    n_items += 1
                    item_rows = await self._run_item(item, tracer)
                    for row in item_rows:
                        writer.write(row)
                        rows.append(row)
            except _Abort as abort:
                # A run that stops early still owes an account of every question
                # it scheduled: the completed rows, then one UNATTEMPTED row per
                # question that never ran. Otherwise the denominator silently
                # shrinks to whatever finished, which flatters the result.
                for row in abort.rows:
                    writer.write(row)
                    rows.append(row)
                for query in abort.remaining:
                    row = self._unattempted(abort.item, query)
                    writer.write(row)
                    rows.append(row)
                # R3-8: "scheduled" means within the run's own caps, so the unrun
                # remainder honours max_items and max_queries_per_item too.
                for item in items:
                    if max_items is not None and n_items >= max_items:
                        break
                    n_items += 1
                    for query in self._capped_queries(item):
                        row = self._unattempted(item, query)
                        writer.write(row)
                        rows.append(row)
                aborted = abort.cause
            except BaseException as exc:
                # C-2: a crash (an adapter error at insert, a kill signal) still owes the
                # spend record; it is written below, marked aborted, and re-raised.
                crashed = exc
                raise
            finally:
                try:
                    summary = aggregate(manifest, rows, self.ledger)
                    writer.write_summary(summary)
                    self._write_summary_file(manifest, summary, aborted=crashed or aborted)
                except Exception:
                    if crashed is None:
                        raise
                    # the original crash matters more than a failed summary
        if aborted is not None:
            raise aborted
        return summary

    def _row_spend(self, before: float | None) -> dict[str, float]:
        """C-2: per-row spend for the result row: this question's metered dollars
        (retrieval, reader, judge) and the run's running total, so a run killed before
        its summary can still be costed from its rows."""
        now = self._spent()
        if now is None or before is None:
            return {}
        return {"spend_usd": round(now - before, 8), "spend_usd_run": round(now, 8)}

    def _unattempted(self, item: EvalItem, query: Query) -> ResultRow:
        return self._blank_row(
            item,
            query,
            status=RowStatus.UNATTEMPTED.value,
            error="run stopped before this question was scheduled",
        )

    def _blank_row(
        self, item: EvalItem, query: Query, status: str, error: str | None = None
    ) -> ResultRow:
        return ResultRow(
            run_id=self.config.run_id,
            item_id=item.item_id,
            query_id=query.query_id,
            question=query.text,
            gold=query.gold,
            answer="",
            score=0.0,
            scale=self.judge.spec.scale.value,
            status=status,
            protocol_id=self.config.protocol.protocol_id,
            dataset_revision=self.dataset.info().revision_id,
            system_id=self.system.system_id,
            seed=self.config.protocol.seed,
            type_label=query.type_label,
            error=error,
        )

    def _write_summary_file(
        self,
        manifest: RunManifest,
        summary: RunSummary,
        aborted: BaseException | None = None,
    ) -> None:
        import json

        payload = {
            # C-2: written on every exit; an aborted run says so and why
            "aborted": aborted is not None,
            "abort_reason": type(aborted).__name__ if aborted is not None else None,
            "abort_message": str(aborted)[:500] if aborted is not None else None,
            "manifest": manifest.to_dict(),
            "summary": summary.to_dict(),
            "score_matrix_row": manifest.score_matrix_row(summary.score_mean, summary.n_queries),
            "judge_model_calls": self._judge_calls,
            "loop_model_calls": self._model_calls - self._judge_calls,
            # per loop stage (D / K / R), per engine LLM role: calls and tokens
            "engine_llm_usage": self.engine_llm,
            # #33: the same engine calls per loop stage, per prompt version
            "engine_prompt_usage": self.engine_prompts,
            # C-6: embedding tokens / rerank searches the engine reported
            "engine_services": self.engine_services,
            # C-5: did the configured reranker run (per query), and how often did it fail
            "rerank": self.rerank_summary(),
            "rerank_unavailable": self.rerank_summary()["rerank_unavailable"],
            "spend": self._meter.summary() if self._meter is not None else None,
        }
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )

    def _capped_queries(self, item: EvalItem) -> list[Query]:
        queries = list(item.queries)
        if self.config.max_queries_per_item is not None:
            queries = queries[: self.config.max_queries_per_item]
        return queries

    async def _run_item(self, item: EvalItem, tracer: TraceWriter) -> list[ResultRow]:
        await self.system.reset(item.item_id)
        queries = self._capped_queries(item)

        # Streaming datasets pin a query to the turn it must follow; everything
        # else runs once the whole history is in.
        pending: dict[str, list[Query]] = {}
        tail: list[Query] = []
        for query in queries:
            if query.after_turn:
                pending.setdefault(query.after_turn, []).append(query)
            else:
                tail.append(query)

        rows: list[ResultRow] = []
        scheduled = [q for turn in item.history for q in pending.get(turn.turn_id, ())] + tail
        done: set[str] = set()
        try:
            for t, turn in enumerate(item.history):
                await self._insert(item.item_id, t, turn, tracer)
                if turn.turn_id in pending:
                    # G9: a system that buffers turns writes them before a
                    # query pinned here, so the query sees the whole prefix.
                    await self._flush(item.item_id, t, turn, tracer)
                for query in pending.get(turn.turn_id, ()):
                    rows.append(await self._answer(item, query, t, tracer))
                    done.add(query.query_id)
            if item.history:
                last = len(item.history) - 1
                await self._flush(item.item_id, last, item.history[last], tracer)
            await self._build(item.item_id)
            for i, query in enumerate(tail):
                rows.append(await self._answer(item, query, len(item.history) + i, tracer))
                done.add(query.query_id)
        except (ModelCallBudgetExceeded, UnexpectedModelCall, ProviderCredentialError) as exc:
            raise _Abort(
                cause=exc,
                item=item,
                rows=rows,
                remaining=[q for q in scheduled if q.query_id not in done],
            ) from exc
        return rows

    async def _guard_auth(self, what: str, call: Callable[[], Any]) -> Any:
        """Await ``call()``; a credential failure becomes ``ProviderCredentialError`` (C-3)."""
        try:
            return await call()
        except (ModelCallBudgetExceeded, UnexpectedModelCall, ProviderCredentialError):
            raise
        except Exception as exc:
            if is_auth_error(exc):
                raise ProviderCredentialError(
                    f"{what}: credentials rejected ({type(exc).__name__}: {str(exc)[:300]})"
                ) from exc
            raise

    async def _insert(self, item_id: str, t: int, turn: Turn, tracer: TraceWriter) -> None:
        self._check_spend(f"{self.system.system_id}.insert")
        started = time.perf_counter()
        deposit = await self._guard_auth(
            f"{self.system.system_id}.insert", lambda: self.system.insert(turn)
        )
        latency = (time.perf_counter() - started) * 1000
        self._record_deposit(item_id, t, turn, deposit, latency, tracer)

    async def _flush(self, item_id: str, t: int, turn: Turn, tracer: TraceWriter) -> None:
        """Optional adapter hook (G9): write turns the system has buffered. Its
        cost is deposit (D), traced under the turn that triggered the flush."""
        flush = getattr(self.system, "flush", None)
        if flush is None:
            return
        self._check_spend(f"{self.system.system_id}.flush")
        started = time.perf_counter()
        deposit = await self._guard_auth(f"{self.system.system_id}.flush", flush)
        if not deposit.n_records and not deposit.model_calls and not deposit.meta:
            return  # nothing was buffered
        latency = (time.perf_counter() - started) * 1000
        self._record_deposit(item_id, t, turn, deposit, latency, tracer)

    def _record_deposit(
        self,
        item_id: str,
        t: int,
        turn: Turn,
        deposit: DepositResult,
        latency: float,
        tracer: TraceWriter,
    ) -> None:
        self._charge_engine(Stage.DEPOSIT, deposit.meta)
        self._charge_services(deposit.meta)
        self._account_model_calls(deposit.model_calls, f"{self.system.system_id}.insert")
        self.ledger.add(
            Stage.DEPOSIT,
            calls=deposit.model_calls,
            latency_ms=deposit.latency_ms or latency,
        )
        # An adapter that cannot see its own write-path cost says so, and the
        # ledger stops claiming a complete total (loop-metric contract §5).
        if deposit.meta.get("cost_observable") is False:
            self.ledger.mark_unknown(
                Stage.DEPOSIT,
                str(deposit.meta.get("cost_unknown_reason", "adapter cannot observe deposit cost")),
            )
        tracer.write(
            DepositTrace(
                item_id=item_id,
                turn_id=turn.turn_id,
                t=t,
                n_records=deposit.n_records,
                record_ids=deposit.record_ids,
                latency_ms=deposit.latency_ms or latency,
                model_calls=deposit.model_calls,
                meta=deposit.meta,
            )
        )

    async def _build(self, item_id: str) -> None:
        """Optional adapter hook between ingestion and the tail queries (e.g. a
        sleep cycle). Its cost is synthesis (K), not deposit."""
        build = getattr(self.system, "build", None)
        if build is None:
            return
        # R3-10: the build hook's calls are charged only after it returns, so a
        # system that can bound itself is told what is left before it starts.
        if hasattr(self.system, "remaining_model_calls"):
            cap = self.config.max_model_calls
            self.system.remaining_model_calls = (  # type: ignore[attr-defined]
                None if cap is None else max(cap - self._model_calls, 0)
            )
        self._check_spend(f"{self.system.system_id}.build")
        started = time.perf_counter()
        result = await self._guard_auth(f"{self.system.system_id}.build", build)
        self._charge_engine(Stage.SYNTHESISE, result.meta)
        self._charge_services(result.meta)
        self._account_model_calls(result.model_calls, f"{self.system.system_id}.build")
        self.ledger.add(
            Stage.SYNTHESISE,
            calls=result.model_calls,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        if result.meta.get("cost_observable") is False:
            self.ledger.mark_unknown(
                Stage.SYNTHESISE,
                str(result.meta.get("cost_unknown_reason", "adapter cannot observe build cost")),
            )

    async def _answer(self, item: EvalItem, query: Query, t: int, tracer: TraceWriter) -> ResultRow:
        protocol = self.config.protocol
        spend_before = self._spent()
        try:
            self._check_spend(f"{self.system.system_id}.query")
            started = time.perf_counter()
            context = await self.system.query(query.text, protocol.budget_tokens, protocol.top_k)
            latency_retrieve = (time.perf_counter() - started) * 1000
            self._charge_engine(Stage.RETRIEVE, context.meta)
            self._charge_services(context.meta)
            self._audit_rerank(context.meta)

            # The protocol owns the budget, not the system: truncate here even
            # when the system says it already did.
            text, tokens, truncated = truncate_to_budget(
                context.text, protocol.budget_tokens, self.counter
            )
            if truncated:
                # R3-7: evidence whose unit was cut away is not retrieved evidence.
                context = type(context)(
                    text=text,
                    tokens=tokens,
                    evidence=visible_evidence(context.evidence, len(text)),
                    truncated=True,
                    boundary_index=context.boundary_index,
                    meta=context.meta,
                )
            self.ledger.add(
                Stage.RETRIEVE,
                latency_ms=latency_retrieve,
                calls=int(context.meta.get("model_calls", 0)),
            )
            if context.meta.get("cost_observable") is False:
                self.ledger.mark_unknown(
                    Stage.RETRIEVE,
                    str(context.meta.get("cost_unknown_reason", "query cost not observable")),
                )
            self._account_model_calls(
                int(context.meta.get("model_calls", 0)), f"{self.system.system_id}.query"
            )
            self.ledger.add(Stage.COMPOSE, prompt_tokens=context.tokens)

            if query.meta.get("abstention") and not getattr(
                self.judge, "handles_abstention", False
            ):
                # R3-1: an unanswerable question's gold is a refusal; a judge that
                # only compares strings cannot grade it, and must not be asked to.
                raise ValueError(
                    f"judge {self.judge.spec.judge_id!r} cannot grade abstention questions; "
                    "exclude them (--categories) or use an abstention-aware judge"
                )

            # R3-6: the date the question is asked, for readers whose prompt uses it.
            question_date = query.meta.get("question_date")
            if self._reader_takes_date:
                answer = await self.reader.answer(
                    query.text,
                    context.text,
                    question_date=None if question_date is None else str(question_date),
                )
            else:
                answer = await self.reader.answer(query.text, context.text)
            if self._own_meter and answer.model_calls:
                # a reader without a provider budget is charged after the fact
                self._meter.charge(
                    self.reader.model, answer.prompt_tokens, answer.completion_tokens
                )
            self._account_model_calls(answer.model_calls, self.reader.reader_id)
            self.ledger.add(
                Stage.GENERATE,
                prompt_tokens=answer.prompt_tokens,
                completion_tokens=answer.completion_tokens,
                calls=answer.model_calls,
                latency_ms=answer.latency_ms,
                cost_usd=self.config.cost_model.cost(
                    self.reader.model, answer.prompt_tokens, answer.completion_tokens
                ),
            )

            score_query = getattr(self.judge, "score_query", None)
            if score_query is not None:
                verdict = await score_query(query, answer.text)
            else:
                verdict = await self.judge.score(query.text, answer.text, query.gold)
            self._judge_calls += verdict.model_calls
            self._account_model_calls(verdict.model_calls, self.judge.spec.judge_id)

            retrieved_ids = tuple(dict.fromkeys(e.turn_id for e in context.evidence))
            # R@k is defined over a *ranking*. Full-context replay returns turns
            # in recency order, so scoring it would hand the baseline a free
            # perfect recall and make every comparison against it meaningless.
            # R3-7: k counts ranked units (a chunk is one unit, expanded to all of
            # its turns), "R@k" is any-hit and "R_all@k" needs every gold turn.
            ranked = bool(context.meta.get("ranked", True))
            units = unit_ranking(context.evidence)
            recall: dict[str, float | None] = {}
            for k in protocol.recall_ks:
                recall[f"R@{k}"] = (
                    recall_over_units(units, query.gold_turn_ids, k) if ranked else None
                )
                recall[f"R_all@{k}"] = (
                    recall_over_units(units, query.gold_turn_ids, k, require_all=True)
                    if ranked
                    else None
                )

            tracer.write(
                cycle_from_context(
                    item_id=item.item_id,
                    query_id=query.query_id,
                    t=t,
                    context=context,
                    answer_text=answer.text,
                    prompt_tokens=answer.prompt_tokens,
                    completion_tokens=answer.completion_tokens,
                    latency_retrieve_ms=latency_retrieve,
                    latency_answer_ms=answer.latency_ms,
                    model_calls=answer.model_calls + verdict.model_calls,
                    include_content=self.config.include_trace_content,
                )
            )
            return ResultRow(
                run_id=self.config.run_id,
                item_id=item.item_id,
                query_id=query.query_id,
                question=query.text,
                gold=query.gold,
                answer=answer.text,
                score=verdict.score,
                scale=verdict.scale.value,
                status=(
                    RowStatus.TRUNCATED.value if answer.truncated else RowStatus.COMPLETED.value
                ),
                protocol_id=protocol.protocol_id,
                dataset_revision=self.dataset.info().revision_id,
                system_id=self.system.system_id,
                seed=protocol.seed,
                answer_truncated=answer.truncated,
                type_label=query.type_label,
                context_tokens=context.tokens,
                context_truncated=context.truncated,
                prompt_tokens=answer.prompt_tokens,
                cached_prompt_tokens=answer.cached_prompt_tokens,
                completion_tokens=answer.completion_tokens,
                latency_retrieve_ms=latency_retrieve,
                latency_answer_ms=answer.latency_ms,
                latency_judge_ms=verdict.latency_ms,
                model_calls=answer.model_calls + verdict.model_calls,
                retrieved_ids=retrieved_ids,
                recall=recall,
                meta={
                    **({"judge_raw": verdict.raw[:200]} if verdict.raw else {}),
                    **(
                        {"judge_prompt_id": verdict.meta["prompt_id"]}
                        if verdict.meta.get("prompt_id")
                        else {}
                    ),
                    **(
                        {"reranked": context.meta["reranked"]} if "reranked" in context.meta else {}
                    ),
                    **self._row_spend(spend_before),
                },
            )
        except (ModelCallBudgetExceeded, UnexpectedModelCall, ProviderCredentialError):
            raise
        except Exception as exc:
            if is_auth_error(exc):
                # C-3: an expired / invalid credential fails every later call too; stop
                # the run (UNATTEMPTED rest) instead of writing ERROR rows until the end.
                raise ProviderCredentialError(
                    f"{self.system.system_id}.answer: credentials rejected "
                    f"({type(exc).__name__}: {str(exc)[:300]})"
                ) from exc
            if self.config.fail_fast:
                raise
            # A failed question stays in the file and in the denominator. It is
            # not scored as wrong: an error is a missing measurement, and the
            # two are only the same if you never look.
            return self._blank_row(
                item,
                query,
                status=RowStatus.ERROR.value,
                error=f"{type(exc).__name__}: {exc}",
            )


async def run_matrix(
    dataset: DatasetAdapter,
    systems: Sequence[SystemAdapter],
    reader: Reader,
    judge: Judge,
    config: RunConfig,
    token_counter: TokenCounter | None = None,
    reader_judge_factory: Callable[[], tuple[Reader, Judge]] | None = None,
) -> list[RunSummary]:
    """Run several systems over one dataset under one protocol.

    This is the shape every comparison in the papers needs, and the shape no
    published table in the field currently has: the protocol object is shared
    by construction, so the systems cannot silently differ in budget, reader,
    judge or seed.

    R3-3: every arm gets the same caps, never the remainder of another arm's.
    ``max_model_calls`` is counted per arm; a provider-side budget is per arm when
    ``reader_judge_factory`` builds a fresh reader and judge for each one. An arm
    that exhausts its cap stops with UNATTEMPTED rows, the remaining arms still
    run, and the first budget error is raised once all of them have.
    """
    summaries: list[RunSummary] = []
    budget_error: ModelCallBudgetExceeded | None = None
    for system in systems:
        per_system = RunConfig(
            run_id=f"{config.run_id}--{system.system_id}",
            protocol=config.protocol,
            out_dir=config.out_dir,
            cost_model=config.cost_model,
            include_trace_content=config.include_trace_content,
            max_items=config.max_items,
            max_queries_per_item=config.max_queries_per_item,
            max_model_calls=config.max_model_calls,
            expect_model_calls=config.expect_model_calls,
            fail_fast=config.fail_fast,
            labels=config.labels,
            max_usd=config.max_usd,
            prices_per_mtok=config.prices_per_mtok,
            service_prices=config.service_prices,
            extra_limits={
                **dict(config.extra_limits),
                "model_call_cap_scope": "per-arm",
                "provider_budget_scope": (
                    "per-arm" if reader_judge_factory is not None else "shared-by-caller"
                ),
            },
        )
        arm_reader, arm_judge = (
            reader_judge_factory() if reader_judge_factory is not None else (reader, judge)
        )
        runner = EvalRunner(dataset, system, arm_reader, arm_judge, per_system, token_counter)
        try:
            summaries.append(await runner.run())
        except ModelCallBudgetExceeded as exc:
            budget_error = budget_error or exc
        finally:
            await system.close()
    if budget_error is not None:
        raise budget_error
    return summaries
