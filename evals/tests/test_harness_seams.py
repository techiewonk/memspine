"""Regression tests for the seams between the harness's independently-built parts.

Six modules landed in parallel — two dataset adapters, a config schema, a result
schema, a cost ledger, a run driver — and every defect this file pins down was a place
where two of them agreed on a name and disagreed on a meaning. None of these is an
arithmetic bug; each one produces a number that looks fine and is wrong, which is the
only kind of bug that survives to publication.

Nothing here touches a corpus, a model or an engine. Everything is schema and
arithmetic, so it runs in CI with no data and no keys.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.harness.accounting import (  # noqa: E402
    CostLedger,
    Stage,
    TokenUsage,
    frontier_row,
    percentile,
)
from evals.harness.config import (  # noqa: E402
    READ_SIDE_ABLATIONS,
    WRITE_SIDE_ABLATIONS,
    AblationConfig,
    Dataset,
    DatasetConfig,
    GenerationConfig,
    JudgeConfig,
    ModelConfig,
    RunConfig,
)
from evals.harness.results import (  # noqa: E402
    SCHEMA_VERSION,
    BuildRecord,
    ItemResult,
    JudgeResult,
    LoopStage,
    RunResults,
    SchemaVersionError,
    StageCost,
    TokenCounts,
    build_results,
    stage_costs_from_report,
)
from evals.legacy_datasets.base import (  # noqa: E402
    LOADERS,
    BuildCache,
    IngestionStats,
    sample_namespace,
)
from evals.run import (  # noqa: E402
    FRONTIER_LADDER,
    TOGGLE_SEAMS,
    SeamUnavailableError,
    _clear_stale_verdicts,
    memspine_overrides,
    resolve_ladder,
)


def _model() -> ModelConfig:
    return ModelConfig(provider="openai", model="gpt-4o-mini")


def _config(**updates: object) -> RunConfig:
    base = RunConfig(
        run_id="seam-test",
        dataset=DatasetConfig(name=Dataset.LOCOMO),
        generation=GenerationConfig(backbone=_model()),
        judge=JudgeConfig(model=_model()),
    )
    return base.model_copy(update=updates) if updates else base


def _item(item_id: str = "q1", **updates: object) -> ItemResult:
    return ItemResult(item_id=item_id, sample_id="s1", question="q?", **updates)  # type: ignore[arg-type]


# ── the build cache key ──────────────────────────────────────────────────────


def test_read_side_ablations_share_one_build_digest() -> None:
    """MAGMA_HARVEST §2.3's whole economy: construction is paid for once.

    The runner writes the *entire* override map into ``system.overrides`` because that
    is the only field it can. If ``build_payload`` digested that verbatim, flipping
    ``read.hybrid`` would rebuild every sample's memory — slow, not wrong, but it makes
    the read-side ablation matrix unaffordable, which is the same as not running it.
    """
    reference = _config()
    read_only = _config(
        system=reference.system.model_copy(
            update={"overrides": {"read.hybrid": False, "read.assembly.mmr_lambda": 1.0}}
        )
    )
    assert read_only.build_digest() == reference.build_digest()
    assert read_only.protocol_digest() != reference.protocol_digest()


def test_write_side_and_unrecognised_overrides_do_bust_the_build_digest() -> None:
    """The narrowing must fail safe: an override nobody classified invalidates."""
    reference = _config()
    write_side = _config(
        system=reference.system.model_copy(
            update={"overrides": {"memories.semantic.policies.trust.quarantine_below": 0.0}}
        )
    )
    unknown = _config(
        system=reference.system.model_copy(update={"overrides": {"brand.new.knob": 1}})
    )
    assert write_side.build_digest() != reference.build_digest()
    assert unknown.build_digest() != reference.build_digest()


def test_question_selection_does_not_rebuild_the_memory() -> None:
    """A 10-sample pilot and the full run share every build they have in common, and a
    per-category ablation re-ingests nothing: the questions never touch the store."""
    reference = _config()
    narrowed = _config(
        dataset=reference.dataset.model_copy(
            update={"limit": 10, "categories": ("multi-hop",), "max_questions_per_sample": 5}
        )
    )
    assert narrowed.build_digest() == reference.build_digest()
    assert narrowed.protocol_digest() != reference.protocol_digest()


def test_corpus_and_config_contents_are_in_the_protocol_digest() -> None:
    """Two runs over two different copies of "LoCoMo", or two edits of one config file,
    must not share a comparability key. A path is not a protocol."""
    reference = _config()
    other_corpus = _config(dataset=reference.dataset.model_copy(update={"corpus_sha256": "a" * 64}))
    other_config = _config(system=reference.system.model_copy(update={"config_sha256": "b" * 64}))
    assert other_corpus.protocol_digest() != reference.protocol_digest()
    assert other_config.protocol_digest() != reference.protocol_digest()


def test_ablation_partition_is_exhaustive_and_disjoint() -> None:
    """An unclassified toggle drops out of the build key silently."""
    declared = set(AblationConfig.model_fields)
    assert declared == WRITE_SIDE_ABLATIONS | READ_SIDE_ABLATIONS
    assert not WRITE_SIDE_ABLATIONS & READ_SIDE_ABLATIONS


# ── the dataset seam ─────────────────────────────────────────────────────────


def test_both_adapters_namespace_identically() -> None:
    assert sample_namespace("locomo", "conv-26") == "locomo/conv-26"
    assert sample_namespace("longmemeval", "gpt4_2655b836") == "longmemeval/gpt4_2655b836"


def test_lossy_sample_ids_do_not_collide_into_one_namespace() -> None:
    """Two samples sharing a namespace is cross-contamination reported as a score."""
    assert sample_namespace("d", "ABC") != sample_namespace("d", "abc")
    assert sample_namespace("d", "a.b") != sample_namespace("d", "a-b")


def test_both_adapters_use_one_build_cache_keyed_the_same_way() -> None:
    """The two adapters shipped with two incompatible cache layouts and two different
    key definitions. One frontier table cannot be built from two of those."""
    for dataset in ("locomo", "longmemeval"):
        cache = BuildCache(
            root=Path("/tmp/cache"), dataset=dataset, profile="lean", config_hash="c" * 64
        )
        slot = cache.slot("sample-1", options_hash="d" * 64)
        assert slot.path.parent.name == dataset
        assert slot.key.config_hash == "c" * 64
        assert slot.key.options_hash == "d" * 64


def test_every_cli_dataset_has_a_loader() -> None:
    """``--dataset X`` that parses and then dies after the first sample is ingested is
    the expensive way to discover a missing adapter."""
    assert {item.value for item in Dataset} <= set(LOADERS)


def test_build_cache_slot_lifecycle(tmp_path: Path) -> None:
    cache = BuildCache(root=tmp_path, dataset="locomo", profile="lean", config_hash="c" * 64)
    slot = cache.slot("s1", options_hash="o" * 64)
    stats = IngestionStats(
        sample_id="s1",
        namespace="locomo/s1",
        sessions=2,
        turns=10,
        records=10,
        characters=100,
        sleep_cycles=0,
        wall_seconds=1.0,
    )

    assert slot.prepare("hash-a") is True
    slot.commit(content_hash="hash-a", namespace="locomo/s1", stats=stats)
    assert slot.matches("hash-a") is True
    assert slot.prepare("hash-a") is False
    # Edited corpus, same key: a miss, and the stale store is cleared.
    assert slot.prepare("hash-b") is True
    assert slot.matches("hash-a") is False
    # Replayed stats survive the round trip, so a cache hit still reports write cost.
    slot.commit(content_hash="hash-b", namespace="locomo/s1", stats=stats)
    manifest = slot.read_manifest()
    assert manifest is not None
    assert manifest.stats == stats


# ── the cost seam ────────────────────────────────────────────────────────────


def test_one_percentile_definition_across_the_seam() -> None:
    """The live ledger and the persisted summary must not report two different p95s for
    the same latencies. Nearest-rank, so the answer is a latency that happened."""
    import evals.harness.results as results_module

    assert getattr(results_module, "_percentile") is percentile  # noqa: B009
    assert percentile([0.1, 0.3, 0.3, 0.5, 1.0], 95) == 1.0
    assert percentile([], 95) is None


def test_cost_report_is_a_snapshot_that_does_not_move() -> None:
    """``CostReport`` is frozen but its buckets are not; a shallow copy handed out live
    handles, so a per-sample snapshot silently reported the whole run's totals."""
    ledger = CostLedger()
    with ledger.stage(Stage.GENERATE) as span:
        span.record_llm(prompt_tokens=80, completion_tokens=20, model="m")
    snapshot = ledger.report()
    before = snapshot.by_stage[Stage.GENERATE].total_tokens

    with ledger.stage(Stage.GENERATE) as span:
        span.record_llm(prompt_tokens=800, completion_tokens=200, model="m")

    assert snapshot.by_stage[Stage.GENERATE].total_tokens == before == 100
    assert ledger.report().by_stage[Stage.GENERATE].total_tokens == 1100


