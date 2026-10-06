"""Screening: the disk cache of paid calls, retrieval-only runs, and screen_compare.

Every test runs offline: the stub LiteLLM transport and the hash embedder.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import screen_compare as sc
from memspine_evals.bedrock import QWEN3_32B
from memspine_evals.call_cache import (
    chat_key,
    embed_key,
    install_call_cache,
    reader_scope,
)
from memspine_evals.cli import build_parser
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.experiments import C01Config, run_c0_1
from memspine_evals.results import read_run
from memspine_evals.screen import coverage, coverage_summary, normalise_evidence
from memspine_evals.stub_llm import install_stub_litellm

#: fast offline engine with LLM roles at write time: hash embeddings, fact mining on
MINING = {
    "embedding": {"provider": "hash"},
    "memories": {
        "episodic": {
            "enabled": True,
            "policies": {
                "consolidation": {
                    "mine_facts": True,
                    "min_session_records": 2,
                    "session_gap_minutes": 100_000,
                }
            },
        },
        "semantic": {"enabled": True},
    },
}

MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "hello /no_think"}]


def _summary(out: Path, run_id: str) -> dict[str, Any]:
    return json.loads((out / run_id / "summary.json").read_text(encoding="utf-8"))


def _rows(out: Path, run_id: str) -> list[dict[str, Any]]:
    _, rows, _ = read_run(out / run_id / "results.jsonl")
    return rows


# -- keys ----------------------------------------------------------------------


def test_chat_keys_are_stable_and_complete() -> None:
    a = chat_key({"model": "m", "messages": MESSAGES, "temperature": 0, "max_tokens": 64})
    b = chat_key({"max_tokens": 64, "temperature": 0.0, "messages": MESSAGES, "model": "m"})
    assert a == b  # option order and 0 vs 0.0 do not matter
    # credentials, endpoints and timeouts are not part of the key
    assert a == chat_key(
        {
            "model": "m",
            "messages": MESSAGES,
            "temperature": 0,
            "max_tokens": 64,
            "timeout": 5,
            "aws_region_name": "us-east-1",
        }
    )
    for change in (
        {"model": "m2"},
        {"messages": [*MESSAGES, {"role": "user", "content": "x"}]},
        {"max_tokens": 65},
        {"response_format": {"type": "json_object"}},
        {"temperature": None},
    ):
        assert (
            chat_key(
                {"model": "m", "messages": MESSAGES, "temperature": 0, "max_tokens": 64, **change}
            )
            != a
        )
    # a fixed request has a fixed key across processes (canonical JSON + SHA-256)
    assert len(a) == 64 and a == chat_key(
        json.loads(
            json.dumps({"model": "m", "messages": MESSAGES, "temperature": 0, "max_tokens": 64})
        )
    )


def test_embed_keys_cover_model_input_type_and_dimensions() -> None:
    key = embed_key("e", "text", "search_query", 1024)
    assert key == embed_key("e", "text", "search_query", 1024)
    assert (
        len(
            {
                key,
                embed_key("e2", "text", "search_query", 1024),
                embed_key("e", "text", "search_document", 1024),
                embed_key("e", "text", "search_query", 512),
                embed_key("e", "text2", "search_query", 1024),
            }
        )
        == 5
    )


# -- the cache -----------------------------------------------------------------


def test_completion_hit_and_miss_accounting(tmp_path: Path) -> None:
    async def go() -> list[Any]:
        import litellm

        replies = []
        for _ in range(3):
            replies.append(
                await litellm.acompletion(model=QWEN3_32B, messages=MESSAGES, max_tokens=32)
            )
        return replies

    with install_stub_litellm() as stub, install_call_cache(tmp_path) as cache:
        first, second, _third = asyncio.run(go())
    assert sum(stub.calls.values()) == 1  # only the miss reached the provider
    assert cache.stats.chat_misses == 1 and cache.stats.chat_hits == 2
    assert second.choices[0].message.content == first.choices[0].message.content
    assert second.usage.prompt_tokens == 0 and second.usage.completion_tokens == 0
    assert first.usage.prompt_tokens > 0
    # what the hits saved: the original tokens, priced
    hit_in, hit_out = cache.stats.chat_hit_tokens[QWEN3_32B]
    assert hit_in == 2 * first.usage.prompt_tokens and hit_out == 2 * first.usage.completion_tokens
    saved = cache.stats.usd_saved({QWEN3_32B: (1e6, 0.0)})
    assert saved == pytest.approx(hit_in)
    # the cache persists: a new process (a fresh CallCache) on the same dir hits
    with install_stub_litellm() as stub2, install_call_cache(tmp_path) as cache2:
        asyncio.run(go())
    assert sum(stub2.calls.values()) == 0 and cache2.stats.chat_hits == 3


def test_non_zero_temperature_is_never_cached(tmp_path: Path) -> None:
    async def go() -> None:
        import litellm

        for _ in range(2):
            await litellm.acompletion(model="m", messages=MESSAGES, temperature=0.7)

    with install_stub_litellm() as stub, install_call_cache(tmp_path) as cache:
        asyncio.run(go())
    assert sum(stub.calls.values()) == 2
    assert cache.stats.uncacheable == 2 and cache.stats.chat_hits == cache.stats.chat_misses == 0


def test_reader_scope_bypasses_unless_cache_reader(tmp_path: Path) -> None:
    async def go() -> None:
        import litellm

        with reader_scope():
            for _ in range(2):
                await litellm.acompletion(model="m", messages=MESSAGES, temperature=0)

    with install_stub_litellm() as stub, install_call_cache(tmp_path / "a") as cache:
        asyncio.run(go())
    assert sum(stub.calls.values()) == 2 and cache.stats.bypassed == 2
    with (
        install_stub_litellm() as stub,
        install_call_cache(tmp_path / "b", cache_reader=True) as cache,
    ):
        asyncio.run(go())
    assert sum(stub.calls.values()) == 1 and cache.stats.chat_hits == 1


def test_embedding_batches_split_into_hits_and_misses(tmp_path: Path) -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        import litellm

        response = await litellm.aembedding(
            model="bedrock/cohere.embed-v4:0",
            input=texts,
            dimensions=64,
            input_type="search_document",
        )
        return [list(item["embedding"]) for item in response.data]

    with install_stub_litellm() as stub, install_call_cache(tmp_path) as cache:
        first = asyncio.run(embed(["alpha beta", "gamma"]))
        mixed = asyncio.run(embed(["gamma", "delta", "alpha beta"]))
    assert stub.embeddings == 3  # alpha beta, gamma, then only delta
    assert cache.stats.embed_misses == 3 and cache.stats.embed_hits == 2
    assert mixed[0] == first[1] and mixed[2] == first[0]  # bit-identical, in input order
    assert len(mixed) == 3 and len(mixed[1]) == 64
    assert cache.stats.embed_hit_tokens["bedrock/cohere.embed-v4:0"] > 0


# -- coverage ------------------------------------------------------------------


def test_evidence_normalisation_and_coverage() -> None:
    assert normalise_evidence(["D8:6; D9:17", "D1:05", "D8:6", "x"]) == (
        "D8:6",
        "D9:17",
        "D1:5",
        "x",
    )
    assert coverage(["D1:1", "D2:3"], ["D1:1", "D2:3"]) == {
        "ev_all": True,
        "ev_any": True,
        "ev_frac": 1.0,
    }
    assert coverage(["D1:1"], ["D1:1; D2:3"]) == {"ev_all": False, "ev_any": True, "ev_frac": 0.5}
    assert coverage(["D1:1"], []) == {"ev_all": None, "ev_any": None, "ev_frac": None}
    summary = coverage_summary(
        [
            {
                "status": "completed",
                "type_label": "cat1",
                "context_tokens": 10,
                "meta": {"ev_all": True, "ev_any": True, "ev_frac": 1.0},
            },
            {
                "status": "completed",
                "type_label": "cat1",
                "context_tokens": 30,
                "meta": {"ev_all": False, "ev_any": False, "ev_frac": 0.0},
            },
            {
                "status": "completed",
                "type_label": "cat5",
                "context_tokens": 20,
                "meta": {"ev_all": None, "ev_any": None, "ev_frac": None},
            },
        ]
    )
    assert summary["cat1"]["ev_all"] == 0.5 and summary["cat1"]["context_tokens"] == 20
    assert summary["cat5"]["ev_all"] is None and summary["cat5"]["n"] == 1
    assert summary["all"]["n"] == 3 and summary["all"]["n_with_evidence"] == 2


# -- retrieval-only runs -------------------------------------------------------


def _retrieval_only(**extra: Any) -> C01Config:
    return C01Config(
        retrieval_only=True,
        include_memspine=True,
        only_systems=("memspine",),
        memspine_config={"embedding": {"provider": "hash"}},
        **extra,
    )


def test_retrieval_only_rows_carry_coverage_and_call_no_model(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=2, turns_per_item=12, facts_per_item=3)
    with install_stub_litellm() as stub:
        asyncio.run(run_c0_1(dataset, _retrieval_only(), tmp_path, run_id="ro"))
    assert sum(stub.calls.values()) == 0 and stub.embeddings == 0
    rows = _rows(tmp_path, "ro--memspine")
    assert len(rows) == sum(len(item.queries) for item in dataset.items())
    for row in rows:
        assert row["status"] == "completed" and row["answer"] == ""
        assert row["protocol_id"] == "c0-1-retrieval-only"
        assert {"ev_all", "ev_any", "ev_frac", "gold_evidence"} <= set(row["meta"])
        assert row["meta"]["gold_evidence"] and row["context_tokens"] > 0
        assert row["score"] == (1.0 if row["meta"]["ev_all"] else 0.0)
    summary = _summary(tmp_path, "ro--memspine")
    assert summary["coverage"]["all"]["n"] == len(rows)
    assert summary["coverage"]["all"]["ev_all"] is not None
    assert summary["judge_model_calls"] == 0 and summary["loop_model_calls"] == 0
    labels = summary["manifest"]["labels"]
    assert labels["mode"] == "retrieval-only" and labels["retrieval_only"] is True
    assert summary["manifest"]["reader"]["model"] == "none"
    assert "cache" not in summary  # no --cache-dir


def test_retrieval_only_matches_the_qa_read(tmp_path: Path) -> None:
    """The read path is the QA run's: same retrieved ids and context per question."""
    dataset = SyntheticDataset(n_items=1, turns_per_item=12, facts_per_item=3)
    with install_stub_litellm():
        asyncio.run(run_c0_1(dataset, _retrieval_only(), tmp_path, run_id="ro"))
        asyncio.run(
            run_c0_1(
                dataset,
                C01Config(
                    mode="qa",
                    bedrock=True,
                    max_model_calls=1000,
                    include_memspine=True,
                    only_systems=("memspine",),
                    memspine_config={"embedding": {"provider": "hash"}},
                ),
                tmp_path,
                run_id="qa",
            )
        )
    ro = {r["query_id"]: r for r in _rows(tmp_path, "ro--memspine")}
    qa = {r["query_id"]: r for r in _rows(tmp_path, "qa--memspine")}
    assert ro.keys() == qa.keys()
    for qid, row in qa.items():
        assert ro[qid]["retrieved_ids"] == row["retrieved_ids"]
        assert ro[qid]["context_tokens"] == row["context_tokens"]


