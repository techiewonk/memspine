"""N41 rerank blend + confidence gate, N42 rerank with session neighbours (all off)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


class FakeReranker:
    """Scores a document by a fixed table of phrases; records what it was shown."""

    reranker_id = "fake"

    def __init__(self, table: dict[str, float]) -> None:
        self.table = table
        self.seen: list[list[str]] = []

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.seen.append(documents)
        return [next((v for k, v in self.table.items() if k in d), 0.0) for d in documents]


async def _search(read: dict[str, Any], reranker: FakeReranker) -> list[str]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "rerank": "fastembed", **read},
    )
    await eng.start()
    eng._reranker = reranker
    try:
        turns = [
            "Ana: where should we go camping this summer?",
            "Ben: yes, the lake",
            "Ana: camping camping camping gear list",
        ]
        for i, text in enumerate(turns):
            await eng.write(
                text,
                namespace="a",
                memory_type="episodic",
                session_id="s1",
                valid_from=T0 + timedelta(minutes=i),
            )
        hits = await eng.search("camping", namespace="a", top_k=3)
        return [r.content for r, _ in hits]
    finally:
        await eng.stop()


def test_keys_are_off_by_default() -> None:
    read = ReadConfig()
    assert (read.rerank_blend, read.rerank_gate, read.rerank_context) == (None, None, 0)


async def test_blend_zero_keeps_the_retrieval_order() -> None:
    table = {"the lake": 9.0}
    replaced = await _search({}, FakeReranker(table))
    blended = await _search({"rerank_blend": 0.0}, FakeReranker(table))
    assert replaced[0] == "Ben: yes, the lake"
    assert blended[0] != "Ben: yes, the lake"


async def test_gate_keeps_the_retrieval_order_when_the_reranker_is_unsure() -> None:
    table = {"the lake": 0.2}
    baseline = await _search({"rerank_blend": 0.0}, FakeReranker(table))
    gated = await _search({"rerank_gate": 1.0}, FakeReranker(table))
    assert gated == baseline


async def test_context_shows_the_reranker_the_neighbouring_turns() -> None:
    fake = FakeReranker({})
    await _search({"rerank_context": 1}, fake)
    reply = next(d for d in fake.seen[0] if "[type: episodic]\nBen: yes" in d)
    assert "where should we go camping" in reply  # the question before it
    assert "gear list" in reply  # and the turn after


async def test_no_context_shows_each_turn_alone() -> None:
    fake = FakeReranker({})
    await _search({}, fake)
    reply = next(d for d in fake.seen[0] if "[type: episodic]\nBen: yes" in d)
    assert "where should we go camping" not in reply