def test_consolidation_context_reaches_nested_spans() -> None:
    """A retrieve issued during a sleep cycle is consolidation cost, not query cost.

    Billing it to the read side inflates the very query-leg figure that gets compared
    against peer systems and deflates the layer we are trying to price.
    """
    from evals.harness.accounting import Phase, Side

    ledger = CostLedger()
    with ledger.stage(Stage.SYNTHESISE) as outer:
        outer.record_llm(prompt_tokens=400, completion_tokens=100, model="m")
        with ledger.stage(Stage.RETRIEVE) as inner:
            inner.record_llm(prompt_tokens=250, completion_tokens=50, model="m")

    report = ledger.report()
    assert report.by_phase[Phase.CONSOLIDATION].total_tokens == 800
    assert Phase.QUERY not in report.by_phase
    assert Side.READ not in report.by_side


def test_unattributed_tokens_make_the_headline_none_not_zero() -> None:
    """The frontier's x-axis must never read 0.0 for a run whose cost never reached a
    phase. Refusing the number is the same discipline as refusing a 0/0 ratio."""
    ledger = CostLedger()
    ledger.add_turns(4)
    ledger.add_queries(2)
    ledger.record_usage(TokenUsage(prompt_tokens=4000, completion_tokens=1000, calls=1))

    report = ledger.report()
    assert report.unattributed.total_tokens == 5000
    assert report.per_cycle.tokens_per_cycle is None
    assert report.per_cycle.unattributed_tokens == 5000

    row = frontier_row(config="lean", report=report)
    assert row["tokens_per_cycle"] is None
    assert row["unattributed_tokens"] == 5000
    assert row["accuracy"] is None  # never derived, defaulted or guessed


