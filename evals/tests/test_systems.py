"""The floor, the ceiling and the contender — behaviour, not vibes."""

from __future__ import annotations

import asyncio

from memspine_evals.contracts import Turn
from memspine_evals.systems import BM25Retriever, FullContextSystem, NaiveRAGSystem, VerbatimSystem
from memspine_evals.systems.retrievers import Unit, cosine, tokenize

HISTORY = [
    Turn(turn_id="t0", session_id="s1", speaker="user", text="The weather is unpredictable."),
    Turn(turn_id="t1", session_id="s1", speaker="user", text="My allergy is to walnuts."),
    Turn(turn_id="t2", session_id="s1", speaker="assistant", text="Noted, I will remember that."),
    Turn(turn_id="t3", session_id="s2", speaker="user", text="Traffic was terrible this morning."),
    Turn(turn_id="t4", session_id="s2", speaker="user", text="My favourite city is Lisbon."),
]


async def feed(system, turns=HISTORY) -> None:
    await system.reset("item")
    for turn in turns:
        await system.insert(turn)


def test_bm25_ranks_the_relevant_unit_first() -> None:
    retriever = BM25Retriever()
    for turn in HISTORY:
        retriever.add(Unit(unit_id=turn.turn_id, text=turn.text, turn_ids=(turn.turn_id,)))
    hits = retriever.search("what is my allergy", top_k=2)
    assert hits[0][0].unit_id == "t1"


def test_bm25_is_deterministic_across_instances() -> None:
    def run() -> list[str]:
        retriever = BM25Retriever()
        for turn in HISTORY:
            retriever.add(Unit(unit_id=turn.turn_id, text=turn.text, turn_ids=(turn.turn_id,)))
        return [unit.unit_id for unit, _ in retriever.search("favourite city", top_k=3)]

    assert run() == run()


def test_verbatim_retrieves_the_gold_turn() -> None:
    system = VerbatimSystem()
    asyncio.run(feed(system))
    context = asyncio.run(system.query("what is my allergy?", budget_tokens=200, top_k=3))
    assert "walnuts" in context.text
    assert context.evidence[0].turn_id == "t1"
    assert context.meta["ranked"] is True


def test_verbatim_makes_no_model_calls() -> None:
    system = VerbatimSystem()
    asyncio.run(feed(system))
    deposit = asyncio.run(system.insert(HISTORY[0]))
    assert deposit.model_calls == 0


def test_full_context_returns_everything_and_declines_to_rank() -> None:
    system = FullContextSystem()
    asyncio.run(feed(system))
    context = asyncio.run(system.query("anything", budget_tokens=10_000, top_k=3))
    assert context.meta["ranked"] is False
    assert context.truncated is False
    assert all(turn.text in context.text for turn in HISTORY)


def test_full_context_truncation_keeps_the_most_recent_turns() -> None:
    system = FullContextSystem()
    asyncio.run(feed(system))
    context = asyncio.run(system.query("anything", budget_tokens=20, top_k=3))
    assert context.truncated is True
    assert "Lisbon" in context.text  # newest survives
    assert "unpredictable" not in context.text  # oldest is dropped


def test_naive_rag_tail_is_reachable_before_a_chunk_closes() -> None:
    # chunk_chars is large enough that nothing flushes during insert: without
    # the query-time flush the most recent turns would be invisible.
    system = NaiveRAGSystem(chunk_chars=10_000)
    asyncio.run(feed(system))
    context = asyncio.run(system.query("favourite city", budget_tokens=500, top_k=2))
    assert "Lisbon" in context.text


def test_naive_rag_chunks_carry_every_source_turn_id() -> None:
    system = NaiveRAGSystem(chunk_chars=120, overlap_turns=0)
    asyncio.run(feed(system))
    context = asyncio.run(system.query("allergy walnuts", budget_tokens=500, top_k=3))
    assert {e.turn_id for e in context.evidence} & {"t1"}


def test_reset_clears_state_between_items() -> None:
    system = VerbatimSystem()
    asyncio.run(feed(system))
    asyncio.run(system.reset("item-2"))
    context = asyncio.run(system.query("allergy", budget_tokens=200, top_k=3))
    assert context.text == ""
    assert context.evidence == ()


def test_budget_is_respected_by_the_system_itself() -> None:
    system = VerbatimSystem()
    asyncio.run(feed(system))
    context = asyncio.run(system.query("allergy walnuts city", budget_tokens=8, top_k=5))
    assert context.tokens <= 8
    assert context.truncated is True


def test_describe_exposes_the_retriever_for_the_manifest() -> None:
    system = VerbatimSystem()
    described = system.describe()
    assert described["extraction"] == "none"
    assert described["retriever"]["retriever_id"] == "bm25"
    assert described["retriever"]["dense"] is False


def test_tokenize_and_cosine_edge_cases() -> None:
    assert tokenize("Hello, World! 42") == ["hello", "world", "42"]
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
