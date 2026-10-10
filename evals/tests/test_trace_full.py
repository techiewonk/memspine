"""``--trace-full``: opt-in logging of exact prompts, raw replies and the write trace.

Off by default (rows stay byte-identical, which ``test_reader_raw`` also pins); on, a row's
``meta["trace_full"]`` holds the reader and judge calls of that question; scoring never changes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from memspine_evals import readers, trace_full
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.judge import ContainsJudge
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import QA_PROMPTS, OpenAICompatReader
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems import VerbatimSystem

from memspine.core.records import MemoryRecord, SourceInfo

REPLY = "Answer: a dog"


def _fake_httpx(reply: str) -> Any:
    class FakeResponse:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {
                "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }

    class FakeClient:
        def __init__(self, **_: Any) -> None: ...

        async def post(self, url: str, json: dict[str, Any], headers: Any) -> FakeResponse:
            return FakeResponse()

    return type("H", (), {"AsyncClient": FakeClient})


def _run(tmp_path: Path, run_id: str, *, trace: bool, prompt: Any = None) -> list[dict[str, Any]]:
    reader = OpenAICompatReader(model="m", prompt=prompt or QA_PROMPTS["dated"])
    reader._httpx = _fake_httpx(REPLY)
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = RunConfig(
        run_id=run_id,
        protocol=RunProtocol(protocol_id="tf", budget_tokens=400, top_k=5, seed=3),
        out_dir=tmp_path,
        trace_full=trace,
    )
    asyncio.run(EvalRunner(dataset, VerbatimSystem(), reader, ContainsJudge(), config).run())
    lines = (tmp_path / run_id / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return [r for r in map(json.loads, lines) if r.get("kind") == "result"]


def _reads(tmp_path: Path, run_id: str) -> list[dict[str, Any]]:
    path = tmp_path / f"{run_id}--trace" / "reads.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _strip(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    skip = ("latency_retrieve_ms", "latency_answer_ms", "latency_judge_ms", "run_id")
    return [{k: v for k, v in r.items() if k not in skip} for r in rows]


def test_off_by_default_writes_no_trace_folder(tmp_path: Path) -> None:
    _run(tmp_path, "off", trace=False)
    assert not (tmp_path / "off--trace").exists()


def test_on_logs_prompt_and_raw_reply_without_changing_rows(tmp_path: Path) -> None:
    off = _run(tmp_path, "a", trace=False)
    on = _run(tmp_path, "b", trace=True)
    assert _strip(on) == _strip(off)  # rows, scores and meta are untouched
    reads = _reads(tmp_path, "b")
    assert [r["query_id"] for r in reads] == [r["query_id"] for r in on]
    for row, read in zip(on, reads, strict=True):
        call = read["reader_calls"][0]
        assert call["kind"] == "reader" and call["reply"] == REPLY
        assert row["question"] in call["prompt"] and read["context_text"] in call["prompt"]
        assert read["judge_calls"] == []  # ContainsJudge makes no model call
        assert read["verdict"]["score"] == row["score"]


def test_system_prompt_is_recorded_for_op_bench_style_prompts(tmp_path: Path) -> None:
    from memspine_evals.readers import SYSTEM_QA_PROMPTS

    _run(tmp_path, "op", trace=True, prompt=SYSTEM_QA_PROMPTS["opbench_assistant"])
    call = _reads(tmp_path, "op")[0]["reader_calls"][0]
    assert call["system"].startswith("You are a communication expert")
    assert "Latest Input" in call["prompt"]


def test_finalize_gzips_only_large_files(tmp_path: Path) -> None:
    import gzip

    (tmp_path / "big.jsonl").write_text("x" * 100, encoding="utf-8")
    (tmp_path / "small.jsonl").write_text("y", encoding="utf-8")
    assert trace_full.finalize(tmp_path, threshold=50) == ["big.jsonl"]
    assert not (tmp_path / "big.jsonl").exists() and (tmp_path / "small.jsonl").exists()
    with gzip.open(tmp_path / "big.jsonl.gz", "rt") as fh:
        assert fh.read() == "x" * 100


def test_llm_chat_calls_are_recorded(monkeypatch: Any) -> None:
    class Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "CORRECT"}}]}

    class Client:
        async def post(self, url: str, json: dict[str, Any], headers: Any) -> Resp:
            return Resp()

    monkeypatch.setattr(readers, "_shared_client", lambda httpx, timeout: Client())
    chat = readers.openai_compat_chat("judge-model")

    async def go() -> list[dict[str, Any]]:
        with trace_full.capture(True) as sink:
            assert await chat("grade this", "be strict") == "CORRECT"
        assert sink is not None
        return sink

    sink = asyncio.run(go())
    assert sink[0]["kind"] == "llm"
    assert (sink[0]["system"], sink[0]["prompt"], sink[0]["reply"]) == (
        "be strict",
        "grade this",
        "CORRECT",
    )
    # outside a capture (or disabled) nothing is recorded and nothing fails
    asyncio.run(chat("x"))
    with trace_full.capture(False) as off:
        trace_full.record("llm", prompt="x")
    assert off is None


def test_write_trace_row_keeps_stored_fields_and_signals() -> None:
    rec = MemoryRecord(
        namespace="n",
        memory_type="episodic",
        content="Caroline: Call me on 555-123-4567.",
        tags=["spk:caroline"],
        source=SourceInfo(parents=["p1"]),
    )
    row = trace_full.write_trace_row(rec, "D1:3", rec.content)
    assert row["turn"] == "D1:3" and row["written"] and row["record_id"] == rec.record_id
    assert row["stored"]["tags"] == ["spk:caroline"]
    assert row["stored"]["source"]["parents"] == ["p1"]
    assert row["signals"]["pii"] == ["phone"]
    assert row["signals"]["validation"] == {"empty": False, "chars": len(rec.content)}
    derived = trace_full.derived_row(rec, {"p1": "D1:1"})
    assert derived["derived"] and derived["parent_turns"] == ["D1:1"]
    missing = trace_full.write_trace_row(None, "D1:4", "x")
    assert missing["written"] is False and missing["stored"] is None


def test_query_analysis_is_deterministic_and_flags_set_questions() -> None:
    a = trace_full.query_analysis("What books has Melanie read?")
    assert a == trace_full.query_analysis("What books has Melanie read?")
    assert a["flags"]["is_set_question"] is True
    assert trace_full.query_analysis("When did she move?")["flags"]["is_set_question"] is False


def test_cli_flag_defaults_off() -> None:
    from memspine_evals.cli import build_parser

    parser = build_parser()
    base = ["c0-1", "--dataset", "synthetic"]
    assert parser.parse_args(base).trace_full is False
    assert parser.parse_args([*base, "--trace-full"]).trace_full is True


# -- the memspine adapter: write trace and read extras --------------------------------------


def _adapter_inputs() -> tuple[Any, Any]:
    from types import SimpleNamespace

    from memspine_evals.contracts import Turn

    turn = Turn(turn_id="D1:1", session_id="s1", speaker="Ann", text="hi", timestamp=None)
    record = SimpleNamespace(
        record_id="r-1", content="Ann: hi", valid_from=None, memory_type="episodic",
        group_id="s1", session_id="s1", quarantined=False, trust=0.8, tags=["spk:ann"],
        status=SimpleNamespace(value="activated"),
    )  # fmt: skip
    return turn, record


def test_adapter_writes_the_write_trace_only_when_asked(tmp_path: Path, monkeypatch: Any) -> None:
    from memspine_evals.systems.memspine_system import MemspineSystem

    turn, record = _adapter_inputs()
    monkeypatch.setenv("MEMSPINE_FORENSICS_DIR", str(tmp_path / "off"))
    off = MemspineSystem()
    off.begin_run("r--memspine")
    off._write_ingest_log([record], [turn], ["Ann: hi"])
    assert (tmp_path / "off" / "ingest.jsonl").exists()
    assert not (tmp_path / "off" / "write_trace.jsonl").exists()

    monkeypatch.delenv("MEMSPINE_FORENSICS_DIR")
    on = MemspineSystem()
    on.set_trace_dir(str(tmp_path / "on"))  # the trace folder alone is enough
    on.begin_run("r--memspine")
    on._item_id = "conv-1"
    on._write_ingest_log([record], [turn], ["Ann: hi"])
    assert (tmp_path / "on" / "ingest.jsonl").exists()  # the write audit rides along
    row = json.loads((tmp_path / "on" / "write_trace.jsonl").read_text(encoding="utf-8"))
    assert row["turn"] == "D1:1" and row["item"] == "conv-1"
    assert row["stored"]["tags"] == ["spk:ann"] and row["stored"]["trust"] == 0.8
    assert row["signals"]["instruction_shaped"] is False


def test_adapter_logs_derived_records_with_parents(tmp_path: Path, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from memspine_evals.systems.memspine_system import MemspineSystem

    system = MemspineSystem()
    system.set_trace_dir(str(tmp_path))
    system.begin_run("r--memspine")
    system._origin = {"r-1": "D1:1"}
    fact = SimpleNamespace(
        record_id="f-1", content="Ann likes tea", memory_type="semantic", tags=[],
        source=SimpleNamespace(role="system", channel="internal", principal=None, parents=["r-1"]),
    )  # fmt: skip
    own = SimpleNamespace(record_id="r-1", content="Ann: hi", memory_type="episodic", tags=[])

    async def list_records(namespace: str) -> list[Any]:
        return [own, fact]

    system._engine = SimpleNamespace(_storage=SimpleNamespace(list_records=list_records))
    asyncio.run(system._write_derived_trace())
    asyncio.run(system._write_derived_trace())  # a second call repeats nothing
    lines = (tmp_path / "write_trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["derived"] and row["parent_turns"] == ["D1:1"] and row["content"] == "Ann likes tea"


def test_forensics_row_gets_the_trace_full_block_only_when_on(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from types import SimpleNamespace

    from memspine_evals.systems.memspine_system import MemspineSystem

    stages = {"perspective": {"asker": "ann"}, "dedupe_dropped": [("a", "b", 0.97)]}
    for flag, name in ((True, "on"), (False, "off")):
        d = tmp_path / name
        monkeypatch.setenv("MEMSPINE_FORENSICS_DIR", str(d))
        system = MemspineSystem()
        if flag:
            system.set_trace_dir(str(d))
        system.begin_run("r--memspine")
        system._write_forensics(
            str(d), "What books has Ann read?", stages, SimpleNamespace(records=[])
        )
    on = json.loads((tmp_path / "on" / "forensics.jsonl").read_text(encoding="utf-8"))
    off = json.loads((tmp_path / "off" / "forensics.jsonl").read_text(encoding="utf-8"))
    assert "trace_full" not in off
    block = on["trace_full"]
    assert block["query_analysis"]["flags"]["is_set_question"] is True
    assert block["perspective"] == {"asker": "ann"} and block["dedupe_dropped"] == [
        ["a", "b", 0.97]
    ]


def test_real_engine_run_writes_the_whole_trace_folder(tmp_path: Path) -> None:
    """End to end on the real adapter (hash embeddings, CPU, fake reader): the trace folder
    holds the write trace with timers, the read-path rows and the exact prompts."""
    from memspine_evals.systems.memspine_system import MemspineSystem

    system = MemspineSystem(config={"embedding": {"provider": "hash", "dim": 64}})
    reader = OpenAICompatReader(model="m", prompt=QA_PROMPTS["dated"])
    reader._httpx = _fake_httpx(REPLY)
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = RunConfig(
        run_id="e2e--memspine",
        protocol=RunProtocol(protocol_id="tf", budget_tokens=400, top_k=5, seed=3),
        out_dir=tmp_path,
        trace_full=True,
    )
    asyncio.run(EvalRunner(dataset, system, reader, ContainsJudge(), config).run())
    folder = tmp_path / "e2e--trace"
    names = sorted(p.name for p in folder.iterdir())
    assert {"write_trace.jsonl", "ingest.jsonl", "forensics.jsonl", "reads.jsonl"} <= set(names)
    writes = [json.loads(x) for x in (folder / "write_trace.jsonl").read_text().splitlines()]
    assert writes and writes[0]["written"] and "tags" in writes[0]["stored"]
    assert any(w.get("write_timers") for w in writes)  # observability.write_timers was switched on
    fx = [json.loads(x) for x in (folder / "forensics.jsonl").read_text().splitlines()]
    block = fx[0]["trace_full"]
    assert {"query_analysis", "records", "final_not_in_context", "assembled"} <= set(block)
    assert fx[0]["vector"] and fx[0]["context_records"]
    reads = [json.loads(x) for x in (folder / "reads.jsonl").read_text().splitlines()]
    assert len(reads) == len(fx) and reads[0]["reader_calls"][0]["prompt"]