def test_a_failed_llm_call_is_still_priced() -> None:
    """Retries in a benchmark run are not free. The prompt was sent."""

    class Boom:
        provider_id = "boom"

        async def chat(self, messages: list[dict[str, str]], **options: object) -> str:
            raise RuntimeError("provider 500")

    import asyncio

    from evals.harness.accounting import CountingLLM

    ledger = CostLedger()
    wrapped = CountingLLM(Boom(), ledger, model="m")
    with pytest.raises(RuntimeError, match="provider 500"):
        asyncio.run(wrapped.chat([{"role": "user", "content": "x" * 400}]))

    report = ledger.report()
    assert report.total.total_tokens > 0
    assert report.total.usage.calls == 1


def test_accounting_bridges_into_the_persisted_stage_cost_schema() -> None:
    """Two modules grew parallel cost vocabularies. The bridge must actually validate
    against the schema that gets written to disk, in both directions of the stage set."""
    ledger = CostLedger()
    with ledger.stage(Stage.RETRIEVE):
        pass
    with ledger.stage(Stage.GENERATE) as span:
        span.record_llm(prompt_tokens=10, completion_tokens=5, model="m")

    bridged = stage_costs_from_report(ledger.report())
    assert set(bridged) <= set(LoopStage)
    assert bridged[LoopStage.GENERATE].tokens.total == 15
    assert isinstance(bridged[LoopStage.RETRIEVE], StageCost)


# ── the result file ──────────────────────────────────────────────────────────


def test_unmeasured_cost_is_none_not_zero() -> None:
    """ "We did not measure this" and "this was free" are different claims, and the
    second one is the more attractive of the two to publish by accident."""
    results = build_results(config=_config(), items=[_item("a"), _item("b")])
    cpc = results.summary.cost_per_cycle
    assert cpc.cost_recorded is False
    assert cpc.tokens_per_question is None
    assert cpc.query_tokens_per_question is None
    assert results.summary.overall.tokens_mean is None
    assert results.summary.overall.tokens_total == 0  # a count of nothing is 0


def test_item_level_build_stage_cost_reaches_the_headline() -> None:
    """A summary must not report two mutually inconsistent totals for one run."""
    item = _item(
        costs={
            LoopStage.GENERATE: StageCost(tokens=TokenCounts(prompt=100, completion=20)),
            LoopStage.DEPOSIT: StageCost(tokens=TokenCounts(prompt=999)),
        }
    )
    summary = build_results(config=_config(), items=[item]).summary
    assert summary.overall.tokens_total == 1119
    assert summary.cost_per_cycle.build_tokens_total == 999
    assert summary.cost_per_cycle.query_tokens_total == 120
    assert summary.cost_per_cycle.tokens_per_question == 1119.0


def test_saving_never_persists_stale_aggregates(tmp_path: Path) -> None:
    """A ``--score-only`` pass that forgot ``refresh_summary`` used to write a file
    whose headline number contradicted its own items."""

    def judged(score: float) -> JudgeResult:
        return JudgeResult(
            score=score,
            kind="graded_reference",
            judge_model="m",
            rubric_id="r",
            rubric_version="v1",
        )

    results = build_results(config=_config(), items=[_item("a", judge=judged(1.0))])
    results.items.append(_item("b", judge=judged(0.0)))
    assert results.summary.overall.judge_score_mean == 1.0  # stale on purpose

    stored = json.loads(results.save(tmp_path / "run.json").read_text(encoding="utf-8"))
    assert stored["summary"]["overall"]["judge_score_mean"] == 0.5
    assert stored["summary"]["n_judged"] == 2