def test_cached_engine_roles_make_a_rerun_free(tmp_path: Path) -> None:
    """Populate the cache with one run; the same run again makes 0 provider calls,
    counts 0 model calls, charges $0 and reports what the hits saved."""
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = _retrieval_only(
        memspine_llm="bedrock-qwen3",
        memspine_build_sleep=True,
        max_model_calls=1000,
        cache_dir=str(tmp_path / "cache"),
        prices_per_mtok=((QWEN3_32B, 1.0, 2.0),),
    )
    config = type(config)(**{**config.__dict__, "memspine_config": MINING})
    with install_stub_litellm() as first:
        asyncio.run(run_c0_1(dataset, config, tmp_path, run_id="fill"))
    filled = _summary(tmp_path, "fill--memspine")
    assert sum(first.calls.values()) >= 1
    assert filled["cache_misses"] >= 1 and filled["loop_model_calls"] >= 1
    assert first.calls["reader"] == 0 and first.calls["harness_judge"] == 0

    with install_stub_litellm() as second:
        asyncio.run(run_c0_1(dataset, config, tmp_path, run_id="screen"))
    summary = _summary(tmp_path, "screen--memspine")
    assert sum(second.calls.values()) == 0, dict(second.calls)
    assert summary["cache_misses"] == 0
    assert summary["cache_hits"] == filled["cache_misses"]
    assert summary["loop_model_calls"] == 0 and summary["judge_model_calls"] == 0
    assert summary["spend"]["usd"] == 0.0 and summary["spend"]["engine_calls"] == 0
    assert summary["usd_saved"] == pytest.approx(filled["spend"]["usd"])
    assert summary["cache"]["chat_hits"] == summary["cache_hits"]
    assert summary["manifest"]["labels"]["call_cache"]["cache_reader"] is False

    # the screen read what the populating run read (derived records get fresh ids per
    # run, so compare the raw turns and the coverage)
    def reads(run_id: str) -> dict[str, Any]:
        return {
            r["query_id"]: ([i for i in r["retrieved_ids"] if "-t" in i], r["meta"]["ev_frac"])
            for r in _rows(tmp_path, run_id)
        }

    assert reads("fill--memspine") == reads("screen--memspine")


