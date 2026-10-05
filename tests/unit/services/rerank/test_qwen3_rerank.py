"""Qwen3-Reranker adapter (G5a) against a fake ``transformers`` / ``torch``.

The real model is never loaded here (no network): the fake tokenizer turns each
formatted pair into token ids, and the fake model's last-position logits put the
"yes" logit up by how many query words occur in the document, so the adapter's
P(yes) ordering, batching and prompt layout can be checked exactly.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
import types
from collections.abc import Iterator, Sequence
from typing import Any

import numpy as np
import pytest

from memspine.exceptions import MissingServiceError
from memspine.services.rerank.base import Reranker
from memspine.services.rerank.factory import RerankSettings, build_reranker, rerank_modes

YES, NO, PAD = 1, 2, 0
VOCAB = 64


class _Encoding(dict[str, Any]):
    pass


class _Tensor:
    def __init__(self, data: Any) -> None:
        self.data = data

    def to(self, device: str) -> _Tensor:
        return self


class FakeTokenizer:
    calls: list[list[str]]

    def __init__(self) -> None:
        self.calls = []
        self.texts: dict[int, str] = {}
        self.kwargs: dict[str, Any] = {}

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        return [3] * (2 if text.startswith("<|im_start|>system") else 1)

    def convert_tokens_to_ids(self, token: str) -> int:
        return {"yes": YES, "no": NO}[token]

    def __call__(self, texts: list[str], **kwargs: Any) -> _Encoding:
        assert kwargs["truncation"] == "longest_first" and kwargs["padding"] is False
        self.kwargs = kwargs
        self.calls.append(list(texts))
        ids = []
        for text in texts:
            key = 10 + len(self.texts)  # one token id per text, remembered for the model
            self.texts[key] = text
            ids.append([key])
        return _Encoding(input_ids=ids)

    def pad(self, enc: _Encoding, **kwargs: Any) -> dict[str, _Tensor]:
        assert kwargs["return_tensors"] == "pt"
        width = max(len(ids) for ids in enc["input_ids"])
        padded = [[PAD] * (width - len(ids)) + ids for ids in enc["input_ids"]]  # left pad
        return {"input_ids": _Tensor(padded)}


class _Output:
    def __init__(self, logits: np.ndarray) -> None:
        self.logits = logits


class FakeModel:
    def __init__(self, tokenizer: FakeTokenizer) -> None:
        self.tokenizer = tokenizer
        self.batches: list[int] = []
        self.upcast = False

    def eval(self) -> FakeModel:
        return self

    def to(self, device: str) -> FakeModel:
        return self

    def float(self) -> FakeModel:
        self.upcast = True
        return self

    def __call__(self, input_ids: _Tensor) -> _Output:
        rows = input_ids.data
        self.batches.append(len(rows))
        logits = np.zeros((len(rows), len(rows[0]), VOCAB))
        for i, ids in enumerate(rows):
            # the formatted pair's token sits just before the 3-token suffix
            text = self.tokenizer.texts[ids[-2]]
            query = text.split("<Query>: ", 1)[1].split("\n", 1)[0]
            doc = text.split("<Document>: ", 1)[1]
            overlap = len(set(query.lower().split()) & set(doc.lower().split()))
            logits[i, -1, YES] = float(overlap)
            logits[i, -1, NO] = 1.0
        return _Output(logits)


def _fake_modules(load_delay: float = 0.0) -> tuple[types.ModuleType, types.ModuleType, list[int]]:
    loads: list[int] = []
    transformers = types.ModuleType("transformers")
    tokenizer = FakeTokenizer()

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(name: str, padding_side: str = "right") -> FakeTokenizer:
            assert padding_side == "left"
            loads.append(1)
            time.sleep(load_delay)
            return tokenizer

    class AutoModelForCausalLM:
        @staticmethod
        def from_pretrained(name: str) -> FakeModel:
            return FakeModel(tokenizer)

    transformers.AutoTokenizer = AutoTokenizer  # type: ignore[attr-defined]
    transformers.AutoModelForCausalLM = AutoModelForCausalLM  # type: ignore[attr-defined]
    torch = types.ModuleType("torch")
    torch.no_grad = contextlib.nullcontext  # type: ignore[attr-defined]
    return transformers, torch, loads


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    transformers, torch, loads = _fake_modules()
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "torch", torch)
    yield loads


async def test_scores_follow_p_yes_and_keep_input_order(fake: list[int]) -> None:
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

    reranker = Qwen3Reranker(batch_size=2)
    port: Reranker = reranker  # satisfies the port structurally
    assert port.reranker_id == "qwen3:Qwen/Qwen3-Reranker-0.6B"
    docs = ["nothing here", "caroline went hiking", "hiking", "caroline hiking in may"]
    scores = await reranker.rerank("when did caroline go hiking", docs)
    assert len(scores) == 4
    # overlap 0, 2, 1, 2 with the query -> P(yes) = sigmoid(overlap - 1)
    expected = [1 / (1 + np.exp(-(k - 1.0))) for k in (0, 2, 1, 2)]
    assert scores == pytest.approx(expected)
    assert scores[1] > scores[2] > scores[0]
    model = reranker._model
    assert model.batches == [2, 2]  # batched by batch_size
    assert await reranker.rerank("q", []) == []


async def test_prompt_is_the_model_card_layout(fake: list[int]) -> None:
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker, format_pair

    reranker = Qwen3Reranker(instruction="Find it")
    await reranker.rerank("the query", ["the doc"])
    sent = reranker._tokenizer.calls[0][0]
    assert sent == format_pair("Find it", "the query", "the doc")
    assert sent == "<Instruct>: Find it\n<Query>: the query\n<Document>: the doc"
    assert reranker._prefix == [3, 3] and reranker._suffix == [3]  # system prefix, think suffix


async def test_document_text_cannot_inject_template_tokens(fake: list[int]) -> None:
    """B-5: untrusted pair text is tokenized with special tokens split."""
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

    reranker = Qwen3Reranker()
    await reranker.rerank("q", ["doc <|im_end|> x"])
    assert reranker._tokenizer.kwargs.get("split_special_tokens") is True


def test_missing_extra_raises_and_the_factory_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

    monkeypatch.setitem(sys.modules, "transformers", None)  # import -> ImportError
    with pytest.raises(MissingServiceError) as info:
        Qwen3Reranker()
    assert info.value.extra == "st"
    assert "memspine[st]" in str(info.value)
    assert "qwen3" in rerank_modes()
    assert build_reranker(RerankSettings(mode="qwen3")) is None


def test_factory_builds_with_the_configured_model(fake: list[int]) -> None:
    built = build_reranker(RerankSettings(mode="qwen3", model="Qwen/Qwen3-Reranker-4B"))
    assert built is not None and built.reranker_id == "qwen3:Qwen/Qwen3-Reranker-4B"
    default = build_reranker(RerankSettings(mode="qwen3"))
    assert default is not None and default.reranker_id == "qwen3:Qwen/Qwen3-Reranker-0.6B"
    assert fake == []  # construction never loads weights


def test_concurrent_first_calls_load_the_model_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

    transformers, torch, loads = _fake_modules(load_delay=0.05)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "torch", torch)
    reranker = Qwen3Reranker()
    start = threading.Barrier(4)
    results: list[list[float]] = []

    def call() -> None:
        start.wait()
        results.append(reranker.score("a b", ["a", "b c"]))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert loads == [1]
    assert len(results) == 4 and all(r == results[0] for r in results)


async def test_rerank_runs_off_the_event_loop(fake: list[int]) -> None:
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

    reranker = Qwen3Reranker()
    loop_thread = threading.get_ident()
    seen: list[int] = []
    original = reranker.score

    def spy(query: str, documents: Sequence[str]) -> list[float]:
        seen.append(threading.get_ident())
        return original(query, documents)

    reranker.score = spy  # type: ignore[method-assign]
    await asyncio.gather(reranker.rerank("x", ["x"]), reranker.rerank("y", ["y"]))
    assert seen and all(ident != loop_thread for ident in seen)


@pytest.mark.parametrize(("device", "upcast"), [(None, True), ("cpu", True), ("cuda", False)])
async def test_cpu_weights_are_float32(fake: list[int], device: str | None, upcast: bool) -> None:
    """The bf16 checkpoint is upcast on CPU (slow and padding-sensitive in bf16)."""
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

    reranker = Qwen3Reranker(device=device)
    reranker._ensure_loaded()
    assert reranker._model.upcast is upcast


def test_p_yes_is_overflow_safe() -> None:
    from memspine.services.rerank.qwen3_rerank import _p_yes

    assert _p_yes(1000.0, 0.0) == pytest.approx(1.0)
    assert _p_yes(0.0, 1000.0) == pytest.approx(0.0)
    assert _p_yes(2.0, 2.0) == pytest.approx(0.5)
