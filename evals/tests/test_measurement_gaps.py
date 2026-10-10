"""Measurement and harness gaps A4, A12, A13, D1, D5, E3/E4/F4 (all offline)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import forensics_report as fr
import pytest
from memspine_evals import timing
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.experiments import JUDGE_CHOICES
from memspine_evals.judge import ExactMatchJudge
from memspine_evals.judge_prompts import JUDGE_SUITES, RoutedLLMJudge
from memspine_evals.provenance import RunProtocol
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems import VerbatimSystem
from memspine_evals.systems.memspine_system import MemspineSystem
from memspine_evals.tokens import (
    HeuristicTokenCounter,
    hit_drop_order,
    truncate_by_hit_rank,
)

# -- A4 ------------------------------------------------------------------------------------


def test_mem0_official_is_selectable_and_uses_the_mem0_text() -> None:
    assert "mem0-official" in JUDGE_CHOICES and "mem0-official" in JUDGE_SUITES
    sent: list[tuple[str, str | None]] = []

    async def chat(prompt: str, system: str | None = None) -> str:
        sent.append((prompt, system))
        return '{"reasoning": "same topic", "label": "CORRECT"}'

    judge = RoutedLLMJudge(chat, model="m", suite="mem0-official")
    verdict = asyncio.run(judge.score("Where?", "Paris trip", "gold shell necklace"))
    assert verdict.score == 1.0
    prompt, system = sent[0]
    assert "be generous with your grading" in prompt and "CORRECT/WRONG" in prompt
    assert "shell necklace" in prompt and system is not None
    assert judge.handles_abstention is False
    assert "Backboard" in judge.spec.params["suite_notes"]


def test_cli_accepts_mem0_official() -> None:
    from memspine_evals.cli import build_parser

    args = build_parser().parse_args(
        ["c0-1", "--dataset", "locomo", "--judge-prompt", "mem0-official", "--categories", "5"]
    )
    assert args.judge_prompt == "mem0-official" and args.categories == "5"


# -- A12 / A13 -----------------------------------------------------------------------------


def _ranks(final_rank: int | None, in_context: bool) -> dict:
    return {
        "vector": 1,
        "lexical": None,
        "extra": {},
        "fused": 1,
        "pool": 1,
        "final": final_rank,
        "in_context": in_context,
    }


def _row(category: str, correct: bool, final_rank: int | None = 1, mode: str = "qa") -> dict:
    gold = (
        [{"in_context": correct, "ranks": _ranks(final_rank, correct)}]
        if category != "adversarial"
        else []
    )
    return {
        "category": category,
        "correct": correct,
        "mode": mode,
        "has_stage_log": True,
        "gold_turns": gold,
        "outcome_class": "correct" if correct else "read_fail",
        "primary_gap": None,
        "flags": [],
        "reader_bucket": None,
        "context_tokens": 10,
        "latency_answer_ms": 5.0,
        "list_recall": None,
    }


def test_summary_reports_recall_at_10_hits_and_sufficiency() -> None:
    rows = [
        _row("single-hop", True, 3, "retrieval"),
        _row("temporal", False, 12, "retrieval"),
        _row("multi-hop", True, None, "retrieval"),
    ]
    s = fr.summarise("r", rows, {}, [], {})
    assert s["recall_at_10_hits"] == {"n": 3, "value": pytest.approx(1 / 3)}
    assert s["sufficiency_on_qa_set"]["n"] == 3
    assert s["sufficiency_on_qa_set"]["value"] == pytest.approx(2 / 3)
    assert s["sufficiency_on_qa_set"]["complete_qa_set"] is False


def test_qa_run_has_no_sufficiency_and_no_stage_log_gives_none() -> None:
    rows = [_row("single-hop", True)]
    rows[0]["has_stage_log"] = False
    s = fr.summarise("r", rows, {}, [], {})
    assert s["sufficiency_on_qa_set"] is None and s["recall_at_10_hits"] is None


def test_adversarial_rows_are_reported_apart_from_the_headline() -> None:
    rows = [
        _row("single-hop", True),
        _row("temporal", False),
        _row("adversarial", True),
        _row("adversarial", True),
    ]
    s = fr.summarise("r", rows, {}, [], {})
    assert s["n_questions"] == 2 and s["accuracy"] == 0.5
    assert "adversarial" not in s["by_category"]
    assert s["n_adversarial"] == 2
    assert s["adversarial"]["accuracy"] == 1.0 and s["adversarial"]["label"] == "adversarial"
    assert fr.CAT["cat5"] == "adversarial"


def test_adversarial_retrieval_only_has_no_accuracy() -> None:
    rows = [
        _row("single-hop", True, mode="retrieval"),
        _row("adversarial", False, mode="retrieval"),
    ]
    s = fr.summarise("r", rows, {}, [], {})
    assert s["adversarial"]["accuracy"] is None


def test_run_summary_schema_accepts_new_fields() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    s = fr.summarise("r", [_row("single-hop", True), _row("adversarial", False)], {}, [], {})
    schema = json.loads((fr.HERE / "schemas" / "forensic_run.schema.json").read_text("utf-8"))
    jsonschema.validate(json.loads(json.dumps(s)), schema)


def test_runner_summary_splits_adversarial() -> None:
    from memspine_evals.results import ResultRow
    from memspine_evals.runner import _adversarial_split

    def row(label: str, score: float) -> ResultRow:
        return ResultRow(
            "r", "i", label + str(score), "q", "g", "a", score, "binary", type_label=label
        )

    split = _adversarial_split([row("cat1", 1.0), row("cat2", 0.0), row("cat5", 1.0)], "binary")
    assert split is not None
    assert split["headline_excluding_adversarial"] == {"n": 2, "accuracy": 0.5}
    assert split["adversarial"]["accuracy"] == 1.0
    assert _adversarial_split([row("cat1", 1.0)], "binary") is None


# -- D1 ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ev:
    turn_id: str
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class _CharCounter(HeuristicTokenCounter):
    def count(self, text: str) -> int:
        return len(text)


def _context(lines: list[tuple[str, int | None]]) -> tuple[str, list[_Ev]]:
    text, evs, off = [], [], 0
    for name, rank in lines:
        text.append(name)
        evs.append(_Ev(name, meta={"hit_rank": rank, "span": (off, off + len(name))}))
        off += len(name) + 1
    return "\n".join(text), evs


def test_hit_drop_order_neighbours_first_then_worst_hits() -> None:
    _, evs = _context([("n0", None), ("h2", 2), ("n1", None), ("h1", 1), ("n2", None)])
    order = hit_drop_order(evs)
    assert order is not None
    names = [evs[i].turn_id for i in order]
    assert set(names[:3]) == {"n0", "n1", "n2"}
    assert names[:2] == ["n1", "n0"]  # both beside the worse hit h2; the later line goes first
    assert names[2] == "n2"  # beside the better hit h1
    assert names[-2:] == ["h2", "h1"]  # hits last, worst first
    assert hit_drop_order([_Ev("a", meta={"hit_rank": None})]) is None


def test_truncate_by_hit_rank_keeps_chronology_and_best_hits() -> None:
    text, evs = _context([("aaaa", None), ("bbbb", 2), ("cccc", None), ("dddd", 1), ("eeee", 3)])
    out = truncate_by_hit_rank(text, evs, 9, _CharCounter())  # fits two 4-char lines
    assert out is not None
    new_text, tokens, truncated, kept = out
    assert truncated and tokens <= 9
    assert new_text == "bbbb\ndddd"  # neighbours gone, hit 3 dropped, order preserved
    assert [e.turn_id for e in kept] == ["bbbb", "dddd"]
    assert [new_text[a:b] for a, b in (e.meta["span"] for e in kept)] == ["bbbb", "dddd"]


def test_truncate_by_hit_rank_untouched_when_it_fits_or_no_hits() -> None:
    text, evs = _context([("aa", 1), ("bb", None)])
    fit = truncate_by_hit_rank(text, evs, 100, _CharCounter())
    assert fit is not None and fit[2] is False
    text, evs = _context([("aa", None), ("bb", None)])
    assert truncate_by_hit_rank(text, evs, 3, _CharCounter()) is None


class _Engine:
    async def read(self, text: str, **kwargs: Any) -> Any:
        recs = [
            SimpleNamespace(
                record_id=rid,
                content=f"Ann: {rid}",
                valid_from=datetime(2023, 1, day, tzinfo=UTC),
            )
            for day, rid in enumerate(["n1", "h2", "n2", "h1"], start=1)
        ]
        return SimpleNamespace(
            context=SimpleNamespace(records=recs, tokens_used=0, abstained=False, evidence=None)
        )


async def _no_flush() -> Any:
    return SimpleNamespace(model_calls=0, n_records=0)


async def test_adapter_stamps_hit_rank_on_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.contextmanager
    def fake_forensics() -> Any:
        yield {"final": [("h1", 0.9), ("h2", 0.8)]}

    monkeypatch.setattr("memspine.engine.search_forensics", fake_forensics)
    system = MemspineSystem()
    system._engine = _Engine()
    system._read_mode = "replay"
    system.flush = _no_flush  # type: ignore[method-assign]
    for name in ("_calls", "_usage", "_prompt_usage", "_rerank_stats"):
        setattr(system, name, lambda: None)
    system._usage_delta = lambda a, b: {}  # type: ignore[method-assign]
    system._prompt_delta = lambda a, b: {}  # type: ignore[method-assign]
    system._rerank_meta = lambda a, b: {}  # type: ignore[method-assign]
    system._embed_services = lambda texts: {}  # type: ignore[method-assign]
    ctx = await system.query("q", 512, 5)
    assert ctx.meta["ranked"] is False
    assert [e.meta["hit_rank"] for e in ctx.evidence] == [None, 2, None, 1]
    out = truncate_by_hit_rank(ctx.text, ctx.evidence, 41, _CharCounter())
    assert out is not None
    assert "Ann: h1" in out[0] and "Ann: h2" in out[0] and "Ann: n1" not in out[0]


# -- D5 / E4 / F4 --------------------------------------------------------------------------


def test_percentiles_and_server_timing() -> None:
    p = timing.percentiles([10, 20, 30, 40, None])
    assert p == {"n": 4, "p50": 20.0, "p95": 40.0, "mean": 25.0}
    assert timing.percentiles([None]) is None
    assert timing.extract_server_timing({"usage": {}}) is None  # Ollama /v1 shape
    native = timing.extract_server_timing({"total_duration": 2_000_000_000, "eval_duration": 5e8})
    assert native == {"total_ms": 2000.0, "eval_ms": 500.0}
    llama = timing.extract_server_timing({"timings": {"prompt_ms": 10, "predicted_ms": 30}})
    assert llama is not None and llama["total_ms"] == 40.0


def test_latency_block_reports_percentiles_server_timings_and_arms() -> None:
    rows = [
        {
            "latency_answer_ms": 100.0,
            "latency_judge_ms": 50.0,
            "meta": {"server_timing": {"total_ms": 80.0}},
        },
        {"latency_answer_ms": 300.0, "latency_judge_ms": 70.0, "meta": {}},
    ]
    block = timing.latency_block(rows, [{"total_ms": 40.0}], {timing.CONCURRENT_ARMS_ENV: "3"})
    assert block["reader_ms"]["p95"] == 300.0 and block["judge_ms"]["p50"] == 50.0
    assert block["server_side_ms"]["reader"]["n"] == 1
    assert block["server_side_ms"]["judge"]["p50"] == 40.0
    assert block["concurrent_arms"] == 3 and block["wall_includes_queueing"] is True
    assert timing.latency_block([], [], {})["concurrent_arms"] is None


def test_reader_records_server_timing_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import memspine_evals.readers as readers

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "total_duration": 3_000_000_000,
            },
        )

    async def call() -> Any:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(readers, "_shared_client", lambda mod, timeout: client)
        return await readers.OpenAICompatReader(model="m").answer("q?", "ctx")

    answer = asyncio.run(call())
    assert answer.extra_meta["server_timing"] == {"total_ms": 3000.0}


def test_ingest_summary_turns_per_second() -> None:
    s = timing.ingest_summary({"a": {"turns": 100, "wall_s": 50.0}, "b": {"turns": 0, "wall_s": 0}})
    assert s["per_item"]["a"] == {"turns": 100, "wall_s": 50.0, "turns_per_s": 2.0}
    assert s["per_item"]["b"]["turns_per_s"] is None
    assert s["turns_per_s"] == 2.0 and s["turns"] == 100


def test_gpu_memory_parses_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timing.shutil, "which", lambda name: None)
    assert timing.gpu_memory() is None
    monkeypatch.setattr(timing.shutil, "which", lambda name: "nvidia-smi")

    def fake_run(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout="0, 1024, 8192\n1, 10, 8192\n")

    monkeypatch.setattr(timing.subprocess, "run", fake_run)
    got = timing.gpu_memory()
    assert got is not None and got["used_mib"] == 1034 and got["total_mib"] == 16384

    def boom(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired("nvidia-smi", 3)

    monkeypatch.setattr(timing.subprocess, "run", boom)
    assert timing.gpu_memory() is None


def test_capture_runtime_records_gpu_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine_evals.provenance import capture_runtime

    monkeypatch.setattr(timing, "gpu_memory", lambda timeout=3.0: {"used_mib": 5, "total_mib": 9})
    rt = capture_runtime(argv=["x"], environ={})
    assert rt["gpu"]["start"] == {"used_mib": 5, "total_mib": 9} and rt["gpu"]["start_at"]


def test_run_writes_latency_ingest_and_gpu_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timing, "gpu_memory", lambda timeout=3.0: {"used_mib": 7, "total_mib": 9})
    dataset = SyntheticDataset(n_items=2, turns_per_item=6, facts_per_item=1)
    config = RunConfig(
        run_id="t",
        protocol=RunProtocol(protocol_id="smoke", budget_tokens=400, top_k=5, seed=11),
        out_dir=tmp_path,
        expect_model_calls=False,
        runtime={"gpu": {"start": {"used_mib": 1, "total_mib": 9}}},
    )
    from memspine_evals.readers import ContextOnlyReader

    runner = EvalRunner(dataset, VerbatimSystem(), ContextOnlyReader(), ExactMatchJudge(), config)
    asyncio.run(runner.run())
    payload = json.loads((tmp_path / "t" / "summary.json").read_text("utf-8"))
    assert payload["latency"]["wall_includes_queueing"] is True
    assert payload["latency"]["reader_ms"] is None or "p95" in payload["latency"]["reader_ms"]
    ing = payload["ingest_timing"]
    assert ing["turns"] > 0 and len(ing["per_item"]) == 2
    assert payload["gpu_memory"]["start"]["used_mib"] == 1
    assert payload["gpu_memory"]["end"]["used_mib"] == 7
    assert payload["manifest"]["runtime"]["gpu"]["end"]["used_mib"] == 7


# -- E3 ------------------------------------------------------------------------------------


def test_ollama_env_script_sets_the_five_variables() -> None:
    text = (fr.HERE / "ollama_env.ps1").read_text("utf-8")
    for pair in (
        "OLLAMA_FLASH_ATTENTION = '1'",
        "OLLAMA_KV_CACHE_TYPE   = 'q8_0'",
        "OLLAMA_CONTEXT_LENGTH  = '8192'",
        "OLLAMA_NUM_PARALLEL    = '2'",
        "OLLAMA_KEEP_ALIVE      = '-1'",
    ):
        assert pair in text
    assert "'User'" in text and "-NoRestart" in text


# -- A5: accuracy with and without the errata questions --------------------------------------


def _qrow(item: str, qid: str, category: str, correct: bool) -> dict:
    return {**_row(category, correct), "item": item, "qid": qid}


def test_errata_block_reports_both_headlines(tmp_path) -> None:
    path = tmp_path / "errata.json"
    path.write_text(json.dumps({"entries": [
        {"item": "c", "qid": "0-1", "tag": "gold_error", "borderline": False},
        {"item": "c", "qid": "0-2", "tag": "needs_image", "borderline": True},
        {"item": "c", "qid": "0-3", "tag": "evidence_label_error", "borderline": False},
    ]}), encoding="utf-8")
    errata = fr.load_errata(path)
    assert set(errata) == {("c", "0-1"), ("c", "0-2")}  # a right answer with a wrong label is kept
    rows = [
        _qrow("c", "0-1", "single-hop", False),  # gold error
        _qrow("c", "0-2", "temporal", False),  # borderline image question
        _qrow("c", "0-3", "single-hop", False),
        _qrow("c", "0-4", "single-hop", True),
    ]
    s = fr.summarise("r", rows, {}, [], {}, errata)
    assert s["accuracy"] == 0.25
    e = s["errata"]
    assert e["strict"]["n"] == 3 and e["strict"]["n_excluded"] == 1
    assert e["strict"]["accuracy"] == pytest.approx(1 / 3)
    assert e["strict"]["by_category"]["single-hop"] == {"n": 2, "n_excluded": 1, "accuracy": 0.5}
    assert e["incl_borderline"]["n"] == 2 and e["incl_borderline"]["accuracy"] == 0.5
    assert fr.summarise("r", rows, {}, [], {})["errata"] is None
    assert fr.load_errata(tmp_path / "missing.json") == {}


def test_run_summary_schema_accepts_errata_block() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    rows = [_qrow("c", "0-1", "single-hop", False), _qrow("c", "0-2", "temporal", True)]
    errata = {("c", "0-1"): {"tag": "gold_error", "borderline": False}}
    s = fr.summarise("r", rows, {}, [], {}, errata)
    schema = json.loads((fr.HERE / "schemas" / "forensic_run.schema.json").read_text("utf-8"))
    jsonschema.validate(json.loads(json.dumps(s)), schema)