def test_cache_never_serves_reader_or_judge_by_default(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = C01Config(
        mode="qa",
        bedrock=True,
        max_model_calls=1000,
        only_systems=("verbatim-bm25",),
        cache_dir=str(tmp_path / "cache"),
    )
    n = sum(len(item.queries) for item in dataset.items())
    for run_id in ("a", "b"):
        with install_stub_litellm() as stub:
            asyncio.run(run_c0_1(dataset, config, tmp_path, run_id=run_id))
        assert stub.calls["reader"] == n
    summary = _summary(tmp_path, "b--verbatim-bm25")
    assert summary["cache_hits"] == 0 and summary["cache"]["bypassed"] == 2 * n


def test_cli_parses_the_screening_flags() -> None:
    args = build_parser().parse_args(
        ["c0-1", "--dataset", "synthetic", "--retrieval-only", "--cache-dir", "c", "--cache-reader"]
    )
    assert args.retrieval_only and args.cache_dir == "c" and args.cache_reader
    plain = build_parser().parse_args(["c0-1", "--dataset", "synthetic"])
    assert not plain.retrieval_only and plain.cache_dir is None and not plain.cache_reader


# -- screen_compare ------------------------------------------------------------


def _write_run(folder: Path, rows: list[dict[str, Any]]) -> Path:
    folder.mkdir(parents=True)
    lines = [json.dumps({"kind": "manifest"})]
    for row in rows:
        lines.append(json.dumps({"kind": "result", "status": "completed", **row}))
    (folder / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def _cov(qid: str, cat: str, ev: bool | None, ctx: int = 100) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "retrieval_only": True,
        "ev_all": ev,
        "ev_any": ev,
        "ev_frac": None if ev is None else float(ev),
    }
    return {
        "item_id": qid.split("-")[0],
        "query_id": qid,
        "type_label": cat,
        "score": float(bool(ev)),
        "context_tokens": ctx,
        "protocol_id": "c0-1-retrieval-only",
        "meta": meta,
    }


def test_screen_compare_coverage_deltas_over_split_runs(tmp_path: Path, capsys: Any) -> None:
    # base split per conversation (two dirs matched by one glob)
    _write_run(
        tmp_path / "s--base--conv-1--memspine",
        [_cov("1-0", "cat1", False), _cov("1-1", "cat1", True)],
    )
    _write_run(
        tmp_path / "s--base--conv-2--memspine",
        [_cov("2-0", "cat2", False), _cov("2-1", "cat5", None)],
    )
    _write_run(
        tmp_path / "s--arm--conv-1--memspine",
        [_cov("1-0", "cat1", True, 150), _cov("1-1", "cat1", True)],
    )
    _write_run(
        tmp_path / "s--arm--conv-2--memspine",
        [_cov("2-0", "cat2", True), _cov("2-1", "cat5", None)],
    )
    out = tmp_path / "cmp.json"
    assert (
        sc.main(
            [
                "--base",
                str(tmp_path / "s--base--conv-*--memspine"),
                "--arm",
                str(tmp_path / "s--arm--conv-*--memspine"),
                "--json",
                str(out),
            ]
        )
        == 0
    )
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["n_paired"] == 4 and result["qa"] is False
    overall = result["categories"]["all"]
    assert overall["n_with_evidence"] == 3
    assert overall["ev_all"]["base"] == pytest.approx(1 / 3) and overall["ev_all"]["arm"] == 1.0
    assert overall["ev_all"]["won"] == 2 and overall["ev_all"]["lost"] == 0
    assert overall["ev_all"]["p"] == pytest.approx(0.5)
    assert overall["context_tokens"]["delta"] == pytest.approx(12.5)
    assert result["categories"]["cat5"]["ev_all"]["delta"] is None
    assert "accuracy" not in overall
    assert "| all | 4 | 3 |" in capsys.readouterr().out


def test_screen_compare_accuracy_sign_test(tmp_path: Path) -> None:
    def qa(qid: str, score: float, status: str = "completed") -> dict[str, Any]:
        return {
            "item_id": "c",
            "query_id": qid,
            "type_label": "cat1",
            "score": score,
            "status": status,
            "retrieved_ids": ["D1:1"],
            "protocol_id": "c0-1-qa",
        }

    base = [qa(f"0-{i}", 0.0) for i in range(8)] + [qa("0-8", 1.0), qa("0-9", 1.0, "error")]
    arm = [qa(f"0-{i}", 1.0) for i in range(8)] + [qa("0-8", 0.0), qa("0-9", 1.0)]
    result = sc.compare(
        sc.load_rows([_write_run(tmp_path / "b", base)]),
        sc.load_rows([_write_run(tmp_path / "a", arm)]),
    )
    acc = result["categories"]["all"]["accuracy"]
    assert result["qa"] is True
    assert acc["won"] == 9 and acc["lost"] == 1  # the base error counts as wrong
    assert acc["delta"] == pytest.approx(0.8)
    assert acc["p"] == pytest.approx(22 / 1024)  # 2 * (C(10,0) + C(10,1)) / 2^10
    assert sc.sign_test(0, 0) == 1.0 and sc.sign_test(5, 5) == 1.0


def test_screen_compare_recomputes_coverage_for_qa_rows(tmp_path: Path) -> None:
    rows = [
        {
            "item_id": "c",
            "query_id": "0-0",
            "type_label": "cat1",
            "score": 1.0,
            "retrieved_ids": ["D1:1"],
            "protocol_id": "c0-1-qa",
        }
    ]
    gold = {("c", "0-0"): ("D1:1; D1:2",)}
    result = sc.compare(
        sc.load_rows([_write_run(tmp_path / "b", rows)]),
        sc.load_rows([_write_run(tmp_path / "a", rows)]),
        gold,
    )
    cell = result["categories"]["all"]
    assert cell["ev_frac"]["base"] == 0.5 and cell["ev_all"]["base"] == 0.0
