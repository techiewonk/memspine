"""OP-Bench response level: parser, judge routing, aggregation, reader prompt, CLI wiring.

No model, no network, no OP-Bench data: a fake checkout holds stand-in judge prompts, the
fixture task file is the synthetic one in ``fixtures/op_bench``, and the chat / embedder /
reader are fakes. The real task file and the real judge prompts stay local and uncommitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from memspine_evals import opbench
from memspine_evals.cli import apply_opbench_protocol, build_parser, cmd_c0_1
from memspine_evals.contracts import Query, ReaderAnswer
from memspine_evals.datasets.op_bench import OPBenchDataset
from memspine_evals.experiments import C01Config, build_judge, build_reader_and_judge
from memspine_evals.judge import JudgeScale
from memspine_evals.metrics import CostModel
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import (
    OPBENCH_ASSISTANT_PROMPT,
    SYSTEM_QA_PROMPTS,
    OpenAICompatReader,
    prompt_describe,
)
from memspine_evals.results import read_run
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems import VerbatimSystem

FIX = Path(__file__).resolve().parent / "fixtures" / "op_bench"

FAKE_PROMPTS = {
    "SCORE_IRRELEVANCE_PROMPT": "IRR Q={question} R={response}",
    "SCORE_SYCOPHANCY_FACT_PROMPT": "FACT Q={question} R={response}",
    "SCORE_SYCOPHANCY_VALUE_PROMPT": "VALUE Q={question} R={response}",
    "SCORE_SYCOPHANCY_MEMORY_PROMPT": "MEM Q={question} R={response}",
}


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    """A stand-in OP-Bench checkout: only src/opbench/prompts.py, plus a non-literal line."""
    src = tmp_path / "ck" / "src" / "opbench"
    src.mkdir(parents=True)
    body = "\n".join(f"{name} = {text!r}" for name, text in FAKE_PROMPTS.items())
    (src / "prompts.py").write_text(
        "import os\nOTHER = os.getcwd()\n" + body + "\n", encoding="utf-8"
    )
    return tmp_path / "ck"


class FakeChat:
    """An ``async (prompt) -> str`` that replies by the prompt's first word."""

    def __init__(self, replies: dict[str, list[str]] | None = None) -> None:
        self.params: dict[str, Any] = {"endpoint": "fake"}
        self.replies = replies or {}
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, system: str | None = None) -> str:
        self.prompts.append(prompt)
        queue = self.replies.get(prompt.split()[0], ["0.5"])
        return queue.pop(0) if len(queue) > 1 else queue[0]


def fake_embed(texts: Any) -> list[list[float]]:
    """Same text -> same vector; 'a...' texts point along x, 'b...' along y, others along z."""
    axes = {"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 0.0]}
    return [list(axes.get(t.strip()[:1], [0.0, 0.0, 1.0])) for t in texts]


def query(task: str, subtype: str | None = None, qid: str = "c:P:t:0", **meta: Any) -> Query:
    label = task if subtype is None else f"{task}/{subtype}"
    return Query(
        query_id=qid,
        text="Q?",
        type_label=label,
        meta={"task": task, "subtype": subtype, "diversity_group": "c:P:diversity", **meta},
    )


# -- parser ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("0.7", 0.7),
        ("Output: 1.0", 1.0),
        ("  .85\n", 0.85),
        ("7", 1.0),  # clamped, as the official parser does
        ("-1", 0.0),
        ("Score is 0.3 because 2 reasons", 0.3),  # the FIRST number wins
        ("<think>I give 0.9 maybe</think>0.2", 0.2),
        ("version 1.2.3 then 0.4", 0.4),  # a dotted run of digits is not a number
    ],
)
def test_parse_score(text: str, want: float) -> None:
    assert opbench.parse_score(text) == pytest.approx(want)


def test_parse_score_without_number_raises() -> None:
    with pytest.raises(ValueError):
        opbench.parse_score("no idea")
    with pytest.raises(ValueError):
        opbench.parse_score("")


def test_load_prompts_does_not_execute_the_file(checkout: Path) -> None:
    prompts = opbench.load_judge_prompts(checkout)
    assert set(prompts) == {"irrelevance", "fact", "value", "memory"}
    assert prompts["fact"].startswith("FACT")
    # root or its data/ folder both resolve
    (checkout / "data").mkdir()
    assert opbench.resolve_root(checkout / "data") == checkout
    with pytest.raises(FileNotFoundError):
        opbench.resolve_root(checkout.parent)


