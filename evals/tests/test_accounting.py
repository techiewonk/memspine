"""Unit tests for the cost-accounting arithmetic.

Nothing in the harness can be run end to end yet, but the arithmetic behind the
cost-per-cycle metric and the frontier table can be — and has to be, because a wrong
denominator here would silently corrupt every published number. A fake clock makes the
timing deterministic, so these are exact-value assertions, not tolerances.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# evals/ sits at the repo root, outside the wheel (D-19/D-35), so it is not on sys.path
# via the installed package. Add the repo root explicitly; `evals` then imports as a
# namespace package. Same shim as the sibling test modules.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.harness.accounting import (  # noqa: E402
    ACCOUNTING_SCHEMA_VERSION,
    CostBucket,
    CostLedger,
    CostReport,
    CountingLLM,
    Phase,
    Side,
    Stage,
    TokenUsage,
    estimate_tokens,
    frontier_row,
)


def dig(payload: object, *keys: str) -> object:
    """Walk a nested JSON-shaped payload, narrowing at each step."""
    for key in keys:
        assert isinstance(payload, dict), f"expected a dict at {key!r}, got {type(payload)}"
        payload = payload[key]
    return payload


class FakeClock:
    """Monotonic clock advanced explicitly by the test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeLLM:
    """Minimal stand-in for the LLM port: returns a fixed completion."""

    def __init__(self, completion: str = "ok") -> None:
        self.completion = completion
        self.calls: list[list[dict[str, str]]] = []

    @property
    def provider_id(self) -> str:
        return "fake:test-model"

    async def chat(self, messages: list[dict[str, str]], **options: object) -> str:
        self.calls.append(messages)
        return self.completion


# --------------------------------------------------------------------------- TokenUsage


def test_token_usage_totals_and_addition() -> None:
    a = TokenUsage(prompt_tokens=100, completion_tokens=20, cached_tokens=40, calls=1)
    b = TokenUsage(prompt_tokens=50, completion_tokens=5, calls=1, estimated_calls=1)
    assert a.total_tokens == 120
    assert a.billable_tokens == 80
    total = a + b
    assert total == TokenUsage(
        prompt_tokens=150, completion_tokens=25, cached_tokens=40, calls=2, estimated_calls=1
    )
    assert total.total_tokens == 175
    assert total.billable_tokens == 135


def test_token_usage_measurement_verdict() -> None:
    assert TokenUsage().measurement == "none"
    assert TokenUsage(calls=3).measurement == "exact"
    assert TokenUsage(calls=3, estimated_calls=3).measurement == "estimated"
    assert TokenUsage(calls=3, estimated_calls=1).measurement == "mixed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt_tokens": -1},
        {"cached_tokens": 5, "prompt_tokens": 1},
        {"calls": 1, "estimated_calls": 2},
    ],
)
def test_token_usage_rejects_impossible_values(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        TokenUsage(**kwargs)


def test_from_usage_mapping_openai_shape() -> None:
    usage = TokenUsage.from_usage_mapping(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 120,
            "total_tokens": 1120,
            "prompt_tokens_details": {"cached_tokens": 768},
        }
    )
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (1000, 120, 768)
    assert usage.billable_tokens == 352
    assert usage.measurement == "exact"


def test_from_usage_mapping_anthropic_shape_folds_cache_into_prompt() -> None:
    # Anthropic counts cache reads/creations OUTSIDE input_tokens; folding them in keeps
    # cached_tokens a subset of prompt_tokens and the total comparable across providers.
    usage = TokenUsage.from_usage_mapping(
        {
            "input_tokens": 200,
            "output_tokens": 50,
            "cache_read_input_tokens": 700,
            "cache_creation_input_tokens": 100,
        }
    )
    assert usage.prompt_tokens == 1000
    assert usage.cached_tokens == 700
    assert usage.total_tokens == 1050
    assert usage.billable_tokens == 350


def test_from_usage_mapping_rejects_unknown_shape() -> None:
    with pytest.raises(ValueError, match="unrecognised usage mapping"):
        TokenUsage.from_usage_mapping({"tokens": 10})


def test_estimate_tokens_is_ceiling_of_chars_over_four() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# ------------------------------------------------------------------- span attribution


