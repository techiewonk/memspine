"""I9: opt-in chunk-and-max reranking of long records (fake scorer)."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from memspine.services.rerank.chunking import ChunkMaxReranker, split_windows
from memspine.services.rerank.factory import RerankSettings, build_reranker


class _Scorer:
    """Scores a text 1.0 if it contains 'NEEDLE' else 0.1; records every call."""

    reranker_id = "fake"

    def __init__(self, head: int | None = None) -> None:
        self.calls: list[list[str]] = []
        self.head = head  # emulate a backend that keeps only the first N characters

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append(list(documents))
        seen = [d[: self.head] if self.head else d for d in documents]
        return [1.0 if "NEEDLE" in d else 0.1 for d in seen]


def test_split_windows_short_text_is_single() -> None:
    assert split_windows("short", 100) == ["short"]


def test_split_windows_cover_text_with_overlap() -> None:
    text = " ".join(f"w{i}" for i in range(200))
    wins = split_windows(text, 100, 20)
    assert len(wins) > 3 and all(len(w) <= 100 for w in wins)
    assert wins[0].startswith("w0 ") and wins[-1].endswith("w199")
    for a, b in pairwise(wins):
        assert b.split(" ")[0] in a  # overlap: the next window opens inside the last


async def test_tail_needle_found_only_with_chunking() -> None:
    long_doc = "filler " * 400 + "NEEDLE"
    assert await _Scorer(head=500).rerank("q", [long_doc]) == [0.1]  # tail cut: missed
    wrapped = ChunkMaxReranker(_Scorer(head=500), 500)
    assert await wrapped.rerank("q", [long_doc, "short NEEDLE", "short"]) == [1.0, 1.0, 0.1]


async def test_short_documents_pass_through_unchanged() -> None:
    inner = _Scorer()
    out = await ChunkMaxReranker(inner, 1000).rerank("q", ["a", "NEEDLE b"])
    assert out == [0.1, 1.0] and inner.calls == [["a", "NEEDLE b"]]


async def test_header_repeated_on_every_window() -> None:
    inner = _Scorer()
    doc = "[Date: 2023-05-01] " + "word " * 300
    await ChunkMaxReranker(inner, 200).rerank("q", [doc])
    assert len(inner.calls[0]) > 1
    assert all(w.startswith("[Date: 2023-05-01] ") for w in inner.calls[0])
    assert all(len(w) <= 200 for w in inner.calls[0])


async def test_empty_and_id() -> None:
    w = ChunkMaxReranker(_Scorer(), 100)
    assert await w.rerank("q", []) == []
    assert w.reranker_id == "fake+chunkmax100"


def test_factory_default_unwrapped(monkeypatch: Any) -> None:
    from memspine.services.rerank import factory

    monkeypatch.setitem(factory._REGISTRY, "fakemode", lambda s: _Scorer())
    assert isinstance(build_reranker(RerankSettings(mode="fakemode")), _Scorer)
    wrapped = build_reranker(RerankSettings(mode="fakemode", chunk_chars=300))
    assert isinstance(wrapped, ChunkMaxReranker)
