"""C1 / H11: the ``routed`` QA prompt picks dated / temporal / inference per question.

Every existing ``--qa-prompt`` keeps its text, its reader ``describe()`` (keys, order and
hash) and its rows; only ``routed`` records the router, the variant hashes and each
row's ``qa_variant``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.experiments import C01Config, build_reader_and_judge
from memspine_evals.judge import ContainsJudge
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import (
    DATED_QA_PROMPT,
    QA_PROMPTS,
    QA_ROUTER_VERSION,
    ROUTED_INFERENCE_RULE,
    ROUTED_QA_PROMPTS,
    ROUTED_QA_VARIANTS,
    ROUTED_TEMPORAL_RULE,
    OpenAICompatReader,
    RoutedQAPrompt,
    qa_shape,
)
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems import VerbatimSystem

TEMPORAL = "When did Caroline go to the LGBTQ support group?"
INFERENCE = "Would Caroline pursue writing as a career?"
PLAIN = "What is Caroline's identity?"
_FMT = {"context": "[2023-05-08] x", "question_date": "unknown"}


@pytest.mark.parametrize(
    ("question", "shape"),
    [
        (TEMPORAL, "temporal"),
        ("How many weeks ago did Ana move?", "temporal"),
        ("When would Melanie likely go camping?", "temporal"),
        (INFERENCE, "inference"),
        ("What might John's financial status be?", "inference"),
        ("Is it likely that Nate has friends besides Joanna?", "inference"),
        ("Could Melanie be a member of the LGBTQ community?", "inference"),
        (PLAIN, "plain"),
        ("How many children does Melanie have?", "plain"),
    ],
)
def test_routing_decision(question: str, shape: str) -> None:
    assert qa_shape(question) == shape
    prompt = ROUTED_QA_PROMPTS["routed"]
    expected = ROUTED_QA_VARIANTS[shape].format(question=question, **_FMT)
    assert prompt.format(question=question, **_FMT) == expected


def test_plain_branch_is_dated_verbatim() -> None:
    assert ROUTED_QA_VARIANTS["plain"] == QA_PROMPTS["dated"]
    rendered = ROUTED_QA_PROMPTS["routed"].format(question=PLAIN, **_FMT)
    assert rendered == QA_PROMPTS["dated"].format(question=PLAIN, **_FMT)


def test_variant_wording_h11() -> None:
    """Temporal: dated + relative-date inference + DD Month YYYY. Inference: dated with
    the refusal replaced by "say 'likely'". Both keep dated's one-sentence answer; no
    terse-answer instruction and no blanket refusal (dated3 lost -3.3 to both)."""
    refusal = "If the context does not contain the answer, say you do not know."
    temporal, inference = ROUTED_QA_VARIANTS["temporal"], ROUTED_QA_VARIANTS["inference"]
    assert temporal == DATED_QA_PROMPT.replace(refusal, ROUTED_TEMPORAL_RULE)
    assert inference == DATED_QA_PROMPT.replace(refusal, ROUTED_INFERENCE_RULE)
    assert "DD Month YYYY" in temporal and "relative phrase" in temporal
    assert "say 'likely'" in inference and "do not know" not in inference
    for text in (temporal, inference):
        assert "Answer in one short sentence." in text
        assert "very concise" not in text and "short answer only" not in text
        assert "Not mentioned" not in text and refusal not in text


def test_existing_prompt_table_is_unchanged() -> None:
    """``routed`` is not a fixed prompt: QA_PROMPTS keeps exactly its old names."""
    assert "routed" not in QA_PROMPTS
    assert set(QA_PROMPTS) == {
        "mab_fc",
        "question_dated",
        "default",
        "dated",
        "dated2",
        "dated_infer",
        "dated3",
        "dated_world",
        "abstain",
        "converse",
        "dated_planned",  # N46
        "dated_noabstain",  # N56
        "evermemos_cot",  # N65
        "grounded",  # reader-gap fix
        "grounded_detail",  # C2
        "grounded_nodate",  # I79
        "grounded_detail_nodate",  # I79
        "grounded_ordered",  # R2-4
        "grounded_v2",  # dev reasoning 2026-10-10
        "grounded_v3",
        "grounded_generic",  # I3
        "grounded_generic_infer",  # B3 / C4 / R2-6 (+ C9 detail clause)
        "grounded_generic_list",  # C6
        "grounded_generic_prefs",  # I18
    }


def _old_describe(reader: Any, bedrock: bool) -> dict[str, Any]:
    """The ``describe()`` both readers produced before C1 (keys and order)."""
    sha = hashlib.sha256(reader.prompt.encode()).hexdigest()
    head: dict[str, Any] = {"reader_id": reader.reader_id, "model": reader.model}
    if bedrock:
        body = {
            "temperature": reader.temperature,
            "max_tokens": reader.max_tokens,
            "no_think": reader.no_think,
            "prompt_sha256": sha,
        }
    else:
        body = {
            "base_url": reader.base_url,
            "temperature": reader.temperature,
            "max_tokens": reader.max_tokens,
            "sampler": reader.sampler.describe(),  # A8: explicit sampler
            "prompt_sha256": sha,
        }
    extra = {"extract_answer": True, "answer_extractor": "v3"} if reader.extract_answer else {}
    return {**head, **body, **extra}


def _build(monkeypatch: pytest.MonkeyPatch, bedrock: bool, qa_prompt: str) -> Any:
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace())
    config = C01Config(
        mode="qa", bedrock=bedrock, max_model_calls=5, qa_prompt=qa_prompt, categories=(1, 2, 3, 4)
    )
    return build_reader_and_judge(config)[0]


@pytest.mark.parametrize("bedrock", [False, True])
@pytest.mark.parametrize("qa_prompt", sorted(QA_PROMPTS))
def test_existing_describe_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch, bedrock: bool, qa_prompt: str
) -> None:
    reader = _build(monkeypatch, bedrock, qa_prompt)
    assert reader.prompt == QA_PROMPTS[qa_prompt]
    described = dict(reader.describe())
    expected = _old_describe(reader, bedrock)
    assert json.dumps(described) == json.dumps(expected)


@pytest.mark.parametrize("bedrock", [False, True])
def test_routed_describe(monkeypatch: pytest.MonkeyPatch, bedrock: bool) -> None:
    reader = _build(monkeypatch, bedrock, "routed")
    assert isinstance(reader.prompt, RoutedQAPrompt)
    assert reader.extract_answer is False
    if bedrock:
        assert reader.max_tokens == 256
    described = dict(reader.describe())
    assert described["qa_prompt"] == "routed"
    assert described["qa_router"] == QA_ROUTER_VERSION
    variants = described["prompt_variants_sha256"]
    assert variants == {
        k: hashlib.sha256(v.encode()).hexdigest() for k, v in ROUTED_QA_VARIANTS.items()
    }
    assert variants["plain"] == hashlib.sha256(QA_PROMPTS["dated"].encode()).hexdigest()
    assert len(set(variants.values())) == 3
    assert described["prompt_sha256"] not in variants.values()


def test_cli_accepts_routed() -> None:
    from memspine_evals.cli import build_parser

    args = build_parser().parse_args(["c0-1", "--dataset", "locomo", "--qa-prompt", "routed"])
    assert args.qa_prompt == "routed"


def test_unknown_prompt_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown qa prompt"):
        _build(monkeypatch, False, "routd")


def _fake_httpx(sent: list[str]) -> Any:
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
            sent.append(json["messages"][0]["content"])
            return FakeResponse()

    return type("H", (), {"AsyncClient": FakeClient})


@pytest.mark.parametrize("question", [TEMPORAL, INFERENCE, PLAIN])
def test_openai_reader_sends_the_routed_variant(question: str) -> None:
    sent: list[str] = []
    reader = OpenAICompatReader(model="m", prompt=ROUTED_QA_PROMPTS["routed"])
    reader._httpx = _fake_httpx(sent)
    out = asyncio.run(reader.answer(question, "[2023-05-08] x"))
    shape = qa_shape(question)
    assert out.prompt_variant == shape
    assert sent == [ROUTED_QA_VARIANTS[shape].format(question=question, **_FMT)]


def test_openai_reader_fixed_prompt_has_no_variant() -> None:
    reader = OpenAICompatReader(model="m", prompt=QA_PROMPTS["dated"])
    reader._httpx = _fake_httpx([])
    assert asyncio.run(reader.answer(TEMPORAL, "x")).prompt_variant is None


@pytest.mark.parametrize("question", [TEMPORAL, INFERENCE, PLAIN])
def test_litellm_reader_sends_the_routed_variant(
    monkeypatch: pytest.MonkeyPatch, question: str
) -> None:
    from memspine_evals.bedrock import CallBudget, LiteLLMReader

    sent: list[str] = []

    async def acompletion(**kwargs: Any) -> object:
        sent.append(kwargs["messages"][0]["content"])
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(
            usage=None, choices=[SimpleNamespace(message=message, finish_reason="stop")]
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace())
    reader = LiteLLMReader(CallBudget(max_calls=3), model="m", prompt=ROUTED_QA_PROMPTS["routed"])
    reader._litellm = SimpleNamespace(acompletion=acompletion)  # type: ignore[assignment]
    out = asyncio.run(reader.answer(question, "[2023-05-08] x"))
    shape = qa_shape(question)
    assert out.prompt_variant == shape
    assert sent == [ROUTED_QA_VARIANTS[shape].format(question=question, **_FMT)]


def test_routed_rows_record_the_variant(tmp_path: Path) -> None:
    reader = OpenAICompatReader(model="m", prompt=ROUTED_QA_PROMPTS["routed"])
    reader._httpx = _fake_httpx([])
    config = RunConfig(
        run_id="routed",
        protocol=RunProtocol(protocol_id="routed", budget_tokens=400, top_k=5, seed=3),
        out_dir=tmp_path,
        expect_model_calls=True,
    )
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    asyncio.run(EvalRunner(dataset, VerbatimSystem(), reader, ContainsJudge(), config).run())
    lines = (tmp_path / "routed" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [r for r in map(json.loads, lines) if r.get("kind") == "result"]
    assert rows
    for row in rows:
        assert row["meta"]["qa_variant"] == qa_shape(row["question"])