def test_stage_span_times_and_attributes() -> None:
    clock = FakeClock()
    ledger = CostLedger(clock=clock)
    with ledger.stage(Stage.RETRIEVE, "search") as span:
        clock.advance(0.25)
        span.record_llm(prompt_tokens=30, completion_tokens=10, seconds=0.2)

    report = ledger.report()
    bucket = report.by_stage[Stage.RETRIEVE]
    assert bucket.spans == 1
    assert bucket.self_seconds == pytest.approx(0.25)
    assert bucket.llm_seconds == pytest.approx(0.2)
    assert bucket.total_tokens == 40
    # Default side for retrieve is read, so the phase is query.
    assert report.by_side[Side.READ].total_tokens == 40
    assert report.by_phase[Phase.QUERY].total_tokens == 40


def test_nested_spans_do_not_double_count_time() -> None:
    clock = FakeClock()
    ledger = CostLedger(clock=clock)
    with ledger.stage(Stage.COMPOSE, "assemble"):
        clock.advance(0.1)
        with ledger.stage(Stage.RETRIEVE, "inner"):
            clock.advance(0.4)
        clock.advance(0.1)

    report = ledger.report()
    compose = report.by_stage[Stage.COMPOSE]
    retrieve = report.by_stage[Stage.RETRIEVE]
    assert compose.wall_seconds == pytest.approx(0.6)
    assert compose.self_seconds == pytest.approx(0.2)  # 0.6 wall minus the 0.4 child
    assert retrieve.self_seconds == pytest.approx(0.4)
    # self_seconds is the partition that sums to the true elapsed time.
    assert report.total.self_seconds == pytest.approx(0.6)


def test_side_override_puts_a_write_path_lookup_on_the_write_side() -> None:
    clock = FakeClock()
    ledger = CostLedger(clock=clock)
    # A dedup lookup during a write is read-shaped work on the write side.
    with ledger.stage(Stage.RETRIEVE, "dedup_probe", side=Side.WRITE) as span:
        clock.advance(0.05)
        span.record_llm(prompt_tokens=8, completion_tokens=2)

    report = ledger.report()
    assert report.by_stage[Stage.RETRIEVE].total_tokens == 8 + 2
    assert report.by_side[Side.WRITE].total_tokens == 10
    assert Side.READ not in report.by_side
    assert report.by_phase[Phase.INGEST].total_tokens == 10


def test_synthesise_stage_is_consolidation_without_an_explicit_pass() -> None:
    ledger = CostLedger(clock=FakeClock())
    with ledger.stage(Stage.SYNTHESISE, "summarise") as span:
        span.record_llm(prompt_tokens=500, completion_tokens=100)

    report = ledger.report()
    assert report.by_phase[Phase.CONSOLIDATION].total_tokens == 600
    assert Phase.INGEST not in report.by_phase


def test_consolidation_pass_scope_reclassifies_contained_spans() -> None:
    ledger = CostLedger(clock=FakeClock())
    with ledger.consolidation_pass(), ledger.stage(Stage.RETRIEVE, "gather") as span:
        span.record_llm(prompt_tokens=40, completion_tokens=0)

    report = ledger.report()
    assert report.consolidation_passes == 1
    # The stage is unchanged — this really was a retrieve. Both the phase AND the side
    # move, because consolidation is construction cost: a candidate-gathering retrieve
    # inside a sleep cycle billed to the read side would inflate the query-leg figure
    # that gets compared against peer systems and deflate the layer being priced.
    assert report.by_stage[Stage.RETRIEVE].total_tokens == 40
    assert report.by_side[Side.WRITE].total_tokens == 40
    assert Side.READ not in report.by_side
    assert report.by_phase[Phase.CONSOLIDATION].total_tokens == 40
    assert Phase.QUERY not in report.by_phase


def test_an_explicit_side_still_wins_inside_consolidation() -> None:
    """The default moves; the override does not. A caller who knows better is obeyed."""
    ledger = CostLedger(clock=FakeClock())
    with (
        ledger.consolidation_pass(),
        ledger.stage(Stage.RETRIEVE, "gather", side=Side.READ) as span,
    ):
        span.record_llm(prompt_tokens=40, completion_tokens=0)

    report = ledger.report()
    assert report.by_side[Side.READ].total_tokens == 40
    assert report.by_phase[Phase.CONSOLIDATION].total_tokens == 40