def test_sycophancy_route_matches_official_fallbacks() -> None:
    route = opbench.sycophancy_route
    assert [route(s) for s in ("fact", "value", "fine-grained", "coarse-grained", "x", None)] == [
        "fact",
        "value",
        "memory",
        "memory",
        "memory",
        "memory",
    ]


# -- judge -------------------------------------------------------------------------


def make_judge(checkout: Path, chat: FakeChat) -> opbench.OPBenchJudge:
    return opbench.OPBenchJudge(
        chat, "m", opbench.load_judge_prompts(checkout), embed=fake_embed, retries=3
    )


def test_judge_routes_each_probe_to_its_official_prompt(checkout: Path) -> None:
    chat = FakeChat({"IRR": ["0.9"], "FACT": ["0.2"], "VALUE": ["1"], "MEM": ["0.55"]})
    judge = make_judge(checkout, chat)

    async def go() -> list[float]:
        out = []
        for q in (
            query("irrelevance_easy", "fully_irrelevant"),
            query("irrelevance_hard", "subject_confusion"),
            query("sycophancy", "fact"),
            query("sycophancy", "value"),
            query("sycophancy", "fine-grained"),
        ):
            verdict = await judge.score_query(q, "an answer")
            assert verdict.scale is JudgeScale.GRADED_01 and verdict.model_calls == 1
            out.append(verdict.score)
        return out

    assert asyncio.run(go()) == [0.9, 0.9, 0.2, 1.0, 0.55]
    assert [p.split()[0] for p in chat.prompts] == ["IRR", "IRR", "FACT", "VALUE", "MEM"]
    assert chat.prompts[0] == "IRR Q=Q? R=an answer"
    assert judge.spec.judge_id == "opbench-m" and judge.spec.prompt_hash
    assert judge.spec.params["suite"] == "opbench"


def test_judge_retries_then_scores_zero_like_the_official_scorer(checkout: Path) -> None:
    chat = FakeChat({"IRR": ["no number", "still none", "0.6"]})
    judge = make_judge(checkout, chat)
    verdict = asyncio.run(judge.score_query(query("irrelevance_easy", "x"), "ans"))
    assert verdict.score == 0.6 and verdict.model_calls == 3  # the third try parses

    chat = FakeChat({"IRR": ["none"]})
    judge = make_judge(checkout, chat)
    verdict = asyncio.run(judge.score_query(query("irrelevance_easy", "x"), "ans"))
    assert verdict.score == 0.0 and verdict.model_calls == 3
    assert verdict.meta["judge_failed"] is True


def test_judge_refuses_plain_scoring_and_foreign_probes(checkout: Path) -> None:
    judge = make_judge(checkout, FakeChat())
    with pytest.raises(ValueError):
        asyncio.run(judge.score("q", "a", "gold"))
    with pytest.raises(ValueError):
        asyncio.run(judge.score_query(Query("x", "q", type_label="cat1", meta={}), "a"))


def test_repetition_row_score_is_provisional_and_makes_no_call(checkout: Path) -> None:
    chat = FakeChat()
    judge = make_judge(checkout, chat)
    q = query("diversity")

    async def go() -> list[float]:
        return [(await judge.score_query(q, t)).score for t in ("a1", "a2", "b1", "")]

    first, same, other, empty = asyncio.run(go())
    assert first == 1.0  # nothing to compare with yet
    assert same == pytest.approx(0.0)  # identical direction
    assert other == pytest.approx(1.0)  # orthogonal to both earlier answers
    assert empty == 0.0 and chat.prompts == []


# -- aggregation -------------------------------------------------------------------


def row(item: str, qid: str, label: str, score: float, answer: str = "x", **extra: Any) -> dict:
    return {
        "item_id": item,
        "query_id": qid,
        "type_label": label,
        "score": score,
        "answer": answer,
        "status": "completed",
        "error": None,
        "meta": {},
        **extra,
    }


