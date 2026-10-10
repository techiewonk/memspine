"""The reader's raw reply is kept in a row's ``meta`` only when answer extraction ran.

An extracted answer (``dated3``) drops the reasoning, so a cut-off multi-line list
cannot be audited from the stored answer alone; ``meta["reader_raw"]`` keeps the
reply. Every non-extracting prompt's rows must stay byte-identical to before
(published runs remain reproducible), checked against a fixed fixture.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.judge import ContainsJudge
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import (
    QA_PROMPTS,
    READER_RAW_MAX_CHARS,
    OpenAICompatReader,
    reader_raw_meta,
)
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems import VerbatimSystem

FIXTURE = Path(__file__).parent / "fixtures" / "reader_raw_rows_no_extract.json"
REPLY = "Line 3 lists her pets.\n- a dog\n- a cat\nAnswer: a dog, a cat"
#: Wall-clock fields: the only ones that differ between two identical runs.
_TIMING = ("latency_retrieve_ms", "latency_answer_ms", "latency_judge_ms")


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

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None: ...

        async def post(self, url: str, json: dict[str, Any], headers: Any) -> FakeResponse:
            return FakeResponse()

    return type("H", (), {"AsyncClient": FakeClient})


def _rows(qa_prompt: str, tmp_path: Path, extract: bool) -> list[dict[str, Any]]:
    reader = OpenAICompatReader(model="m", prompt=QA_PROMPTS[qa_prompt], extract_answer=extract)
    reader._httpx = _fake_httpx(REPLY)
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = RunConfig(
        run_id="raw",
        protocol=RunProtocol(protocol_id="raw", budget_tokens=400, top_k=5, seed=3),
        out_dir=tmp_path,
        expect_model_calls=True,
    )
    runner = EvalRunner(dataset, VerbatimSystem(), reader, ContainsJudge(), config)
    asyncio.run(runner.run())
    lines = (tmp_path / "raw" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    rows = [r for r in rows if r.get("kind") == "result"]
    for row in rows:
        for key in _TIMING:
            row[key] = 0.0
    return rows


def test_extraction_keeps_the_raw_reply_in_meta(tmp_path: Path) -> None:
    rows = _rows("dated3", tmp_path, extract=True)
    assert rows
    for row in rows:
        assert row["answer"] == "a dog, a cat"
        assert row["meta"]["reader_raw"] == REPLY
        assert "reader_raw_truncated" not in row["meta"]


@pytest.mark.parametrize("qa_prompt", ["dated", "dated2", "dated_infer", "converse"])
def test_no_extraction_rows_are_unchanged(qa_prompt: str, tmp_path: Path) -> None:
    rows = _rows(qa_prompt, tmp_path, extract=False)
    for row in rows:
        assert "reader_raw" not in row["meta"]
        assert "reader_raw_truncated" not in row["meta"]
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert rows == expected


def test_raw_reply_is_capped_and_flagged() -> None:
    assert reader_raw_meta(None) == {}
    assert reader_raw_meta("") == {"reader_raw": ""}
    exact = "x" * READER_RAW_MAX_CHARS
    assert reader_raw_meta(exact) == {"reader_raw": exact}
    long = exact + "tail"
    assert reader_raw_meta(long) == {"reader_raw": exact, "reader_raw_truncated": True}


@pytest.mark.parametrize("extract", [False, True])
def test_litellm_reader_sets_raw_text_only_when_extracting(extract: bool) -> None:
    from types import SimpleNamespace

    from memspine_evals.bedrock import CallBudget, LiteLLMReader

    async def acompletion(**kwargs: object) -> object:
        message = SimpleNamespace(content=REPLY)
        return SimpleNamespace(
            usage=None, choices=[SimpleNamespace(message=message, finish_reason="stop")]
        )

    reader = LiteLLMReader(
        CallBudget(max_calls=3), model="m", prompt=QA_PROMPTS["dated3"], extract_answer=extract
    )
    reader._litellm = SimpleNamespace(acompletion=acompletion)  # type: ignore[assignment]
    out = asyncio.run(reader.answer("Which pets?", "ctx"))
    assert out.raw_text == (REPLY if extract else None)
    assert out.text == ("a dog, a cat" if extract else REPLY)


def test_verifying_reader_carries_the_raw_reply() -> None:
    from memspine_evals.verify import VerifyingReader

    class Inner:
        reader_id = "inner"
        model = "m"

        def describe(self) -> dict[str, Any]:
            return {}

        async def answer(
            self, question: str, context: str, question_date: str | None = None
        ) -> ReaderAnswer:
            return ReaderAnswer(text="a dog", model_calls=1, raw_text=REPLY)

    async def verify(question: str, answer: str, context: str) -> dict[str, Any]:
        return {"supported": True}

    out = asyncio.run(VerifyingReader(Inner(), verify).answer("q", "ctx"))
    assert out.raw_text == REPLY


def test_shared_client_never_serves_another_modules_client() -> None:
    """Regression (H5): the pooled client was keyed by ``id()`` of the httpx module and the
    loop, so a recycled address returned a stale client from an earlier test's fake."""
    from memspine_evals.readers import _shared_client

    for _ in range(300):
        fake = type("H", (), {"AsyncClient": type("C", (), {"__init__": lambda s, **k: None})})

        async def get(fake: Any = fake) -> Any:
            return _shared_client(fake, 1.0)

        assert type(asyncio.run(get())) is fake.AsyncClient