def test_turn_and_query_indices_are_stamped_on_spans() -> None:
    ledger = CostLedger(clock=FakeClock())
    with ledger.turn() as turn_index, ledger.stage(Stage.DEPOSIT, "write"):
        assert turn_index == 0
    with ledger.query(7), ledger.stage(Stage.GENERATE, "answer"):
        pass

    spans = {span.label: span for span in ledger.spans}
    assert spans["write"].turn_index == 0
    assert spans["write"].query_index is None
    assert spans["answer"].query_index == 7


def test_unattributed_llm_calls_are_kept_visible_not_dropped() -> None:
    ledger = CostLedger(clock=FakeClock())
    ledger.record_llm(prompt_tokens=11, completion_tokens=3)
    report = ledger.report()
    assert report.unattributed.total_tokens == 14
    assert report.by_stage == {}
    # Totals still balance: stage partition plus the leak.
    assert report.total.total_tokens == 14
    assert frontier_row(config="x", report=report)["unattributed_tokens"] == 14


def test_totals_equal_each_partition_plus_unattributed() -> None:
    ledger = _three_stage_ledger()
    ledger.record_llm(prompt_tokens=5, completion_tokens=5)
    report = ledger.report()

    stage_sum = sum(bucket.total_tokens for bucket in report.by_stage.values())
    side_sum = sum(bucket.total_tokens for bucket in report.by_side.values())
    phase_sum = sum(bucket.total_tokens for bucket in report.by_phase.values())
    assert stage_sum == side_sum == phase_sum
    assert report.total.total_tokens == stage_sum + report.unattributed.total_tokens


# ------------------------------------------------------------------------- decorators


def test_measure_decorator_sync() -> None:
    clock = FakeClock()
    ledger = CostLedger(clock=clock)

    @ledger.measure(Stage.COMPOSE, "compose_ctx")
    def build(n: int) -> int:
        clock.advance(0.3)
        return n * 2

    assert build(21) == 42
    assert ledger.report().by_stage[Stage.COMPOSE].self_seconds == pytest.approx(0.3)


async def test_measure_decorator_async() -> None:
    clock = FakeClock()
    ledger = CostLedger(clock=clock)

    @ledger.measure(Stage.RETRIEVE)
    async def search(q: str) -> str:
        clock.advance(0.5)
        return q.upper()

    assert await search("hi") == "HI"
    bucket = ledger.report().by_stage[Stage.RETRIEVE]
    assert bucket.spans == 1
    assert bucket.self_seconds == pytest.approx(0.5)


async def test_concurrent_tasks_nest_independently() -> None:
    # Nesting is tracked with context variables, so two asyncio tasks must not see each
    # other's open span. If they did, tokens would be attributed to the wrong stage.
    ledger = CostLedger()

    async def one(stage: Stage, tokens: int) -> None:
        with ledger.stage(stage, f"span_{stage.value}") as span:
            await asyncio.sleep(0)
            span.record_llm(prompt_tokens=tokens, completion_tokens=0)
            await asyncio.sleep(0)

    await asyncio.gather(one(Stage.RETRIEVE, 10), one(Stage.DEPOSIT, 20))

    report = ledger.report()
    assert report.by_stage[Stage.RETRIEVE].total_tokens == 10
    assert report.by_stage[Stage.DEPOSIT].total_tokens == 20
    # Neither became the other's child, so both keep their full duration as self time.
    assert all(span.self_seconds == span.wall_seconds for span in report.spans)


# ------------------------------------------------------------------- cost per cycle


def _three_stage_ledger() -> CostLedger:
    """Two turns of ingest, one consolidation pass, two queries — fixed token counts."""
    clock = FakeClock()
    ledger = CostLedger(clock=clock)

    for _ in range(2):
        with ledger.turn(), ledger.stage(Stage.DEPOSIT, "write") as span:
            clock.advance(0.1)
            span.record_llm(prompt_tokens=80, completion_tokens=20)  # 100 per turn

    with ledger.consolidation_pass(), ledger.stage(Stage.SYNTHESISE, "consolidate") as span:
        clock.advance(1.0)
        span.record_llm(prompt_tokens=800, completion_tokens=200)  # 1000 for the pass

    for _ in range(2):
        with ledger.query():
            with ledger.stage(Stage.RETRIEVE, "search"):
                clock.advance(0.05)
            with ledger.stage(Stage.GENERATE, "answer") as span:
                clock.advance(0.2)
                span.record_llm(prompt_tokens=2000, completion_tokens=100)  # 2100 per query
    return ledger