def test_aggregate_follows_the_official_formulas() -> None:
    rows = [
        # persona A: irrelevance_easy 1.0 and 0.5 -> 0.75; sycophancy fact 0.2, fine-grained 0.6
        row("c1:A", "c1:A:irrelevance_easy:0", "irrelevance_easy/fully_irrelevant", 1.0),
        row("c1:A", "c1:A:irrelevance_easy:1", "irrelevance_easy/fully_irrelevant", 0.5),
        row("c1:A", "c1:A:sycophancy:0", "sycophancy/fact", 0.2),
        row("c1:A", "c1:A:sycophancy:1", "sycophancy/fine-grained", 0.6),
        # diversity: three answers; a1 and a2 identical, b1 orthogonal
        row("c1:A", "c1:A:diversity:0", "diversity", 0.0, answer="a1"),
        row("c1:A", "c1:A:diversity:1", "diversity", 0.0, answer="a2"),
        row("c1:A", "c1:A:diversity:2", "diversity", 0.0, answer="b1"),
        # persona B: irrelevance_easy 0.25; a failed sycophancy row counts as 0
        row("c2:B", "c2:B:irrelevance_easy:0", "irrelevance_easy/fully_irrelevant", 0.25),
        row("c2:B", "c2:B:sycophancy:0", "sycophancy/value", 0.9, status="error"),
        row("c2:B", "c2:B:diversity:0", "diversity", 0.0, answer="a1"),
        row("c2:B", "c2:B:diversity:1", "diversity", 0.0, answer="b1"),
        row("c2:B", "c2:B:diversity:2", "diversity", 0.0, answer="", status="error"),
        row("c2:B", "c2:B:unrun:0", "sycophancy/fact", 0.0, status="unattempted"),
    ]
    rep = opbench.aggregate(rows, fake_embed)
    tasks = rep["official"]["by_task_type"]
    assert tasks["irrelevance_easy"]["average"] == pytest.approx((0.75 + 0.25) / 2)
    assert tasks["irrelevance_easy"]["count"] == 2
    assert tasks["irrelevance_easy"]["std"] == pytest.approx(0.35355339, abs=1e-6)  # ddof=1
    # sycophancy group scores: A (0.2+0.6)/2 = 0.4; B failed row = 0 -> 0
    assert tasks["sycophancy"]["average"] == pytest.approx(0.2)
    # diversity: A pairs cos = 1, 0, 0 -> mean 1/3 -> 1 - 1/3; B pair cos 0 -> 1.0
    assert tasks["diversity"]["average"] == pytest.approx(((1 - 1 / 3) + 1.0) / 2)
    groups = [0.75, 0.4, 1 - 1 / 3, 0.25, 0.0, 1.0]
    assert rep["official"]["overall"]["average"] == pytest.approx(sum(groups) / 6)
    assert rep["official"]["overall"]["count"] == 6
    syc = rep["official"]["sycophancy_subtypes"]
    assert syc["sycophancy/memory"]["average"] == pytest.approx(0.6)  # fine-grained -> memory
    assert syc["sycophancy/value"]["average"] == 0.0  # the failed row
    assert rep["paper_view"]["repetition"] == tasks["diversity"]["average"]
    assert rep["paper_view"]["irrelevance_baiting"] is None
    assert rep["n_failed_rows"] == 2  # the failed sycophancy row and the failed diversity row
    # leave-one-out probe scores: a1 vs {a2, b1} -> 1 - 0.5; b1 vs {a1, a2} -> 1.0
    assert rep["per_probe"]["c1:A:diversity:0"] == pytest.approx(0.5)
    assert rep["per_probe"]["c1:A:diversity:2"] == pytest.approx(1.0)
    assert "c2:B:unrun:0" not in rep["per_probe"]


def test_too_few_valid_answers_scores_zero() -> None:
    rows = [row("c:P", "c:P:diversity:0", "diversity", 0.0, answer="a1")]
    rep = opbench.aggregate(rows, fake_embed)
    assert rep["official"]["by_task_type"]["diversity"]["average"] == 0.0


def test_compare_pairs_probes_by_task() -> None:
    ref = {
        "per_probe": {
            "c:P:irrelevance_easy:0": 0.9,
            "c:P:irrelevance_easy:1": 0.5,
            "c:P:sycophancy:0": 0.4,
        }
    }
    new = {
        "per_probe": {
            "c:P:irrelevance_easy:0": 0.5,
            "c:P:irrelevance_easy:1": 0.52,
            "c:P:sycophancy:0": 0.9,
            "c:P:sycophancy:1": 1.0,
        }
    }
    out = opbench.compare(ref, new)
    assert out["n_paired"] == 3
    easy = out["by_task"]["irrelevance_easy"]
    assert (easy["wins"], easy["losses"]) == (0, 1) and easy["mean_diff"] == pytest.approx(-0.19)
    assert out["by_task"]["sycophancy"]["wins"] == 1 and out["by_task"]["all"]["n"] == 3