def test_resolved_engine_config_survives_a_rejudge(tmp_path: Path) -> None:
    """The only field that records what the engine *actually* ran under. Losing it in a
    re-judge would leave a file that names a mutable template and nothing else."""
    results = build_results(config=_config(), items=[_item()])
    results.summary.resolved_system_config = {"read": {"hybrid": True}}
    results.summary.resolved_system_config_sha256 = "deadbeef"
    results.refresh_summary()
    assert results.summary.resolved_system_config == {"read": {"hybrid": True}}

    reloaded = RunResults.load(results.save(tmp_path / "run.json"))
    assert reloaded.summary.resolved_system_config == {"read": {"hybrid": True}}
    assert reloaded.summary.resolved_system_config_sha256 == "deadbeef"


def test_a_foreign_schema_major_is_refused_at_either_level(tmp_path: Path) -> None:
    results = build_results(config=_config(), items=[_item()], builds=[BuildRecord(sample_id="s1")])
    path = results.save(tmp_path / "run.json")
    assert RunResults.load(path).schema_version == SCHEMA_VERSION

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["summary"]["schema_version"] = "9.9.9"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SchemaVersionError):
        RunResults.load(path)


# ── the run driver's toggle bridge ───────────────────────────────────────────


def test_every_toggle_has_a_declared_seam() -> None:
    assert set(AblationConfig.model_fields) == set(TOGGLE_SEAMS)


def test_coupled_toggles_are_rejected_in_both_directions() -> None:
    """``evolve_links`` and ``graph_retrieval`` share one config switch. Accepting
    either half alone records a protocol that did not run — worse than a no-op flag."""
    with pytest.raises(SeamUnavailableError, match="evolve_links"):
        memspine_overrides(AblationConfig(evolve_links=False))
    with pytest.raises(SeamUnavailableError, match="evolve_links"):
        memspine_overrides(AblationConfig(graph_retrieval=False))
    resolved = memspine_overrides(AblationConfig(evolve_links=False, graph_retrieval=False))
    assert resolved.overrides["memories.associative.enabled"] is False


def test_unavailable_toggles_fail_loudly_rather_than_doing_nothing() -> None:
    """A flag memspine cannot express must not put an unearned row on the frontier."""
    for toggle in ("typed_relations", "typed_adaptive_routing", "validity_aware_traversal"):
        with pytest.raises(SeamUnavailableError, match=toggle):
            memspine_overrides(AblationConfig(**{toggle: True}))
    with pytest.raises(SeamUnavailableError, match="dedup"):
        memspine_overrides(AblationConfig(dedup=False))


def test_the_frontier_ladder_accumulates_and_every_rung_is_expressible() -> None:
    rungs = resolve_ladder()
    assert [rung.name for rung, _ in rungs] == [rung.name for rung in FRONTIER_LADDER]
    digests = set()
    for _rung, ablation in rungs:
        memspine_overrides(ablation)  # must not raise: an unrunnable rung is a dead run
        digests.add(_config(ablation=ablation).build_digest())
    # Each rung changes write-side state, so each needs its own build.
    assert len(digests) == len(rungs)


def test_rejudging_with_a_different_instrument_is_not_a_no_op() -> None:
    """``unjudged()`` is the scorer's work list and only holds items with no verdict.

    Without clearing the old ones, ``--score-only --judge-model <other>`` over an
    already-scored file would rewrite the file naming the new judge in its config and
    keeping the old judge's scores in its items — a result that misattributes its own
    numbers, which is worse than one that fails.
    """

    def verdict(model: str, rubric_version: str = "v1") -> JudgeResult:
        return JudgeResult(
            score=1.0,
            kind="graded_reference",
            judge_model=model,
            rubric_id="evals/judge",
            rubric_version=rubric_version,
        )

    config = _config()
    results = build_results(
        config=config,
        items=[
            _item("a", judge=verdict("openai/gpt-4o-mini")),
            _item("b", judge=verdict("openai/gpt-4o-mini", "v2")),
            _item("c"),
        ],
    )
    assert len(results.unjudged()) == 1

    # A different model clears everything scored by the old instrument...
    swapped = config.judge.model_copy(
        update={"model": ModelConfig(provider="openai", model="gpt-4o")}
    )
    assert _clear_stale_verdicts(results, swapped) == 2
    assert len(results.unjudged()) == 3

    # ...but the same instrument keeps its verdicts, so an interrupted scoring pass
    # resumes instead of paying for every item twice.
    resumed = build_results(
        config=config,
        items=[_item("a", judge=verdict("openai/gpt-4o-mini")), _item("b")],
    )
    assert _clear_stale_verdicts(resumed, config.judge) == 0
    assert [item.item_id for item in resumed.unjudged()] == ["b"]