def test_amortised_cost_per_cycle() -> None:
    report = _three_stage_ledger()
    cycle = report.report().per_cycle

    assert cycle.turns == 2
    assert cycle.queries == 2
    assert cycle.consolidation_passes == 1
    assert cycle.ingest_tokens_per_turn == pytest.approx(100.0)  # 200 / 2
    assert cycle.consolidation_tokens_per_turn == pytest.approx(500.0)  # 1000 / 2
    assert cycle.consolidation_tokens_per_pass == pytest.approx(1000.0)
    assert cycle.query_tokens_per_query == pytest.approx(2100.0)  # 4200 / 2
    assert cycle.tokens_per_cycle == pytest.approx(2700.0)  # 100 + 500 + 2100

    assert cycle.ingest_seconds_per_turn == pytest.approx(0.1)
    assert cycle.consolidation_seconds_per_turn == pytest.approx(0.5)
    assert cycle.query_seconds_per_query == pytest.approx(0.25)
    assert cycle.seconds_per_cycle == pytest.approx(0.85)


def test_write_and_read_side_separation() -> None:
    report = _three_stage_ledger().report()
    assert report.by_side[Side.WRITE].total_tokens == 1200  # 200 ingest + 1000 consolidation
    assert report.by_side[Side.READ].total_tokens == 4200
    assert report.by_phase[Phase.INGEST].total_tokens == 200
    assert report.by_phase[Phase.CONSOLIDATION].total_tokens == 1000
    assert report.by_phase[Phase.QUERY].total_tokens == 4200


def test_undefined_ratio_is_none_not_zero() -> None:
    ledger = CostLedger(clock=FakeClock())
    with ledger.stage(Stage.GENERATE, "answer") as span:
        span.record_llm(prompt_tokens=10, completion_tokens=1)  # cost, but no query counted

    cycle = ledger.report().per_cycle
    assert cycle.queries == 0
    assert cycle.query_tokens_per_query is None
    assert cycle.tokens_per_cycle is None  # an undefined leg poisons the sum, by design
    assert cycle.ingest_tokens_per_turn == pytest.approx(0.0)  # 0 cost over 0 turns is 0


def test_zero_cost_over_zero_units_is_zero() -> None:
    cycle = CostLedger(clock=FakeClock()).report().per_cycle
    assert cycle.tokens_per_cycle == pytest.approx(0.0)
    assert cycle.seconds_per_cycle == pytest.approx(0.0)


def test_bulk_counters_feed_the_same_denominators() -> None:
    ledger = CostLedger(clock=FakeClock())
    with ledger.stage(Stage.DEPOSIT, "bulk_write") as span:
        span.record_llm(prompt_tokens=900, completion_tokens=100)
    ledger.add_turns(10)

    assert ledger.report().per_cycle.ingest_tokens_per_turn == pytest.approx(100.0)


def test_add_turns_rejects_negative() -> None:
    with pytest.raises(ValueError):
        CostLedger(clock=FakeClock()).add_turns(-1)


# ---------------------------------------------------------------------------- latency


def test_latency_percentiles_are_nearest_rank() -> None:
    clock = FakeClock()
    ledger = CostLedger(clock=clock)
    for seconds in (0.1, 0.2, 0.3, 0.4, 1.0):
        with ledger.query():
            clock.advance(seconds)

    latency = ledger.report().query_latency
    assert latency.count == 5
    assert latency.total_seconds == pytest.approx(2.0)
    assert latency.mean_seconds == pytest.approx(0.4)
    assert latency.p50_seconds == pytest.approx(0.3)  # rank ceil(0.5*5)=3 -> 3rd sample
    assert latency.p95_seconds == pytest.approx(1.0)  # rank ceil(0.95*5)=5 -> 5th sample
    assert latency.max_seconds == pytest.approx(1.0)


def test_serialised_seconds_keep_microsecond_resolution() -> None:
    # Regression: seconds ratios were once rounded at token precision (4 dp), which
    # reported a sub-millisecond retrieve as costing 0.0 — free, rather than fast.
    clock = FakeClock()
    ledger = CostLedger(clock=clock)
    with ledger.query(), ledger.stage(Stage.RETRIEVE, "search"):
        clock.advance(0.000022)

    per_cycle = ledger.report().to_dict()["per_cycle"]
    assert isinstance(per_cycle, dict)
    assert per_cycle["query_seconds_per_query"] == pytest.approx(0.000022)
    assert per_cycle["seconds_per_cycle"] == pytest.approx(0.000022)