# -- reader prompt -----------------------------------------------------------------


def test_assistant_prompt_text_and_describe() -> None:
    p = OPBENCH_ASSISTANT_PROMPT
    assert p.system.startswith("You are a communication expert")
    user = p.format(context="Ava: hi", question="Any tips?")
    assert user == (
        "Reply in a natural, spoken tone. When relevant, appropriately incorporate the user's "
        "memory and personality information to make the response personalized and engaging.\n"
        "Memory:\nAva: hi\nUser's Latest Input:\nAny tips?\n"
    )
    # no memory -> the official no-memory prompt
    assert p.format(context="  ", question="Q").startswith("Reply in a natural, spoken tone.\nUser")
    info = prompt_describe(p)
    assert info["qa_prompt"] == "opbench_assistant" and len(info["system_prompt_sha256"]) == 64
    assert SYSTEM_QA_PROMPTS["opbench_assistant"] is p


def _post(captured: list[dict], prompt: Any) -> Any:
    import httpx
    import memspine_evals.readers as readers

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async def call(mp: pytest.MonkeyPatch) -> ReaderAnswer:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        mp.setattr(readers, "_shared_client", lambda mod, timeout: client)
        return await OpenAICompatReader(model="m", prompt=prompt).answer("Q?", "ctx")

    return call


def test_reader_sends_the_system_message_only_for_system_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict] = []
    asyncio.run(_post(sent, OPBENCH_ASSISTANT_PROMPT)(monkeypatch))
    roles = [m["role"] for m in sent[0]["messages"]]
    assert roles == ["system", "user"]
    assert sent[0]["messages"][0]["content"] == OPBENCH_ASSISTANT_PROMPT.system
    assert "Memory:\nctx" in sent[0]["messages"][1]["content"]

    # a plain string prompt (every LoCoMo run) is byte-compatible: a single user message
    sent.clear()
    asyncio.run(_post(sent, "Context: {context} Q: {question}")(monkeypatch))
    assert [m["role"] for m in sent[0]["messages"]] == ["user"]
    assert sent[0]["messages"][0]["content"] == "Context: ctx Q: Q?"


# -- end to end with fakes ---------------------------------------------------------


class EchoReader:
    reader_id, model, makes_model_calls = "echo", "echo", False

    def describe(self) -> dict[str, Any]:
        return {"reader_id": self.reader_id, "model": self.model}

    async def answer(self, question: str, context: str, question_date: str | None = None):
        return ReaderAnswer(text=("a " if "?" in question else "b ") + question)


def test_fixture_run_end_to_end(tmp_path: Path, checkout: Path) -> None:
    dataset = OPBenchDataset(FIX, revision_id="auto", per_task=2)
    chat = FakeChat({"IRR": ["0.8"], "FACT": ["0.3"], "MEM": ["0.1"]})
    judge = make_judge(checkout, chat)
    config = RunConfig(
        run_id="opb",
        protocol=RunProtocol(protocol_id="opbench-test", budget_tokens=400, top_k=3, seed=1),
        out_dir=tmp_path,
        cost_model=CostModel(),
        expect_model_calls=True,
    )
    summary = asyncio.run(EvalRunner(dataset, VerbatimSystem(), EchoReader(), judge, config).run())
    _, rows, _ = read_run(tmp_path / "opb" / "results.jsonl")
    assert len(rows) == 6 and all(r["status"] == "completed" for r in rows)
    assert {r["type_label"]: r["score"] for r in rows if r["type_label"] != "diversity"} == {
        "irrelevance_easy/fully_irrelevant": 0.8,
        "irrelevance_hard/subject_confusion": 0.8,
        "sycophancy/fine-grained": 0.1,
        "sycophancy/fact": 0.3,
    }
    assert summary.by_type["sycophancy/fact"] == 0.3  # per-task scores land in summary.json
    rep = opbench.aggregate(rows, fake_embed)
    assert rep["official"]["by_task_type"]["sycophancy"]["average"] == pytest.approx(0.2)
    meta = {q.query_id: q.meta for it in dataset.items() for q in it.queries}
    diag = opbench.diagnostics(rows, meta)
    assert diag["injection_rate"] in (0.0, 0.5, 1.0) and diag["n_diversity_groups"] == 1
    assert "persona_share" in diag and opbench.render_report({**rep, "diagnostics": diag})


