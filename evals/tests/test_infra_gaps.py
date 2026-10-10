"""Harness infrastructure gaps (GAP_REGISTER A8, D1-D4, D7, B11, F2, F3, A10), all offline.

Stub readers, judges, systems and an in-memory HTTP client; no network, no model server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from memspine_evals import readers as readers_mod
from memspine_evals.cli import build_parser, failed_rerank_checks
from memspine_evals.contracts import (
    DepositResult,
    Evidence,
    ReaderAnswer,
    RetrievedContext,
    Turn,
)
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.experiments import (
    C01Config,
    build_reader_and_judge,
    build_token_counter,
    sampler_for,
)
from memspine_evals.judge import ContainsJudge
from memspine_evals.provenance import RunProtocol, capture_runtime
from memspine_evals.readers import (
    CtxGuard,
    OpenAICompatReader,
    SamplerConfig,
    ServerContextExceeded,
    find_guard,
    openai_compat_chat,
)
from memspine_evals.refusal import RefusalRetryReader
from memspine_evals.results import read_run
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems import VerbatimSystem
from memspine_evals.systems.memspine_system import (
    MemspineSystem,
    _merge_deposits,
    parse_turn_stamp,
    prepare_forensics_dir,
)
from memspine_evals.tokens import (
    HeuristicTokenCounter,
    load_reader_tokenizer_counter,
    truncate_by_rank,
)

# -- helpers -------------------------------------------------------------------------------


def _fake_httpx(usage: dict[str, int], sent: list[dict[str, Any]], reply: str = "an answer") -> Any:
    class FakeResponse:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {
                "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                "usage": usage,
            }

    class FakeClient:
        def __init__(self, **_: Any) -> None: ...

        async def post(self, url: str, json: dict[str, Any], headers: Any) -> FakeResponse:
            sent.append(json)
            return FakeResponse()

    return type("H", (), {"AsyncClient": FakeClient})


def _run(
    tmp_path: Path,
    reader: Any,
    system: Any | None = None,
    judge: Any | None = None,
    run_id: str = "gap",
    **config_kwargs: Any,
) -> tuple[EvalRunner, list[dict[str, Any]], dict[str, Any]]:
    dataset = SyntheticDataset(n_items=2, turns_per_item=8, facts_per_item=2)
    config = RunConfig(
        run_id=run_id,
        protocol=RunProtocol(protocol_id="gap", budget_tokens=400, top_k=5, seed=3),
        out_dir=tmp_path,
        expect_model_calls=True,
        **config_kwargs,
    )
    runner = EvalRunner(
        dataset,
        system or VerbatimSystem(),
        reader,
        judge or ContainsJudge(),
        config,
    )
    asyncio.run(runner.run())
    _, rows, _ = read_run(tmp_path / run_id / "results.jsonl")
    summary = json.loads((tmp_path / run_id / "summary.json").read_text(encoding="utf-8"))
    return runner, rows, summary


# -- A8: explicit sampler ---------------------------------------------------------------------


def test_sampler_payload_defaults_and_seed() -> None:
    assert SamplerConfig().payload() == {
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "top_p": 1.0,
    }
    assert SamplerConfig(seed=11).payload()["seed"] == 11


def test_cli_sampler_flag_defaults() -> None:
    args = build_parser().parse_args(["c0-1", "--dataset", "locomo"])
    assert (args.presence_penalty, args.frequency_penalty, args.top_p) == (0.0, 0.0, 1.0)
    assert args.sampler_seed is None and args.server_ctx == 8192 and args.strict_ctx is False
    assert args.token_count == "heuristic"


def test_sampler_seed_defaults_to_the_run_seed() -> None:
    assert sampler_for(C01Config(seed=7)).seed == 7
    assert sampler_for(C01Config(seed=7, sampler_seed=99, top_p=0.9)).payload() == {
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "top_p": 0.9,
        "seed": 99,
    }


async def test_reader_sends_the_sampler_with_every_request() -> None:
    sent: list[dict[str, Any]] = []
    reader = OpenAICompatReader(model="m", sampler=SamplerConfig(top_p=0.8, seed=5))
    reader._httpx = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, sent)
    await reader.answer("q", "c")
    assert sent[0]["presence_penalty"] == 0.0 and sent[0]["frequency_penalty"] == 0.0
    assert sent[0]["top_p"] == 0.8 and sent[0]["seed"] == 5
    assert reader.describe()["sampler"] == {
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "top_p": 0.8,
        "seed": 5,
    }


async def test_judge_chat_sends_the_sampler_and_records_it(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    fake = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, sent, reply="CORRECT")
    monkeypatch.setattr(readers_mod, "_shared_client", lambda httpx, timeout: fake.AsyncClient())
    chat = openai_compat_chat("m", sampler=SamplerConfig(seed=4))
    assert await chat("prompt") == "CORRECT"
    assert sent[0]["seed"] == 4 and sent[0]["top_p"] == 1.0 and sent[0]["presence_penalty"] == 0.0
    assert chat.params["sampler"]["seed"] == 4  # type: ignore[attr-defined]


def test_built_reader_judge_and_labels_carry_the_sampler() -> None:
    config = C01Config(mode="qa", max_model_calls=5, seed=13, categories=(1, 2, 3, 4))
    reader, judge, _ = build_reader_and_judge(config)
    assert reader.describe()["sampler"]["seed"] == 13  # type: ignore[attr-defined]
    assert judge.spec.params["sampler"]["seed"] == 13
    assert find_guard(reader) is not None


# -- D2: context-window guard -----------------------------------------------------------------


def test_guard_threshold() -> None:
    guard = CtxGuard(1000)
    assert guard.check(900, 91, "reader") is False
    assert guard.check(900, 92, "reader") is True  # 992 >= 1000 - 8
    assert guard.check(0, 0, "reader") is False  # no usage reported
    assert guard.suspected["reader"] == 1 and guard.consume() == ["reader"]
    assert guard.consume() == []


def test_strict_guard_raises() -> None:
    with pytest.raises(ServerContextExceeded):
        CtxGuard(1000, strict=True).check(995, 5, "judge")


def test_rows_and_summary_count_suspected_truncation(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []
    guard = CtxGuard(8192)
    reader = OpenAICompatReader(model="m", guard=guard)
    reader._httpx = _fake_httpx({"prompt_tokens": 8190, "completion_tokens": 20}, sent)
    _, rows, summary = _run(tmp_path, reader)
    assert rows and all(r["meta"]["server_truncation_suspected"] is True for r in rows)
    assert rows[0]["meta"]["server_truncation_by"] == ["reader"]
    block = summary["server_truncation_suspected"]
    assert block["rows"] == len(rows) and block["reader"] == len(rows)
    assert block["server_ctx"] == 8192 and block["strict_ctx"] is False


def test_clean_rows_have_no_flag(tmp_path: Path) -> None:
    reader = OpenAICompatReader(model="m", guard=CtxGuard(8192))
    reader._httpx = _fake_httpx({"prompt_tokens": 500, "completion_tokens": 20}, [])
    _, rows, summary = _run(tmp_path, reader)
    assert all("server_truncation_suspected" not in r["meta"] for r in rows)
    assert summary["server_truncation_suspected"]["rows"] == 0


def test_strict_ctx_stops_the_run(tmp_path: Path) -> None:
    reader = OpenAICompatReader(model="m", guard=CtxGuard(8192, strict=True))
    reader._httpx = _fake_httpx({"prompt_tokens": 8192, "completion_tokens": 1}, [])
    with pytest.raises(ServerContextExceeded):
        _run(tmp_path, reader)
    summary_json = json.loads((tmp_path / "gap" / "summary.json").read_text(encoding="utf-8"))
    assert summary_json["aborted"] is True
    assert summary_json["abort_reason"] == "ServerContextExceeded"


def test_wrappers_expose_the_guard() -> None:
    guard = CtxGuard()
    reader = OpenAICompatReader(model="m", guard=guard)
    assert find_guard(RefusalRetryReader(reader)) is guard
    assert find_guard(SimpleNamespace()) is None


# -- D3: refusal retry keeps both calls' tokens -------------------------------------------------


async def test_retry_records_first_and_retry_tokens_separately() -> None:
    class Inner:
        reader_id = "stub"
        model = "m"
        makes_model_calls = True

        def __init__(self) -> None:
            self.replies = [("I do not know", 100, 5), ("May 2023", 130, 7)]

        def describe(self) -> dict[str, Any]:
            return {}

        async def answer(self, q: str, c: str, question_date: str | None = None) -> ReaderAnswer:
            text, p, c_ = self.replies.pop(0)
            return ReaderAnswer(text=text, prompt_tokens=p, completion_tokens=c_, model_calls=1)

    out = await RefusalRetryReader(Inner()).answer("When?", "context line")
    assert out.prompt_tokens == 230 and out.completion_tokens == 12
    meta = out.extra_meta
    assert (meta["first_prompt_tokens"], meta["first_completion_tokens"]) == (100, 5)
    assert (meta["retry_prompt_tokens"], meta["retry_completion_tokens"]) == (130, 7)


# -- D4: runtime block -------------------------------------------------------------------------


def test_capture_runtime_block() -> None:
    runtime = capture_runtime(
        argv=["prog", "--x"],
        environ={"MEMSPINE_A": "1", "OLLAMA_HOST": "h", "MEMSPINE_API_KEY": "s3", "PATH": "/x"},
    )
    assert runtime["argv"] == ["prog", "--x"]
    assert runtime["env"] == {
        "MEMSPINE_A": "1",
        "MEMSPINE_API_KEY": "<redacted>",
        "OLLAMA_HOST": "h",
    }
    assert set(runtime["versions"]) == {
        "memspine",
        "torch",
        "transformers",
        "sentence-transformers",
        "httpx",
        "lancedb",
    }
    assert "memspine_file" in runtime and "engine_git_describe" in runtime
    assert "ollama" not in runtime  # no server probe without a base_url


def test_capture_runtime_skips_a_remote_server() -> None:
    runtime = capture_runtime(base_url="https://api.openai.com/v1", argv=[], environ={})
    assert "skipped" in runtime["ollama"]


def test_runtime_lands_in_the_manifest(tmp_path: Path) -> None:
    reader = OpenAICompatReader(model="m")
    reader._httpx = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, [])
    runtime = {"argv": ["a"], "env": {"MEMSPINE_X": "1"}}
    _, _, summary = _run(tmp_path, reader, runtime=runtime)
    assert summary["manifest"]["runtime"] == runtime


# -- D7: forensic logs --------------------------------------------------------------------------


def test_forensics_dir_guard(tmp_path: Path) -> None:
    prepare_forensics_dir(str(tmp_path), environ={})  # empty dir: fine
    (tmp_path / "forensics.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="MEMSPINE_FORENSICS_OVERWRITE"):
        prepare_forensics_dir(str(tmp_path), environ={})
    prepare_forensics_dir(str(tmp_path), environ={"MEMSPINE_FORENSICS_OVERWRITE": "1"})
    assert not (tmp_path / "forensics.jsonl").exists()


def test_begin_run_refuses_a_used_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ingest.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("MEMSPINE_FORENSICS_DIR", str(tmp_path))
    monkeypatch.delenv("MEMSPINE_FORENSICS_OVERWRITE", raising=False)
    with pytest.raises(RuntimeError, match=r"already contains ingest.jsonl"):
        MemspineSystem().begin_run("r1")


def _turn(turn_id: str = "D1:1", stamp: str | None = "1:56 pm on 8 May, 2023") -> Turn:
    return Turn(turn_id=turn_id, session_id="s1", speaker="Ann", text="hi", timestamp=stamp)


def _record(**kw: Any) -> SimpleNamespace:
    base = {
        "record_id": "r-1",
        "content": "Ann: hi",
        "valid_from": None,
        "memory_type": "episodic",
        "group_id": "s1",
        "session_id": "s1",
        "quarantined": False,
        "trust": 0.8,
        "status": SimpleNamespace(value="activated"),
    }
    return SimpleNamespace(**{**base, **kw})


def test_ingest_and_forensics_rows_carry_run_and_query_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMSPINE_FORENSICS_DIR", str(tmp_path))
    system = MemspineSystem()
    system.begin_run("run-A--memspine")
    system._item_id = "conv-1"
    system._write_ingest_log([_record(quarantined=True, trust=0.1)], [_turn()], ["Ann: hi"])
    system.set_query_id("0-3")
    system._write_forensics(str(tmp_path), "what?", {}, SimpleNamespace(records=[]))
    ingest = json.loads((tmp_path / "ingest.jsonl").read_text(encoding="utf-8"))
    assert ingest["run_id"] == "run-A--memspine"
    assert ingest["quarantined"] is True and ingest["trust"] == 0.1  # F2
    assert ingest["status"] == "activated" and ingest["written"] is True
    fx = json.loads((tmp_path / "forensics.jsonl").read_text(encoding="utf-8"))
    assert fx["run_id"] == "run-A--memspine" and fx["query_id"] == "0-3"


def test_forensics_report_joins_on_query_id_with_text_fallback() -> None:
    import forensics_report as fr

    results = [
        {"run_id": "R", "item_id": "c1", "query_id": "0-1", "question": "same?"},
        {"run_id": "R", "item_id": "c1", "query_id": "0-2", "question": "same?"},  # duplicate text
        {"run_id": "R", "item_id": "c1", "query_id": "0-3", "question": "old?"},
    ]
    fx = [
        {"run_id": "OTHER", "item": "c1", "query_id": "0-2", "query": "same?", "tag": "stale"},
        {"run_id": "R", "item": "c1", "query_id": "0-2", "query": "same?", "tag": "second"},
        {"run_id": "R", "item": "c1", "query_id": "0-1", "query": "same?", "tag": "first"},
        {"item": "c1", "query": "old?", "tag": "legacy"},  # no query_id: text join
    ]
    joined = fr.join_forensics(results, fx)
    assert joined[("c1", "0-1")]["tag"] == "first"
    assert joined[("c1", "0-2")]["tag"] == "second"
    assert joined[("c1", "0-3")]["tag"] == "legacy"


# -- B11: rerank check --------------------------------------------------------------------------


class _RerankSystem(VerbatimSystem):
    """A verbatim system that claims a reranker config and reports its per-query audit."""

    def __init__(self, calls: int, failures: int, mode: str = "fastembed") -> None:
        super().__init__()
        self._audit = (calls, failures)
        self._mode = mode

    def describe(self) -> dict[str, Any]:
        return {**dict(super().describe()), "config": {"read": {"rerank": self._mode}}}

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        ctx = await super().query(text, budget_tokens, top_k)
        calls, failures = self._audit
        meta = {
            **dict(ctx.meta),
            "rerank_mode": self._mode,
            "reranked": calls - failures > 0,
            "rerank_calls": calls,
            "rerank_failures": failures,
        }
        return RetrievedContext(
            text=ctx.text,
            tokens=ctx.tokens,
            evidence=ctx.evidence,
            truncated=ctx.truncated,
            meta=meta,
        )


@pytest.mark.parametrize(
    ("calls", "failures", "mode", "expected"),
    [
        (0, 0, "fastembed", "FAILED"),
        (3, 1, "fastembed", "FAILED"),
        (3, 0, "fastembed", "ok"),
        (0, 0, "off", "not_applicable"),
    ],
)
def test_rerank_check(tmp_path: Path, calls: int, failures: int, mode: str, expected: str) -> None:
    reader = OpenAICompatReader(model="m")
    reader._httpx = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, [])
    _, _, summary = _run(tmp_path, reader, system=_RerankSystem(calls, failures, mode))
    assert summary["rerank_check"] == expected
    summaries = [SimpleNamespace(run_id="gap")]
    assert (failed_rerank_checks(tmp_path, summaries) == ["gap"]) is (expected == "FAILED")


def test_rerank_check_not_applicable_for_a_plain_system(tmp_path: Path) -> None:
    reader = OpenAICompatReader(model="m")
    reader._httpx = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, [])
    _, _, summary = _run(tmp_path, reader)
    assert summary["rerank_check"] == "not_applicable"


# -- F2: quarantine -----------------------------------------------------------------------------


class _QuarantineSystem(VerbatimSystem):
    async def insert(self, turn: Turn) -> DepositResult:
        base = await super().insert(turn)
        return DepositResult(
            n_records=base.n_records, record_ids=base.record_ids, meta={"n_quarantined": 1}
        )


def test_summary_counts_quarantined_records_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reader = OpenAICompatReader(model="m")
    reader._httpx = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, [])
    with caplog.at_level(logging.WARNING, logger="memspine_evals.runner"):
        _, _, summary = _run(tmp_path, reader, system=_QuarantineSystem())
    assert summary["n_quarantined"] == 16  # 2 items x 8 turns
    assert "quarantined at deposit" in caplog.text


def test_merged_deposits_sum_quarantined() -> None:
    merged = _merge_deposits(
        [DepositResult(meta={"n_quarantined": 1}), DepositResult(meta={"n_quarantined": 2})]
    )
    assert merged.meta["n_quarantined"] == 3


# -- F3: timestamps -----------------------------------------------------------------------------


def test_unparseable_timestamp_is_an_error_not_now() -> None:
    assert parse_turn_stamp(_turn()) is not None
    assert parse_turn_stamp(_turn(stamp=None)) is None
    assert parse_turn_stamp(_turn(stamp="  ")) is None
    with pytest.raises(ValueError, match="unparseable timestamp"):
        parse_turn_stamp(_turn(stamp="8th of Mayish 2023"))


# -- D1: reader tokenizer and rank-based truncation --------------------------------------------


class _WordCounter:
    counter_id = "words"

    def count(self, text: str) -> int:
        return len(text.split())

    def describe(self) -> dict[str, Any]:
        return {"counter_id": self.counter_id}


def _ranked_context() -> RetrievedContext:
    lines = ["a1 a2 a3", "b1 b2 b3", "c1 c2 c3", "d1 d2 d3"]
    scores = [1.0, 0.25, 0.5, 0.1]  # b and d are the weak ones
    spans, offset = [], 0
    for line in lines:
        spans.append((offset, offset + len(line)))
        offset += len(line) + 1
    evidence = tuple(
        Evidence(turn_id=f"t{i}", score=scores[i], meta={"span": spans[i]}) for i in range(4)
    )
    return RetrievedContext(text="\n".join(lines), tokens=12, evidence=evidence)


def test_truncate_by_rank_drops_the_lowest_scored_lines_in_order() -> None:
    ctx = _ranked_context()
    out = truncate_by_rank(ctx.text, ctx.evidence, 6, _WordCounter())
    assert out is not None
    text, tokens, truncated, kept = out
    assert truncated and tokens == 6
    assert text == "a1 a2 a3\nc1 c2 c3"  # d (0.1) then b (0.25) dropped; a, c stay chronological
    assert [e.turn_id for e in kept] == ["t0", "t2"]
    assert all(text[e.meta["span"][0] : e.meta["span"][1]] for e in kept)


def test_truncate_by_rank_noop_and_fallback() -> None:
    ctx = _ranked_context()
    text, _, truncated, kept = truncate_by_rank(ctx.text, ctx.evidence, 99, _WordCounter())  # type: ignore[misc]
    assert text == ctx.text and truncated is False and len(kept) == 4
    bare = (Evidence(turn_id="t", score=1.0),)
    assert truncate_by_rank(ctx.text, bare, 3, _WordCounter()) is None


class _RankedSystem(VerbatimSystem):
    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        return _ranked_context()


def test_runner_reader_token_mode_records_both_counts(tmp_path: Path) -> None:
    reader = OpenAICompatReader(model="m")
    reader._httpx = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, [])
    dataset = SyntheticDataset(n_items=1, turns_per_item=4, facts_per_item=1)
    config = RunConfig(
        run_id="tok",
        protocol=RunProtocol(protocol_id="gap", budget_tokens=6, top_k=5, seed=3),
        out_dir=tmp_path,
        token_count="reader",
    )
    runner = EvalRunner(dataset, _RankedSystem(), reader, ContainsJudge(), config, _WordCounter())
    asyncio.run(runner.run())
    _, rows, _ = read_run(tmp_path / "tok" / "results.jsonl")
    assert rows
    for row in rows:
        assert row["context_tokens"] == 6 and row["meta"]["engine_tokens"] == 12
        assert row["context_truncated"] is True
        assert row["retrieved_ids"] == ["t0", "t2"]


def test_heuristic_mode_rows_are_unchanged(tmp_path: Path) -> None:
    reader = OpenAICompatReader(model="m")
    reader._httpx = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, [])
    _, rows, _ = _run(tmp_path, reader)
    assert all("engine_tokens" not in r["meta"] for r in rows)


def test_token_counter_selection_and_fallback(caplog: pytest.LogCaptureFixture) -> None:
    assert build_token_counter(C01Config()) is None  # heuristic: the runner's default
    with pytest.raises(ValueError):
        build_token_counter(C01Config(token_count="bogus"))
    with caplog.at_level(logging.WARNING, logger="memspine_evals.tokens"):
        counter = build_token_counter(
            C01Config(token_count="reader", tokenizer_id="no-such-org/no-such-tokenizer")
        )
    assert isinstance(counter, HeuristicTokenCounter)
    assert "heuristic" in caplog.text


def test_reader_tokenizer_loads_offline_when_cached() -> None:
    counter = load_reader_tokenizer_counter()
    if counter is None:
        pytest.skip("no Qwen3 tokenizer in the local Hugging Face cache")
    assert counter.count("") == 0
    n = counter.count("Caroline went hiking last Friday.")
    assert 4 <= n <= 12
    assert counter.describe()["counter_id"].startswith("hf-Qwen/Qwen3")


# -- A10: cluster-bootstrap CI ------------------------------------------------------------------


def test_summary_carries_a_cluster_bootstrap_ci(tmp_path: Path) -> None:
    reader = OpenAICompatReader(model="m")
    reader._httpx = _fake_httpx({"prompt_tokens": 5, "completion_tokens": 2}, [])
    _, _, summary = _run(tmp_path, reader)
    block = summary["summary"]["accuracy_ci"]
    assert block["method"] == "cluster-bootstrap-by-item"
    assert block["n_clusters"] == 2 and block["resamples"] == 2000 and block["level"] == 0.95
    lo, hi = block["ci95"]
    assert 0.0 <= lo <= block["accuracy"] <= hi <= 1.0
    assert block["accuracy"] == summary["summary"]["score_mean"]
    assert block["ci95"] == summary["summary"]["score_ci95"]  # no failed rows: same sample


def test_ingest_rows_carry_the_batch_write_timers_only_when_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I73: the batch's per-step timings ride the first ingest line; absent when off."""
    monkeypatch.setenv("MEMSPINE_FORENSICS_DIR", str(tmp_path))
    system = MemspineSystem()
    system.begin_run("run-T--memspine")
    steps = {"embed": {"count": 2, "total_ms": 3.0, "mean_ms": 1.5, "p50_ms": 1.0, "p95_ms": 2.0}}
    calls: list[bool] = []

    def write_timers(*, reset: bool = False) -> dict[str, Any]:
        calls.append(reset)
        return steps

    system._engine = SimpleNamespace(write_timers=write_timers)
    turns = [_turn("D1:1"), _turn("D1:2")]
    system._write_ingest_log([_record(), _record(record_id="r-2")], turns, ["Ann: hi", "Ann: yo"])
    path = tmp_path / "ingest.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["write_timers"] == steps and "write_timers" not in lines[1]
    assert calls == [True]  # taken as a per-batch delta
    system._engine = SimpleNamespace(write_timers=lambda *, reset=False: {})
    system._write_ingest_log([_record()], [_turn()], ["Ann: hi"])
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert "write_timers" not in last