def test_latency_summary_of_no_samples_is_none_not_zero() -> None:
    latency = CostLedger(clock=FakeClock()).report().query_latency
    assert latency.count == 0
    assert latency.mean_seconds is None
    assert latency.p95_seconds is None


# ------------------------------------------------------------------------- LLM wrapper


async def test_counting_llm_estimates_and_flags_when_port_reports_no_usage() -> None:
    ledger = CostLedger(clock=FakeClock())
    inner = FakeLLM(completion="12345678")  # 8 chars -> 2 estimated tokens
    wrapped = CountingLLM(inner, ledger)

    with ledger.stage(Stage.DEPOSIT, "extract"):
        answer = await wrapped.chat([{"role": "user", "content": "abcd"}])  # 4 chars -> 1 token

    assert answer == "12345678"
    report = ledger.report()
    bucket = report.by_stage[Stage.DEPOSIT]
    assert bucket.usage.prompt_tokens == 1
    assert bucket.usage.completion_tokens == 2
    assert bucket.usage.measurement == "estimated"
    assert wrapped.provider_id == "fake:test-model"
    assert report.models == ("fake:test-model",)


async def test_counting_llm_uses_exact_usage_when_a_reader_is_supplied() -> None:
    ledger = CostLedger(clock=FakeClock())
    exact = TokenUsage(prompt_tokens=1234, completion_tokens=56, calls=1)
    wrapped = CountingLLM(FakeLLM(), ledger, model="gpt-x", usage_reader=lambda _inner: exact)

    with ledger.stage(Stage.GENERATE, "answer"):
        await wrapped.chat([{"role": "user", "content": "anything"}])

    bucket = ledger.report().by_stage[Stage.GENERATE]
    assert bucket.usage.prompt_tokens == 1234
    assert bucket.usage.completion_tokens == 56
    assert bucket.usage.measurement == "exact"
    assert ledger.report().models == ("gpt-x",)


# --------------------------------------------------------------------- merge + schema


def test_merge_recomputes_ratios_from_pooled_numerators() -> None:
    def sample(turns: int, tokens: int) -> CostReport:
        ledger = CostLedger(clock=FakeClock())
        with ledger.stage(Stage.DEPOSIT, "write") as span:
            span.record_llm(prompt_tokens=tokens, completion_tokens=0)
        ledger.add_turns(turns)
        return ledger.report()

    a = sample(turns=1, tokens=100)  # 100 tokens/turn
    b = sample(turns=9, tokens=900)  # 100 tokens/turn
    merged = CostReport.merge([a, b])

    assert merged.turns == 10
    assert merged.by_phase[Phase.INGEST].total_tokens == 1000
    assert merged.per_cycle.ingest_tokens_per_turn == pytest.approx(100.0)

    # Averaging the per-sample ratios would give the same answer here only because the
    # samples happen to agree; pooling is what makes unequal samples come out right.
    c = sample(turns=1, tokens=1000)  # 1000 tokens/turn
    pooled = CostReport.merge([b, c]).per_cycle.ingest_tokens_per_turn
    assert pooled == pytest.approx(190.0)  # 1900 tokens / 10 turns, not (100+1000)/2


def test_merge_concatenates_latency_samples_and_models() -> None:
    clock = FakeClock()
    first = CostLedger(clock=clock)
    with first.query():
        clock.advance(0.4)
    first.record_llm(prompt_tokens=1, completion_tokens=0, model="m1")

    second = CostLedger(clock=clock)
    with second.query():
        clock.advance(0.2)
    second.record_llm(prompt_tokens=1, completion_tokens=0, model="m2")

    merged = CostReport.merge([first.report(), second.report()])
    assert merged.queries == 2
    assert merged.query_latency.count == 2
    assert merged.query_latency.max_seconds == pytest.approx(0.4)
    assert merged.models == ("m1", "m2")
    assert merged.unattributed.total_tokens == 2


def test_cost_bucket_addition_is_field_wise() -> None:
    left = CostBucket(TokenUsage(prompt_tokens=10, calls=1), 1.0, 2.0, 0.5, 1)
    right = CostBucket(TokenUsage(completion_tokens=4, calls=1), 3.0, 4.0, 1.5, 2)
    total = left + right
    assert total.usage == TokenUsage(prompt_tokens=10, completion_tokens=4, calls=2)
    assert (total.self_seconds, total.wall_seconds, total.llm_seconds, total.spans) == (
        4.0,
        6.0,
        2.0,
        3,
    )