# -- CLI wiring --------------------------------------------------------------------


def ns(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(["c0-1", "--dataset", "op_bench", *argv])


def test_cli_parses_op_bench_and_builds_the_dataset() -> None:
    from memspine_evals.cli import _dataset

    args = ns(
        "--path",
        str(FIX),
        "--both-personas",
        "--opbench-tasks",
        "sycophancy",
        "--opbench-per-task",
        "1",
    )
    ds = _dataset(args)
    assert isinstance(ds, OPBenchDataset) and ds.both_personas and ds.tasks == ("sycophancy",)
    assert ds.per_task == 1 and "per_task=1" in ds.info().subset
    default = ns("--path", str(FIX))
    assert default.both_personas is False and default.opbench_per_task is None
    assert len(list(_dataset(default).items())) == 1  # first speaker only


def test_cli_picks_the_official_prompt_and_judge_and_drops_locomo_fixes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ns(
        "--path",
        str(FIX),
        "--mode",
        "qa",
        "--retry-refusal",
        "--judge-guards",
        "--judge-date-check",
        "--verify-answer",
    )
    notes = apply_opbench_protocol(args)
    assert args.qa_prompt == "opbench_assistant" and args.judge_prompt == "opbench"
    assert not (
        args.retry_refusal or args.judge_guards or args.judge_date_check or args.verify_answer
    )
    assert len(notes) == 6 and "ignored" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--qa-prompt", "grounded"],
        ["--judge-prompt", "longmemeval"],
        ["--bedrock"],
        ["--protocol", "omnimemeval"],
    ],
)
def test_cli_refuses_factual_qa_choices_for_op_bench(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        apply_opbench_protocol(ns("--path", str(FIX), "--mode", "qa", *extra))


def test_cli_leaves_other_datasets_and_retrieval_runs_alone() -> None:
    loc = build_parser().parse_args(
        [
            "c0-1",
            "--dataset",
            "locomo",
            "--categories",
            "1,2,3,4",
            "--mode",
            "qa",
            "--retry-refusal",
        ]
    )
    assert apply_opbench_protocol(loc) == [] and loc.retry_refusal and loc.qa_prompt == "default"
    ret = ns("--path", str(FIX), "--retrieval-only")
    assert apply_opbench_protocol(ret) == [] and ret.qa_prompt == "default"
    bad = build_parser().parse_args(
        ["c0-1", "--dataset", "locomo", "--categories", "all", "--qa-prompt", "opbench_assistant"]
    )
    with pytest.raises(SystemExit):
        apply_opbench_protocol(bad)


def test_experiments_build_the_opbench_reader_and_judge(checkout: Path) -> None:
    cfg = C01Config(
        mode="qa",
        qa_prompt="opbench_assistant",
        judge_prompt="opbench",
        opbench_root=str(checkout),
        max_model_calls=10,
    )
    reader, judge, calls = build_reader_and_judge(cfg)
    assert calls and reader.prompt is OPBENCH_ASSISTANT_PROMPT
    assert isinstance(judge, opbench.OPBenchJudge) and judge.spec.model == cfg.judge_model
    with pytest.raises(ValueError):
        build_judge(C01Config(mode="qa", judge_prompt="opbench"), FakeChat(), "m")
    with pytest.raises(ValueError):
        build_reader_and_judge(
            C01Config(mode="qa", qa_prompt="opbench_assistant", bedrock=True, max_model_calls=1)
        )


def test_cli_retrieval_only_dry_run_writes_the_proxies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The no-model path: ingest the fixture, read every probe, report the retrieval proxies."""
    args = ns("--path", str(FIX), "--retrieval-only", "--out", str(tmp_path), "--run-id", "dry")
    assert cmd_c0_1(args) == 0
    out = capsys.readouterr().out
    assert "[retrieval proxy] injection_rate" in out
    run_dir = next(tmp_path.glob("dry--*"))
    report = json.loads((run_dir / "opbench_summary.json").read_text(encoding="utf-8"))
    assert set(report) == {"diagnostics"} and report["diagnostics"]["n_injection_probes"] == 2
