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

import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import DatasetAdapter, EvalItem, Query, Reader, SystemAdapter, Turn
from .judge import Judge, recall_at_k
from .metrics import CostModel, Ledger, Stage
from .provenance import ReaderSpec, RunManifest, RunProtocol, SystemSpec
from .results import ResultRow, ResultWriter, RowStatus, RunSummary, aggregate
from .tokens import HeuristicTokenCounter, TokenCounter, truncate_to_budget
from .trace import DepositTrace, TraceWriter, cycle_from_context


class ModelCallBudgetExceeded(RuntimeError):
    """Raised before the call that would breach ``max_model_calls``."""


class UnexpectedModelCall(RuntimeError):
    """Raised when a run declared offline makes a model call anyway."""


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
        )

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
        with (
            ResultWriter(results_path, manifest) as writer,
            TraceWriter(trace_path, self.config.include_trace_content) as tracer,
        ):
            items = self.dataset.items()
            try:
                for n_items, item in enumerate(items):
                    if self.config.max_items is not None and n_items >= self.config.max_items:
                        break
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
                for item in items:
                    for query in item.queries:
                        row = self._unattempted(item, query)
                        writer.write(row)
                        rows.append(row)
                aborted = abort.cause
            summary = aggregate(manifest, rows, self.ledger)
            writer.write_summary(summary)
        self.summary_path = out_dir / "summary.json"
        self._write_summary_file(manifest, summary)
        if aborted is not None:
            raise aborted
        return summary

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

    def _write_summary_file(self, manifest: RunManifest, summary: RunSummary) -> None:
        import json

        payload = {
            "manifest": manifest.to_dict(),
            "summary": summary.to_dict(),
            "score_matrix_row": manifest.score_matrix_row(summary.score_mean, summary.n_queries),
            "judge_model_calls": self._judge_calls,
            "loop_model_calls": self._model_calls - self._judge_calls,
        }
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )

    async def _run_item(self, item: EvalItem, tracer: TraceWriter) -> list[ResultRow]:
        await self.system.reset(item.item_id)
        queries = list(item.queries)
        if self.config.max_queries_per_item is not None:
            queries = queries[: self.config.max_queries_per_item]

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
                for query in pending.get(turn.turn_id, ()):
                    rows.append(await self._answer(item, query, t, tracer))
                    done.add(query.query_id)
            for i, query in enumerate(tail):
                rows.append(await self._answer(item, query, len(item.history) + i, tracer))
                done.add(query.query_id)
        except (ModelCallBudgetExceeded, UnexpectedModelCall) as exc:
            raise _Abort(
                cause=exc,
                item=item,
                rows=rows,
                remaining=[q for q in scheduled if q.query_id not in done],
            ) from exc
        return rows

    async def _insert(self, item_id: str, t: int, turn: Turn, tracer: TraceWriter) -> None:
        started = time.perf_counter()
        deposit = await self.system.insert(turn)
        latency = (time.perf_counter() - started) * 1000
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

    async def _answer(self, item: EvalItem, query: Query, t: int, tracer: TraceWriter) -> ResultRow:
        protocol = self.config.protocol
        try:
            started = time.perf_counter()
            context = await self.system.query(query.text, protocol.budget_tokens, protocol.top_k)
            latency_retrieve = (time.perf_counter() - started) * 1000

            # The protocol owns the budget, not the system: truncate here even
            # when the system says it already did.
            text, tokens, truncated = truncate_to_budget(
                context.text, protocol.budget_tokens, self.counter
            )
            if truncated:
                context = type(context)(
                    text=text,
                    tokens=tokens,
                    evidence=context.evidence,
                    truncated=True,
                    boundary_index=context.boundary_index,
                    meta=context.meta,
                )
            self.ledger.add(
                Stage.RETRIEVE,
                latency_ms=latency_retrieve,
                calls=int(context.meta.get("model_calls", 0)),
            )
            self._account_model_calls(
                int(context.meta.get("model_calls", 0)), f"{self.system.system_id}.query"
            )
            self.ledger.add(Stage.COMPOSE, prompt_tokens=context.tokens)

            answer = await self.reader.answer(query.text, context.text)
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

            verdict = await self.judge.score(query.text, answer.text, query.gold)
            self._judge_calls += verdict.model_calls
            self._account_model_calls(verdict.model_calls, self.judge.spec.judge_id)

            retrieved_ids = tuple(e.turn_id for e in context.evidence)
            # R@k is defined over a *ranking*. Full-context replay returns turns
            # in recency order, so scoring it would hand the baseline a free
            # perfect recall and make every comparison against it meaningless.
            ranked = bool(context.meta.get("ranked", True))
            recall: dict[str, float | None] = {
                f"R@{k}": (recall_at_k(retrieved_ids, query.gold_turn_ids, k) if ranked else None)
                for k in protocol.recall_ks
            }

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
            )
        except (ModelCallBudgetExceeded, UnexpectedModelCall):
            raise
        except Exception as exc:
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
) -> list[RunSummary]:
    """Run several systems over one dataset under one protocol.

    This is the shape every comparison in the papers needs, and the shape no
    published table in the field currently has: the protocol object is shared
    by construction, so the systems cannot silently differ in budget, reader,
    judge or seed.
    """
    summaries: list[RunSummary] = []
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
        )
        runner = EvalRunner(dataset, system, reader, judge, per_system, token_counter)
        try:
            summaries.append(await runner.run())
        finally:
            await system.close()
    return summaries