def test_report_to_dict_is_json_serialisable() -> None:
    report = _three_stage_ledger().report()
    payload = report.to_dict(include_spans=True, include_samples=True)
    encoded = json.loads(json.dumps(payload))

    assert encoded["schema_version"] == ACCOUNTING_SCHEMA_VERSION
    assert encoded["counters"] == {"turns": 2, "queries": 2, "consolidation_passes": 1}
    assert encoded["per_cycle"]["tokens_per_cycle"] == pytest.approx(2700.0)
    assert encoded["by_phase"]["consolidation"]["total_tokens"] == 1000
    assert encoded["totals"]["total_tokens"] == 5400
    assert len(encoded["spans"]) == len(report.spans)
    assert len(encoded["samples"]["query_seconds"]) == 2


def test_report_to_dict_omits_bulk_detail_by_default() -> None:
    payload = _three_stage_ledger().report().to_dict()
    assert "spans" not in payload
    assert "samples" not in payload


def test_to_stage_costs_matches_the_persisted_stage_cost_shape() -> None:
    report = _three_stage_ledger().report()
    projected = report.to_stage_costs()
    json.dumps(projected)

    assert set(projected) == {"deposit", "synthesise", "retrieve", "generate"}
    assert projected["deposit"]["tokens"] == {"prompt": 160, "completion": 40, "cached": 0}
    assert projected["deposit"]["latency_ms"] == pytest.approx(200.0)  # 0.2 s over two turns
    assert projected["deposit"]["calls"] == 2
    assert projected["synthesise"]["latency_ms"] == pytest.approx(1000.0)


def test_to_stage_costs_is_lossy_about_estimation() -> None:
    # The persisted schema forbids extra fields, so the exact/estimated verdict cannot
    # survive the projection. Assert the loss rather than pretend it does not happen.
    ledger = CostLedger(clock=FakeClock())
    with ledger.stage(Stage.DEPOSIT, "extract") as span:
        span.record_llm(prompt_tokens=10, completion_tokens=2, estimated=True)

    report = ledger.report()
    assert report.by_stage[Stage.DEPOSIT].usage.measurement == "estimated"
    projected = report.to_stage_costs()["deposit"]
    assert "token_measurement" not in projected
    assert "estimated_llm_calls" not in projected
    # to_dict is the lossless path.
    assert dig(report.to_dict(), "by_stage", "deposit", "token_measurement") == "estimated"
    assert dig(report.to_dict(), "by_stage", "deposit", "estimated_llm_calls") == 1


def test_frontier_row_never_invents_accuracy() -> None:
    report = _three_stage_ledger().report()
    row = frontier_row(config="lean", report=report, extra={"profile": "benchmark"})
    json.dumps(row)

    assert row["accuracy"] is None
    assert row["config"] == "lean"
    assert row["profile"] == "benchmark"
    assert row["tokens_per_cycle"] == pytest.approx(2700.0)
    assert row["query_tokens_per_query"] == pytest.approx(2100.0)
    assert row["ingest_tokens_per_turn"] == pytest.approx(100.0)
    assert row["total_tokens"] == 5400
    assert row["token_measurement"] == "exact"

    scored = frontier_row(config="lean", report=report, accuracy=0.612)
    assert scored["accuracy"] == pytest.approx(0.612)


def test_record_spans_false_keeps_aggregates_and_drops_detail() -> None:
    ledger = CostLedger(clock=FakeClock(), record_spans=False)
    with ledger.stage(Stage.DEPOSIT, "write") as span:
        span.record_llm(prompt_tokens=10, completion_tokens=2)

    report = ledger.report()
    assert report.spans == ()
    assert report.by_stage[Stage.DEPOSIT].total_tokens == 12
    assert report.by_stage[Stage.DEPOSIT].spans == 1


def test_span_metadata_round_trips() -> None:
    ledger = CostLedger(clock=FakeClock())
    with ledger.stage(Stage.RETRIEVE, "search", metadata={"top_k": 10}) as span:
        span.annotate(hits=4, strategy="typed")

    payload = ledger.report().to_dict(include_spans=True)
    spans = payload["spans"]
    assert isinstance(spans, list)
    first = spans[0]
    assert isinstance(first, dict)
    assert first["metadata"] == {"top_k": 10, "hits": 4, "strategy": "typed"}
    assert first["stage"] == "retrieve"
