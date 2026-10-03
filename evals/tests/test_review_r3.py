"""Regression tests for the 2 Oct 2026 harness review (R3-1 to R3-12, R4-4, R4-6).

Each test names its gap ID and fails on the harness as it was at 149a086.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import runpy
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest
from memspine_evals.contracts import (
    DatasetInfo,
    DepositResult,
    EvalItem,
    Evidence,
    Query,
    ReaderAnswer,
    RetrievedContext,
    Turn,
)
from memspine_evals.datasets import LoCoMoDataset, LongMemEvalDataset
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.judge import ABSTENTION_GOLD, ContainsJudge, ExactMatchJudge
from memspine_evals.metrics import Ledger
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import ContextOnlyReader
from memspine_evals.results import RowStatus, read_run
from memspine_evals.runner import EvalRunner, ModelCallBudgetExceeded, RunConfig, run_matrix
from memspine_evals.systems import NaiveRAGSystem, VerbatimSystem

PROTOCOL = RunProtocol(protocol_id="r3", budget_tokens=400, top_k=5, seed=1)
LOCOMO_REAL = Path(r"D:\mem\memory research\memspine\evals\data\locomo10.json")
LOCOMO_PLUS_PROMPT = Path(
    r"D:\mem\memory research\memspine\evals\data\locomo_plus\evaluation_framework"
    r"\task_eval\prompt.py"
)
LIGHTMEM_LME = Path(r"D:\mem\LightMem\experiments\longmemeval\run_lightmem_gpt.py")


# -- fixtures -----------------------------------------------------------------------


class ListDataset:
    def __init__(self, items: list[EvalItem]) -> None:
        self._items = items

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="fixture",
            revision_id="r",
            licence="none",
            source_path="",
            content_sha256="",
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items


def _item(item_id: str, n_queries: int = 1, **meta: Any) -> EvalItem:
    turns = (Turn(turn_id=f"{item_id}-t0", session_id="s1", speaker="u", text="hello world"),)
    queries = tuple(
        Query(query_id=f"{item_id}-q{i}", text=f"q{i}?", gold="x", meta=dict(meta))
        for i in range(n_queries)
    )
    return EvalItem(item_id=item_id, history=turns, queries=queries)


def _config(tmp_path: Path, run_id: str = "r3", **kw: Any) -> RunConfig:
    return RunConfig(run_id=run_id, protocol=PROTOCOL, out_dir=tmp_path, **kw)


class CountingReader:
    """One model call per answer; optionally draws on a shared-or-own CallBudget."""

    reader_id = "counting"
    model = "fake-model"
    makes_model_calls = True

    def __init__(self, budget: Any = None) -> None:
        self.budget = budget
        self.dates: list[str | None] = []

    def describe(self) -> Mapping[str, Any]:
        return {"reader_id": self.reader_id}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        if self.budget is not None:
            self.budget.reserve()
        self.dates.append(question_date)
        return ReaderAnswer(text="x", model_calls=1)


LOCOMO_FIXTURE = [
    {
        "sample_id": "conv-1",
        "conversation": {
            "speaker_a": "Ana",
            "speaker_b": "Ben",
            "session_1_date_time": "2:00 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Ana", "dia_id": "D1:1", "text": "I adopted a greyhound."},
                {"speaker": "Ana", "dia_id": "D1:2", "text": "Her name is Juno."},
            ],
        },
        "qa": [
            {"question": "Dog's name?", "answer": "Juno", "evidence": ["D1:2"], "category": 1},
            {
                "question": "What breed is Ben's cat?",
                "adversarial_answer": "a greyhound",
                "evidence": ["D1:1"],
                "category": 5,
            },
        ],
    }
]


@pytest.fixture
def locomo_path(tmp_path: Path) -> Path:
    path = tmp_path / "locomo10.json"
    path.write_text(json.dumps(LOCOMO_FIXTURE), encoding="utf-8")
    return path


def _fake_chat(replies: list[str], seen: list[str]) -> Any:
    async def chat(prompt: str) -> str:
        seen.append(prompt)
        return replies.pop(0) if replies else '{"label": "CORRECT"}'

    chat.params = {"temperature": 0.0}  # type: ignore[attr-defined]
    return chat


# -- R3-1 -----------------------------------------------------------------------------


def test_r3_1_locomo_categories_must_be_explicit() -> None:
    from memspine_evals.cli import resolve_categories

    args = argparse.Namespace(command="c0-1", dataset="locomo", categories=None, protocol=None)
    with pytest.raises(SystemExit, match="--categories"):
        resolve_categories(args)
    args.categories = "1,2,3,4"
    assert resolve_categories(args) == (1, 2, 3, 4)
    args.categories, args.protocol = None, "omnimemeval"
    assert resolve_categories(args) == (1, 2, 3, 4)


def test_r3_1_omnimemeval_preset_sets_categories_1_to_4() -> None:
    from memspine_evals.experiments import C01Config, apply_protocol_preset

    cfg = apply_protocol_preset(C01Config(mode="qa"), "omnimemeval")
    assert cfg.categories == (1, 2, 3, 4)
    with pytest.raises(ValueError, match="fixes categories"):
        apply_protocol_preset(C01Config(mode="qa", categories=(1, 5)), "omnimemeval")


@pytest.mark.skipif(not LOCOMO_REAL.exists(), reason="LoCoMo not on disk")
def test_r3_1_preset_loads_1540_questions_and_no_cat5_gold_is_a_distractor() -> None:
    four = LoCoMoDataset(LOCOMO_REAL, revision_id="auto", categories=(1, 2, 3, 4))
    assert four.info().n_queries == 1540
    full = LoCoMoDataset(LOCOMO_REAL, revision_id="auto")
    cat5 = [q for i in full.items() for q in i.queries if q.type_label == "cat5"]
    assert cat5 and all(q.gold == ABSTENTION_GOLD for q in cat5)
    assert not any(q.gold == q.meta["adversarial_answer"] for q in cat5)


def test_r3_1_abstention_question_gets_the_abstention_judge() -> None:
    from memspine_evals.judge_prompts import RoutedLLMJudge

    seen: list[str] = []
    judge = RoutedLLMJudge(_fake_chat([], seen), model="m", suite="rubric")
    query = next(
        q for q in next(LoCoMoDataset_fixture().items()).queries if q.meta.get("abstention")
    )
    verdict = asyncio.run(judge.score_query(query, "Not mentioned."))
    assert verdict.meta["prompt_id"] == "memspine/abstention"
    assert "a greyhound" not in seen[0]  # the distractor is never shown
    assert judge.handles_abstention


def LoCoMoDataset_fixture() -> LoCoMoDataset:
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "l.json"
    tmp.write_text(json.dumps(LOCOMO_FIXTURE), encoding="utf-8")
    return LoCoMoDataset(tmp, revision_id="fixture")


def test_r3_1_string_judge_cannot_grade_abstention(tmp_path: Path, locomo_path: Path) -> None:
    from memspine_evals.experiments import C01Config, run_c0_1

    dataset = LoCoMoDataset(locomo_path, revision_id="fixture")
    with pytest.raises(ValueError, match="abstention"):
        asyncio.run(run_c0_1(dataset, C01Config(mode="retrieval", top_k=3), tmp_path))
    # and in the runner itself: an ERROR row, never a scored one
    runner = EvalRunner(
        dataset, VerbatimSystem(), ContextOnlyReader(), ContainsJudge(),
        _config(tmp_path, expect_model_calls=False),
    )
    asyncio.run(runner.run())
    _, rows, _ = read_run(tmp_path / "r3" / "results.jsonl")
    cat5 = [r for r in rows if r["type_label"] == "cat5"]
    assert cat5 and all(r["status"] == RowStatus.ERROR.value for r in cat5)


# -- R3-2 -----------------------------------------------------------------------------


def test_r3_2_naive_rag_pending_unit_is_replaced_in_place() -> None:
    system = NaiveRAGSystem(chunk_chars=10_000)

    async def go() -> list[int]:
        await system.reset("i")
        for n in range(5):
            await system.insert(Turn(f"t{n}", "s", "u", f"fact number {n}"))
        counts = []
        for _ in range(3):
            await system.query("fact", 500, 5)
            counts.append(len(system._retriever._units))  # type: ignore[attr-defined]
        return counts

    assert asyncio.run(go()) == [1, 1, 1]


def test_r3_2_pending_unit_is_dropped_when_the_chunk_closes() -> None:
    system = NaiveRAGSystem(chunk_chars=60, overlap_turns=0)

    async def go() -> list[str]:
        await system.reset("i")
        await system.insert(Turn("t0", "s", "u", "short"))
        await system.query("short", 500, 5)  # indexes the pending tail
        await system.insert(Turn("t1", "s", "u", "a much longer turn that closes the chunk"))
        return [u.unit_id for u in system._retriever._units]  # type: ignore[attr-defined]

    assert asyncio.run(go()) == ["chunk-0000"]


# -- R3-3 -----------------------------------------------------------------------------


def test_r3_3_provider_budget_error_is_a_clean_abort() -> None:
    from memspine_evals.bedrock import BudgetExceeded

    assert issubclass(BudgetExceeded, ModelCallBudgetExceeded)


def test_r3_3_two_arms_cap_4_each_arm_gets_its_own_budget(tmp_path: Path) -> None:
    from memspine_evals.bedrock import CallBudget

    dataset = ListDataset([_item("a", 2), _item("b", 2)])  # 4 questions per arm
    systems = [VerbatimSystem(system_id="arm1"), VerbatimSystem(system_id="arm2")]
    summaries = asyncio.run(
        run_matrix(
            dataset, systems, CountingReader(), ExactMatchJudge(), _config(tmp_path),
            reader_judge_factory=lambda: (CountingReader(CallBudget(max_calls=4)),
                                          ExactMatchJudge()),
        )
    )
    assert [s.n_errors for s in summaries] == [0, 0]
    assert [s.n_queries for s in summaries] == [4, 4]


def test_r3_3_exhausted_budget_gives_unattempted_rows_and_later_arms_run(tmp_path: Path) -> None:
    from memspine_evals.bedrock import CallBudget

    dataset = ListDataset([_item("a", 2), _item("b", 2)])
    systems = [VerbatimSystem(system_id="arm1"), VerbatimSystem(system_id="arm2")]
    with pytest.raises(ModelCallBudgetExceeded):
        asyncio.run(
            run_matrix(
                dataset, systems, CountingReader(), ExactMatchJudge(), _config(tmp_path),
                reader_judge_factory=lambda: (CountingReader(CallBudget(max_calls=3)),
                                              ExactMatchJudge()),
            )
        )
    for arm in ("arm1", "arm2"):
        _, rows, _ = read_run(tmp_path / f"r3--{arm}" / "results.jsonl")
        statuses = [r["status"] for r in rows]
        assert statuses.count("completed") == 3
        assert statuses.count("unattempted") == 1
        assert "error" not in statuses


# -- R3-4 -----------------------------------------------------------------------------


def test_r3_4_openai_compat_path_wires_qa_and_judge_prompts() -> None:
    from memspine_evals.contracts import sha256_text
    from memspine_evals.experiments import C01Config, build_reader_and_judge
    from memspine_evals.judge import DEFAULT_BINARY_PROMPT
    from memspine_evals.readers import DATED_QA_PROMPT

    reader, judge, _ = build_reader_and_judge(C01Config(mode="qa", qa_prompt="dated"))
    assert reader.prompt == DATED_QA_PROMPT  # type: ignore[attr-defined]
    assert judge.spec.prompt_id == "suite:rubric"
    assert judge.spec.prompt_hash != sha256_text(DEFAULT_BINARY_PROMPT)
    _, constraint, _ = build_reader_and_judge(C01Config(mode="qa", judge_prompt="constraint"))
    assert constraint.spec.params["routes"]["default"]["prompt_id"] == "memspine/constraint"


def test_r3_4_dated_prompt_reaches_the_reader_payload() -> None:
    from memspine_evals.readers import DATED_QA_PROMPT, OpenAICompatReader

    sent: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class FakeClient:
        def __init__(self, **_: Any) -> None: ...

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None: ...

        async def post(self, url: str, json: dict[str, Any], headers: Any) -> FakeResponse:
            sent.append(json)
            return FakeResponse()

    reader = OpenAICompatReader(model="m", prompt=DATED_QA_PROMPT)
    reader._httpx = type("H", (), {"AsyncClient": FakeClient})  # type: ignore[assignment]
    asyncio.run(reader.answer("when?", "[2023-05-08] x"))
    assert "Each line starts with the date" in sent[0]["messages"][0]["content"]


# -- R3-5 -----------------------------------------------------------------------------


def test_r3_5_locomo_plus_prompts_are_verbatim() -> None:
    from memspine_evals.judge_prompts import JUDGE_PROMPTS, PromptStatus
    from memspine_evals.official_prompts import LOCOMO_PLUS_TEMPLATES

    cognitive = JUDGE_PROMPTS["locomo_plus/Cognitive"]
    assert cognitive.status is PromptStatus.OFFICIAL
    assert cognitive.sha256 == (
        "83875348ab831a4f2932457bc951d37654db9f77ecaf50e7a8aacf5ff566a08b"
    )
    if LOCOMO_PLUS_PROMPT.exists():
        official = runpy.run_path(str(LOCOMO_PLUS_PROMPT))["PROMPT_TEMPLATES"]
        assert official == LOCOMO_PLUS_TEMPLATES


def test_r3_5_longmemeval_prompts_are_verified_against_upstream() -> None:
    """N12: byte-identical to xiaowu0162/LongMemEval@9e0b455 (checked 2026-10-03)."""
    import ast
    import re

    from memspine_evals.judge_prompts import JUDGE_PROMPTS, PromptStatus
    from memspine_evals.official_prompts import LONGMEMEVAL_ANSCHECK

    for name in LONGMEMEVAL_ANSCHECK:
        prompt = JUDGE_PROMPTS[f"longmemeval/{name}"]
        assert prompt.status is PromptStatus.OFFICIAL
        assert "github.com/xiaowu0162/LongMemEval@9e0b455" in prompt.source
    assert JUDGE_PROMPTS["longmemeval/abstention"].sha256 == (
        "5c0b365a1e1d06db36377c735432b56e122ca3c428f89faf61d43a0d5a7e050b"
    )
    if LIGHTMEM_LME.exists():
        src = LIGHTMEM_LME.read_text(encoding="utf-8")
        seg = src[src.index("def get_anscheck_prompt") : src.index("def true_or_false")]
        texts = [ast.literal_eval(m) for m in re.findall(r'template = (".*?")\n', seg)]
        assert texts == list(LONGMEMEVAL_ANSCHECK.values())


def test_r3_5_placeholder_prompt_refuses_to_run() -> None:
    from memspine_evals.judge_prompts import (
        JUDGE_PROMPTS,
        JudgePrompt,
        OfficialPromptMissing,
        PromptStatus,
    )

    placeholder = JudgePrompt("x/judge", None, PromptStatus.PLACEHOLDER, "somewhere")
    with pytest.raises(OfficialPromptMissing):
        placeholder.render("q", "g", "a")
    assert not any(p.status is PromptStatus.PLACEHOLDER for p in JUDGE_PROMPTS.values())


def test_n12_omnimemeval_judge_is_ported_verbatim() -> None:
    """N12: MemTensor/OmniMemEval@0b1ea8d scripts/utils/prompts.py JUDGE_PROMPT (+ system)."""
    from memspine_evals.experiments import C01Config, build_reader_and_judge
    from memspine_evals.judge_prompts import JUDGE_PROMPTS, PromptStatus, RoutedLLMJudge
    from memspine_evals.official_prompts import OMNIMEMEVAL_JUDGE_SYSTEM

    prompt = JUDGE_PROMPTS["omnimemeval/judge"]
    assert prompt.status is PromptStatus.OFFICIAL
    assert "MemTensor/OmniMemEval@0b1ea8d:scripts/utils/prompts.py" in prompt.source
    assert prompt.sha256 == "6baa73873a66c6ea712e3a4f07f200870ca15d73330f719b5ad9df10954abea3"
    assert prompt.describe()["system_sha256"] == (
        "8d1b3c72c9b7febd0bf07e4a8c17d3d9632b5d463556558fd67eed807aafeb29"
    )
    rendered = prompt.render("Where?", "Paris", "In Paris.")
    assert "Question: Where?" in rendered and "Gold answer: Paris" in rendered
    assert "Generated answer: In Paris." in rendered

    calls: list[tuple[str, str | None]] = []

    async def chat(text: str, system: str | None = None) -> str:
        calls.append((text, system))
        return 'Same city. {"label": "CORRECT"}'

    judge = RoutedLLMJudge(chat, model="m", suite="omnimemeval")
    verdict = asyncio.run(judge.score("Where?", "In Paris.", "Paris"))
    assert verdict.score == 1.0 and calls[0][1] == OMNIMEMEVAL_JUDGE_SYSTEM
    _, judge2, _ = build_reader_and_judge(
        C01Config(mode="qa", judge_prompt="omnimemeval", reader_model="m", judge_model="m",
                  base_url="http://localhost:1/v1")
    )
    assert judge2.spec.prompt_id == "suite:omnimemeval"


def test_n12_omnimemeval_preset_uses_the_ported_judge() -> None:
    from memspine_evals.experiments import C01Config, apply_protocol_preset

    cfg = apply_protocol_preset(C01Config(mode="qa"), "omnimemeval")
    assert cfg.judge_prompt == "omnimemeval"
    with pytest.raises(ValueError, match="fixes judge prompt"):
        apply_protocol_preset(C01Config(mode="qa", judge_prompt="longmemeval"), "omnimemeval")


def test_r3_5_longmemeval_routes_by_type_and_abstention(tmp_path: Path) -> None:
    from memspine_evals.judge_prompts import RoutedLLMJudge

    sample = {
        "question_id": "q9_abs",
        "question_type": "temporal-reasoning",
        "question": "How many days?",
        "answer": "You did not mention this.",
        "question_date": "2023/05/30 (Tue) 21:39",
        "haystack_session_ids": ["s"],
        "haystack_dates": ["2023/05/01"],
        "haystack_sessions": [[{"role": "user", "content": "hi"}]],
    }
    path = tmp_path / "lme.json"
    path.write_text(json.dumps([sample, {**sample, "question_id": "q9"}]), encoding="utf-8")
    abs_q, temporal_q = (i.queries[0] for i in LongMemEvalDataset(path, "auto").items())
    assert abs_q.meta["abstention"] is True and temporal_q.meta["abstention"] is False

    seen: list[str] = []
    judge = RoutedLLMJudge(_fake_chat(["yes", "no"], seen), model="m", suite="longmemeval")
    v1 = asyncio.run(judge.score_query(abs_q, "I don't know"))
    v2 = asyncio.run(judge.score_query(temporal_q, "18 days"))
    assert (v1.meta["prompt_id"], v1.score) == ("longmemeval/abstention", 1.0)
    assert (v2.meta["prompt_id"], v2.score) == ("longmemeval/temporal-reasoning", 0.0)
    assert "unanswerable question" in seen[0] and "off-by-one" in seen[1]


def test_r3_5_locomo_plus_v2_grades_with_partial_credit() -> None:
    from memspine_evals.judge import JudgeScale
    from memspine_evals.judge_prompts import RoutedLLMJudge

    seen: list[str] = []
    judge = RoutedLLMJudge(
        _fake_chat(['{"label": "partial", "reason": "x"}'], seen), model="m",
        suite="locomo-plus-v2",
    )
    query = next(LoCoMoDataset_fixture().items()).queries[0]
    verdict = asyncio.run(judge.score_query(query, "Juno, I think"))
    assert judge.spec.scale is JudgeScale.GRADED_01
    assert verdict.score == 0.5 and verdict.meta["prompt_id"] == "locomo_plus/multi-hop"
    assert "Her name is Juno." in seen[0]  # official evidence block
    plus = Query("p", "trigger", gold="cue", meta={"benchmark": "locomo_plus"})
    assert judge.prompt_for(plus).prompt_id == "locomo_plus/Cognitive"


# -- R3-6 -----------------------------------------------------------------------------


def test_r3_6_question_date_reaches_the_reader(tmp_path: Path) -> None:
    from memspine_evals.readers import QA_PROMPTS

    reader = CountingReader()
    dataset = ListDataset([_item("a", 1, question_date="2023/05/30 (Tue) 21:39")])
    runner = EvalRunner(dataset, VerbatimSystem(), reader, ExactMatchJudge(), _config(tmp_path))
    asyncio.run(runner.run())
    assert reader.dates == ["2023/05/30 (Tue) 21:39"]
    rendered = QA_PROMPTS["question_dated"].format(
        context="c", question="q", question_date=reader.dates[0]
    )
    assert "2023/05/30 (Tue) 21:39" in rendered


# -- R3-7 -----------------------------------------------------------------------------


class FixedContextSystem:
    """Returns a preset context; evidence carries unit ids and spans."""

    system_id = "fixed"

    def __init__(self, text: str, evidence: tuple[Evidence, ...]) -> None:
        self._text, self._evidence = text, evidence

    def describe(self) -> Mapping[str, Any]:
        return {"system_id": self.system_id}

    async def reset(self, item_id: str) -> None: ...

    async def insert(self, turn: Turn) -> DepositResult:
        return DepositResult()

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        return RetrievedContext(text=self._text, tokens=len(self._text) // 4,
                                evidence=self._evidence, meta={"ranked": True})

    async def close(self) -> None: ...


def _recall_run(tmp_path: Path, system: Any, gold: tuple[str, ...], budget: int = 4000) -> Any:
    item = EvalItem(
        item_id="i",
        history=(Turn("t0", "s", "u", "x"),),
        queries=(Query("q", "q?", gold="x", gold_turn_ids=gold),),
    )
    config = RunConfig(
        run_id="rec", out_dir=tmp_path, expect_model_calls=False,
        protocol=RunProtocol(protocol_id="r", budget_tokens=budget, top_k=5, seed=1),
    )
    runner = EvalRunner(ListDataset([item]), system, ContextOnlyReader(), ContainsJudge(), config)
    return asyncio.run(runner.run())


def test_r3_7_a_12_turn_chunk_is_one_unit(tmp_path: Path) -> None:
    chunk = tuple(Evidence(f"t{n}", 1.0, meta={"unit_id": "c0"}) for n in range(12))
    other = (Evidence("u0", 0.5, meta={"unit_id": "c1"}),)
    summary = _recall_run(tmp_path, FixedContextSystem("x", chunk + other), gold=("t7",))
    assert summary.recall["R@1"] == 1.0  # was 0: only t0 was in retrieved_ids[:1]


def test_r3_7_recall_all_needs_every_gold_turn(tmp_path: Path) -> None:
    ev = (
        Evidence("a", 1.0, meta={"unit_id": "u1"}),
        Evidence("b", 0.9, meta={"unit_id": "u2"}),
        Evidence("c", 0.8, meta={"unit_id": "u3"}),
    )
    summary = _recall_run(tmp_path, FixedContextSystem("x", ev), gold=("a", "c"))
    assert summary.recall["R@1"] == 1.0
    assert summary.recall["R_all@1"] == 0.0
    assert summary.recall["R_all@5"] == 1.0


def test_r3_7_evidence_cut_by_truncation_is_dropped(tmp_path: Path) -> None:
    head, tail = "h" * 40, "t" * 4000
    text = f"{head}\n{tail}"
    ev = (
        Evidence("head", 1.0, meta={"unit_id": "u1", "span": (0, 40)}),
        Evidence("tail", 0.9, meta={"unit_id": "u2", "span": (41, len(text))}),
    )
    summary = _recall_run(tmp_path, FixedContextSystem(text, ev), gold=("tail",), budget=50)
    assert summary.recall["R@5"] == 0.0  # the reader never saw the tail


# -- R3-8 -----------------------------------------------------------------------------


def test_r3_8_abort_path_honours_max_items(tmp_path: Path) -> None:
    dataset = ListDataset([_item(f"i{n}", 2) for n in range(5)])
    config = _config(tmp_path, max_items=2, max_model_calls=1)
    runner = EvalRunner(dataset, VerbatimSystem(), CountingReader(), ExactMatchJudge(), config)
    with pytest.raises(ModelCallBudgetExceeded):
        asyncio.run(runner.run())
    _, rows, summary = read_run(tmp_path / "r3" / "results.jsonl")
    assert summary is not None and summary["n_scheduled"] == 4  # 2 items x 2, not 5 x 2
    assert {r["item_id"] for r in rows} == {"i0", "i1"}


def test_r3_8_by_type_counts_errors_at_the_failure_score(tmp_path: Path) -> None:
    from memspine_evals.judge import JudgeScale, JudgeSpec
    from memspine_evals.provenance import ReaderSpec, RunManifest, SystemSpec
    from memspine_evals.results import ResultRow, aggregate

    manifest = RunManifest.build(
        run_id="m", dataset=ListDataset([]).info(), system=SystemSpec("s"),
        reader=ReaderSpec("r", "m", False), judge=JudgeSpec("j", JudgeScale.BINARY),
        protocol=PROTOCOL, token_counter={},
    )
    base = {"run_id": "m", "item_id": "i", "question": "q", "gold": "g", "answer": "a",
            "scale": "binary", "type_label": "x"}
    rows = [
        ResultRow(query_id="1", score=1.0, **base),
        ResultRow(query_id="2", score=0.0, status=RowStatus.ERROR.value, error="boom", **base),
    ]
    summary = aggregate(manifest, rows, Ledger())
    assert summary.by_type["x"] == 0.5  # was 1.0: the error was left out
    assert summary.by_type_n["x"] == {"scored": 1, "errors": 1, "unattempted": 0}


# -- R3-9 -----------------------------------------------------------------------------


def _write_run(runs: Path, run_id: str, rows: list[tuple[str, str]], calls: int = 0) -> None:
    d = runs / run_id
    d.mkdir(parents=True)
    lines = [json.dumps({"kind": "manifest"})]
    lines += [json.dumps({"kind": "result", "item_id": i, "status": s}) for i, s in rows]
    (d / "results.jsonl").write_text("\n".join(lines), encoding="utf-8")
    (d / "summary.json").write_text(
        json.dumps({"judge_model_calls": 1, "loop_model_calls": calls - 1}), encoding="utf-8"
    )


def test_r3_9_unattempted_or_error_items_are_not_done(tmp_path: Path) -> None:
    import run_chunked

    _write_run(tmp_path, "job-chunk00--memspine", [
        ("a", "completed"), ("b", "unattempted"), ("c", "completed"), ("c", "error"),
    ])
    assert run_chunked.done_items(tmp_path, "job", "memspine") == {"a"}


def test_r3_9_runs_dir_out_flag_and_job_wide_cap(tmp_path: Path) -> None:
    import run_chunked
    from memspine_evals.cli import DEFAULT_OUT

    assert run_chunked.DEFAULT_RUNS == DEFAULT_OUT
    rest, cap = run_chunked.split_cap(["--mode", "qa", "--max-model-calls", "100", "--out", "X"])
    assert cap == 100 and "--max-model-calls" not in rest
    assert run_chunked.runs_dir_from(rest) == Path("X")
    _write_run(tmp_path, "job-chunk00--memspine", [("a", "completed")], calls=40)
    assert run_chunked.spent_calls(tmp_path, "job-chunk00", "memspine") == 40
    cmd = run_chunked.chunk_command(rest, "memspine", ["b"], "job-chunk01", tmp_path, 60)
    assert cmd[cmd.index("--out") + 1] == str(tmp_path) and cmd.count("--out") == 1
    assert cmd[cmd.index("--max-model-calls") + 1] == "60"


# -- R3-10 ----------------------------------------------------------------------------


class _EngineNoCounts:
    async def sleep(self) -> dict[str, dict[str, Any]]:
        return {"mine_facts": {"status": "ok"}}


def test_r3_10_uncountable_sleep_marks_k_unknown(tmp_path: Path) -> None:
    from memspine_evals.systems.memspine_system import MemspineSystem

    system = MemspineSystem(build_sleep=True)
    system._engine = _EngineNoCounts()
    result = asyncio.run(system.build())
    assert result.meta["cost_observable"] is False

    class BuildSystem(VerbatimSystem):
        async def build(self) -> DepositResult:
            return result

    runner = EvalRunner(
        ListDataset([_item("a")]), BuildSystem(), ContextOnlyReader(), ContainsJudge(),
        _config(tmp_path, expect_model_calls=False),
    )
    summary = asyncio.run(runner.run())
    assert runner.ledger.complete is False and summary.cost_accounting_complete is False


def test_r3_10_sleep_is_refused_below_the_declared_estimate() -> None:
    from memspine_evals.systems.memspine_system import MemspineSystem

    class Engine(_EngineNoCounts):
        slept = 0

        def model_calls(self) -> dict[str, int]:
            return {}

        async def sleep(self) -> dict[str, dict[str, Any]]:
            Engine.slept += 1
            return {}

    system = MemspineSystem(build_sleep=True, sleep_calls_per_session=3)
    system._engine = Engine()
    system._sessions = {"s1", "s2"}
    system.remaining_model_calls = 5
    with pytest.raises(ModelCallBudgetExceeded):
        asyncio.run(system.build())
    assert Engine.slept == 0


def test_r3_10_runner_passes_the_remaining_budget(tmp_path: Path) -> None:
    class Bounded(VerbatimSystem):
        remaining_model_calls: int | None = None
        seen: ClassVar[list[int | None]] = []

        async def build(self) -> DepositResult:
            Bounded.seen.append(self.remaining_model_calls)
            return DepositResult()

    runner = EvalRunner(
        ListDataset([_item("a")]), Bounded(), CountingReader(), ExactMatchJudge(),
        _config(tmp_path, max_model_calls=10),
    )
    asyncio.run(runner.run())
    assert Bounded.seen == [10]


# -- R3-11 ----------------------------------------------------------------------------


def test_r3_11_manifest_records_version_limits_and_judge_params(tmp_path: Path) -> None:
    from memspine_evals.judge_prompts import RoutedLLMJudge
    from memspine_evals.systems.memspine_system import MemspineSystem

    assert MemspineSystem().describe()["version"] not in ("unknown", "")
    judge = RoutedLLMJudge(_fake_chat([], []), model="m", suite="rubric")
    assert judge.spec.params["temperature"] == 0.0
    assert judge.spec.params["routes"]["default"]["status"] == "memspine"
    runner = EvalRunner(
        SyntheticDataset(n_items=1, turns_per_item=4), VerbatimSystem(), ContextOnlyReader(),
        ContainsJudge(), _config(tmp_path, max_items=1, expect_model_calls=False),
    )
    limits = runner.build_manifest().to_dict()["limits"]
    assert limits["max_items"] == 1 and limits["expect_model_calls"] is False


def test_r3_11_convomem_records_a_content_hash(tmp_path: Path) -> None:
    from memspine_evals.datasets import ConvoMemDataset

    d = tmp_path / "core_benchmark" / "evidence_questions" / "user" / "1_evidence"
    d.mkdir(parents=True)
    (d / "p.json").write_text(json.dumps({"evidence_items": [{
        "question": "q", "answer": "a", "message_evidences": [{"text": "hi"}],
        "conversations": [{"id": "c1", "messages": [{"speaker": "u", "text": "hi"}]}],
    }]}), encoding="utf-8")
    info = ConvoMemDataset(tmp_path, revision_id="r").info()
    assert len(info.content_sha256) == 64


# -- R3-12 ----------------------------------------------------------------------------


def test_r3_12_feature_audit_has_behavioural_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    import feature_audit

    assert feature_audit.SRC.exists()
    assert len(feature_audit.BEHAVIOURAL) >= 6
    assert asyncio.run(feature_audit.check_h1_relative_dates()) and feature_audit.check_qa_prompts()
    monkeypatch.setattr(
        feature_audit, "BEHAVIOURAL", [("X", "broken", lambda: 1 / 0)]  # type: ignore[arg-type]
    )
    assert feature_audit.behavioural_audit() == ["X"]


# -- R4-4 / R4-6 ----------------------------------------------------------------------


def test_r4_4_temporal_check_reports_exact_day_and_coverage(tmp_path: Path) -> None:
    import temporal_check

    sample = [{
        "sample_id": "c",
        "conversation": {
            "speaker_a": "A", "speaker_b": "B",
            "session_1_date_time": "2:00 pm on 15 July, 2023",
            "session_1": [{"speaker": "A", "dia_id": "D1:1", "text": "I ran a race last Friday."}],
        },
        "qa": [
            {"question": "When?", "answer": "14 July 2023", "evidence": ["D1:1"], "category": 2},
            {"question": "When?", "answer": "July 2023", "evidence": ["D1:1"], "category": 2},
            {"question": "When?", "answer": "sometime", "evidence": [], "category": 2},
        ],
    }]
    path = tmp_path / "l.json"
    path.write_text(json.dumps(sample), encoding="utf-8")
    r = temporal_check.evaluate(LoCoMoDataset(path, revision_id="t"))
    assert r["n_cat2"] == 3 and r["covered"] == 2
    assert r["coverage"] == pytest.approx(2 / 3)
    assert r["agree_overlap"] == 2
    assert (r["n_single_day"], r["agree_exact_day"]) == (1, 1)
    assert r["agree_exact_range"] == 1  # "last Friday" is not all of July


def test_r4_6_dense_same_embedder_and_matched_budget_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine_evals import experiments
    from memspine_evals.systems import retrievers

    class FakeEmbed(retrievers.BM25Retriever):
        def __init__(self, model: str) -> None:
            super().__init__()
            self.retriever_id = f"fastembed:{model}"

    monkeypatch.setattr(retrievers, "FastEmbedRetriever", FakeEmbed)
    cfg = experiments.C01Config(
        naive_dense_same_embedder=True, matched_budget_tokens=900,
        memspine_config={"embedding": {"provider": "fastembed", "model": "BAAI/x"}},
    )
    systems = experiments.build_systems(cfg)
    ids = [s.system_id for s in systems]
    assert "naive-rag-dense-memspine-embedder" in ids and "naive-rag-bm25-matched900" in ids
    dense = systems[ids.index("naive-rag-dense-memspine-embedder")]
    assert dense.describe()["retriever"]["retriever_id"] == "fastembed:BAAI/x"
    capped = systems[ids.index("naive-rag-bm25-matched900")]
    assert capped.describe()["matched_budget_tokens"] == 900
    with pytest.raises(ValueError, match="fastembed"):
        experiments.memspine_embedding_model(
            experiments.C01Config(memspine_config={"embedding": {"provider": "litellm"}})
        )


def test_r4_6_matched_budget_arm_caps_the_context() -> None:
    from memspine_evals.systems.baselines import BudgetCappedSystem

    capped = BudgetCappedSystem(VerbatimSystem(), 10)

    async def go() -> RetrievedContext:
        await capped.reset("i")
        for n in range(6):
            await capped.insert(Turn(f"t{n}", "s", "u", f"walnut allergy fact {n} " * 3))
        return await capped.query("walnut allergy", 4000, 5)

    assert asyncio.run(go()).tokens <= 10


def test_r4_6_arms_are_declared_in_the_manifest(tmp_path: Path) -> None:
    from memspine_evals.experiments import C01Config, run_c0_1

    summaries = asyncio.run(run_c0_1(
        SyntheticDataset(n_items=1, turns_per_item=6), C01Config(matched_budget_tokens=200),
        tmp_path, run_id="arms",
    ))
    manifest, _, _ = read_run(tmp_path / summaries[0].run_id / "results.jsonl")
    assert "naive-rag-bm25-matched200" in manifest["labels"]["arms"]
    assert manifest["labels"]["matched_budget_tokens"] == 200
    assert "harness-protocol=r3-2026-10-02" in manifest["protocol"]["notes"]


#: N5: at most this many features may stay identifier-only (none today; the slack is for a
#: feature added before its behavioural check lands).
MAX_IDENTIFIER_ONLY = 2


def test_n5_every_feature_behaves_offline(capsys: pytest.CaptureFixture[str]) -> None:
    """N5 (R3-12): the full behavioural audit, offline (hash embedder, stub LLM providers)."""
    import feature_audit

    statuses = feature_audit.audit()
    capsys.readouterr()
    failed = [fid for fid, s in statuses.items() if s == "behaviour-fail"]
    missing = [fid for fid, s in statuses.items() if s == "identifier-missing"]
    only = [fid for fid, s in statuses.items() if s == "identifier-only"]
    assert not failed, f"behaviour-fail: {failed}"
    assert not missing, f"identifier-missing: {missing}"
    assert len(only) <= MAX_IDENTIFIER_ONLY, f"identifier-only: {only}"
    assert set(statuses) == {fid for fid, *_ in feature_audit.FEATURES}
    # every behavioural check names a real feature
    assert {fid for fid, _, _ in feature_audit.BEHAVIOURAL} <= set(statuses)


def test_n5_statuses_classify_fail_and_identifier_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import feature_audit

    monkeypatch.setattr(
        feature_audit, "BEHAVIOURAL", [("B0", "x", lambda: True), ("B1", "y", lambda: False)]
    )
    statuses = feature_audit.feature_statuses(["B1"])
    assert statuses["B0"] == "behaviour-ok" and statuses["B1"] == "behaviour-fail"
    assert statuses["B2"] == "identifier-only"
